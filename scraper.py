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
GRPC_SVC     = "bondsearch-grpc/BondSearchGrpc.Models.BondSearchGrpcService"

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
}

# ─── gRPC helpers ─────────────────────────────────────────────────────────────

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

def _proto_varint(field_num, value):
    tag = (field_num << 3) | 0
    return bytes([tag]) + _encode_varint(int(value))

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

# ─── Login ────────────────────────────────────────────────────────────────────

_cached_token = None

def get_bearer_token(session):
    global _cached_token
    if _cached_token:
        return _cached_token
    if not THAIBMA_USERNAME or not THAIBMA_PASSWORD:
        return None
    proto   = _proto_string(1, THAIBMA_USERNAME) + _proto_string(2, THAIBMA_PASSWORD)
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
            data=payload, headers=headers, timeout=20,
        )
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

def grpc_call(method, proto_bytes, token, session):
    payload = _grpc_encode(proto_bytes)
    headers = {
        **HEADERS_BASE,
        "Accept":        "application/grpc-web-text",
        "Content-Type":  "application/grpc-web-text",
        "X-Grpc-Web":    "1",
        "Authorization": f"Bearer {token}",
        "Origin":        BASE_IBOND,
        "Referer":       f"{BASE_IBOND}/bondsearch/bondsearchpage",
    }
    try:
        resp = session.post(
            f"{BASE_IBOND}/grpc/{GRPC_SVC}/{method}",
            data=payload, headers=headers, timeout=30,
        )
        logger.info(f"[grpc] {method}: status={resp.status_code}, len={len(resp.text)}")
        if resp.status_code != 200:
            return []
        return _grpc_decode_all(resp.text)
    except Exception as e:
        logger.warning(f"[grpc] {method}: {e}")
        return []

# ─── GetSearchResult ─────────────────────────────────────────────────────────

def search_bond_detail(symbol, token, session):
    """
    เรียก GetSearchResult ด้วย symbol filter
    แล้ว parse ข้อมูลหุ้นกู้แต่ละตัวออกมา
    """
    # Build search request proto
    # Field 1 = symbol search text
    # อาจมี pagination fields อื่นๆ ด้วย
    proto = _proto_string(1, symbol)  # search by symbol
    frames = grpc_call("GetSearchResult", proto, token, session)

    logger.info(f"[search_result] {symbol}: {len(frames)} frames")

    results = []
    for fi, frame in enumerate(frames):
        logger.info(f"[search_result] frame {fi} len={len(frame)}, hex={frame[:40].hex()}")
        # Parse top-level: list of bond records
        for fnum, wtype, val in _parse_proto(frame):
            if wtype == 2:
                # Each field 1 entry = one bond record
                s = _decode_str(val)
                if s:
                    logger.info(f"[search_result] f{fnum} str: {s[:80]!r}")
                else:
                    # nested proto = bond record
                    bond_data = _extract_bond_from_search(val, symbol)
                    if bond_data:
                        results.append(bond_data)
                        logger.info(f"[search_result] found bond: {bond_data}")

    return results


def _extract_bond_from_search(data, target_symbol):
    """Parse one bond record from GetSearchResult"""
    fields = _parse_proto(data)
    record = {}
    for fnum, wtype, val in fields:
        if wtype == 2:
            s = _decode_str(val)
            if s:
                record[fnum] = s
            else:
                # nested
                sub = {}
                for sf, sw, sv in _parse_proto(val):
                    if sw == 2:
                        ss = _decode_str(sv)
                        if ss:
                            sub[sf] = ss
                    elif sw in (0, 1, 5):
                        sub[sf] = float(sv)
                if sub:
                    record[f"nested_{fnum}"] = sub
        elif wtype in (0, 1, 5):
            record[fnum] = float(val)

    logger.info(f"[bond_record] {record}")

    # ตรวจว่าเป็น bond ที่เราต้องการ
    sym_val = next((v for k, v in record.items() if isinstance(v, str) and target_symbol in v.upper()), None)
    if not sym_val and target_symbol not in str(record):
        return None

    return record


# ─── Parse coupon from search result ──────────────────────────────────────────

def get_coupon_from_search(symbol, token, session):
    """ลอง GetSearchResult เพื่อดึง coupon rate"""
    frames = grpc_call("GetSearchResult", _proto_string(1, symbol), token, session)
    logger.info(f"[coupon_search] {symbol}: {len(frames)} frames")

    for frame in frames:
        # dump all strings และ numbers ทั้งหมดออกมา
        all_data = _dump_all(frame, "")
        logger.info(f"[coupon_search] all_data: {str(all_data)[:500]}")

        # หา pattern ที่น่าจะเป็น coupon rate (เช่น 7.5)
        for path, val in all_data:
            if isinstance(val, (int, float)):
                n = float(val)
                # coupon rate 0.5% - 30% น่าจะอยู่ช่วงนี้
                if 0.5 <= n <= 30:
                    logger.info(f"[coupon_search] rate candidate {path}={n}")
            elif isinstance(val, str):
                m = re.search(r"\b(\d+\.?\d*)\b", val)
                if m:
                    try:
                        n = float(m.group(1))
                        if 0.5 <= n <= 30:
                            logger.info(f"[coupon_search] str candidate {path}={n} from {val[:30]!r}")
                    except Exception:
                        pass
    return "-"


def _dump_all(data, path, depth=0):
    """Dump all values recursively"""
    if depth > 5:
        return []
    results = []
    try:
        fields = _parse_proto(data)
        for fnum, wtype, val in fields:
            cur = f"{path}.{fnum}"
            if wtype in (0, 1, 5):
                results.append((cur, float(val)))
            elif wtype == 2:
                s = _decode_str(val)
                if s:
                    results.append((cur, s))
                if len(val) > 2:
                    results.extend(_dump_all(val, cur, depth+1))
    except Exception:
        pass
    return results


# ─── Participants ─────────────────────────────────────────────────────────────

def get_participants(symbol, role, token, session):
    proto  = _proto_string(1, symbol) + _proto_string(2, role)
    frames = grpc_call("GetParticipants", proto, token, session)
    names  = []
    for frame in frames:
        for fnum, wtype, val in _parse_proto(frame):
            if wtype == 2:
                s = _decode_str(val)
                if s and len(s) > 3 and s not in names:
                    names.append(s)
                elif not s:
                    for sf, sw, sv in _parse_proto(val):
                        if sw == 2:
                            ss = _decode_str(sv)
                            if ss and len(ss) > 3 and ss not in names:
                                names.append(ss)
    result = " / ".join(names) if names else "-"
    logger.info(f"[participant] {role}: {result[:80]}")
    return result


# ─── REST helpers ─────────────────────────────────────────────────────────────

def api_get(url, session, ref=None):
    try:
        r = session.get(url, headers={**HEADERS_BASE, "Accept": "application/json, */*", "Referer": ref or BASE_THAIBMA}, timeout=15)
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
    logger.info(f"[main] token: {bool(token)}")

    bonds = fetch_bond_list(abbr, session)
    if not bonds:
        return []

    for b in bonds[:15]:
        symbol = b.get("symbol", "")
        if not symbol or not token:
            continue
        time.sleep(0.3)
        # ลอง GetSearchResult เพื่อดู structure
        get_coupon_from_search(symbol, token, session)
        time.sleep(0.2)
        uw = get_participants(symbol, "UDW", token, session)
        if uw != "-":
            b["underwriters"] = uw
        time.sleep(0.2)
        rept = get_participants(symbol, "REPT", token, session)
        if rept != "-":
            b["bondholder_rep"] = rept

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
