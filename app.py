import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from collections import Counter
from datetime import datetime, timezone

import requests
from flask import Flask, request, abort, jsonify, Response

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "maison_lumi.db")
ADMIN_USER_ID_ENV = os.getenv("ADMIN_USER_ID", "").strip()

PLUS_RE = re.compile(r"^\s*\+(\d+)\s*$")
CANCEL_RE = re.compile(r"^\s*(取消|刪單|cancel)\s*$", re.IGNORECASE)
ADMIN_CMD_RE = re.compile(
    r"^\s*(A\d{3,})\s*(查單|名單|結單|開單)\s*$",
    re.IGNORECASE,
)
PRODUCT_LIST_RE = re.compile(r"^\s*(商品列表|商品清單)\s*$")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def column_exists(conn, table, column):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_images (
            message_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            sender_user_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code TEXT UNIQUE,
            group_id TEXT NOT NULL,
            image_message_id TEXT NOT NULL UNIQUE,
            owner_user_id TEXT,
            is_closed INTEGER NOT NULL DEFAULT 0,
            image_key TEXT,
            image_blob BLOB,
            image_mime TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staff (
            user_id TEXT PRIMARY KEY,
            display_name TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staff_invites (
            code TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            used_by TEXT,
            used_at TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            display_name TEXT,
            quantity INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(product_id, user_id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        """)

        for column, ddl in [
            ("owner_user_id", "ALTER TABLE products ADD COLUMN owner_user_id TEXT"),
            ("is_closed", "ALTER TABLE products ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0"),
            ("image_key", "ALTER TABLE products ADD COLUMN image_key TEXT"),
            ("image_blob", "ALTER TABLE products ADD COLUMN image_blob BLOB"),
            ("image_mime", "ALTER TABLE products ADD COLUMN image_mime TEXT"),
        ]:
            if not column_exists(conn, "products", column):
                conn.execute(ddl)


init_db()


def get_setting(key):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


def get_admin_user_id():
    return ADMIN_USER_ID_ENV or get_setting("admin_user_id")


def is_admin(user_id):
    admin_id = get_admin_user_id()
    return bool(admin_id and user_id and admin_id == user_id)


def is_staff(user_id):
    if not user_id:
        return False

    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM staff WHERE user_id=?",
            (user_id,),
        ).fetchone()

    return bool(row)


def can_manage_orders(user_id):
    return is_admin(user_id) or is_staff(user_id)


def generate_staff_invite():
    for _ in range(20):
        code = f"{secrets.randbelow(1000000):06d}"

        try:
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO staff_invites(code, created_at)
                    VALUES(?, ?)
                    """,
                    (code, now_iso()),
                )
            return code

        except sqlite3.IntegrityError:
            continue

    raise RuntimeError("Could not generate invite code")


def redeem_staff_invite(code, user_id):
    with db() as conn:
        invite = conn.execute(
            """
            SELECT *
            FROM staff_invites
            WHERE code=? AND used_by IS NULL
            """,
            (code,),
        ).fetchone()

        if not invite:
            return False

        conn.execute(
            """
            UPDATE staff_invites
            SET used_by=?, used_at=?
            WHERE code=? AND used_by IS NULL
            """,
            (user_id, now_iso(), code),
        )

        conn.execute(
            """
            INSERT INTO staff(user_id, display_name, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, None, now_iso()),
        )

    return True


def short_code(user_id):
    return hashlib.sha256(
        (user_id or "?").encode()
    ).hexdigest()[:4].upper()


def staff_list_text():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, display_name, created_at
            FROM staff
            ORDER BY created_at ASC
            """
        ).fetchall()

    if not rows:
        return "目前沒有小幫手。"

    lines = ["👥 小幫手列表"]

    for i, row in enumerate(rows, start=1):
        name = (
            row["display_name"]
            or f"小幫手 #{short_code(row['user_id'])}"
        )
        lines.append(f"{i}. {name}")

    return "\n".join(lines)


def verify_signature(raw_body, signature):
    if not CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        CHANNEL_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).digest()

    expected = base64.b64encode(digest).decode()

    return hmac.compare_digest(
        expected,
        signature,
    )


def auth_headers(json_mode=False):
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
    }

    if json_mode:
        headers["Content-Type"] = "application/json"

    return headers


def reply_messages(reply_token, messages):
    if not CHANNEL_ACCESS_TOKEN or not reply_token:
        return

    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=auth_headers(json_mode=True),
            json={
                "replyToken": reply_token,
                "messages": messages[:5],
            },
            timeout=10,
        )

    except requests.RequestException:
        pass


def push_messages(user_id, messages):
    if not CHANNEL_ACCESS_TOKEN or not user_id:
        return

    try:
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=auth_headers(json_mode=True),
            json={
                "to": user_id,
                "messages": messages[:5],
            },
            timeout=10,
        )

    except requests.RequestException:
        pass


def reply_text(reply_token, text):
    reply_messages(
        reply_token,
        [
            {
                "type": "text",
                "text": text[:5000],
            }
        ],
    )


def get_user_profile(user_id):
    if not CHANNEL_ACCESS_TOKEN or not user_id:
        return None

    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/profile/{user_id}",
            headers=auth_headers(),
            timeout=10,
        )

        if r.ok:
            return r.json()

    except requests.RequestException:
        pass

    return None


def refresh_staff_name(user_id):
    if not is_staff(user_id):
        return

    profile = get_user_profile(user_id)
    name = (profile or {}).get("displayName")

    if name:
        with db() as conn:
            conn.execute(
                """
                UPDATE staff
                SET display_name=?
                WHERE user_id=?
                """,
                (name, user_id),
            )


def get_group_profile(group_id, user_id):
    if not CHANNEL_ACCESS_TOKEN or not group_id or not user_id:
        return {
            "displayName": user_id or "未知會員",
            "pictureUrl": None,
        }

    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}",
            headers=auth_headers(),
            timeout=10,
        )

        if r.ok:
            data = r.json()

            return {
                "displayName": data.get("displayName") or user_id,
                "pictureUrl": data.get("pictureUrl"),
            }

    except requests.RequestException:
        pass

    return {
        "displayName": user_id or "未知會員",
        "pictureUrl": None,
    }


def remember_image(message_id, group_id, sender_user_id):
    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_images(
                message_id,
                group_id,
                sender_user_id,
                created_at
            )
            VALUES(?,?,?,?)
            """,
            (
                message_id,
                group_id,
                sender_user_id,
                now_iso(),
            ),
        )


def get_pending_image(group_id, message_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM pending_images
            WHERE message_id=? AND group_id=?
            """,
            (
                message_id,
                group_id,
            ),
        ).fetchone()


def fetch_line_image_preview(message_id):
    if not CHANNEL_ACCESS_TOKEN or not message_id:
        return None, None

    for suffix in ["/preview", ""]:
        try:
            r = requests.get(
                f"https://api-data.line.me/v2/bot/message/{message_id}/content{suffix}",
                headers=auth_headers(),
                timeout=20,
            )

            if r.ok and r.content:
                mime = (
                    r.headers.get("Content-Type")
                    or "image/jpeg"
                ).split(";")[0]

                return r.content, mime

        except requests.RequestException:
            pass

    return None, None


def save_product_image(product_id, message_id):
    blob, mime = fetch_line_image_preview(
        message_id
    )

    if not blob:
        return False

    with db() as conn:
        conn.execute(
            """
            UPDATE products
            SET image_blob=?, image_mime=?
            WHERE id=?
            """,
            (
                sqlite3.Binary(blob),
                mime,
                product_id,
            ),
        )

    return True


def create_product(group_id, image_message_id, owner_user_id):
    with db() as conn:
        existing = conn.execute(
            """
            SELECT *
            FROM products
            WHERE image_message_id=?
            """,
            (image_message_id,),
        ).fetchone()

        if existing:
            return existing, False

        image_key = secrets.token_urlsafe(16)

        cur = conn.execute(
            """
            INSERT INTO products(
                product_code,
                group_id,
                image_message_id,
                owner_user_id,
                is_closed,
                image_key,
                created_at
            )
            VALUES('',?,?,?,?,?,?)
            """,
            (
                group_id,
                image_message_id,
                owner_user_id,
                0,
                image_key,
                now_iso(),
            ),
        )

        product_id = cur.lastrowid
        product_code = f"A{product_id:03d}"

        conn.execute(
            """
            UPDATE products
            SET product_code=?
            WHERE id=?
            """,
            (
                product_code,
                product_id,
            ),
        )

    save_product_image(
        product_id,
        image_message_id,
    )

    return (
        get_product_by_code(product_code),
        True,
    )


def get_product_by_code(product_code):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM products
            WHERE UPPER(product_code)=?
            """,
            (product_code.upper(),),
        ).fetchone()


def get_product_by_image(image_message_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM products
            WHERE image_message_id=?
            """,
            (image_message_id,),
        ).fetchone()


def ensure_product_image(product):
    if not product:
        return None

    if not product["image_blob"]:
        save_product_image(
            product["id"],
            product["image_message_id"],
        )

        product = get_product_by_code(
            product["product_code"]
        )

    return product


def product_image_url(product):
    product = ensure_product_image(product)

    if not product or not product["image_blob"]:
        return None

    base = request.host_url.rstrip("/")

    return (
        f"{base}/product-image/"
        f"{product['product_code']}/"
        f"{product['image_key']}"
    )


def add_order(product_id, user_id, display_name, qty):
    with db() as conn:
        current = conn.execute(
            """
            SELECT quantity
            FROM orders
            WHERE product_id=? AND user_id=?
            """,
            (
                product_id,
                user_id,
            ),
        ).fetchone()

        new_qty = (
            current["quantity"]
            if current
            else 0
        ) + qty

        conn.execute(
            """
            INSERT INTO orders(
                product_id,
                user_id,
                display_name,
                quantity,
                updated_at
            )
            VALUES(?,?,?,?,?)

            ON CONFLICT(product_id,user_id)
            DO UPDATE SET
                display_name=excluded.display_name,
                quantity=excluded.quantity,
                updated_at=excluded.updated_at
            """,
            (
                product_id,
                user_id,
                display_name,
                new_qty,
                now_iso(),
            ),
        )


def cancel_order(product_id, user_id):
    with db() as conn:
        conn.execute(
            """
            DELETE FROM orders
            WHERE product_id=? AND user_id=?
            """,
            (
                product_id,
                user_id,
            ),
        )


def get_orders(product_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE product_id=? AND quantity>0
            ORDER BY updated_at ASC
            """,
            (product_id,),
        ).fetchall()


def set_product_closed(product_id, closed):
    with db() as conn:
        conn.execute(
            """
            UPDATE products
            SET is_closed=?
            WHERE id=?
            """,
            (
                1 if closed else 0,
                product_id,
            ),
        )


def build_product_flex(product, title=None):
    product = ensure_product_image(product)
    orders = get_orders(product["id"])

    counts = Counter(
        (
            row["display_name"]
            or "未知會員"
        )
        for row in orders
    )

    duplicate_names = {
        name
        for name, count in counts.items()
        if count > 1
    }

    total_qty = sum(
        row["quantity"]
        for row in orders
    )

    body_contents = [
        {
            "type": "text",
            "text": (
                title
                or f"商品 {product['product_code']}"
            ),
            "weight": "bold",
            "size": "xl",
            "wrap": True,
        },
        {
            "type": "text",
            "text": (
                "🔒 已結單"
                if product["is_closed"]
                else "🟢 開放喊單"
            ),
            "size": "sm",
            "color": "#666666",
            "margin": "sm",
        },
        {
            "type": "separator",
            "margin": "lg",
        },
    ]

    if orders:
        for order in orders:
            name = (
                order["display_name"]
                or "未知會員"
            )

            if name in duplicate_names:
                name = (
                    f"{name} "
                    f"#{short_code(order['user_id'])}"
                )

            profile = get_group_profile(
                product["group_id"],
                order["user_id"],
            )

            row_contents = []

            if profile.get("pictureUrl"):
                row_contents.append({
                    "type": "image",
                    "url": profile["pictureUrl"],
                    "size": "xxs",
                    "aspectMode": "cover",
                    "aspectRatio": "1:1",
                    "flex": 0,
                })

            row_contents.extend([
                {
                    "type": "text",
                    "text": name,
                    "size": "sm",
                    "flex": 1,
                    "margin": "md",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": f"× {order['quantity']}",
                    "size": "sm",
                    "weight": "bold",
                    "align": "end",
                    "flex": 0,
                },
            ])

            body_contents.append({
                "type": "box",
                "layout": "horizontal",
                "alignItems": "center",
                "margin": "md",
                "contents": row_contents,
            })

    else:
        body_contents.append({
            "type": "text",
            "text": "目前還沒有人喊單",
            "size": "sm",
            "color": "#777777",
            "margin": "lg",
        })

    body_contents.extend([
        {
            "type": "separator",
            "margin": "lg",
        },
        {
            "type": "text",
            "text": (
                f"👥 {len(orders)} 人　"
                f"📦 總數 {total_qty}"
            ),
            "size": "sm",
            "weight": "bold",
            "margin": "lg",
        },
    ])

    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body_contents,
        },
    }

    image_url = product_image_url(product)

    if image_url:
        bubble["hero"] = {
            "type": "image",
            "url": image_url,
            "size": "full",
            "aspectRatio": "1:1",
            "aspectMode": "cover",
        }

    return {
        "type": "flex",
        "altText": f"{product['product_code']} 查單",
        "contents": bubble,
    }


def build_created_product_flex(product):
    image_url = product_image_url(product)

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": product["product_code"],
                    "weight": "bold",
                    "size": "xxl",
                    "align": "center",
                },
                {
                    "type": "text",
                    "text": "新商品已建立",
                    "size": "sm",
                    "align": "center",
                    "margin": "sm",
                },
                {
                    "type": "text",
                    "text": (
                        f"私訊「{product['product_code']} 查單」"
                        "即可查看"
                    ),
                    "size": "xs",
                    "wrap": True,
                    "align": "center",
                    "margin": "md",
                },
            ],
        },
    }

    if image_url:
        bubble["hero"] = {
            "type": "image",
            "url": image_url,
            "size": "full",
            "aspectRatio": "1:1",
            "aspectMode": "cover",
        }

    return {
        "type": "flex",
        "altText": (
            f"新商品 {product['product_code']}"
        ),
        "contents": bubble,
    }


def format_product_list():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                p.*,
                COUNT(o.id) AS people,
                COALESCE(SUM(o.quantity), 0) AS qty
            FROM products p
            LEFT JOIN orders o
              ON o.product_id=p.id
             AND o.quantity>0
            GROUP BY p.id
            ORDER BY p.id DESC
            LIMIT 20
            """
        ).fetchall()

    if not rows:
        return "目前還沒有商品。"

    lines = ["🛍️ 最近商品"]

    for row in rows:
        status = (
            "🔒"
            if row["is_closed"]
            else "🟢"
        )

        lines.append(
            f"{status} "
            f"{row['product_code']}｜"
            f"{row['people']}人｜"
            f"{row['qty']}件"
        )

    lines.extend([
        "",
        "可輸入：A001 查單 / A001 結單 / A001 開單",
    ])

    return "\n".join(lines)


@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "Maison Lumi LINE Bot",
        "version": "6-owner-staff-photo",
    })


@app.get("/product-image/<product_code>/<image_key>")
def serve_product_image(
    product_code,
    image_key,
):
    product = get_product_by_code(
        product_code
    )

    if (
        not product
        or product["image_key"] != image_key
        or not product["image_blob"]
    ):
        abort(404)

    return Response(
        bytes(product["image_blob"]),
        mimetype=(
            product["image_mime"]
            or "image/jpeg"
        ),
    )


@app.post("/webhook")
def webhook():
    raw = request.get_data()

    if not verify_signature(
        raw,
        request.headers.get(
            "X-Line-Signature",
            "",
        ),
    ):
        abort(400)

    body = (
        request.get_json(
            silent=True
        )
        or {}
    )

    for event in body.get(
        "events",
        [],
    ):
        if event.get("type") != "message":
            continue

        source = event.get("source", {})
        source_type = source.get("type")
        user_id = source.get("userId")

        message = event.get("message", {})
        message_type = message.get("type")
        reply_token = event.get("replyToken")

        # PRIVATE CHAT
        if source_type == "user":
            if (
                message_type != "text"
                or not user_id
            ):
                continue

            text = message.get(
                "text",
                "",
            ).strip()

            if text in (
                "設定Owner",
                "設定管理員",
            ):
                current = get_admin_user_id()

                if (
                    current
                    and current != user_id
                ):
                    reply_text(
                        reply_token,
                        "⚠️ Owner 已經設定完成，"
                        "無法由其他帳號變更。",
                    )

                else:
                    set_setting(
                        "admin_user_id",
                        user_id,
                    )

                    reply_text(
                        reply_token,
                        "👑 Owner 設定完成。\n"
                        "之後只有你可以產生"
                        "小幫手邀請碼。",
                    )

                continue

            join_match = re.match(
                r"^\s*加入小幫手\s+(\d{6})\s*$",
                text,
            )

            if join_match:
                if redeem_staff_invite(
                    join_match.group(1),
                    user_id,
                ):
                    refresh_staff_name(
                        user_id
                    )

                    reply_text(
                        reply_token,
                        "✅ 已加入成為小幫手。\n"
                        "現在可以查單、結單、開單、"
                        "查看商品列表，也可以在群組"
                        "貼商品照片建立編號。",
                    )

                else:
                    reply_text(
                        reply_token,
                        "⚠️ 邀請碼無效或已使用。",
                    )

                continue

            if text == "產生小幫手邀請碼":
                if not is_admin(user_id):
                    reply_text(
                        reply_token,
                        "⚠️ 只有 Owner 可以產生"
                        "小幫手邀請碼。",
                    )

                else:
                    invite_code = (
                        generate_staff_invite()
                    )

                    reply_text(
                        reply_token,
                        "👥 小幫手一次性邀請碼："
                        f"{invite_code}\n\n"
                        "請小幫手加官方帳號好友後，"
                        "私訊：\n"
                        f"加入小幫手 {invite_code}\n\n"
                        "此邀請碼只能使用一次。",
                    )

                continue

            if text == "小幫手列表":
                if not is_admin(user_id):
                    reply_text(
                        reply_token,
                        "⚠️ 只有 Owner 可以查看"
                        "小幫手列表。",
                    )

                else:
                    reply_text(
                        reply_token,
                        staff_list_text(),
                    )

                continue

            if not can_manage_orders(
                user_id
            ):
                continue

            if PRODUCT_LIST_RE.match(text):
                reply_text(
                    reply_token,
                    format_product_list(),
                )
                continue

            cmd = ADMIN_CMD_RE.match(text)

            if not cmd:
                help_text = (
                    "管理指令：\n"
                    "A001 查單\n"
                    "A001 結單\n"
                    "A001 開單\n"
                    "商品列表"
                )

                if is_admin(user_id):
                    help_text += (
                        "\n產生小幫手邀請碼"
                        "\n小幫手列表"
                    )

                reply_text(
                    reply_token,
                    help_text,
                )
                continue

            product_code = (
                cmd.group(1).upper()
            )
            action = cmd.group(2)

            product = get_product_by_code(
                product_code
            )

            if not product:
                reply_text(
                    reply_token,
                    f"找不到商品 {product_code}。",
                )
                continue

            if action in (
                "查單",
                "名單",
            ):
                reply_messages(
                    reply_token,
                    [
                        build_product_flex(
                            product,
                            f"商品 {product_code} 查單",
                        )
                    ],
                )

            elif action == "結單":
                set_product_closed(
                    product["id"],
                    True,
                )

                reply_messages(
                    reply_token,
                    [
                        build_product_flex(
                            get_product_by_code(
                                product_code
                            ),
                            f"🔒 {product_code} 已結單",
                        )
                    ],
                )

            elif action == "開單":
                set_product_closed(
                    product["id"],
                    False,
                )

                reply_messages(
                    reply_token,
                    [
                        build_product_flex(
                            get_product_by_code(
                                product_code
                            ),
                            f"🟢 {product_code} 已重新開單",
                        )
                    ],
                )

            continue

        # GROUP CHAT
        if source_type != "group":
            continue

        group_id = source.get("groupId")

        if not group_id or not user_id:
            continue

        # Owner or staff posts product photo:
        # create code and privately send photo + code.
        if message_type == "image":
            message_id = message.get("id")

            if not message_id:
                continue

            remember_image(
                message_id,
                group_id,
                user_id,
            )

            if can_manage_orders(user_id):
                product, created = (
                    create_product(
                        group_id,
                        message_id,
                        user_id,
                    )
                )

                if created:
                    push_messages(
                        user_id,
                        [
                            build_created_product_flex(
                                product
                            )
                        ],
                    )

            continue

        if message_type != "text":
            continue

        text = message.get(
            "text",
            "",
        ).strip()

        quoted_message_id = (
            message.get(
                "quotedMessageId"
            )
        )

        if not quoted_message_id:
            continue

        plus = PLUS_RE.match(text)
        cancel = CANCEL_RE.match(text)

        if not plus and not cancel:
            continue

        product = get_product_by_image(
            quoted_message_id
        )

        if not product:
            image = get_pending_image(
                group_id,
                quoted_message_id,
            )

            if (
                image
                and can_manage_orders(
                    image["sender_user_id"]
                )
            ):
                product, _ = create_product(
                    group_id,
                    quoted_message_id,
                    image["sender_user_id"],
                )

            else:
                continue

        if product["is_closed"]:
            continue

        profile = get_group_profile(
            group_id,
            user_id,
        )

        display_name = (
            profile.get("displayName")
            or user_id
        )

        if plus:
            qty = int(
                plus.group(1)
            )

            if 1 <= qty <= 99:
                add_order(
                    product["id"],
                    user_id,
                    display_name,
                    qty,
                )

            # Silent in group.
            continue

        if cancel:
            cancel_order(
                product["id"],
                user_id,
            )
            # Silent in group.
            continue

    return "OK"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8080",
            )
        ),
    )
