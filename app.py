import base64
import hashlib
import hmac
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

import requests
from flask import Flask, request, abort, jsonify

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "maison_lumi.db")
ADMIN_USER_ID_ENV = os.getenv("ADMIN_USER_ID", "").strip()

PLUS_RE = re.compile(r"^\s*\+(\d+)\s*$")
CANCEL_RE = re.compile(r"^\s*(取消|刪單|cancel)\s*$", re.IGNORECASE)
ADMIN_CMD_RE = re.compile(r"^\s*(A\d{3,})\s*(查單|名單|結單|開單)\s*$", re.IGNORECASE)
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
            conn.execute("ALTER TABLE products ADD COLUMN owner_user_id TEXT")
        if not column_exists(conn, "products", "is_closed"):
            conn.execute("ALTER TABLE products ADD COLUMN is_closed INTEGER NOT NULL DEFAULT 0")


init_db()


def get_setting(key):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with db() as conn:
        conn.execute("""
            INSERT INTO settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))


def get_admin_user_id():
    return ADMIN_USER_ID_ENV or get_setting("admin_user_id")


def is_admin(user_id):
    admin_id = get_admin_user_id()
    return bool(admin_id and user_id and admin_id == user_id)


def verify_signature(raw_body, signature):
    if not CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(CHANNEL_SECRET.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def api_headers():
    return {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def reply_text(reply_token, text):
    if not CHANNEL_ACCESS_TOKEN or not reply_token:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers=api_headers(),
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text[:5000]}]},
            timeout=10,
        )
    except requests.RequestException:
        pass


def push_text(user_id, text):
    if not CHANNEL_ACCESS_TOKEN or not user_id:
        return
    try:
        requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers=api_headers(),
            json={"to": user_id, "messages": [{"type": "text", "text": text[:5000]}]},
            timeout=10,
        )
    except requests.RequestException:
        pass


def get_group_display_name(group_id, user_id):
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}",
            headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
            timeout=10,
        )
        if r.ok:
            return r.json().get("displayName") or user_id
    except requests.RequestException:
        pass
    return user_id or "未知會員"


def short_code(user_id):
    return hashlib.sha256((user_id or "?").encode()).hexdigest()[:4].upper()


def remember_image(message_id, group_id, sender_user_id):
    with db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO pending_images(message_id, group_id, sender_user_id, created_at)
            VALUES(?,?,?,?)
        """, (message_id, group_id, sender_user_id, now_iso()))


def get_pending_image(group_id, message_id):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM pending_images
            WHERE message_id=? AND group_id=?
        """, (message_id, group_id)).fetchone()


def create_product(group_id, image_message_id, owner_user_id):
    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM products WHERE image_message_id=?", (image_message_id,)
        ).fetchone()
        if existing:
            return existing, False

        cur = conn.execute("""
            INSERT INTO products(
                product_code, group_id, image_message_id, owner_user_id, is_closed, created_at
            )
            VALUES('',?,?,?,?,?)
        """, (group_id, image_message_id, owner_user_id, 0, now_iso()))

        product_id = cur.lastrowid
        product_code = f"A{product_id:03d}"
        conn.execute("UPDATE products SET product_code=? WHERE id=?", (product_code, product_id))
        product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return product, True


def get_product_by_code(product_code):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE UPPER(product_code)=?", (product_code.upper(),)
        ).fetchone()


def get_product_by_image(image_message_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM products WHERE image_message_id=?", (image_message_id,)
        ).fetchone()


def add_order(product_id, user_id, display_name, qty):
    with db() as conn:
        current = conn.execute("""
            SELECT quantity FROM orders
            WHERE product_id=? AND user_id=?
        """, (product_id, user_id)).fetchone()

        new_qty = (current["quantity"] if current else 0) + qty
        conn.execute("""
            INSERT INTO orders(product_id,user_id,display_name,quantity,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(product_id,user_id)
            DO UPDATE SET
                display_name=excluded.display_name,
                quantity=excluded.quantity,
                updated_at=excluded.updated_at
        """, (product_id, user_id, display_name, new_qty, now_iso()))


def cancel_order(product_id, user_id):
    with db() as conn:
        conn.execute("DELETE FROM orders WHERE product_id=? AND user_id=?", (product_id, user_id))


def get_orders(product_id):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM orders
            WHERE product_id=? AND quantity>0
            ORDER BY updated_at ASC
        """, (product_id,)).fetchall()


def set_product_closed(product_id, closed):
    with db() as conn:
        conn.execute("UPDATE products SET is_closed=? WHERE id=?", (1 if closed else 0, product_id))


def format_order_list(product):
    orders = get_orders(product["id"])
    status = "🔒 已結單" if product["is_closed"] else "🟢 開放喊單"

    if not orders:
        return f"📋 商品 {product['product_code']}\n目前還沒有人喊單\n\n{status}"

    counts = Counter((row["display_name"] or "未知會員") for row in orders)
    lines = [f"📋 商品 {product['product_code']} 查單"]
    total_qty = 0

    for i, row in enumerate(orders, start=1):
        name = row["display_name"] or "未知會員"
        if counts[name] > 1:
            name = f"{name} #{short_code(row['user_id'])}"
        qty = row["quantity"]
        total_qty += qty
        lines.append(f"{i}. {name} × {qty}")

    lines += [
        "",
        f"👥 喊單人數：{len(orders)}",
        f"📦 商品總數：{total_qty}",
        status,
    ]
    return "\n".join(lines)


def format_product_list():
    with db() as conn:
        rows = conn.execute("""
            SELECT p.*, COUNT(o.id) AS people, COALESCE(SUM(o.quantity), 0) AS qty
            FROM products p
            LEFT JOIN orders o ON o.product_id=p.id AND o.quantity>0
            GROUP BY p.id
            ORDER BY p.id DESC
            LIMIT 20
        """).fetchall()

    if not rows:
        return "目前還沒有商品。"

    lines = ["🛍️ 最近商品"]
    for row in rows:
        status = "🔒" if row["is_closed"] else "🟢"
        lines.append(f"{status} {row['product_code']}｜{row['people']}人｜{row['qty']}件")
    lines += ["", "可輸入：A001 查單 / A001 結單 / A001 開單"]
    return "\n".join(lines)


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "Maison Lumi LINE Bot", "version": "3-private-admin"})


@app.post("/webhook")
def webhook():
    raw = request.get_data()
    if not verify_signature(raw, request.headers.get("X-Line-Signature", "")):
        abort(400)

    body = request.get_json(silent=True) or {}

    for event in body.get("events", []):
        if event.get("type") != "message":
            continue

        source = event.get("source", {})
        source_type = source.get("type")
        user_id = source.get("userId")
        message = event.get("message", {})
        message_type = message.get("type")
        reply_token = event.get("replyToken")

        # 私訊管理端
        if source_type == "user":
            if message_type != "text" or not user_id:
                continue

            text = message.get("text", "").strip()

            if text == "設定管理員":
                current_admin = get_admin_user_id()

                if ADMIN_USER_ID_ENV:
                    if user_id == ADMIN_USER_ID_ENV:
                        reply_text(reply_token, "✅ 你已經是 Maison Lumi 管理員。")
                    else:
                        reply_text(reply_token, "⚠️ 此帳號不是系統設定的管理員。")
                    continue

                if current_admin and current_admin != user_id:
                    reply_text(reply_token, "⚠️ 系統已經有其他管理員，無法在 LINE 內變更。")
                    continue

                set_setting("admin_user_id", user_id)
                reply_text(
                    reply_token,
                    "✅ 管理員設定完成。\n之後商品編號與查單資料會私訊到這裡。"
                )
                continue

            if not is_admin(user_id):
                continue

            if PRODUCT_LIST_RE.match(text):
                reply_text(reply_token, format_product_list())
                continue

            cmd = ADMIN_CMD_RE.match(text)
            if not cmd:
                reply_text(
                    reply_token,
                    "管理指令：\nA001 查單\nA001 結單\nA001 開單\n商品列表"
                )
                continue

            product_code = cmd.group(1).upper()
            action = cmd.group(2)
            product = get_product_by_code(product_code)

            if not product:
                reply_text(reply_token, f"找不到商品 {product_code}。")
                continue

            if action in ("查單", "名單"):
                reply_text(reply_token, format_order_list(product))
            elif action == "結單":
                set_product_closed(product["id"], True)
                product = get_product_by_code(product_code)
                reply_text(reply_token, "🔒 已結單\n\n" + format_order_list(product))
            elif action == "開單":
                set_product_closed(product["id"], False)
                product = get_product_by_code(product_code)
                reply_text(reply_token, f"🟢 商品 {product_code} 已重新開單。\n\n" + format_order_list(product))
            continue

        # 群組端
        if source_type != "group":
            continue

        group_id = source.get("groupId")
        if not group_id or not user_id:
            continue

        # 管理員丟照片：自動建編號，僅私訊管理員
        if message_type == "image":
            message_id = message.get("id")
            if not message_id:
                continue

            remember_image(message_id, group_id, user_id)

            if is_admin(user_id):
                product, created = create_product(group_id, message_id, user_id)
                if created:
                    push_text(
                        user_id,
                        f"🛍️ 新商品已建立：{product['product_code']}\n"
                        f"群組喊單會靜默累積。\n"
                        f"查詢請私訊：{product['product_code']} 查單"
                    )
            continue

        if message_type != "text":
            continue

        text = message.get("text", "").strip()
        quoted_message_id = message.get("quotedMessageId")

        # 群組管理指令不回覆，避免洗版
        if not quoted_message_id:
            continue

        plus = PLUS_RE.match(text)
        cancel = CANCEL_RE.match(text)
        if not plus and not cancel:
            continue

        product = get_product_by_image(quoted_message_id)

        if not product:
            image = get_pending_image(group_id, quoted_message_id)
            admin_id = get_admin_user_id()
            if image and admin_id and image["sender_user_id"] == admin_id:
                product, _ = create_product(group_id, quoted_message_id, admin_id)
            else:
                continue

        if product["is_closed"]:
            continue

        display_name = get_group_display_name(group_id, user_id)

        if plus:
            qty = int(plus.group(1))
            if 1 <= qty <= 99:
                add_order(product["id"], user_id, display_name, qty)
            continue

        if cancel:
            cancel_order(product["id"], user_id)
            continue

    return "OK"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
