import os
import struct
import base64
import requests
import logging
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)

THAIBMA_USERNAME = os.environ.get("THAIBMA_USERNAME", "")
THAIBMA_PASSWORD = os.environ.get("THAIBMA_PASSWORD", "")

BASE_IBOND   = "https://www.ibond.thaibma.or.th"
BASE_THAIBMA = "https://www.thaibma.or.th"
REGISSUE_URL = f"{BASE_THAIBMA}/issuer/regissue"
ISSUER_URL   = f"{BASE_THAIBMA}/EN/Issuer/IssuerDetail.aspx"

GRPC_BOND_SVC   = "bond-grpc/bond.BondGrpcService"
GRPC_SEARCH_SVC = "bondsearch-grpc/BondSearchGrpc.Models.BondSearchGrpcService"

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "X-User-Agent": "grpc-web-javascript/0.1",
}

def _encode_varint(n):
    result = []
    while n > 127:
        result.append((n & 0x7f) | 0x80)
        n >>= 7
    result.append(n)
    return bytes(result)

def _proto_string(field_num, value):
    b = value.encode("utf-8") if isinstance(value, str) else value
    tag = (field_num << 3) | 2
    return bytes([tag]) + _encode_varint(len(b)) + b

def _grpc_encode(proto_bytes):
    header = struct.pack(">BI", 0, len(proto_bytes))
    return base64.b64encode(header + proto_bytes).decode("ascii")

def _grpc_decode_all(b64_text):
    try:
        raw = base64.b64decode(b64_text + "==")
    except Exception:
        return []
    frames = []
    i = 0
    while i + 5 <= len(raw):
        flag   = raw[i]
        length = struct.unpack(">I", raw[i+1:i+5])[0]
        i += 5
        if flag == 0 and length > 0:
            frames.append(raw[i:i+length])
        i += length
    return frames

def _parse_proto(data):
    results = []
    i = 0
    while i < len(data):
        try:
            tag_byte  = data[i]; i += 1
            field_num = tag_byte >> 3
            wire_type = tag_byte & 0x07
            if wire_type == 0:
                val = 0; shift = 0
                while True:
                    b = data[i]; i += 1
                    val |= (b & 0x7f) << shift; shift += 7
                    if not (b & 0x80): break
                results.append((field_num, 0, val))
            elif wire_type == 1:
                val = struct.unpack_from("<d", data, i)[0]; i += 8
                results.append((field_num, 1, val))
            elif wire_type == 2:
                length = 0; shift = 0
                while True:
                    b = data[i]; i += 1
                    length |= (b & 0x7f) << shift; shift += 7
                    if not (b & 0x80): break
                val_bytes = data[i:i+length]; i += length
                results.append((field_num, 2, val_bytes))
            elif wire_type == 5:
                val = struct.unpack_from("<f", data, i)[0]; i += 4
                results.append((field_num, 5, val))
            else:
                break
        except Exception:
            break
    return results

def _decode_str(b):
    try:
        return b.decode("utf-8")
    except Exception:
        return None


def extract_coupon_from_raw(frame):
    """
    ค้นหา coupon rate จาก raw frame
    Key insight: coupon rate เช่น 7.5, 6.5, 5.75 มี <= 2 decimal places
    ขณะที่ garbage decimals เช่น 4.4766 มี > 2 decimal places → กรองออก
    """
    text = frame.decode("latin-1")
    matches = re.findall(r'\b(\d{1,2}\.\d{1,4})\b', text)
    logger.info(f"[coupon_raw] all decimals: {matches[:15]}")

    for m in reversed(matches):
        decimal_part = m.split('.')[1]
        if len(decimal_part) > 2:
            # มีทศนิยมมากกว่า 2 ตำแหน่ง → ไม่ใช่ coupon rate ปกติ
            continue
        try:
            n = float(m)
            if 0.1 <= n <= 30:
                if n < 1: n *= 100
                result = f"{n:.4f}".rstrip("0").rstrip(".") + "%"
                logger.info(f"[coupon_raw] found: {m} → {result}")
                return result
        except Exception:
            pass
    return "-"



# ── ชื่อย่อบริษัทหลักทรัพย์ไทย ──────────────────────────────────────────────
SECURITIES_ABBR = {
    "KRUNGTHAI XSPRING": "KTX",
    "KTX": "KTX",
    "ASIA PLUS": "ASPS",
    "KASIKORN": "KS",
    "BUALUANG": "BLS",
    "SCB SECURITIES": "SCBS",
    "PHATRA": "PHATRA",
    "TRINITY": "TRINITY",
    "CIMB SECURITIES": "CIMBS",
    "CGS-CIMB": "CGS",
    "CGS CIMB": "CGS",
    "FINANSIA SYRUS": "FSS",
    "KGI SECURITIES": "KGI",
    "MAYBANK": "MBS",
    "RHB SECURITIES": "RHB",
    "TISCO": "TISCO",
    "YUANTA": "YUANTA",
    "MBK SECURITIES": "MBKS",
    "PHILLIP": "PST",
    "DBS VICKERS": "DBS",
    "GLOBLEX": "GLBL",
    "SEAMICO": "ZMICO",
    "ZMICO": "ZMICO",
    "BLUEBELL": "BLUE",
    "KIATNAKIN PHATRA": "KKP",
    "KIATNAKIN": "KKP",
    "PHATRA SECURITIES": "PHATRA",
    "UOB KAY HIAN": "UOBKH",
    "KRUNGSRI": "KSS",
    "BANGKOK SECURITIES": "BS",
    "หลักทรัพย์กรุงไทย เอ็กซ์สปริง": "KTX",
    "หลักทรัพย์เอเซีย พลัส": "ASPS",
    "หลักทรัพย์กสิกรไทย": "KS",
    "หลักทรัพย์บัวหลวง": "BLS",
}


def abbr_company(name):
    """แปลงชื่อเต็มเป็นชื่อย่อ"""
    if not name or name == "-":
        return name
    upper = name.upper()
    for key, abbr in SECURITIES_ABBR.items():
        if key.upper() in upper:
            return abbr
    # ถ้าไม่มีใน mapping ตัดคำว่า SECURITIES/COMPANY/LIMITED ออก
    name = re.sub(r'\s*(SECURITIES|COMPANY|LIMITED|CO\.,?\s*LTD\.?|PUBLIC)\s*', ' ', name, flags=re.I)
    name = name.strip().rstrip(',').strip()
    return name


def clean_participant(val):
    """
    ตัดเบอร์โทรออก แล้วแปลงเป็นชื่อย่อ
    รองรับหลายบริษัทที่คั่นด้วย | เช่น 'BLUEBELL...|KRUNGTHAI...'
    """
    if not val or val == "-":
        return val
    # แยกหลายบริษัทด้วย | ก่อน
    companies = [c.strip() for c in val.split("|") if c.strip()]
    results = []
    for company in companies:
        # ตัดเบอร์โทร (ส่วนหลัง ":")
        # ระวัง: เบอร์โทรมีรูปแบบ "02-xxx" ส่วน "CO., LTD." ไม่มี ":"
        # ตัด ":XXXXXXXXX" ที่เป็นตัวเลขหรือเครื่องหมายออก
        clean = re.sub(r':[\d\-\s]+$', '', company).strip()
        abbr = abbr_company(clean)
        if abbr and abbr not in results:
            results.append(abbr)
    return " / ".join(results) if results else val


def abbr_participants(names_str):
    """แปลง list ชื่อ (คั่นด้วย / ) เป็นชื่อย่อ"""
    if not names_str or names_str == "-":
        return names_str
    parts = [p.strip() for p in names_str.split("/") if p.strip()]
    abbrs = [abbr_company(p.split(":")[0].strip()) for p in parts]
    # กรองซ้ำ
    seen = []
    for a in abbrs:
        if a not in seen:
            seen.append(a)
    return " / ".join(seen)


# ─── Login ────────────────────────────────────────────────────────────────────

_cached_token = None

def get_bearer_token(session):
    global _cached_token
    if _cached_token:
        return _cached_token
    if not THAIBMA_USERNAME or not THAIBMA_PASSWORD:
        return None
    try:
        session.get(f"{BASE_IBOND}/login", headers={**HEADERS_BASE, "Accept": "text/html"}, timeout=10)
    except Exception:
        pass
    proto   = _proto_string(1, THAIBMA_USERNAME) + _proto_string(2, THAIBMA_PASSWORD)
    payload = _grpc_encode(proto)
    headers = {**HEADERS_BASE, "Accept": "application/grpc-web-text",
               "Content-Type": "application/grpc-web-text", "X-Grpc-Web": "1",
               "Origin": BASE_IBOND, "Referer": f"{BASE_IBOND}/login"}
    try:
        resp = session.post(
            f"{BASE_IBOND}/grpc/authen-grpc/authen.AuthenGrpcService/Authenticate",
            data=payload, headers=headers, timeout=20)
        frames = _grpc_decode_all(resp.text)
        for frame in frames:
            for fnum, wtype, val in _parse_proto(frame):
                if wtype == 2:
                    s = _decode_str(val)
                    if s and len(s) > 30 and "." in s:
                        _cached_token = s
                        logger.info(f"[login] token ok: {s[:30]}...")
                        return s
    except Exception as e:
        logger.exception(f"[login] error: {e}")
    return None

# ─── gRPC call ────────────────────────────────────────────────────────────────

def grpc_call(svc, method, proto_bytes, token, session, referer=None):
    payload = _grpc_encode(proto_bytes)
    headers = {**HEADERS_BASE, "Accept": "application/grpc-web-text",
               "Content-Type": "application/grpc-web-text", "X-Grpc-Web": "1",
               "Authorization": f"Bearer {token}", "Origin": BASE_IBOND,
               "Referer": referer or f"{BASE_IBOND}/bonds"}
    try:
        resp = session.post(f"{BASE_IBOND}/grpc/{svc}/{method}",
                            data=payload, headers=headers, timeout=30)
        logger.info(f"[grpc] {method}: status={resp.status_code}, len={len(resp.text)}")
        if resp.status_code != 200 or not resp.text:
            return []
        return _grpc_decode_all(resp.text)
    except Exception as e:
        logger.warning(f"[grpc] {method}: {e}")
        return []

# ─── GetBondFeature ───────────────────────────────────────────────────────────

def get_bond_detail(symbol, issue_uuid, token, session):
    """
    GetBondFeature ด้วย IssueID UUID ใน field 1
    ได้ข้อมูล: coupon (str "7.5" ใน raw), UW (field 11), BH Rep (field 12)
    """
    ref = f"{BASE_IBOND}/bonds?symbol={symbol}"
    try:
        session.get(ref, headers={**HEADERS_BASE, "Accept": "text/html", "Referer": BASE_IBOND}, timeout=10)
    except Exception:
        pass

    if not issue_uuid or issue_uuid == "-":
        return {}

    proto  = _proto_string(1, issue_uuid)
    frames = grpc_call(GRPC_BOND_SVC, "GetBondFeature", proto, token, session, referer=ref)
    logger.info(f"[GetBondFeature] {symbol}: {len(frames)} frames")
    if not frames:
        return {}

    result = {}
    frame  = frames[0]

    # ── Coupon Rate ──
    coupon = extract_coupon_from_raw(frame)
    if coupon != "-":
        result["coupon_rate"] = coupon

    # ── Underwriter (field 11) & BH Rep (field 12) ──
    # Log all string fields for debugging
    uw_list = []
    bh_list = []
    for fnum, wtype, val in _parse_proto(frame):
        if wtype == 2:
            s = _decode_str(val)
            if s and len(s) > 3:
                logger.info(f"[field] {fnum} = {s[:80]!r}")
                if fnum == 11:
                    abbr = clean_participant(s)
                    if abbr and abbr not in uw_list:
                        uw_list.append(abbr)
                elif fnum == 12:
                    abbr = clean_participant(s)
                    if abbr and abbr not in bh_list:
                        bh_list.append(abbr)
    if uw_list:
        result["underwriters"] = " / ".join(uw_list)
        logger.info(f"[GetBondFeature] UW: {result['underwriters']}")
    if bh_list:
        result["bondholder_rep"] = " / ".join(bh_list)
        logger.info(f"[GetBondFeature] BH Rep: {result['bondholder_rep']}")

    return result


# ─── REST helpers ─────────────────────────────────────────────────────────────

def api_get(url, session, ref=None):
    try:
        r = session.get(url, headers={**HEADERS_BASE, "Accept": "application/json, */*",
                                      "Referer": ref or BASE_THAIBMA}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"[api_get] {url}: {e}")
        return None

def fmt_date(raw):
    if not raw or raw in ["-", "null", "None"]:
        return "-"
    raw = raw.split("T")[0].strip()
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"]:
        try:
            return datetime.strptime(raw, fmt).strftime("%-d %b %Y")
        except ValueError:
            continue
    return raw

def fmt_number(raw):
    if not raw or raw == "-":
        return "-"
    try:
        n = float(raw)
        return f"{int(n):,}" if n == int(n) else f"{n:,.2f}"
    except Exception:
        return raw

# ─── Bond List ────────────────────────────────────────────────────────────────

def fetch_bond_list(abbr_name, session):
    all_bonds = []
    ref = f"{ISSUER_URL}?issuer={abbr_name.lower()}"
    for term in ["long", "short"]:
        data = api_get(f"{REGISSUE_URL}?abbrName={abbr_name}&term={term}", session, ref)
        if not data:
            continue
        items = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in ["data", "result", "bonds", "items", "records", "value"]:
                if key in data and isinstance(data[key], list):
                    items = data[key]; break
        for item in items:
            bond = _item_to_bond(item, term)
            if bond:
                all_bonds.append(bond)
        logger.info(f"[api] {term}: {len(items)} items")
    logger.info(f"[api] Total: {len(all_bonds)}")
    return all_bonds

def _item_to_bond(item, term_type):
    if not isinstance(item, dict):
        return None
    def g(*keys):
        for k in keys:
            if k in item and item[k] is not None:
                v = str(item[k]).strip()
                if v and v not in ["", "null", "None"]:
                    return v
        return "-"
    symbol = g("Symbol", "symbol", "ThaiBMASymbol")
    if symbol == "-":
        return None
    secure_code   = g("SecureCode", "securedType", "SecuredType")
    secured_label = "🔒 มีหลักประกัน" if (secure_code != "-" and "unsecure" not in secure_code.lower()) else "🔓 ไม่มีหลักประกัน"
    return {
        "symbol":           symbol,
        "issue_id":         g("IssueID", "issueId"),
        "term_type":        "Long Term" if term_type == "long" else "Short Term",
        "issue_date":       fmt_date(g("IssuedDate", "IssueDate")),
        "maturity_date":    fmt_date(g("MaturityDate", "maturityDate")),
        "tenor":            g("Term", "term", "tenor"),
        "coupon_rate":      "-",
        "issue_size":       fmt_number(g("IssueSize", "issueSize")),
        "outstanding_size": fmt_number(g("CurrentOutstanding", "IssueOutstanding")),
        "secured_label":    secured_label,
        "registrar":        g("Registrar", "registrar"),
        "bondholder_rep":   g("BondholderRepresentative"),
        "underwriters":     g("Underwriter", "underwriter"),
        "issue_rating":     g("IssueRating", "issueRating"),
        "issuer_rating":    g("CompanyRating", "issuerRating"),
        "distribution":     g("DistributionDisplay", "distribution"),
        "isin":             g("IssueLegacyID", "isinCode"),
    }

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name):
    session = requests.Session()
    abbr    = company_name.strip().upper()
    logger.info(f"[main] === Searching: '{abbr}' ===")
    token = get_bearer_token(session)
    bonds = fetch_bond_list(abbr, session)
    if not bonds:
        return []

    for b in bonds[:15]:
        symbol   = b.get("symbol", "")
        issue_id = b.get("issue_id", "-")
        if not symbol or not token:
            continue
        time.sleep(0.3)
        detail = get_bond_detail(symbol, issue_id, token, session)
        for k, v in detail.items():
            if k not in b or b[k] == "-":
                b[k] = v

    logger.info(f"[main] Done: {len(bonds)} bonds")
    return bonds

# ─── FORMAT ───────────────────────────────────────────────────────────────────

def format_bond_message(bonds, company_name):
    if not bonds:
        return (
            f"❌ ไม่พบข้อมูลหุ้นกู้ของ \"{company_name}\"\n\n"
            "💡 ลองพิมพ์ชื่อย่อ:\n"
            "  เช่น PTT, CPALL, CI, ASW, KBANK\n\n"
            "📌 ข้อมูลจาก ThaiBMA"
        )
    long_bonds  = [b for b in bonds if "Long"  in b.get("term_type", "")]
    short_bonds = [b for b in bonds if "Short" in b.get("term_type", "")]
    lines = [f"📋 หุ้นกู้ {company_name.upper()} ({len(bonds)} รุ่น)", "─" * 28]

    def add_bonds(bond_list, label):
        if not bond_list:
            return
        lines.append(f"\n{label} ({len(bond_list)} รุ่น)")
        for b in bond_list:
            lines.extend([
                f"\n🔹 {b.get('symbol','-')}",
                f"  📅 ออก: {b.get('issue_date','-')}",
                f"  📅 ครบกำหนด: {b.get('maturity_date','-')}",
                f"  ⏳ อายุ: {b.get('tenor','-')}",
                f"  💰 ดอกเบี้ย: {b.get('coupon_rate','-')}",
                f"  💵 Outstanding: {b.get('outstanding_size', b.get('issue_size','-'))} ลบ.",
                f"  {b.get('secured_label','🔓 ไม่มีหลักประกัน')}",
                f"  📢 ขายให้: {b.get('distribution','-')}",
                f"  📊 Issue Rating: {b.get('issue_rating','-')}",
                f"  📊 Issuer Rating: {b.get('issuer_rating','-')}",
                f"  🏦 Registrar: {b.get('registrar','-')}",
                f"  👤 BH Rep: {b.get('bondholder_rep','-')}",
                f"  📢 UWs: {b.get('underwriters','-')}",
            ])

    add_bonds(long_bonds,  "📌 Long Term Debenture")
    add_bonds(short_bonds, "📌 Short Term Debenture")
    lines.extend(["", "─" * 28, "📌 ข้อมูลจาก ThaiBMA"])
    return "\n".join(lines)
