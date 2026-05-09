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

LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_REPLY_URL            = "https://api.line.me/v2/bot/message/reply"

HELP_TEXT = (
    "🤖 Bond Info Bot\n\n"
    "พิมพ์ชื่อย่อบริษัท เพื่อดูข้อมูลหุ้นกู้\n\n"
    "ตัวอย่าง:\n"
    "  PTT / CPALL / CI / ASW / TRUE\n\n"
    "ข้อมูลจาก ThaiBMA"
)


def verify_signature(body: bytes, signature: str) -> bool:
    hash_value = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_value).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def split_message(text: str, max_len: int = 4900) -> list:
    """
    แบ่งข้อความให้แต่ละ chunk ไม่เกิน max_len chars
    พยายามตัดที่ bond boundary (บรรทัดที่ขึ้นต้นด้วย 🔹) ไม่ใช่กลางบรรทัด
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    lines  = text.split("\n")
    current = ""

    for line in lines:
        # ถ้าใส่บรรทัดนี้แล้วจะเกิน limit → flush chunk ก่อน
        test = current + "\n" + line if current else line
        if len(test) > max_len and current:
            chunks.append(current.rstrip())
            current = line
        else:
            current = test

    if current.strip():
        chunks.append(current.rstrip())

    return chunks


def reply_text(reply_token: str, text: str):
    """ส่ง text reply กลับไปยัง LINE (รองรับข้อความยาวสูงสุด 5 messages)"""
    if not text:
        return

    chunks = split_message(text)
    # LINE รองรับสูงสุด 5 messages ต่อ reply
    chunks = chunks[:5]

    messages = [{"type": "text", "text": c} for c in chunks]

    payload = {"replyToken": reply_token, "messages": messages}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }

    logger.info(f"[reply] Sending {len(messages)} message(s), total chars: {len(text)}")
    logger.info(f"[reply] First 200 chars: {text[:200]}")

    try:
        resp = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=15)
        logger.info(f"[reply] LINE API status: {resp.status_code}")
        if resp.status_code != 200:
            logger.error(f"[reply] LINE API error: {resp.status_code} | {resp.text}")
        else:
            logger.info(f"[reply] LINE API success: {resp.text[:100]}")
    except Exception as e:
        logger.exception(f"[reply] Exception: {e}")


def handle_message(event: dict):
    reply_token = event.get("replyToken", "")
    msg = event.get("message", {})

    if msg.get("type") != "text":
        reply_text(reply_token, "กรุณาพิมพ์ชื่อย่อบริษัทที่ต้องการครับ 🙏")
        return

    user_text = msg.get("text", "").strip()
    logger.info(f"[msg] User input: '{user_text}'")

    if user_text.lower() in ["help", "ช่วยเหลือ", "วิธีใช้", "?"]:
        reply_text(reply_token, HELP_TEXT)
        return

    try:
        bonds = search_bonds_by_company(user_text)
        logger.info(f"[msg] Got {len(bonds)} bonds for '{user_text}'")
        response_msg = format_bond_message(bonds, user_text)
        logger.info(f"[msg] Formatted message length: {len(response_msg)}")
    except Exception as e:
        logger.exception(f"[msg] Error: {e}")
        response_msg = "❌ เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้งครับ"

    reply_text(reply_token, response_msg)


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_signature(body, signature):
        logger.warning("[webhook] Invalid signature")
        abort(400)

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        abort(400)

    for event in payload.get("events", []):
        if event.get("type") == "message":
            handle_message(event)
        elif event.get("type") == "follow":
            reply_text(event.get("replyToken", ""), HELP_TEXT)

    return "OK", 200


@app.route("/", methods=["GET"])
def health():
    return "Bond Bot is running 🟢", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
