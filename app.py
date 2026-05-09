import os
import hashlib
import hmac
import base64
import json
import logging
import time
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
LINE_MAX_CHARS            = 3500
LINE_MAX_MSGS             = 5

# ── Session: จำว่า user ไหนเพิ่งเรียก Bot (expire 5 นาที) ──────────────────
# key = user_id, value = timestamp ที่ activate
ACTIVE_SESSIONS: dict = {}
SESSION_TTL = 5 * 60  # 5 นาที

TRIGGER_WORDS = ["bond bot", "bondbot", "บอนด์บอท", "บอทหุ้นกู้"]

GREETING_TEXT = (
    "ดีจ้า 🤖💚\n"
    "อยากค้นหา Bond ของ Issuer หนายจ๊ะ?\n\n"
    "พิมพ์ชื่อย่อ Issuer ได้เลยนะจ๊ะ เช่น:\n"
    "  SIRI / CPALL / PTT / ASW / CI\n\n"
    "แล้วจะตอบกลับทันทีเลย~ 💪"
)

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
    if not text:
        return []
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


def reply_messages(reply_token: str, chunks: list):
    if not chunks:
        return
    messages = [{"type": "text", "text": c} for c in chunks[:LINE_MAX_MSGS]]
    payload  = {"replyToken": reply_token, "messages": messages}
    headers  = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    logger.info(f"[reply] {len(messages)} msg(s), chars: {[len(c) for c in messages]}")
    try:
        resp = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=15)
        logger.info(f"[reply] LINE API status: {resp.status_code}")
        if resp.status_code != 200:
            logger.error(f"[reply] error: {resp.text[:200]}")
    except Exception as e:
        logger.exception(f"[reply] Exception: {e}")


def push_messages(to_id: str, chunks: list):
    if not chunks:
        return
    for i in range(0, len(chunks), LINE_MAX_MSGS):
        batch    = chunks[i:i+LINE_MAX_MSGS]
        messages = [{"type": "text", "text": c} for c in batch]
        payload  = {"to": to_id, "messages": messages}
        headers  = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        }
        try:
            resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=15)
            logger.info(f"[push] batch {i//LINE_MAX_MSGS+1}: status={resp.status_code}")
        except Exception as e:
            logger.exception(f"[push] Exception: {e}")


def is_session_active(user_id: str) -> bool:
    """ตรวจสอบว่า user เพิ่งเรียก Bot ไว้ไม่เกิน SESSION_TTL"""
    ts = ACTIVE_SESSIONS.get(user_id)
    if ts and (time.time() - ts) < SESSION_TTL:
        return True
    # expire
    ACTIVE_SESSIONS.pop(user_id, None)
    return False


def activate_session(user_id: str):
    ACTIVE_SESSIONS[user_id] = time.time()


def deactivate_session(user_id: str):
    ACTIVE_SESSIONS.pop(user_id, None)


def handle_message(event: dict):
    reply_token = event.get("replyToken", "")
    source      = event.get("source", {})
    user_id     = source.get("userId", "")
    source_type = source.get("type", "user")  # "user", "group", "room"

    # push_to: ส่ง push กลับไปที่กลุ่มหรือ user
    if source_type == "group":
        push_to = source.get("groupId", user_id)
    elif source_type == "room":
        push_to = source.get("roomId", user_id)
    else:
        push_to = user_id

    msg = event.get("message", {})
    if msg.get("type") != "text":
        return

    user_text  = msg.get("text", "").strip()
    text_lower = user_text.lower()
    logger.info(f"[msg] source={source_type} user={user_id} text='{user_text}'")

    # ── 1. Help command ──────────────────────────────────────────────────────
    if text_lower in ["help", "ช่วยเหลือ", "วิธีใช้", "?"]:
        reply_messages(reply_token, [HELP_TEXT])
        return

    # ── 2. Trigger word → Toggle session ────────────────────────────────────
    if any(tw in text_lower for tw in TRIGGER_WORDS):
        if is_session_active(user_id):
            # พิมซ้ำภายใน 5 นาที → ปิด session
            deactivate_session(user_id)
            reply_messages(reply_token, ["โอเคจ้า 👋 ปิด Bond Bot แล้วนะจ๊ะ~"])
        else:
            # เปิด session ใหม่
            activate_session(user_id)
            reply_messages(reply_token, [GREETING_TEXT])
        return

    # ── 3. Direct chat (1:1) → ค้นหาเลย ────────────────────────────────────
    if source_type == "user":
        _do_search(user_text, reply_token, push_to)
        return

    # ── 4. Group/Room chat → ต้องมี session ก่อน ────────────────────────────
    if source_type in ("group", "room"):
        if is_session_active(user_id):
            # User พิมพ์ชื่อ issuer หลังจาก activate Bot แล้ว
            deactivate_session(user_id)
            _do_search(user_text, reply_token, push_to)
        # ถ้าไม่มี session → เพิกเฉย (ไม่ตอบทุก message ในกลุ่ม)
        return


def _do_search(user_text: str, reply_token: str, push_to: str):
    """ค้นหา bond และส่งผลลัพธ์"""
    try:
        bonds = search_bonds_by_company(user_text)
        logger.info(f"[search] Got {len(bonds)} bonds for '{user_text}'")
        response_msg = format_bond_message(bonds, user_text)
        logger.info(f"[search] Formatted {len(response_msg)} chars")
    except Exception as e:
        logger.exception(f"[search] Error: {e}")
        reply_messages(reply_token, ["❌ เกิดข้อผิดพลาด กรุณาลองใหม่อีกครั้งครับ"])
        return

    chunks = hard_split(response_msg)
    logger.info(f"[search] {len(chunks)} chunks")

    if len(chunks) <= LINE_MAX_MSGS:
        reply_messages(reply_token, chunks)
    else:
        reply_messages(reply_token, chunks[:LINE_MAX_MSGS])
        if push_to:
            push_messages(push_to, chunks[LINE_MAX_MSGS:])


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data()

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


# ── DEBUG endpoint: เปิด browser ไปที่ /debug-sec/ABBR เพื่อดู log ──────────
@app.route("/debug-sec/<abbr>", methods=["GET"])
def debug_sec(abbr):
    """
    Debug SEC scraper — แสดง raw API response ทุก step
    ตัวอย่าง: https://line-bond-bot.onrender.com/debug-sec/BANPU
    """
    lines = []
    log   = lines.append

    SEC_BASE    = "https://market.sec.or.th/public/idisc"
    COMPANY_URL = f"{SEC_BASE}/api/company/valuebyuniqueid"
    FILING_URL  = f"{SEC_BASE}/api/product/GetViewFiling"
    SEC_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "th,en;q=0.9",
        "Content-Type": "application/json",
        "Referer": "https://market.sec.or.th/public/idisc/th/Product/Filing",
        "X-Requested-With": "XMLHttpRequest",
    }

    log(f"🔍 DEBUG SEC SCRAPER — ABBR: {abbr.upper()}")
    log("=" * 60)

    session = requests.Session()

    # ── STEP 0: warm-up ───────────────────────────────────────
    log("\n[STEP 0] warm-up GET")
    try:
        r0 = session.get("https://market.sec.or.th/public/idisc/th/Product/Filing", timeout=10)
        log(f"  status  = {r0.status_code}")
        log(f"  cookies = {dict(session.cookies)}")
    except Exception as e:
        log(f"  ❌ error: {e}")

    # ── STEP 1: POST valuebyuniqueid — ดู raw response ────────
    log(f"\n[STEP 1] POST {COMPANY_URL}")
    log(f"  payload = {{\"lang\": \"th\", \"content\": \"{abbr.upper()}\"}}")
    try:
        r1 = session.post(
            COMPANY_URL,
            json={"lang": "th", "content": abbr.upper()},
            headers=SEC_HEADERS,
            timeout=15,
        )
        log(f"  status       = {r1.status_code}")
        log(f"  content-type = {r1.headers.get('Content-Type', '-')}")
        log(f"  response len = {len(r1.text)} chars")
        log(f"\n  ── RAW RESPONSE (ทั้งหมด) ──")
        log(r1.text[:3000])  # แสดงสูงสุด 3000 ตัวอักษร

        if r1.status_code != 200:
            log(f"\n  ❌ non-200 → หยุด")
            return "<pre>" + "\n".join(lines) + "</pre>"

        # พยายาม parse JSON
        try:
            data = r1.json()
            log(f"\n  ── PARSED JSON ──")
            log(f"  type = {type(data).__name__}")
            if isinstance(data, list):
                log(f"  length = {len(data)}")
                if data:
                    log(f"  [0] keys = {list(data[0].keys()) if isinstance(data[0], dict) else 'not a dict'}")
                    log(f"  [0] full = {data[0]}")
            elif isinstance(data, dict):
                log(f"  keys = {list(data.keys())}")
                log(f"  full = {data}")
            else:
                log(f"  value = {data}")
        except Exception as e:
            log(f"\n  ❌ JSON parse error: {e}")
            log("  → response ไม่ใช่ JSON จริง")

    except Exception as e:
        log(f"  ❌ request error: {e}")
        return "<pre>" + "\n".join(lines) + "</pre>"

    log("\n" + "=" * 60)
    log("📋 copy ผลนี้ส่งมาให้ดูได้เลย จะ fix ให้ทันที")
    return "<pre>" + "\n".join(lines) + "</pre>"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
