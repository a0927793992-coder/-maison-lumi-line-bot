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


def db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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

init_db()


def verify_signature(raw_body: bytes, signature: str) -> bool:
    if not CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(CHANNEL_SECRET.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def reply(reply_token: str, text: str):
    if not CHANNEL_ACCESS_TOKEN or not reply_token:
        return
    requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"},
        json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
        timeout=10,
    )


def get_display_name(group_id: str, user_id: str) -> str:
    if not CHANNEL_ACCESS_TOKEN or not group_id or not user_id:
        return user_id or "未知會員"
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/group/{group_id}/member/{user_id}",
            headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}, timeout=10,
        )
        if r.ok:
            return r.json().get("displayName") or user_id
    except requests.RequestException:
        pass
    return user_id


def get_or_create_product(group_id: str, image_message_id: str):
    with db() as conn:
        row = conn.execute("SELECT * FROM products WHERE image_message_id=?", (image_message_id,)).fetchone()
        if row:
            return row, False
        cur = conn.execute(
            "INSERT INTO products(product_code, group_id, image_message_id, created_at) VALUES('',?,?,?)",
            (group_id, image_message_id, datetime.now(timezone.utc).isoformat()),
        )
        product_id = cur.lastrowid
        product_code = f"A{product_id:03d}"
        conn.execute("UPDATE products SET product_code=? WHERE id=?", (product_code, product_id))
        return conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone(), True


def add_order(product_id: int, user_id: str, display_name: str, qty: int):
    with db() as conn:
        current = conn.execute(
            "SELECT quantity FROM orders WHERE product_id=? AND user_id=?", (product_id, user_id)
        ).fetchone()
        total = qty + (current["quantity"] if current else 0)
        conn.execute("""
            INSERT INTO orders(product_id,user_id,display_name,quantity,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(product_id,user_id)
            DO UPDATE SET display_name=excluded.display_name, quantity=excluded.quantity, updated_at=excluded.updated_at
        """, (product_id, user_id, display_name, total, datetime.now(timezone.utc).isoformat()))
        return total


def cancel_order(product_id: int, user_id: str):
    with db() as conn:
        conn.execute("DELETE FROM orders WHERE product_id=? AND user_id=?", (product_id, user_id))


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "Maison Lumi LINE Bot"})


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
        if source.get("type") != "group":
            continue

        group_id = source.get("groupId")
        user_id = source.get("userId")
        message = event.get("message", {})

        if message.get("type") == "image":
            message_id = message.get("id")
            if message_id and group_id:
                with db() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO pending_images(message_id,group_id,sender_user_id,created_at) VALUES(?,?,?,?)",
                        (message_id, group_id, user_id, datetime.now(timezone.utc).isoformat()),
                    )
            continue

        if message.get("type") != "text":
            continue

        text = message.get("text", "")
        quoted_message_id = message.get("quotedMessageId")
        if not quoted_message_id:
            continue

        with db() as conn:
            image = conn.execute(
                "SELECT * FROM pending_images WHERE message_id=? AND group_id=?",
                (quoted_message_id, group_id),
            ).fetchone()
        if not image:
            continue

        plus = PLUS_RE.match(text)
        cancel = CANCEL_RE.match(text)
        if not plus and not cancel:
            continue

        product, created = get_or_create_product(group_id, quoted_message_id)
        display_name = get_display_name(group_id, user_id)

        if plus:
            qty = int(plus.group(1))
            if qty < 1 or qty > 99:
                reply(event.get("replyToken"), "數量請輸入 +1 ～ +99")
                continue
            total = add_order(product["id"], user_id, display_name, qty)
            prefix = "📝 已建立商品記事\n" if created else ""
            reply(event.get("replyToken"), f"{prefix}🛍️ 商品 {product['product_code']}\n✅ {display_name} 登記 +{qty}\n目前你的數量：{total}")
        else:
            cancel_order(product["id"], user_id)
            reply(event.get("replyToken"), f"🛍️ 商品 {product['product_code']}\n已取消 {display_name} 的喊單")

    return "OK"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
