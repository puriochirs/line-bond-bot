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
ISSUE_URL    = f"{BASE_THAIBMA}/issue"
ISSUER_URL   = f"{BASE_THAIBMA}/EN/Issuer/IssuerDetail.aspx"

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
}

# ─── gRPC-web-text helpers ────────────────────────────────────────────────────

def _encode_varint(n):
    result = []
    while n > 127:
        result.append((n & 0x7f) | 0x80)
        n >>= 7
    result.append(n)
    return bytes(result)

def _proto_string(field_num, value):
    """Encode protobuf string field"""
    b = value.encode("utf-8") if isinstance(value, str) else value
    tag = (field_num << 3) | 2
    return bytes([tag]) + _encode_varint(len(b)) + b

def _grpc_encode(proto_bytes):
    """Wrap protobuf in gRPC-web-text frame → base64 string"""
    header = struct.pack(">BI", 0, len(proto_bytes))
    return base64.b64encode(header + proto_bytes).decode("ascii")

def _grpc_decode(b64_text):
    """Decode gRPC-web-text response → protobuf bytes"""
    try:
        raw = base64.b64decode(b64_text + "==")
        if len(raw) < 5:
            return None
        length = struct.unpack(">I", raw[1:5])[0]
        return raw[5:5 + length]
    except Exception:
        return None

def _proto_read_string(data, field_num):
    """Read first string field with given number from protobuf bytes"""
    i = 0
    while i < len(data):
        try:
            tag = data[i]; i += 1
            f = tag >> 3
            wt = tag & 0x07
            if wt == 2:
                # read varint length
                length = 0; shift = 0
                while True:
                    b = data[i]; i += 1
                    length |= (b & 0x7f) << shift
                    shift += 7
                    if not (b & 0x80): break
                val = data[i:i + length]; i += length
                if f == field_num:
                    return val.decode("utf-8", errors="replace")
            elif wt == 0:
                while data[i] & 0x80: i += 1
                i += 1
            else:
                break
        except Exception:
            break
    return None


# ─── Login to iBond → get Bearer token ───────────────────────────────────────

_cached_token = None

def get_bearer_token(session):
    global _cached_token
    if _cached_token:
        return _cached_token

    if not THAIBMA_USERNAME or not THAIBMA_PASSWORD:
        logger.warning("[login] No credentials in env vars")
        return None

    proto = _proto_string(1, THAIBMA_USERNAME) + _proto_string(2, THAIBMA_PASSWORD)
    payload = _grpc_encode(proto)

    headers = {
        **HEADERS_BASE,
        "Accept": "application/grpc-web-text",
        "Content-Type": "application/grpc-web-text",
        "X-Grpc-Web": "1",
        "Origin": BASE_IBOND,
        "Referer": f"{BASE_IBOND}/login",
    }

    try:
        resp = session.post(
            f"{BASE_IBOND}/grpc/authen-grpc/authen.AuthenGrpcService/Authenticate",
            data=payload,
            headers=headers,
            timeout=20,
        )
        logger.info(f"[login] status={resp.status_code}, len={len(resp.text)}")

        if resp.status_code != 200:
            logger.warning(f"[login] failed: {resp.text[:100]}")
            return None

        proto_resp = _grpc_decode(resp.text)
        if not proto_resp:
            logger.warning(f"[login] decode failed. raw: {resp.text[:80]}")
            return None

        logger.info(f"[login] proto_resp bytes: {proto_resp[:30].hex()}")

        # Token likely in field 1
        token = _proto_read_string(proto_resp, 1)
        if token and len(token) > 20:
            _cached_token = token
            logger.info(f"[login] token ok: {token[:30]}...")
            return token

        # Try field 2 if field 1 not right
        token2 = _proto_read_string(proto_resp, 2)
        if token2 and len(token2) > 20:
            _cached_token = token2
            logger.info(f"[login] token field2: {token2[:30]}...")
            return token2

        logger.warning(f"[login] no token. proto: {proto_resp.hex()}")
        return None

    except Exception as e:
        logger.exception(f"[login] error: {e}")
        return None


# ─── API helpers ──────────────────────────────────────────────────────────────

def api_get_json(url, session, token=None, ref=None):
    headers = {
        **HEADERS_BASE,
        "Accept": "application/json, */*",
        "Referer": ref or BASE_THAIBMA,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = session.get(url, headers=headers, timeout=15)
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


def parse_coupon(data):
    if not data:
        return "-"
    logger.info(f"[coupon] raw: {str(data)[:200]}")
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ["CouponRate", "couponRate", "Rate", "rate", "Value", "value",
                    "Reference", "reference"]:
            val = item.get(key)
            if val is None or str(val).strip() in ["", "null", "None", "0", "0.0", "-"]:
                continue
            v = str(val).strip()
            m = re.search(r"Fixed\s*:?\s*([\d.]+)", v, re.I)
            if m:
                n = float(m.group(1))
                if n < 1: n *= 100
                return f"{n:.4f}".rstrip("0").rstrip(".") + "%"
            try:
                n = float(v)
                if 0 < n <= 50:
                    if n < 1: n *= 100
                    return f"{n:.4f}".rstrip("0").rstrip(".") + "%"
            except ValueError:
                pass
            if any(k in v.upper() for k in ["FRN", "FLOAT", "MLR", "MOR", "TBR"]):
                return v[:40]
    return "-"


def parse_participant(data, role):
    if not data:
        return "-"
    names = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ["InstitutionName", "Name", "name", "institutionName"]:
            n = item.get(key, "")
            if n and str(n).strip():
                names.append(str(n).strip())
                break
    result = " / ".join(names) if names else "-"
    logger.info(f"[participant] {role}: {result[:80]}")
    return result


# ─── Bond List ────────────────────────────────────────────────────────────────

def fetch_bond_list(abbr_name, session):
    all_bonds = []
    ref = f"{ISSUER_URL}?issuer={abbr_name.lower()}"
    for term in ["long", "short"]:
        url = f"{REGISSUE_URL}?abbrName={abbr_name}&term={term}"
        data = api_get_json(url, session, ref=ref)
        if not data:
            continue
        items = data if isinstance(data, list) else []
        if isinstance(data, dict):
            for key in ["data", "result", "bonds", "items", "records", "value"]:
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break
        for item in items:
            bond = _item_to_bond(item, term)
            if bond:
                all_bonds.append(bond)
        logger.info(f"[api] {term}: {len(items)} items")
    logger.info(f"[api] Total: {len(all_bonds)} bonds")
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

    secure_code = g("SecureCode", "securedType", "SecuredType")
    secured_label = "🔒 มีหลักประกัน" if (secure_code != "-" and "unsecure" not in secure_code.lower()) else "🔓 ไม่มีหลักประกัน"

    return {
        "symbol":           symbol,
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
        "option":           g("OptionCode", "optionCode"),
    }


# ─── Fetch coupon + participants using Bearer token ───────────────────────────

def fetch_bond_detail_with_token(symbol, token, session):
    detail = {}
    ref = f"{BASE_THAIBMA}/EN/BondInfo/BondFeature/Issue.aspx"

    # Coupon
    coupon_data = api_get_json(
        f"{ISSUE_URL}/couponpaymentreference?Symbol={symbol}",
        session, token=token, ref=ref
    )
    coupon = parse_coupon(coupon_data)
    if coupon != "-":
        detail["coupon_rate"] = coupon
        logger.info(f"[detail] coupon: {coupon}")
    time.sleep(0.2)

    # Underwriter
    uw_data = api_get_json(
        f"{ISSUE_URL}/participant?Symbol={symbol}&InstitutionRole=UDW",
        session, token=token, ref=ref
    )
    uw = parse_participant(uw_data, "UDW")
    if uw != "-":
        detail["underwriters"] = uw
    time.sleep(0.2)

    # BH Rep
    rept_data = api_get_json(
        f"{ISSUE_URL}/participant?Symbol={symbol}&InstitutionRole=REPT",
        session, token=token, ref=ref
    )
    rept = parse_participant(rept_data, "REPT")
    if rept != "-":
        detail["bondholder_rep"] = rept
    time.sleep(0.2)

    # Registrar
    regt_data = api_get_json(
        f"{ISSUE_URL}/participant?Symbol={symbol}&InstitutionRole=REGT",
        session, token=token, ref=ref
    )
    regt = parse_participant(regt_data, "REGT")
    if regt != "-":
        detail["registrar"] = regt

    return detail


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name):
    session = requests.Session()
    abbr = company_name.strip().upper()
    logger.info(f"[main] === Searching: '{abbr}' ===")

    # Login → get token
    token = get_bearer_token(session)
    logger.info(f"[main] token available: {bool(token)}")

    bonds = fetch_bond_list(abbr, session)
    if not bonds:
        return []

    results = []
    for b in bonds[:15]:
        symbol = b.get("symbol", "")
        if symbol and token:
            time.sleep(0.2)
            detail = fetch_bond_detail_with_token(symbol, token, session)
            for k, v in detail.items():
                if k not in b or b[k] == "-":
                    b[k] = v
        results.append(b)

    logger.info(f"[main] Done: {len(results)} bonds")
    return results


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
