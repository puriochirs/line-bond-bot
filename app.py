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
LINE_PUSH_URL             = "https://api.line.me/v2/bot/message/push"
LINE_MAX_CHARS            = 4900   # safe limit per message
LINE_MAX_MSGS             = 5      # LINE allows max 5 per reply

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


def hard_split(text: str, max_len: int = LINE_MAX_CHARS) -> list:
    """ตัดข้อความเป็น chunks แบบ hard cut ไม่เกิน max_len chars"""
    if not text:
        return []
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


def reply_messages(reply_token: str, chunks: list):
    """ส่ง reply สูงสุด 5 messages"""
    if not chunks:
        return
    messages = [{"type": "text", "text": c} for c in chunks[:LINE_MAX_MSGS]]
    payload  = {"replyToken": reply_token, "messages": messages}
    headers  = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    logger.info(f"[reply] Sending {len(messages)} msg(s), chars: {[len(c) for c in chunks[:LINE_MAX_MSGS]]}")
    try:
        resp = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=15)
        logger.info(f"[reply] LINE API status: {resp.status_code}")
        if resp.status_code != 200:
            logger.error(f"[reply] error: {resp.text[:200]}")
    except Exception as e:
        logger.exception(f"[reply] Exception: {e}")


def push_messages(user_id: str, chunks: list):
    """Push ข้อความที่เกิน 5 messages ผ่าน push API"""
    if not chunks:
        return
    # ส่งเป็นกลุ่มๆ ละ 5
    for i in range(0, len(chunks), LINE_MAX_MSGS):
        batch = chunks[i:i+LINE_MAX_MSGS]
        messages = [{"type": "text", "text": c} for c in batch]
        payload  = {"to": user_id, "messages": messages}
        headers  = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        }
        try:
            resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=15)
            logger.info(f"[push] batch {i//LINE_MAX_MSGS+1}: status={resp.status_code}")
        except Exception as e:
            logger.exception(f"[push] Exception: {e}")


def handle_message(event: dict):
    reply_token = event.get("replyToken", "")
    source      = event.get("source", {})
    user_id     = source.get("userId", "")
    msg         = event.get("message", {})

    if msg.get("type") != "text":
        reply_messages(reply_token, ["กรุณาพิมพ์ชื่อย่อบริษัทที่ต้องการครับ 🙏"])
        return

    user_text = msg.get("text", "").strip()
    logger.info(f"[msg] User input: '{user_text}'")

    if user_text.lower() in ["help", "ช่วยเหลือ", "วิธีใช้", "?"]:
        reply_messages(reply_token, [HELP_TEXT])
        return

    try:
        bonds = search_bonds_by_company(user_text)
        logger.info(f"[msg] Got {len(bonds)} bonds for '{user_text}'")
        response_msg = format_bond_message(bonds, user_text)
        logger.info(f"[msg] Formatted message length: {len(response_msg)}")
    except Exception as e:
        logger.exception(f"[msg] Error: {e}")
        reply_messages(reply_token, ["❌ เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้งครับ"])
        return

    chunks = hard_split(response_msg)
    logger.info(f"[msg] Split into {len(chunks)} chunks")

    if len(chunks) <= LINE_MAX_MSGS:
        # ส่งด้วย reply ครั้งเดียว
        reply_messages(reply_token, chunks)
    else:
        # ส่ง 5 แรกด้วย reply แล้วที่เหลือ push
        reply_messages(reply_token, chunks[:LINE_MAX_MSGS])
        if user_id:
            remaining = chunks[LINE_MAX_MSGS:]
            logger.info(f"[msg] Pushing {len(remaining)} more chunks to {user_id}")
            push_messages(user_id, remaining)
        else:
            logger.warning("[msg] No user_id, cannot push remaining chunks")


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
            reply_messages(event.get("replyToken", ""), [HELP_TEXT])

    return "OK", 200


@app.route("/", methods=["GET"])
def health():
    return "Bond Bot is running 🟢", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
