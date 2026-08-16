import base64
import hashlib
import hmac
import os
import re
import sqlite3
from datetime import datetime, timezone

import requests
from flask import Flask, request, abort, jsonify

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "maison_lumi.db")

PLUS_RE = re.compile(r"^\s*\+(\d+)\s*$")
CANCEL_RE = re.compile(r"^\s*(取消|刪單|cancel)\s*$", re.IGNORECASE)
CHECK_RE = re.compile(r"^\s*(查單|查詢)\s*$", re.IGNORECASE)
LIST_RE = re.compile(r"^\s*(名單|喊單名單)\s*$", re.IGNORECASE)
CLOSE_RE = re.compile(r"^\s*(結單|關單)\s*$", re.IGNORECASE)
OPEN_RE = re.compile(r"^\s*(開單|重新開單)\s*$", re.IGNORECASE)


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
            created_at TEXT NOT NULL
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

        if not column_exists(conn, "products", "owner_user_id"):
            conn.execute(
                "ALTER TABLE products ADD COLUMN owner_user_id TEXT"
            )

        if not column_exists(conn, "products", "is_closed"):
            conn.execute(
                "ALTER TABLE products ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0"
            )


init_db()


def verify_signature(raw_body: bytes, signature: str) -> bool:
    if not CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        CHANNEL_SECRET.encode(),
        raw_body,
        hashlib.sha256
    ).digest()

    expected = base64.b64encode(digest).decode()

    return hmac.compare_digest(expected, signature)


def reply(reply_token: str, text: str):
    if not CHANNEL_ACCESS_TOKEN or not reply_token:
        return

    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "replyToken": reply_token,
                "messages": [
                    {
                        "type": "text",
                        "text": text
                    }
                ],
            },
            timeout=10,
        )
    except requests.RequestException:
        pass


def get_display_name(group_id: str, user_id: str) -> str:
    if not CHANNEL_ACCESS_TOKEN or not group_id or not user_id:
        return user_id or "未知會員"

    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}",
            headers={
                "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"
            },
            timeout=10,
        )

        if r.ok:
            return r.json().get("displayName") or user_id

    except requests.RequestException:
        pass

    return user_id or "未知會員"


def get_pending_image(group_id: str, message_id: str):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM pending_images
            WHERE message_id=? AND group_id=?
            """,
            (message_id, group_id),
        ).fetchone()


def get_or_create_product(
    group_id: str,
    image_message_id: str,
    owner_user_id: str
):
    with db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM products
            WHERE image_message_id=?
            """,
            (image_message_id,),
        ).fetchone()

        if row:
            if not row["owner_user_id"] and owner_user_id:
                conn.execute(
                    """
                    UPDATE products
                    SET owner_user_id=?
                    WHERE id=?
                    """,
                    (owner_user_id, row["id"]),
                )

                row = conn.execute(
                    "SELECT * FROM products WHERE id=?",
                    (row["id"],),
                ).fetchone()

            return row, False

        cur = conn.execute(
            """
            INSERT INTO products(
                product_code,
                group_id,
                image_message_id,
                owner_user_id,
                is_closed,
                created_at
            )
            VALUES('',?,?,?,?,?)
            """,
            (
                group_id,
                image_message_id,
                owner_user_id,
                0,
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
            (product_code, product_id),
        )

        return conn.execute(
            "SELECT * FROM products WHERE id=?",
            (product_id,),
        ).fetchone(), True


def add_order(
    product_id: int,
    user_id: str,
    display_name: str,
    qty: int
):
    with db() as conn:
        current = conn.execute(
            """
            SELECT quantity
            FROM orders
            WHERE product_id=? AND user_id=?
            """,
            (product_id, user_id),
        ).fetchone()

        old_qty = current["quantity"] if current else 0
        total = old_qty + qty

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
                total,
                now_iso(),
            ),
        )

        return total


def get_user_order(product_id: int, user_id: str):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM orders
            WHERE product_id=? AND user_id=?
            """,
            (product_id, user_id),
        ).fetchone()


def cancel_order(product_id: int, user_id: str):
    with db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM orders
            WHERE product_id=? AND user_id=?
            """,
            (product_id, user_id),
        ).fetchone()

        if not row:
            return False

        conn.execute(
            """
            DELETE FROM orders
            WHERE product_id=? AND user_id=?
            """,
            (product_id, user_id),
        )

        return True


def get_order_list(product_id: int):
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


def set_product_closed(product_id: int, closed: bool):
    with db() as conn:
        conn.execute(
            """
            UPDATE products
            SET is_closed=?
            WHERE id=?
            """,
            (1 if closed else 0, product_id),
        )


def is_owner(product, user_id):
    return (
        product
        and product["owner_user_id"]
        and product["owner_user_id"] == user_id
    )


def format_order_list(product):
    orders = get_order_list(product["id"])

    if not orders:
        return (
            f"🛍️ 商品 {product['product_code']}\n"
            "目前還沒有人喊單。"
        )

    lines = [
        f"📋 商品 {product['product_code']} 喊單名單"
    ]

    total_qty = 0

    for index, order in enumerate(orders, start=1):
        name = order["display_name"] or "未知會員"
        qty = order["quantity"]

        total_qty += qty

        lines.append(
            f"{index}. {name} × {qty}"
        )

    lines.append("")
    lines.append(f"👥 共 {len(orders)} 人")
    lines.append(f"🛍️ 共 {total_qty} 件")

    if product["is_closed"]:
        lines.append("🔒 狀態：已結單")
    else:
        lines.append("🟢 狀態：開放喊單")

    return "\n".join(lines)


@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "Maison Lumi LINE Bot",
        "version": "2"
    })


@app.post("/webhook")
def webhook():
    raw = request.get_data()

    signature = request.headers.get(
        "X-Line-Signature",
        ""
    )

    if not verify_signature(raw, signature):
        abort(400)

    body = request.get_json(silent=True) or {}

    for event in body.get("events", []):
        if event.get("type") != "message":
            continue

        source = event.get("source", {})

        if source.get("type") != "group":
            continue

        group_id = source.get("groupId")
        user_id = source.get("userId")

        if not group_id or not user_id:
            continue

        message = event.get("message", {})
        message_type = message.get("type")

        # 收到商品照片時先記住圖片 message ID
        if message_type == "image":
            message_id = message.get("id")

            if message_id:
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
                            user_id,
                            now_iso(),
                        ),
                    )

            continue

        if message_type != "text":
            continue

        text = message.get("text", "").strip()

        quoted_message_id = message.get(
            "quotedMessageId"
        )

        if not quoted_message_id:
            continue

        image = get_pending_image(
            group_id,
            quoted_message_id
        )

        if not image:
            continue

        plus = PLUS_RE.match(text)
        cancel = CANCEL_RE.match(text)
        check = CHECK_RE.match(text)
        order_list = LIST_RE.match(text)
        close_order = CLOSE_RE.match(text)
        open_order = OPEN_RE.match(text)

        if not any([
            plus,
            cancel,
            check,
            order_list,
            close_order,
            open_order
        ]):
            continue

        product, created = get_or_create_product(
            group_id,
            quoted_message_id,
            image["sender_user_id"],
        )

        reply_token = event.get("replyToken")

        display_name = get_display_name(
            group_id,
            user_id
        )

        # +1、+2、+3...
        if plus:
            if product["is_closed"]:
                reply(
                    reply_token,
                    (
                        f"🔒 商品 {product['product_code']} 已結單\n"
                        "目前無法再喊單。"
                    )
                )
                continue

            qty = int(plus.group(1))

            if qty < 1 or qty > 99:
                reply(
                    reply_token,
                    "數量請輸入 +1 ～ +99"
                )
                continue

            total = add_order(
                product["id"],
                user_id,
                display_name,
                qty,
            )

            prefix = (
                "📝 已建立商品記事\n"
                if created
                else ""
            )

            reply(
                reply_token,
                (
                    f"{prefix}"
                    f"🛍️ 商品 {product['product_code']}\n"
                    f"✅ {display_name} 登記 +{qty}\n"
                    f"目前你的數量：{total}"
                ),
            )

            continue

        # 取消自己的喊單
        if cancel:
            existed = cancel_order(
                product["id"],
                user_id
            )

            if existed:
                reply(
                    reply_token,
                    (
                        f"🛍️ 商品 {product['product_code']}\n"
                        f"✅ 已取消 {display_name} 的喊單"
                    ),
                )
            else:
                reply(
                    reply_token,
                    (
                        f"🛍️ 商品 {product['product_code']}\n"
                        "你目前沒有這個商品的喊單。"
                    ),
                )

            continue

        # 查自己的單
        if check:
            order = get_user_order(
                product["id"],
                user_id
            )

            if not order:
                reply(
                    reply_token,
                    (
                        f"🛍️ 商品 {product['product_code']}\n"
                        f"{display_name} 目前沒有喊單。"
                    ),
                )
            else:
                reply(
                    reply_token,
                    (
                        f"🔎 商品 {product['product_code']}\n"
                        f"{display_name}\n"
                        f"目前數量：{order['quantity']}"
                    ),
                )

            continue

        # 以下功能只有上傳商品照片的人可以操作
        if order_list or close_order or open_order:
            if not is_owner(product, user_id):
                reply(
                    reply_token,
                    "⚠️ 此功能只有商品照片上傳者可以操作。"
                )
                continue

        # 查看完整名單
        if order_list:
            reply(
                reply_token,
                format_order_list(product)
            )
            continue

        # 結單
        if close_order:
            set_product_closed(
                product["id"],
                True
            )

            refreshed = None

            with db() as conn:
                refreshed = conn.execute(
                    """
                    SELECT *
                    FROM products
                    WHERE id=?
                    """,
                    (product["id"],),
                ).fetchone()

            summary = format_order_list(
                refreshed
            )

            reply(
                reply_token,
                (
                    f"🔒 商品 {product['product_code']} 已結單\n\n"
                    f"{summary}"
                ),
            )

            continue

        # 重新開單
        if open_order:
            set_product_closed(
                product["id"],
                False
            )

            reply(
                reply_token,
                (
                    f"🟢 商品 {product['product_code']} 已重新開單\n"
                    "現在可以繼續喊 +1、+2..."
                ),
            )

            continue

    return "OK"


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "8080"
            )
        ),
    )
    
