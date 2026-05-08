import os
import hashlib
import hmac
import base64
import json
import logging
from flask import Flask, request, abort
import requests
from scraper import search_bonds_by_company, format_bond_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Line API Config ────────────────────────────────────────────────────────
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "YOUR_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

HELP_TEXT = (
    "🤖 Bond Info Bot\n\n"
    "พิมพ์ชื่อบริษัท หรือชื่อย่อ เพื่อดูข้อมูลหุ้นกู้\n\n"
    "ตัวอย่าง:\n"
    "  • PTT\n"
    "  • CPALL\n"
    "  • ปตท\n"
    "  • กรุงเทพ\n\n"
    "ข้อมูลจาก ThaiBMA (thaibma.or.th)"
)


# ─── Signature Validation ────────────────────────────────────────────────────
def verify_signature(body: bytes, signature: str) -> bool:
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# ─── Line Reply Helper ───────────────────────────────────────────────────────
def reply_text(reply_token: str, text: str):
    # Line has 5000 char limit per message; split if needed
    messages = []
    while text:
        chunk = text[:4990]
        text = text[4990:]
        messages.append({"type": "text", "text": chunk})
        if len(messages) >= 5:  # max 5 messages per reply
            break

    payload = {"replyToken": reply_token, "messages": messages}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    resp = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        logger.error(f"Line reply failed: {resp.status_code} {resp.text}")


def reply_loading(reply_token: str):
    """Send a 'searching...' message first."""
    reply_text(reply_token, "⏳ กำลังค้นหาข้อมูลหุ้นกู้ กรุณารอสักครู่...")


# ─── Event Handlers ──────────────────────────────────────────────────────────
def handle_message(event: dict):
    reply_token = event.get("replyToken", "")
    msg = event.get("message", {})

    if msg.get("type") != "text":
        reply_text(reply_token, "กรุณาพิมพ์ชื่อบริษัทที่ต้องการค้นหาหุ้นกู้ครับ 🙏")
        return

    user_text = msg.get("text", "").strip()

    # Commands
    if user_text.lower() in ["help", "ช่วยเหลือ", "วิธีใช้", "?"]:
        reply_text(reply_token, HELP_TEXT)
        return

    logger.info(f"Searching bonds for: {user_text}")

    try:
        bonds = search_bonds_by_company(user_text)
        response_msg = format_bond_message(bonds, user_text)
    except Exception as e:
        logger.exception(f"Error searching bonds: {e}")
        response_msg = (
            f"❌ เกิดข้อผิดพลาดในการค้นหา\n"
            f"กรุณาลองใหม่อีกครั้ง หรือพิมพ์ 'help' เพื่อดูวิธีใช้"
        )

    reply_text(reply_token, response_msg)


# ─── Webhook Endpoint ────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        logger.warning("Invalid signature")
        abort(400)

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        abort(400)

    for event in payload.get("events", []):
        event_type = event.get("type")
        if event_type == "message":
            handle_message(event)
        elif event_type == "follow":
            reply_text(event.get("replyToken", ""), HELP_TEXT)

    return "OK", 200


@app.route("/", methods=["GET"])
def health():
    return "Bond Bot is running 🟢", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
