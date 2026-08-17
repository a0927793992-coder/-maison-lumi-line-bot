import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import string
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import quote_plus, unquote_plus

import requests
from flask import Flask, request, abort, jsonify, Response

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "maison_lumi.db")
ADMIN_USER_ID_ENV = os.getenv("ADMIN_USER_ID", "").strip()

PLUS_ONLY_RE = re.compile(r"^\s*\+(\d+)\s*$")
SPEC_PLUS_RE = re.compile(r"^\s*(.+?)\s*\+(\d+)\s*$")
CANCEL_ONLY_RE = re.compile(r"^\s*(取消|刪單|cancel)\s*$", re.IGNORECASE)
SPEC_CANCEL_RE = re.compile(r"^\s*(.+?)\s*(取消|刪單)\s*$", re.IGNORECASE)
ADMIN_QUERY_RE = re.compile(r"^\s*(.+?[A-Z]\d{3,})\s*(查單|結單)\s*$", re.IGNORECASE)
START_SESSION_RE = re.compile(r"^\s*開始連線\s*(.+?)\s*$")
AUTO_SESSION_DATE_FIRST_RE = re.compile(
    r"^\s*\d{1,2}/\d{1,2}"
    r"(?:\s*[-~～至]\s*(?:\d{1,2}/)?\d{1,2})?"
    r"\s*[^\d/].+?\s*$"
)

AUTO_SESSION_PLACE_FIRST_RE = re.compile(
    r"^\s*[^\d/].*?"
    r"\d{1,2}/\d{1,2}"
    r"(?:\s*[-~～至]\s*(?:\d{1,2}/)?\d{1,2})?"
    r"\s*$"
)
END_SESSION_RE = re.compile(r"^\s*結束連線(?:\s+(.+?))?\s*$")
JOIN_STAFF_RE = re.compile(r"^\s*加入小幫手\s+(\d{6})\s*$")
PRODUCT_LIST_RE = re.compile(r"^\s*(商品列表|商品清單)\s*$")
STAFF_LIST_RE = re.compile(r"^\s*小幫手列表\s*$")
INVITE_RE = re.compile(r"^\s*產生小幫手邀請碼\s*$")
SESSION_LOOKUP_RE = re.compile(
    r"^\s*(?:"
    r"(?P<date_first>\d{1,2}/\d{1,2})(?:\s*(?P<place_after>[^\d/].*?))?"
    r"|"
    r"(?P<place_before>[^\d/].*?)\s*(?P<date_after>\d{1,2}/\d{1,2})"
    r")\s*$"
)


PRICE_ONLY_RE = re.compile(
    r"^\s*(?:NT\$|NTD|\$)?\s*(\d{2,6})\s*(?:元)?\s*$",
    re.IGNORECASE,
)
PRICE_WITH_LABEL_RE = re.compile(
    r"^\s*(.+?)\s*(?:NT\$|NTD|\$)?\s*(\d{2,6})\s*(?:元)?\s*$",
    re.IGNORECASE,
)


def parse_price_text(text):
    """
    Boss / 小幫手回覆商品照片可用：
      199
      199元
      $199
      玲娜199
      玲娜199元

    有前綴文字時，前綴會當作規格標籤。
    """
    text = (text or "").strip()

    if not text:
        return None

    m = PRICE_ONLY_RE.fullmatch(text)
    if m:
        price = int(m.group(1))
        return {
            "price": price,
            "label": "",
        }

    m = PRICE_WITH_LABEL_RE.fullmatch(text)
    if not m:
        return None

    label = m.group(1).strip()
    price = int(m.group(2))

    if not label or price <= 0:
        return None

    # 避免把 +1、日期等普通訊息誤判成價格標籤。
    if "+" in label or "/" in label:
        return None

    return {
        "price": price,
        "label": label,
    }


def remember_price_message(
    message_id,
    group_id,
    image_message_id,
    price,
    label,
    sender_user_id,
):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO price_messages(
                message_id,
                group_id,
                image_message_id,
                price,
                label,
                sender_user_id,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(message_id)
            DO UPDATE SET
                image_message_id=excluded.image_message_id,
                price=excluded.price,
                label=excluded.label,
                sender_user_id=excluded.sender_user_id,
                created_at=excluded.created_at
            """,
            (
                message_id,
                group_id,
                image_message_id,
                int(price),
                label or "",
                sender_user_id,
                now_iso(),
            ),
        )


def get_price_message(message_id):
    if not message_id:
        return None

    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM price_messages
            WHERE message_id=?
            """,
            (message_id,),
        ).fetchone()


def add_order_price_line(
    product_id,
    user_id,
    display_name,
    spec_name,
    unit_price,
    qty,
):
    if unit_price is None:
        return

    with db() as conn:
        current = conn.execute(
            """
            SELECT quantity
            FROM order_price_lines
            WHERE product_id=?
              AND user_id=?
              AND spec_name=?
              AND unit_price=?
            """,
            (
                product_id,
                user_id,
                spec_name,
                int(unit_price),
            ),
        ).fetchone()

        new_qty = (
            int(current["quantity"])
            if current
            else 0
        ) + int(qty)

        conn.execute(
            """
            INSERT INTO order_price_lines(
                product_id,
                user_id,
                display_name,
                spec_name,
                unit_price,
                quantity,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(product_id,user_id,spec_name,unit_price)
            DO UPDATE SET
                display_name=excluded.display_name,
                quantity=excluded.quantity,
                updated_at=excluded.updated_at
            """,
            (
                product_id,
                user_id,
                display_name,
                spec_name,
                int(unit_price),
                new_qty,
                now_iso(),
            ),
        )


def get_order_price_lines(product_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM order_price_lines
            WHERE product_id=? AND quantity>0
            ORDER BY updated_at ASC
            """,
            (product_id,),
        ).fetchall()


def price_summary(product_id):
    rows = get_order_price_lines(product_id)

    by_price = defaultdict(int)
    by_spec_price = defaultdict(int)
    total_amount = 0

    for row in rows:
        price = int(row["unit_price"])
        qty = int(row["quantity"])
        by_price[price] += qty
        by_spec_price[(row["spec_name"], price)] += qty
        total_amount += price * qty

    return rows, by_price, by_spec_price, total_amount



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

        CREATE TABLE IF NOT EXISTS session_staff (
            session_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY(session_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS procurement_items (
            product_id INTEGER NOT NULL,
            spec_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(product_id, spec_name)
        );

        CREATE TABLE IF NOT EXISTS procurement_states (
            operator_user_id TEXT PRIMARY KEY,
            product_id INTEGER NOT NULL,
            selected_spec TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_name TEXT NOT NULL,
            session_code TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            ended_at TEXT
        );

        CREATE TABLE IF NOT EXISTS session_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            group_id TEXT NOT NULL,
            group_letter TEXT NOT NULL,
            next_seq INTEGER NOT NULL DEFAULT 1,
            UNIQUE(session_id, group_id),
            UNIQUE(session_id, group_letter),
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS pending_images (
            message_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            sender_user_id TEXT,
            image_blob BLOB,
            image_mime TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_messages (
            message_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            image_message_id TEXT NOT NULL,
            price INTEGER NOT NULL,
            label TEXT,
            sender_user_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS order_price_lines (
            product_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            display_name TEXT,
            spec_name TEXT NOT NULL,
            unit_price INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(product_id, user_id, spec_name, unit_price)
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code TEXT UNIQUE,
            session_id INTEGER,
            group_id TEXT NOT NULL,
            group_letter TEXT,
            sequence_no INTEGER,
            image_message_id TEXT NOT NULL UNIQUE,
            is_closed INTEGER NOT NULL DEFAULT 0,
            image_key TEXT,
            image_blob BLOB,
            image_mime TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            display_name TEXT,
            spec_name TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(product_id, user_id, spec_name),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        """)

        # Upgrade older products table if needed.
        for column, ddl in [
            ("session_id", "ALTER TABLE products ADD COLUMN session_id INTEGER"),
            ("group_letter", "ALTER TABLE products ADD COLUMN group_letter TEXT"),
            ("sequence_no", "ALTER TABLE products ADD COLUMN sequence_no INTEGER"),
            ("is_closed", "ALTER TABLE products ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0"),
            ("image_key", "ALTER TABLE products ADD COLUMN image_key TEXT"),
            ("image_blob", "ALTER TABLE products ADD COLUMN image_blob BLOB"),
            ("image_mime", "ALTER TABLE products ADD COLUMN image_mime TEXT"),
        ]:
            if not column_exists(conn, "products", column):
                conn.execute(ddl)

        for column, ddl in [
            ("image_blob", "ALTER TABLE pending_images ADD COLUMN image_blob BLOB"),
            ("image_mime", "ALTER TABLE pending_images ADD COLUMN image_mime TEXT"),
        ]:
            if not column_exists(conn, "pending_images", column):
                conn.execute(ddl)

        if not column_exists(conn, "procurement_states", "selected_spec"):
            conn.execute(
                "ALTER TABLE procurement_states ADD COLUMN selected_spec TEXT"
            )


init_db()


# ---------- Settings / roles ----------

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


def get_owner_user_id():
    return ADMIN_USER_ID_ENV or get_setting("owner_user_id") or get_setting("admin_user_id")


def is_owner(user_id):
    owner = get_owner_user_id()
    return bool(owner and user_id and owner == user_id)


def is_staff(user_id):
    if not user_id:
        return False
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM staff WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return bool(row)


def can_query(user_id):
    return is_owner(user_id) or is_staff(user_id)


def generate_staff_invite():
    for _ in range(20):
        code = f"{secrets.randbelow(1000000):06d}"
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO staff_invites(code, created_at) VALUES(?, ?)",
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
            SELECT * FROM staff_invites
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

    refresh_staff_name(user_id)
    return True


def staff_list_text():
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, display_name FROM staff ORDER BY created_at ASC"
        ).fetchall()

    if not rows:
        return "目前沒有小幫手。"

    lines = ["👥 小幫手列表"]
    for i, row in enumerate(rows, start=1):
        name = row["display_name"] or f"小幫手 #{short_code(row['user_id'])}"
        lines.append(f"{i}. {name}")
    return "\n".join(lines)



def procurement_totals(product_id):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT spec_name, quantity
            FROM procurement_items
            WHERE product_id=?
            """,
            (product_id,),
        ).fetchall()

    return {
        row["spec_name"]: int(row["quantity"])
        for row in rows
    }


def procurement_summary(product_id):
    _, _, spec_totals, _, ordered_total = order_summary(product_id)
    purchased = procurement_totals(product_id)

    waiting_by_spec = {}
    purchased_total = 0

    for spec_name, ordered_qty in spec_totals.items():
        bought_qty = min(
            int(purchased.get(spec_name, 0)),
            int(ordered_qty),
        )
        purchased_total += bought_qty
        waiting_by_spec[spec_name] = max(
            int(ordered_qty) - bought_qty,
            0,
        )

    waiting_total = sum(waiting_by_spec.values())

    return (
        spec_totals,
        purchased,
        waiting_by_spec,
        ordered_total,
        purchased_total,
        waiting_total,
    )


def add_procurement(product_id, spec_name, qty):
    qty = int(qty)

    if qty <= 0:
        return

    _, _, waiting_by_spec, _, _, _ = procurement_summary(product_id)
    remaining = int(waiting_by_spec.get(spec_name, 0))

    if remaining <= 0:
        return

    add_qty = min(qty, remaining)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO procurement_items(
                product_id,
                spec_name,
                quantity,
                updated_at
            )
            VALUES(?,?,?,?)
            ON CONFLICT(product_id,spec_name)
            DO UPDATE SET
                quantity=quantity+excluded.quantity,
                updated_at=excluded.updated_at
            """,
            (
                product_id,
                spec_name,
                add_qty,
                now_iso(),
            ),
        )


def set_procurement_state(
    operator_user_id,
    product_id,
    selected_spec=None,
):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO procurement_states(
                operator_user_id,
                product_id,
                selected_spec,
                created_at
            )
            VALUES(?,?,?,?)
            ON CONFLICT(operator_user_id)
            DO UPDATE SET
                product_id=excluded.product_id,
                selected_spec=excluded.selected_spec,
                created_at=excluded.created_at
            """,
            (
                operator_user_id,
                product_id,
                selected_spec,
                now_iso(),
            ),
        )


def get_procurement_state(operator_user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM procurement_states
            WHERE operator_user_id=?
            """,
            (operator_user_id,),
        ).fetchone()


def clear_procurement_state(operator_user_id):
    with db() as conn:
        conn.execute(
            """
            DELETE FROM procurement_states
            WHERE operator_user_id=?
            """,
            (operator_user_id,),
        )


def procurement_prompt_text(product):
    (
        spec_totals,
        purchased,
        waiting,
        ordered_total,
        purchased_total,
        waiting_total,
    ) = procurement_summary(product["id"])

    lines = [
        f"📦 {product['product_code']} 採購入單",
        f"已喊 {ordered_total}｜已拿 {purchased_total}｜待拿 {waiting_total}",
        "",
    ]

    for spec_name, ordered_qty in spec_totals.items():
        got = min(
            int(purchased.get(spec_name, 0)),
            int(ordered_qty),
        )
        left = int(waiting.get(spec_name, 0))
        lines.append(
            f"{spec_name}：喊{ordered_qty}｜拿{got}｜待{left}"
        )

    lines.extend([
        "",
        "輸入這次拿到的數量：",
        "單一規格可打：+14",
        "多規格可打：玲娜+5 包包+3",
        "",
        "輸入「取消入單」離開。",
    ])

    return "\n".join(lines)



def procure_all_remaining(product_id):
    (
        _,
        _,
        waiting_by_spec,
        _,
        _,
        _,
    ) = procurement_summary(product_id)

    for spec_name, waiting_qty in waiting_by_spec.items():
        if int(waiting_qty) > 0:
            add_procurement(
                product_id,
                spec_name,
                int(waiting_qty),
            )


def session_final_summary_messages(session):
    """
    結束連線時輸出總表：
    商品編號、各規格喊單 / 已拿 / 待拿，以及全場總計。
    """
    with db() as conn:
        products = conn.execute(
            """
            SELECT *
            FROM products
            WHERE session_id=?
            ORDER BY group_letter ASC, sequence_no ASC, id ASC
            """,
            (session["id"],),
        ).fetchall()

    grand_ordered = 0
    grand_purchased = 0
    grand_waiting = 0

    sections = [
        f"📋 {session['session_code']} 結束連線總表"
    ]

    if not products:
        sections.append("本場沒有成立商品。")
    else:
        for product in products:
            (
                spec_totals,
                purchased,
                waiting_by_spec,
                ordered_total,
                purchased_total,
                waiting_total,
            ) = procurement_summary(product["id"])

            grand_ordered += int(ordered_total)
            grand_purchased += int(purchased_total)
            grand_waiting += int(waiting_total)

            lines = [
                "",
                f"【{product['product_code']}】",
                f"總喊 {ordered_total}｜已拿 {purchased_total}｜待拿 {waiting_total}",
            ]

            for spec_name, ordered_qty in sorted(
                spec_totals.items(),
                key=lambda x: x[0],
            ):
                got_qty = min(
                    int(purchased.get(spec_name, 0)),
                    int(ordered_qty),
                )
                waiting_qty = int(
                    waiting_by_spec.get(spec_name, 0)
                )

                lines.append(
                    f"・{spec_name}：喊{ordered_qty}｜拿{got_qty}｜待{waiting_qty}"
                )

            sections.append("\\n".join(lines))

    sections.append(
        "\\n"
        f"📦 全場總計\\n"
        f"已喊 {grand_ordered}\\n"
        f"已拿 {grand_purchased}\\n"
        f"待拿 {grand_waiting}"
    )

    # LINE 單則文字上限 5000，最多回覆 5 則；保留第 1 則給結束通知。
    chunks = []
    current = ""

    for section in sections:
        candidate = (
            section
            if not current
            else current + "\\n" + section
        )

        if len(candidate) <= 4500:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = section

    if current:
        chunks.append(current)

    return [
        {"type": "text", "text": chunk[:5000]}
        for chunk in chunks[:4]
    ]


def outstanding_products_text():
    session = get_active_session()

    if not session:
        return "目前沒有進行中的連線。"

    with db() as conn:
        products = conn.execute(
            """
            SELECT *
            FROM products
            WHERE session_id=?
            ORDER BY id ASC
            """,
            (session["id"],),
        ).fetchall()

    lines = [
        f"📍 {session['session_code']}",
        "🛒 待拿商品",
    ]

    found = False

    for product in products:
        (
            _,
            _,
            waiting,
            ordered_total,
            purchased_total,
            waiting_total,
        ) = procurement_summary(product["id"])

        if waiting_total <= 0:
            continue

        found = True
        spec_text = "、".join(
            f"{spec}×{qty}"
            for spec, qty in waiting.items()
            if qty > 0
        )

        lines.append(
            f"{product['product_code']}｜"
            f"喊{ordered_total}｜拿{purchased_total}｜待{waiting_total}"
        )
        if spec_text:
            lines.append(f"　{spec_text}")

    if not found:
        lines.append("✅ 目前全部都拿齊了。")

    return "\n".join(lines)



# ---------- LINE API ----------

def verify_signature(raw_body, signature):
    if not CHANNEL_SECRET or not signature:
        return False

    digest = hmac.new(
        CHANNEL_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).digest()

    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


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
        [{"type": "text", "text": text[:5000]}],
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
    profile = get_user_profile(user_id)
    name = (profile or {}).get("displayName")

    if name:
        with db() as conn:
            conn.execute(
                "UPDATE staff SET display_name=? WHERE user_id=?",
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


def fetch_line_image(message_id):
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


# ---------- Sessions ----------

def clean_session_code(text):
    """
    Accepts:
      香港8/18
      香港 8/18
      8/18香港
      8/18 香港
      香港8/18-19
      8/18-19 香港
      東京10/19
    Produces:
      0818香港連線
      081819香港連線
      1019東京連線
    """
    text = text.strip()

    date_re = re.compile(
        r"(?P<m1>\d{1,2})/(?P<d1>\d{1,2})"
        r"(?:\s*[-~～至]\s*(?:(?P<m2>\d{1,2})/)?(?P<d2>\d{1,2}))?"
    )

    m = date_re.search(text)

    if not m:
        compact = re.sub(r"\s+", "", text)
        return compact if compact.endswith("連線") else compact + "連線"

    month1 = int(m.group("m1"))
    day1 = int(m.group("d1"))
    month2 = int(m.group("m2")) if m.group("m2") else month1
    day2 = int(m.group("d2")) if m.group("d2") else None

    before = text[:m.start()].strip()
    after = text[m.end():].strip()
    place = re.sub(r"\s+", "", f"{before}{after}")

    # Remove accidental command text if explicit syntax is used.
    place = re.sub(r"^開始連線", "", place).strip()

    date_code = f"{month1:02d}{day1:02d}"

    if day2 is not None:
        if month2 == month1:
            date_code += f"{day2:02d}"
        else:
            date_code += f"{month2:02d}{day2:02d}"

    return f"{date_code}{place}連線"


def start_session(name):
    session_code = clean_session_code(name)

    with db() as conn:
        # Only one active session at a time.
        conn.execute(
            """
            UPDATE sessions
            SET is_active=0, ended_at=?
            WHERE is_active=1
            """,
            (now_iso(),),
        )

        cur = conn.execute(
            """
            INSERT INTO sessions(
                session_name,
                session_code,
                is_active,
                created_at
            )
            VALUES(?,?,1,?)
            """,
            (
                name.strip(),
                session_code,
                now_iso(),
            ),
        )

        session_id = cur.lastrowid

    return get_session(session_id)


def get_session(session_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()


def get_active_session():
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM sessions
            WHERE is_active=1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()


def end_active_session():
    with db() as conn:
        session = conn.execute(
            """
            SELECT * FROM sessions
            WHERE is_active=1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if not session:
            return None

        conn.execute(
            """
            UPDATE sessions
            SET is_active=0, ended_at=?
            WHERE id=?
            """,
            (
                now_iso(),
                session["id"],
            ),
        )

    return session


def next_group_letter(session_id):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT group_letter
            FROM session_groups
            WHERE session_id=?
            """,
            (session_id,),
        ).fetchall()

    used = {row["group_letter"] for row in rows}

    for letter in string.ascii_uppercase:
        if letter not in used:
            return letter

    # Fallback if more than 26 groups.
    return f"G{len(used) + 1}"


def get_or_create_session_group(session_id, group_id):
    with db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM session_groups
            WHERE session_id=? AND group_id=?
            """,
            (
                session_id,
                group_id,
            ),
        ).fetchone()

        if row:
            return row

    letter = next_group_letter(session_id)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO session_groups(
                session_id,
                group_id,
                group_letter,
                next_seq
            )
            VALUES(?,?,?,1)
            """,
            (
                session_id,
                group_id,
                letter,
            ),
        )

        return conn.execute(
            """
            SELECT *
            FROM session_groups
            WHERE session_id=? AND group_id=?
            """,
            (
                session_id,
                group_id,
            ),
        ).fetchone()


def allocate_product_code(session, group_id):
    group = get_or_create_session_group(
        session["id"],
        group_id,
    )

    with db() as conn:
        row = conn.execute(
            """
            SELECT next_seq
            FROM session_groups
            WHERE id=?
            """,
            (group["id"],),
        ).fetchone()

        seq = row["next_seq"]

        conn.execute(
            """
            UPDATE session_groups
            SET next_seq=next_seq+1
            WHERE id=?
            """,
            (group["id"],),
        )

    product_code = (
        f"{session['session_code']}"
        f"{group['group_letter']}"
        f"{seq:03d}"
    )

    return (
        product_code,
        group["group_letter"],
        seq,
    )


# ---------- Images / products ----------

def remember_image(message_id, group_id, sender_user_id):
    blob, mime = fetch_line_image(message_id)

    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO pending_images(
                message_id,
                group_id,
                sender_user_id,
                image_blob,
                image_mime,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                message_id,
                group_id,
                sender_user_id,
                sqlite3.Binary(blob) if blob else None,
                mime,
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


def get_product_by_image(message_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM products
            WHERE image_message_id=?
            """,
            (message_id,),
        ).fetchone()


def get_product_by_code(product_code):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM products
            WHERE product_code=?
            """,
            (product_code,),
        ).fetchone()


def create_product_from_pending(image):
    session = get_active_session()

    if not session:
        return None, "NO_ACTIVE_SESSION"

    product_code, group_letter, seq = allocate_product_code(
        session,
        image["group_id"],
    )

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO products(
                product_code,
                session_id,
                group_id,
                group_letter,
                sequence_no,
                image_message_id,
                is_closed,
                image_key,
                image_blob,
                image_mime,
                created_at
            )
            VALUES(?,?,?,?,?,?,0,?,?,?,?)
            """,
            (
                product_code,
                session["id"],
                image["group_id"],
                group_letter,
                seq,
                image["message_id"],
                secrets.token_urlsafe(16),
                image["image_blob"],
                image["image_mime"],
                now_iso(),
            ),
        )
        product_id = cur.lastrowid

    return get_product_by_code(product_code), None


def product_image_url(product):
    if (
        not product
        or not product["image_blob"]
        or not product["image_key"]
    ):
        return None

    base = request.host_url.rstrip("/")

    return (
        f"{base}/product-image/"
        f"{product['id']}/"
        f"{product['image_key']}"
    )


def set_product_closed(product_id, closed=True):
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


# ---------- Orders / specs ----------

def normalize_spec(spec):
    spec = (spec or "").strip()
    return spec if spec else "單一規格"


def parse_order_text(text):
    """
    規格完全由客人輸入內容自動抓，不需要事先設定。

    支援：
      +1
      包包+1
      玲娜+1
      包包+1 玲娜+1
      包包+1、玲娜+1
      包包+1
      玲娜+2

    規則：
      「+數量」前面的文字 = 規格名稱

    回傳：
      ("ADD", [("包包", 1), ("玲娜", 1)])
      ("CANCEL_ALL", [])
      ("CANCEL_SPEC", [("玲娜", None)])
    """
    text = (text or "").strip()

    if not text:
        return None

    # 統一全形符號
    normalized_text = (
        text.replace("＋", "+")
            .replace("，", " ")
            .replace(",", " ")
            .replace("、", " ")
            .replace("；", " ")
            .replace(";", " ")
            .replace("|", " ")
    )

    # 取消全部
    if CANCEL_ONLY_RE.fullmatch(normalized_text.strip()):
        return ("CANCEL_ALL", [])

    # 單一規格取消，例如：玲娜取消
    cancel_match = SPEC_CANCEL_RE.fullmatch(normalized_text.strip())
    if cancel_match:
        return (
            "CANCEL_SPEC",
            [(normalize_spec(cancel_match.group(1)), None)],
        )

    # 純 +1 / +2 = 單一規格
    plus_only = PLUS_ONLY_RE.fullmatch(normalized_text.strip())
    if plus_only:
        qty = int(plus_only.group(1))
        if 1 <= qty <= 99:
            return ("ADD", [("單一規格", qty)])
        return None

    # 動態抓規格：
    # 每一段「任意文字 + 數量」都視為一個規格
    # 例如：包包+1 玲娜+1
    #
    # 利用 lookahead 停在下一個規格前，避免把「玲娜」黏到上一個規格。
    item_re = re.compile(
        r"(?:^|[\s\n\r]+)"
        r"(.+?)"
        r"\s*\+\s*(\d+)"
        r"(?=$|[\s\n\r]+)"
    )

    # 為了讓「包包+1、玲娜+1」先變成空白分隔
    scan_text = re.sub(r"\s+", " ", normalized_text).strip()
    scan_text = " " + scan_text + " "

    items = []

    for match in item_re.finditer(scan_text):
        spec = normalize_spec(match.group(1).strip())
        qty = int(match.group(2))

        if spec and 1 <= qty <= 99:
            items.append((spec, qty))

    # 備援：處理完全無空格黏在一起的簡單格式，例如 包包+1玲娜+1
    if not items:
        compact_re = re.compile(
            r"([^+\d][^+]*?)\s*\+\s*(\d+)"
        )
        for match in compact_re.finditer(normalized_text):
            spec = normalize_spec(match.group(1).strip())
            qty = int(match.group(2))
            if spec and 1 <= qty <= 99:
                items.append((spec, qty))

    if items:
        return ("ADD", items)

    return None


def add_order_item(product_id, user_id, display_name, spec_name, qty):
    with db() as conn:
        current = conn.execute(
            """
            SELECT quantity
            FROM order_items
            WHERE product_id=? AND user_id=? AND spec_name=?
            """,
            (
                product_id,
                user_id,
                spec_name,
            ),
        ).fetchone()

        new_qty = (
            current["quantity"]
            if current
            else 0
        ) + qty

        conn.execute(
            """
            INSERT INTO order_items(
                product_id,
                user_id,
                display_name,
                spec_name,
                quantity,
                updated_at
            )
            VALUES(?,?,?,?,?,?)

            ON CONFLICT(product_id,user_id,spec_name)
            DO UPDATE SET
                display_name=excluded.display_name,
                quantity=excluded.quantity,
                updated_at=excluded.updated_at
            """,
            (
                product_id,
                user_id,
                display_name,
                spec_name,
                new_qty,
                now_iso(),
            ),
        )


def cancel_user_all(product_id, user_id):
    with db() as conn:
        conn.execute(
            """
            DELETE FROM order_items
            WHERE product_id=? AND user_id=?
            """,
            (
                product_id,
                user_id,
            ),
        )
        conn.execute(
            """
            DELETE FROM order_price_lines
            WHERE product_id=? AND user_id=?
            """,
            (
                product_id,
                user_id,
            ),
        )


def cancel_user_spec(product_id, user_id, spec_name):
    with db() as conn:
        conn.execute(
            """
            DELETE FROM order_items
            WHERE product_id=? AND user_id=? AND spec_name=?
            """,
            (
                product_id,
                user_id,
                spec_name,
            ),
        )
        conn.execute(
            """
            DELETE FROM order_price_lines
            WHERE product_id=? AND user_id=? AND spec_name=?
            """,
            (
                product_id,
                user_id,
                spec_name,
            ),
        )


def get_order_items(product_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM order_items
            WHERE product_id=? AND quantity>0
            ORDER BY updated_at ASC
            """,
            (product_id,),
        ).fetchall()


def order_summary(product_id):
    rows = get_order_items(product_id)

    per_user = defaultdict(list)
    spec_totals = defaultdict(int)
    user_ids = set()
    total_qty = 0

    for row in rows:
        per_user[row["user_id"]].append(row)
        spec_totals[row["spec_name"]] += row["quantity"]
        user_ids.add(row["user_id"])
        total_qty += row["quantity"]

    return (
        rows,
        per_user,
        spec_totals,
        len(user_ids),
        total_qty,
    )



def join_active_session_staff(user_id):
    if not is_staff(user_id):
        return None, "NOT_STAFF"

    session = get_active_session()

    if not session:
        return None, "NO_ACTIVE_SESSION"

    with db() as conn:
        conn.execute(
            """
            INSERT INTO session_staff(
                session_id,
                user_id,
                joined_at
            )
            VALUES(?,?,?)
            ON CONFLICT(session_id,user_id)
            DO NOTHING
            """,
            (
                session["id"],
                user_id,
                now_iso(),
            ),
        )

    return session, None


def leave_active_session_staff(user_id):
    session = get_active_session()

    if not session:
        return None

    with db() as conn:
        conn.execute(
            """
            DELETE FROM session_staff
            WHERE session_id=? AND user_id=?
            """,
            (
                session["id"],
                user_id,
            ),
        )

    return session


def get_session_staff_user_ids(session_id):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id
            FROM session_staff
            WHERE session_id=?
            ORDER BY joined_at ASC
            """,
            (session_id,),
        ).fetchall()

    return [row["user_id"] for row in rows]


def live_notification_user_ids(session_id):
    ids = []

    owner_id = get_owner_user_id()
    if owner_id:
        ids.append(owner_id)

    for staff_user_id in get_session_staff_user_ids(session_id):
        if staff_user_id not in ids:
            ids.append(staff_user_id)

    return ids


def build_live_order_card(product, viewer_user_id):
    (
        _,
        _,
        spec_totals,
        people_count,
        total_qty,
    ) = order_summary(product["id"])

    (
        price_rows,
        by_price,
        by_spec_price,
        total_amount,
    ) = price_summary(product["id"])

    body = [
        {
            "type": "text",
            "text": f"🔔 {product['product_code']} 喊單更新",
            "weight": "bold",
            "size": "lg",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"👥 {people_count} 人｜📦 累計 {total_qty} 件",
            "size": "sm",
            "margin": "sm",
            "weight": "bold",
        },
        {
            "type": "text",
            "text": (
                f"💰 累計金額 ${total_amount}"
                if total_amount
                else "💰 尚未記錄價格"
            ),
            "size": "sm",
            "margin": "sm",
            "weight": "bold",
        },
        {
            "type": "separator",
            "margin": "lg",
        },
        {
            "type": "text",
            "text": "規格累計",
            "weight": "bold",
            "size": "sm",
            "margin": "lg",
        },
    ]

    if spec_totals:
        for spec_name, qty in sorted(
            spec_totals.items(),
            key=lambda x: x[0],
        ):
            body.append({
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": spec_name,
                        "size": "sm",
                        "flex": 1,
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": f"× {qty}",
                        "size": "sm",
                        "weight": "bold",
                        "align": "end",
                        "flex": 0,
                    },
                ],
            })
    else:
        body.append({
            "type": "text",
            "text": "目前 0 件",
            "size": "sm",
            "color": "#777777",
            "margin": "sm",
        })

    if by_spec_price:
        body.extend([
            {
                "type": "separator",
                "margin": "lg",
            },
            {
                "type": "text",
                "text": "價格累計",
                "weight": "bold",
                "size": "sm",
                "margin": "lg",
            },
        ])

        for (spec_name, price), qty in sorted(
            by_spec_price.items(),
            key=lambda x: (x[0][0], x[0][1]),
        ):
            body.append({
                "type": "text",
                "text": f"{spec_name}｜${price} × {qty}",
                "size": "xs",
                "margin": "sm",
                "wrap": True,
            })

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "action": {
                        "type": "postback",
                        "label": "查看完整查單",
                        "data": f"action=query&product_id={product['id']}",
                        "displayText": f"{product['product_code']} 查單",
                    },
                }
            ],
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
        "altText": f"{product['product_code']} 累計喊單 {total_qty}",
        "contents": bubble,
    }


def notify_live_order_update(product):
    if not product:
        return

    for target_user_id in live_notification_user_ids(
        product["session_id"]
    ):
        push_messages(
            target_user_id,
            [
                build_live_order_card(
                    product,
                    target_user_id,
                )
            ],
        )


def build_final_product_bubble(product):
    (
        rows,
        per_user,
        spec_totals,
        people_count,
        total_qty,
    ) = order_summary(product["id"])

    (
        price_rows,
        by_price,
        by_spec_price,
        total_amount,
    ) = price_summary(product["id"])

    name_counts = Counter(
        (
            items[0]["display_name"]
            or "未知會員"
        )
        for items in per_user.values()
    )

    body = [
        {
            "type": "text",
            "text": product["product_code"],
            "weight": "bold",
            "size": "md",
            "wrap": True,
        },
        {
            "type": "text",
            "text": f"👥 {people_count} 人｜📦 {total_qty} 件",
            "size": "sm",
            "weight": "bold",
            "margin": "sm",
        },
        {
            "type": "text",
            "text": (
                f"💰 ${total_amount}"
                if total_amount
                else "💰 未記錄價格"
            ),
            "size": "xs",
            "weight": "bold",
            "margin": "sm",
        },
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": "喊單人",
            "weight": "bold",
            "size": "sm",
            "margin": "md",
        },
    ]

    if per_user:
        for user_id, items in per_user.items():
            display_name = (
                items[0]["display_name"]
                or "未知會員"
            )

            if name_counts[display_name] > 1:
                display_name = (
                    f"{display_name} "
                    f"#{short_code(user_id)}"
                )

            body.append({
                "type": "text",
                "text": display_name,
                "size": "xs",
                "weight": "bold",
                "margin": "sm",
                "wrap": True,
            })

            for item in items:
                body.append({
                    "type": "box",
                    "layout": "horizontal",
                    "margin": "xs",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"・{item['spec_name']}",
                            "size": "xxs",
                            "color": "#555555",
                            "flex": 1,
                            "wrap": True,
                        },
                        {
                            "type": "text",
                            "text": f"× {item['quantity']}",
                            "size": "xxs",
                            "weight": "bold",
                            "align": "end",
                            "flex": 0,
                        },
                    ],
                })
    else:
        body.append({
            "type": "text",
            "text": "目前沒有喊單",
            "size": "xs",
            "color": "#777777",
            "margin": "sm",
        })

    body.extend([
        {
            "type": "separator",
            "margin": "md",
        },
        {
            "type": "text",
            "text": "規格總計",
            "weight": "bold",
            "size": "sm",
            "margin": "md",
        },
    ])

    if spec_totals:
        for spec_name, qty in sorted(
            spec_totals.items(),
            key=lambda x: x[0],
        ):
            body.append({
                "type": "box",
                "layout": "horizontal",
                "margin": "xs",
                "contents": [
                    {
                        "type": "text",
                        "text": spec_name,
                        "size": "xs",
                        "flex": 1,
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": f"× {qty}",
                        "size": "xs",
                        "weight": "bold",
                        "align": "end",
                        "flex": 0,
                    },
                ],
            })

    if by_spec_price:
        body.extend([
            {
                "type": "separator",
                "margin": "md",
            },
            {
                "type": "text",
                "text": "價格",
                "weight": "bold",
                "size": "sm",
                "margin": "md",
            },
        ])

        for (spec_name, price), qty in sorted(
            by_spec_price.items(),
            key=lambda x: (x[0][0], x[0][1]),
        ):
            body.append({
                "type": "text",
                "text": f"{spec_name}｜${price} × {qty}",
                "size": "xxs",
                "margin": "xs",
                "wrap": True,
            })

    bubble = {
        "type": "bubble",
        "size": "micro",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body,
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

    return bubble

def build_final_session_carousels(session):
    with db() as conn:
        products = conn.execute(
            """
            SELECT *
            FROM products
            WHERE session_id=?
            ORDER BY group_letter ASC, sequence_no ASC, id ASC
            """,
            (session["id"],),
        ).fetchall()

    if not products:
        return [
            {
                "type": "text",
                "text": f"📋 {session['session_code']} 本場沒有成立商品。",
            }
        ]

    messages = []

    # LINE Flex carousel 分批顯示，左滑查看所有商品。
    for start in range(0, len(products), 10):
        batch = products[start:start + 10]

        messages.append({
            "type": "flex",
            "altText": (
                f"{session['session_code']} "
                f"總表 {start + 1}-{start + len(batch)}"
            ),
            "contents": {
                "type": "carousel",
                "contents": [
                    build_final_product_bubble(product)
                    for product in batch
                ],
            },
        })

    return messages


def send_final_session_carousels(
    session,
    reply_token,
    owner_user_id,
):
    messages = build_final_session_carousels(session)

    first_batch = messages[:4]

    reply_messages(
        reply_token,
        [
            {
                "type": "text",
                "text": (
                    f"✅ 已結束 {session['session_code']}\n"
                    "以下是本場全部商品喊單總表，左右滑動查看。"
                ),
            },
            *first_batch,
        ],
    )

    remaining = messages[4:]

    # 超過一則回覆可承載數量時，繼續用 push 傳給 Owner。
    for i in range(0, len(remaining), 5):
        push_messages(
            owner_user_id,
            remaining[i:i + 5],
        )



# ---------- Flex cards ----------

def short_code(user_id):
    return hashlib.sha256(
        (user_id or "?").encode()
    ).hexdigest()[:4].upper()


def product_action_buttons(product, viewer_user_id):
    buttons = [
        {
            "type": "button",
            "style": "primary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "查單",
                "data": f"action=query&product_id={product['id']}",
                "displayText": f"{product['product_code']} 查單",
            },
        }
    ]

    if is_owner(viewer_user_id):
        buttons.append({
            "type": "button",
            "style": "secondary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "結單",
                "data": f"action=close&product_id={product['id']}",
                "displayText": f"{product['product_code']} 結單",
            },
        })

    return buttons


def _short_button_label(spec_name):
    text = f"{spec_name} +1"
    return text if len(text) <= 20 else text[:19] + "…"


def product_procurement_buttons(product, viewer_user_id):
    buttons = [
        {
            "type": "button",
            "style": "primary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "查單",
                "data": f"action=query&product_id={product['id']}",
                "displayText": f"{product['product_code']} 查單",
            },
        }
    ]

    if can_query(viewer_user_id):
        (
            spec_totals,
            purchased,
            waiting_by_spec,
            ordered_total,
            purchased_total,
            waiting_total,
        ) = procurement_summary(product["id"])

        pending_specs = [
            (spec_name, int(waiting_qty))
            for spec_name, waiting_qty in waiting_by_spec.items()
            if int(waiting_qty) > 0
        ]

        if waiting_total > 0:
            buttons.append({
                "type": "button",
                "style": "primary",
                "height": "sm",
                "action": {
                    "type": "postback",
                    "label": f"全部全入（{waiting_total}）",
                    "data": (
                        f"action=procure_all_product"
                        f"&product_id={product['id']}"
                    ),
                    "displayText": f"{product['product_code']} 全部全入",
                },
            })

        for spec_name, waiting_qty in sorted(
            pending_specs,
            key=lambda x: x[0],
        ):
            encoded_spec = quote_plus(spec_name)

            buttons.append({
                "type": "box",
                "layout": "vertical",
                "margin": "md",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": f"{spec_name}｜待拿 {waiting_qty}",
                        "size": "sm",
                        "weight": "bold",
                        "wrap": True,
                    },
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "spacing": "sm",
                        "contents": [
                            {
                                "type": "button",
                                "style": "secondary",
                                "height": "sm",
                                "flex": 1,
                                "action": {
                                    "type": "postback",
                                    "label": "輸入數量",
                                    "data": (
                                        f"action=procure_qty"
                                        f"&product_id={product['id']}"
                                        f"&spec={encoded_spec}"
                                    ),
                                    "displayText": (
                                        f"{product['product_code']} "
                                        f"{spec_name} 輸入數量"
                                    ),
                                },
                            },
                            {
                                "type": "button",
                                "style": "secondary",
                                "height": "sm",
                                "flex": 1,
                                "action": {
                                    "type": "postback",
                                    "label": f"全入 {waiting_qty}",
                                    "data": (
                                        f"action=procure_all_spec"
                                        f"&product_id={product['id']}"
                                        f"&spec={encoded_spec}"
                                    ),
                                    "displayText": (
                                        f"{product['product_code']} "
                                        f"{spec_name} 全入"
                                    ),
                                },
                            },
                        ],
                    },
                ],
            })

        if waiting_total <= 0:
            buttons.append({
                "type": "button",
                "style": "secondary",
                "height": "sm",
                "action": {
                    "type": "postback",
                    "label": "✅ 已全部拿完",
                    "data": f"action=query&product_id={product['id']}",
                    "displayText": f"{product['product_code']} 查單",
                },
            })

    if is_owner(viewer_user_id):
        buttons.append({
            "type": "button",
            "style": "secondary",
            "height": "sm",
            "action": {
                "type": "postback",
                "label": "結單",
                "data": f"action=close&product_id={product['id']}",
                "displayText": f"{product['product_code']} 結單",
            },
        })

    return buttons


def build_product_card(
    product,
    viewer_user_id,
    title=None,
    procurement_mode=False,
):
    (
        rows,
        per_user,
        spec_totals,
        people_count,
        total_qty,
    ) = order_summary(product["id"])

    name_counts = Counter(
        (
            items[0]["display_name"]
            or "未知會員"
        )
        for items in per_user.values()
    )

    body = [
        {
            "type": "text",
            "text": title or product["product_code"],
            "weight": "bold",
            "size": "lg",
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

    if per_user:
        for user_id, items in per_user.items():
            display_name = items[0]["display_name"] or "未知會員"

            if name_counts[display_name] > 1:
                display_name = (
                    f"{display_name} "
                    f"#{short_code(user_id)}"
                )

            profile = get_group_profile(
                product["group_id"],
                user_id,
            )

            header_contents = []

            if profile.get("pictureUrl"):
                header_contents.append({
                    "type": "image",
                    "url": profile["pictureUrl"],
                    "size": "xxs",
                    "aspectMode": "cover",
                    "aspectRatio": "1:1",
                    "flex": 0,
                })

            header_contents.append({
                "type": "text",
                "text": display_name,
                "size": "sm",
                "weight": "bold",
                "flex": 1,
                "margin": "md",
                "wrap": True,
            })

            body.append({
                "type": "box",
                "layout": "horizontal",
                "alignItems": "center",
                "margin": "lg",
                "contents": header_contents,
            })

            for item in items:
                body.append({
                    "type": "box",
                    "layout": "horizontal",
                    "margin": "sm",
                    "contents": [
                        {
                            "type": "text",
                            "text": f"・{item['spec_name']}",
                            "size": "xs",
                            "color": "#555555",
                            "flex": 1,
                            "wrap": True,
                        },
                        {
                            "type": "text",
                            "text": f"× {item['quantity']}",
                            "size": "xs",
                            "weight": "bold",
                            "align": "end",
                            "flex": 0,
                        },
                    ],
                })

    else:
        body.append({
            "type": "text",
            "text": "目前還沒有人喊單",
            "size": "sm",
            "color": "#777777",
            "margin": "lg",
        })

    body.extend([
        {
            "type": "separator",
            "margin": "lg",
        },
        {
            "type": "text",
            "text": "📦 規格總計",
            "weight": "bold",
            "size": "sm",
            "margin": "lg",
        },
    ])

    if spec_totals:
        for spec_name, qty in sorted(
            spec_totals.items(),
            key=lambda x: x[0],
        ):
            body.append({
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": spec_name,
                        "size": "sm",
                        "flex": 1,
                        "wrap": True,
                    },
                    {
                        "type": "text",
                        "text": f"× {qty}",
                        "size": "sm",
                        "weight": "bold",
                        "align": "end",
                        "flex": 0,
                    },
                ],
            })
    else:
        body.append({
            "type": "text",
            "text": "尚無規格資料",
            "size": "xs",
            "color": "#777777",
            "margin": "sm",
        })

    body.extend([
        {
            "type": "separator",
            "margin": "lg",
        },
        {
            "type": "box",
            "layout": "horizontal",
            "margin": "lg",
            "contents": [
                {
                    "type": "text",
                    "text": f"👥 {people_count} 人",
                    "size": "sm",
                    "flex": 1,
                },
                {
                    "type": "text",
                    "text": f"📦 總數 {total_qty}",
                    "size": "sm",
                    "weight": "bold",
                    "align": "end",
                    "flex": 1,
                },
            ],
        },
    ])

    bubble = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": product_action_buttons(
                product,
                viewer_user_id,
            ),
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

def notify_owner_product_created(product):
    owner_id = get_owner_user_id()

    if not owner_id:
        return

    push_messages(
        owner_id,
        [
            build_product_card(
                product,
                owner_id,
                title=f"🛍️ 新商品 {product['product_code']}",
            )
        ],
    )



def find_session_for_lookup(text):
    """
    支援小幫手 / Owner：
      8/16
      8/16香港
      8/16 香港
      香港8/16
      香港 8/16

    只有日期時：優先目前進行中的同日期場次。
    有地區時：優先找同日期 + 地區的場次。
    """
    text = (text or "").strip()
    match = SESSION_LOOKUP_RE.match(text)

    if not match:
        return None

    date_text = match.group("date_first") or match.group("date_after")
    place = (
        match.group("place_after")
        or match.group("place_before")
        or ""
    ).strip()

    month, day = date_text.split("/", 1)
    prefix = f"{int(month):02d}{int(day):02d}"

    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM sessions
            WHERE session_code LIKE ?
            ORDER BY is_active DESC, id DESC
            """,
            (prefix + "%",),
        ).fetchall()

    if not rows:
        return None

    if place:
        compact_place = re.sub(r"\s+", "", place)
        for row in rows:
            if compact_place in row["session_code"]:
                return row

    return rows[0]


def get_current_session_for_cards():
    session = get_active_session()
    if session:
        return session

    with db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM sessions
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()


def build_session_products_carousel(session, viewer_user_id):
    """顯示該場次最近 10 個商品；小幫手只有查單按鈕。"""
    with db() as conn:
        products = conn.execute(
            """
            SELECT *
            FROM products
            WHERE session_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (session["id"],),
        ).fetchall()

    if not products:
        return {
            "type": "text",
            "text": f"📍 {session['session_code']}\n目前還沒有成立的商品。",
        }

    bubbles = []

    for product in reversed(products):
        _, _, spec_totals, people_count, total_qty = order_summary(product["id"])
        (
            _,
            _,
            waiting_by_spec,
            ordered_total,
            purchased_total,
            waiting_total,
        ) = procurement_summary(product["id"])

        body = [
            {
                "type": "text",
                "text": product["product_code"],
                "weight": "bold",
                "size": "md",
                "wrap": True,
            },
            {
                "type": "text",
                "text": "🔒 已結單" if product["is_closed"] else "🟢 開放喊單",
                "size": "xs",
                "color": "#666666",
                "margin": "sm",
            },
            {
                "type": "text",
                "text": f"👥 {people_count} 人　📦 {total_qty} 件",
                "size": "sm",
                "margin": "md",
            },
            {
                "type": "text",
                "text": f"🛒 拿 {purchased_total}｜待 {waiting_total}",
                "size": "xs",
                "weight": "bold",
                "margin": "sm",
            },
        ]

        if spec_totals:
            summary_text = "｜".join(
                f"{spec}×{qty}"
                for spec, qty in list(spec_totals.items())[:4]
            )
            body.append({
                "type": "text",
                "text": summary_text,
                "size": "xs",
                "color": "#555555",
                "wrap": True,
                "margin": "sm",
            })

        bubble = {
            "type": "bubble",
            "size": "micro",
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": body,
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": product_action_buttons(product, viewer_user_id),
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

        bubbles.append(bubble)

    return {
        "type": "flex",
        "altText": f"{session['session_code']} 商品列表",
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }


def product_list_text():
    session = get_active_session()

    if not session:
        return "目前沒有進行中的連線。"

    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM products
            WHERE session_id=?
            ORDER BY id DESC
            LIMIT 30
            """,
            (session["id"],),
        ).fetchall()

    if not rows:
        return (
            f"📍 {session['session_code']}\n"
            "目前還沒有成立的商品。"
        )

    lines = [
        f"📍 {session['session_code']}",
        "最近商品：",
    ]

    for product in rows:
        (
            _,
            _,
            _,
            people_count,
            total_qty,
        ) = order_summary(product["id"])

        status = "🔒" if product["is_closed"] else "🟢"

        lines.append(
            f"{status} {product['product_code']}｜"
            f"{people_count}人｜{total_qty}件"
        )

    return "\n".join(lines)


# ---------- Routes ----------

@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "Maison Lumi LINE Bot",
        "version": "21-price-thread-orders",
    })


@app.get("/product-image/<int:product_id>/<image_key>")
def serve_product_image(product_id, image_key):
    with db() as conn:
        product = conn.execute(
            "SELECT * FROM products WHERE id=?",
            (product_id,),
        ).fetchone()

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
        headers={
            "Cache-Control": "private, max-age=86400",
        },
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

    body = request.get_json(silent=True) or {}

    for event in body.get("events", []):
        event_type = event.get("type")

        # 客人收回 LINE 訊息時，LINE 會送 unsend 事件。
        # 這裡刻意忽略：已成立的喊單不會因收回訊息而刪除。
        if event_type == "unsend":
            continue

        source = event.get("source", {})
        source_type = source.get("type")
        user_id = source.get("userId")
        reply_token = event.get("replyToken")

        # ---------- POSTBACK BUTTONS ----------
        if event_type == "postback":
            if source_type != "user" or not user_id:
                continue

            data = event.get("postback", {}).get("data", "")
            params = dict(
                pair.split("=", 1)
                for pair in data.split("&")
                if "=" in pair
            )

            action = params.get("action")
            product_id = params.get("product_id")

            if not product_id or not product_id.isdigit():
                continue

            with db() as conn:
                product = conn.execute(
                    "SELECT * FROM products WHERE id=?",
                    (int(product_id),),
                ).fetchone()

            if not product:
                reply_text(reply_token, "找不到這個商品。")
                continue

            if action == "query":
                if not can_query(user_id):
                    continue

                reply_messages(
                    reply_token,
                    [
                        build_product_card(
                            product,
                            user_id,
                            title=f"📋 {product['product_code']} 查單",
                        )
                    ],
                )
                continue

            if action == "procure":
                if not can_query(user_id):
                    continue

                set_procurement_state(
                    user_id,
                    product["id"],
                )

                reply_messages(
                    reply_token,
                    [
                        build_product_card(
                            product,
                            user_id,
                            title=f"📦 {product['product_code']} 採購進度",
                            procurement_mode=True,
                        )
                    ],
                )
                continue

            if action == "procure_qty":
                if not can_query(user_id):
                    continue

                spec_name = unquote_plus(
                    params.get("spec", "")
                ).strip()

                if not spec_name:
                    continue

                set_procurement_state(
                    user_id,
                    product["id"],
                    selected_spec=spec_name,
                )

                (
                    _,
                    _,
                    waiting_by_spec,
                    _,
                    _,
                    _,
                ) = procurement_summary(product["id"])

                waiting_qty = int(
                    waiting_by_spec.get(spec_name, 0)
                )

                reply_text(
                    reply_token,
                    f"✍️ {product['product_code']}\n"
                    f"規格：{spec_name}\n"
                    f"目前待拿：{waiting_qty}\n\n"
                    "請直接輸入這次拿到的數量，例如：5\n"
                    "輸入「取消入單」離開。",
                )
                continue

            if action == "procure_all_spec":
                if not can_query(user_id):
                    continue

                spec_name = unquote_plus(
                    params.get("spec", "")
                ).strip()

                (
                    _,
                    _,
                    waiting_by_spec,
                    _,
                    _,
                    _,
                ) = procurement_summary(product["id"])

                waiting_qty = int(
                    waiting_by_spec.get(spec_name, 0)
                )

                if waiting_qty > 0:
                    add_procurement(
                        product["id"],
                        spec_name,
                        waiting_qty,
                    )

                product = get_product_by_code(
                    product["product_code"]
                )

                reply_messages(
                    reply_token,
                    [
                        build_product_card(
                            product,
                            user_id,
                            title=f"📦 {product['product_code']} 採購進度",
                            procurement_mode=True,
                        )
                    ],
                )
                continue

            if action == "procure_all_product":
                if not can_query(user_id):
                    continue

                procure_all_remaining(
                    product["id"]
                )

                clear_procurement_state(user_id)

                product = get_product_by_code(
                    product["product_code"]
                )

                reply_messages(
                    reply_token,
                    [
                        build_product_card(
                            product,
                            user_id,
                            title=f"✅ {product['product_code']} 已全部入單",
                            procurement_mode=True,
                        )
                    ],
                )
                continue

            if action == "close":
                if not is_owner(user_id):
                    continue

                set_product_closed(
                    product["id"],
                    True,
                )

                product = get_product_by_code(
                    product["product_code"]
                )

                reply_messages(
                    reply_token,
                    [
                        build_product_card(
                            product,
                            user_id,
                            title=f"🔒 {product['product_code']} 已結單",
                        )
                    ],
                )
                continue

            continue

        # Only message events below.
        if event_type != "message":
            continue

        message = event.get("message", {})
        message_type = message.get("type")

        # ---------- PRIVATE CHAT ----------
        if source_type == "user":
            if message_type != "text" or not user_id:
                continue

            text = message.get("text", "").strip()

            # Owner / 小幫手採購入單模式
            procurement_state = get_procurement_state(user_id)

            if procurement_state:
                if text == "取消入單":
                    clear_procurement_state(user_id)
                    reply_text(
                        reply_token,
                        "✅ 已離開採購入單模式。",
                    )
                    continue

                with db() as conn:
                    product = conn.execute(
                        "SELECT * FROM products WHERE id=?",
                        (procurement_state["product_id"],),
                    ).fetchone()

                if not product:
                    clear_procurement_state(user_id)
                    reply_text(
                        reply_token,
                        "找不到商品，已離開入單模式。",
                    )
                    continue

                selected_spec = (
                    procurement_state["selected_spec"]
                    if "selected_spec" in procurement_state.keys()
                    else None
                )

                (
                    spec_totals,
                    _,
                    _,
                    _,
                    _,
                    _,
                ) = procurement_summary(product["id"])

                if selected_spec:
                    qty_text = text.strip().lstrip("+").strip()

                    if not qty_text.isdigit():
                        reply_text(
                            reply_token,
                            f"請輸入「{selected_spec}」這次拿到的數量，"
                            "例如：5\n"
                            "輸入「取消入單」離開。",
                        )
                        continue

                    qty = int(qty_text)

                    if qty <= 0:
                        reply_text(
                            reply_token,
                            "數量需大於 0。",
                        )
                        continue

                    add_procurement(
                        product["id"],
                        selected_spec,
                        qty,
                    )

                    # 輸入一次數量後就清除選定規格，避免下一句誤入單。
                    set_procurement_state(
                        user_id,
                        product["id"],
                        selected_spec=None,
                    )

                else:
                    parsed = parse_order_text(text)

                    if not parsed or parsed[0] != "ADD":
                        reply_text(
                            reply_token,
                            "請先在商品卡按「入單」，"
                            "再選規格的「輸入數量」或「全入」。",
                        )
                        continue

                    items = parsed[1]
                    real_specs = list(spec_totals.keys())

                    normalized_items = []

                    for spec_name, qty in items:
                        if (
                            spec_name == "單一規格"
                            and len(real_specs) == 1
                        ):
                            normalized_items.append(
                                (real_specs[0], qty)
                            )
                        else:
                            normalized_items.append(
                                (spec_name, qty)
                            )

                    for spec_name, qty in normalized_items:
                        if spec_name not in spec_totals:
                            continue

                        add_procurement(
                            product["id"],
                            spec_name,
                            qty,
                        )

                product = get_product_by_code(
                    product["product_code"]
                )

                (
                    _,
                    _,
                    _,
                    ordered_total,
                    purchased_total,
                    waiting_total,
                ) = procurement_summary(product["id"])

                reply_messages(
                    reply_token,
                    [
                        {
                            "type": "text",
                            "text": (
                                f"✅ 採購入單完成\n"
                                f"已喊 {ordered_total}｜"
                                f"已拿 {purchased_total}｜"
                                f"待拿 {waiting_total}"
                            ),
                        },
                        build_product_card(
                            product,
                            user_id,
                            title=f"📦 {product['product_code']} 採購進度",
                            procurement_mode=True,
                        ),
                    ],
                )

                # 待拿歸零就自動離開這個商品的入單模式
                if waiting_total <= 0:
                    clear_procurement_state(user_id)

                continue

            if text in ("設定Owner", "設定管理員"):
                current = get_owner_user_id()

                if current and current != user_id:
                    reply_text(
                        reply_token,
                        "⚠️ Owner 已經設定完成，無法由其他帳號變更。",
                    )
                else:
                    set_setting(
                        "owner_user_id",
                        user_id,
                    )
                    reply_text(
                        reply_token,
                        "👑 Owner 設定完成。",
                    )
                continue

            join = JOIN_STAFF_RE.match(text)
            if join:
                if redeem_staff_invite(
                    join.group(1),
                    user_id,
                ):
                    reply_text(
                        reply_token,
                        "✅ 已加入成為小幫手。\n"
                        "小幫手僅有查單權限。",
                    )
                else:
                    reply_text(
                        reply_token,
                        "⚠️ 邀請碼無效或已使用。",
                    )
                continue

            if INVITE_RE.match(text):
                if not is_owner(user_id):
                    reply_text(
                        reply_token,
                        "⚠️ 只有 Owner 可以產生小幫手邀請碼。",
                    )
                else:
                    invite_code = generate_staff_invite()
                    reply_text(
                        reply_token,
                        f"👥 小幫手一次性邀請碼：{invite_code}\n\n"
                        f"請小幫手私訊：\n加入小幫手 {invite_code}",
                    )
                continue

            if STAFF_LIST_RE.match(text):
                if not is_owner(user_id):
                    continue
                reply_text(
                    reply_token,
                    staff_list_text(),
                )
                continue

            if text == "加入連線":
                if not is_staff(user_id):
                    reply_text(
                        reply_token,
                        "⚠️ 只有已設定的小幫手可以加入連線通知。",
                    )
                    continue

                session, error = join_active_session_staff(user_id)

                if error == "NO_ACTIVE_SESSION":
                    reply_text(
                        reply_token,
                        "目前沒有進行中的連線。",
                    )
                else:
                    reply_text(
                        reply_token,
                        "✅ 已加入本場連線通知\n"
                        f"場次：{session['session_code']}\n"
                        "之後客人喊單時，你會收到累計商品卡。",
                    )
                continue

            if text == "離開連線":
                if not is_staff(user_id):
                    continue

                session = leave_active_session_staff(user_id)

                if not session:
                    reply_text(
                        reply_token,
                        "目前沒有進行中的連線。",
                    )
                else:
                    reply_text(
                        reply_token,
                        "✅ 已離開本場連線通知。",
                    )
                continue

            # Owner / 小幫手：輸入日期、日期+地區、查單、商品列表
            # 都可以直接叫出商品卡。
            if can_query(user_id):
                lookup_session = find_session_for_lookup(text)

                if lookup_session:
                    reply_messages(
                        reply_token,
                        [
                            build_session_products_carousel(
                                lookup_session,
                                user_id,
                            )
                        ],
                    )
                    continue

                if text in ("查單", "商品列表", "商品清單"):
                    session = get_current_session_for_cards()

                    if not session:
                        reply_text(
                            reply_token,
                            "目前找不到可查看的連線場次。",
                        )
                    else:
                        reply_messages(
                            reply_token,
                            [
                                build_session_products_carousel(
                                    session,
                                    user_id,
                                )
                            ],
                        )
                    continue

            start_match = START_SESSION_RE.match(text)
            auto_date_first = AUTO_SESSION_DATE_FIRST_RE.match(text)
            auto_place_first = AUTO_SESSION_PLACE_FIRST_RE.match(text)

            if start_match or auto_date_first or auto_place_first:
                if not is_owner(user_id):
                    continue

                session_name = (
                    start_match.group(1)
                    if start_match
                    else text
                )

                session = start_session(
                    session_name
                )

                reply_text(
                    reply_token,
                    "✅ 已開始連線\n"
                    f"場次：{session['session_code']}\n\n"
                    "客人對照片喊 +1 或「規格+1」後會建立商品，"
                    "之後每次喊單都會把『最新累計商品卡』通知給你。\n\n"
                    "今天要一起連線的小幫手請私訊：加入連線",
                )
                continue

            end_match = END_SESSION_RE.match(text)
            if end_match:
                if not is_owner(user_id):
                    continue

                session = end_active_session()

                if not session:
                    reply_text(
                        reply_token,
                        "目前沒有進行中的連線。",
                    )
                else:
                    send_final_session_carousels(
                        session,
                        reply_token,
                        user_id,
                    )
                continue

            if PRODUCT_LIST_RE.match(text):
                if not can_query(user_id):
                    continue

                reply_text(
                    reply_token,
                    product_list_text(),
                )
                continue

            query_match = ADMIN_QUERY_RE.match(text)
            if query_match:
                product_code = query_match.group(1)
                action = query_match.group(2)

                product = get_product_by_code(
                    product_code
                )

                if not product:
                    reply_text(
                        reply_token,
                        f"找不到商品 {product_code}。",
                    )
                    continue

                if action == "查單":
                    if not can_query(user_id):
                        continue

                    reply_messages(
                        reply_token,
                        [
                            build_product_card(
                                product,
                                user_id,
                                title=f"📋 {product_code} 查單",
                            )
                        ],
                    )
                    continue

                if action == "結單":
                    if not is_owner(user_id):
                        continue

                    set_product_closed(
                        product["id"],
                        True,
                    )

                    product = get_product_by_code(
                        product_code
                    )

                    reply_messages(
                        reply_token,
                        [
                            build_product_card(
                                product,
                                user_id,
                                title=f"🔒 {product_code} 已結單",
                            )
                        ],
                    )
                    continue

            # Small helpers can only query; owner gets help.
            if is_owner(user_id):
                reply_text(
                    reply_token,
                    "Owner 指令：\n"
                    "香港8/18\n"
                    "東京10/19\n"
                    "香港8/18-19\n"
                    "或：開始連線 8/18-19 香港\n"
                    "結束連線\n"
                    "商品列表\n"
                    "產生小幫手邀請碼\n"
                    "小幫手列表",
                )
            elif is_staff(user_id):
                reply_text(
                    reply_token,
                    "小幫手可使用：\n"
                    "加入連線 → 收到本場即時喊單通知\n"
                    "離開連線 → 停止本場通知\n"
                    "8/16、8/16香港、香港8/16 → 商品卡\n"
                    "查單／商品列表 → 目前場次商品卡",
                )

            continue

        # ---------- GROUP CHAT ----------
        if source_type != "group":
            continue

        group_id = source.get("groupId")

        if not group_id or not user_id:
            continue

        # Every photo is only remembered.
        # It does NOT become an order/product yet.
        if message_type == "image":
            message_id = message.get("id")

            if message_id:
                remember_image(
                    message_id,
                    group_id,
                    user_id,
                )

            continue

        if message_type != "text":
            continue

        quoted_message_id = message.get(
            "quotedMessageId"
        )

        if not quoted_message_id:
            continue

        # Boss / 小幫手回覆「商品照片」時，可以直接設定價格。
        # 例如：199元、玲娜199元。
        if can_query(user_id):
            price_info = parse_price_text(
                message.get("text", "")
            )

            pending_price_image = get_pending_image(
                group_id,
                quoted_message_id,
            )
            existing_price_product = get_product_by_image(
                quoted_message_id
            )

            if (
                price_info
                and (
                    pending_price_image
                    or existing_price_product
                )
            ):
                remember_price_message(
                    message.get("id"),
                    group_id,
                    quoted_message_id,
                    price_info["price"],
                    price_info["label"],
                    user_id,
                )
                # 設定價格不在群組回覆，保持安靜。
                continue

        parsed = parse_order_text(
            message.get("text", "")
        )

        if not parsed:
            continue

        # 客人可以直接回覆照片，也可以回覆 Boss / 小幫手的價格訊息。
        price_message = get_price_message(
            quoted_message_id
        )

        order_image_message_id = (
            price_message["image_message_id"]
            if price_message
            else quoted_message_id
        )

        unit_price = (
            int(price_message["price"])
            if price_message
            else None
        )

        price_label = (
            (price_message["label"] or "").strip()
            if price_message
            else ""
        )

        pending = get_pending_image(
            group_id,
            order_image_message_id,
        )

        product = get_product_by_image(
            order_image_message_id
        )

        created_now = False
        action, items = parsed

        # First valid +1 / 規格+1 creates the product.
        if not product:
            if not pending:
                continue

            # Cancel cannot create a product.
            if action != "ADD" or not items:
                continue

            product, error = create_product_from_pending(
                pending
            )

            if not error:
                created_now = True

            if error == "NO_ACTIVE_SESSION":
                # Keep group quiet; no product is created.
                continue

        # 結束連線後，舊照片仍可查單，但不再接受新的喊單/取消。
        product_session = get_session(
            product["session_id"]
        )

        if (
            not product_session
            or not product_session["is_active"]
            or product["is_closed"]
        ):
            continue

        profile = get_group_profile(
            group_id,
            user_id,
        )

        display_name = (
            profile.get("displayName")
            or user_id
        )

        if action == "ADD":
            for spec_name, qty in items:
                if 1 <= qty <= 99:
                    final_spec_name = spec_name

                    # 客人只回 +1，而價格訊息有「玲娜199元」這種標籤時，
                    # 自動把玲娜當成規格。
                    if (
                        price_label
                        and spec_name == "單一規格"
                    ):
                        final_spec_name = normalize_spec(
                            price_label
                        )

                    add_order_item(
                        product["id"],
                        user_id,
                        display_name,
                        final_spec_name,
                        qty,
                    )

                    add_order_price_line(
                        product["id"],
                        user_id,
                        display_name,
                        final_spec_name,
                        unit_price,
                        qty,
                    )

            # 每一筆喊單後，都通知 Owner + 本場已加入的小幫手。
            # 通知卡顯示的是這個商品「目前累計數量」。
            product = get_product_by_image(
                order_image_message_id
            )
            notify_live_order_update(
                product
            )

            # Group remains completely silent.
            continue

        if action == "CANCEL_ALL":
            cancel_user_all(
                product["id"],
                user_id,
            )
            product = get_product_by_image(
                order_image_message_id
            )
            notify_live_order_update(
                product
            )
            continue

        if action == "CANCEL_SPEC":
            for spec_name, _ in items:
                cancel_user_spec(
                    product["id"],
                    user_id,
                    spec_name,
                )

            product = get_product_by_image(
                order_image_message_id
            )
            notify_live_order_update(
                product
            )
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
