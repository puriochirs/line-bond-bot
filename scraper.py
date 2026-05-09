import os
import struct
import base64
import requests
import logging
import re
import time
from bs4 import BeautifulSoup
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


# ─── Fetch ibond bond page ────────────────────────────────────────────────────

def get_coupon_from_ibond_page(symbol, token, session):
    """
    เข้าหน้า ibond.thaibma.or.th/bonds?symbol=PCLV268A
    แล้ว parse coupon rate ออกจากหน้า HTML หรือ JSON response
    """
    url = f"{BASE_IBOND}/bonds?symbol={symbol}"
    headers = {
        **HEADERS_BASE,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*",
        "Authorization": f"Bearer {token}",
        "Referer": f"{BASE_IBOND}/bondsearch/bondsearchpage",
    }

    try:
        resp = session.get(url, headers=headers, timeout=20)
        logger.info(f"[ibond_page] {url}: status={resp.status_code}, len={len(resp.text)}, ct={resp.headers.get('Content-Type','')}")
        logger.info(f"[ibond_page] response preview: {resp.text[:500]}")

        if resp.status_code != 200:
            return "-"

        ct = resp.headers.get("Content-Type", "")

        # JSON response
        if "json" in ct:
            try:
                data = resp.json()
                logger.info(f"[ibond_page] json keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
                coupon = _extract_coupon_from_json(data)
                if coupon != "-":
                    return coupon
            except Exception:
                pass

        # HTML response — parse ด้วย BeautifulSoup
        if "html" in ct or resp.text.strip().startswith("<"):
            soup = BeautifulSoup(resp.text, "lxml")
            full_text = soup.get_text(separator=" ")
            logger.info(f"[ibond_page] page text preview: {full_text[:300]}")

            # หา coupon rate pattern ในข้อความ
            patterns = [
                r"Coupon\s*(?:Rate|Payment)?[:\s]+(\d+\.?\d*)\s*%",
                r"Fixed\s*:?\s*(\d+\.?\d*)\s*%?",
                r"(\d+\.?\d*)\s*%\s*(?:per\s*annum|p\.a\.)",
                r"อัตราดอกเบี้ย[:\s]+(\d+\.?\d*)",
            ]
            for pat in patterns:
                m = re.search(pat, full_text, re.I)
                if m:
                    n = float(m.group(1))
                    if 0.1 <= n <= 50:
                        if n < 1: n *= 100
                        result = f"{n:.4f}".rstrip("0").rstrip(".") + "%"
                        logger.info(f"[ibond_page] coupon found: {result}")
                        return result

            # หา JSON ที่ embed ใน script tag
            for script in soup.find_all("script"):
                js = script.get_text()
                if "coupon" in js.lower() or "interest" in js.lower():
                    m = re.search(r'"(?:coupon|interest|rate)[Rr]ate"\s*:\s*"?([\d.]+)"?', js, re.I)
                    if m:
                        n = float(m.group(1))
                        if 0.1 <= n <= 50:
                            if n < 1: n *= 100
                            result = f"{n:.4f}".rstrip("0").rstrip(".") + "%"
                            logger.info(f"[ibond_page] coupon from script: {result}")
                            return result

        # ลอง API endpoint อื่นๆ ของ ibond
        api_urls = [
            f"{BASE_IBOND}/api/bonds/{symbol}",
            f"{BASE_IBOND}/api/bond?symbol={symbol}",
            f"{BASE_IBOND}/bonddetail?symbol={symbol}",
        ]
        for api_url in api_urls:
            try:
                r = session.get(api_url, headers=headers, timeout=10)
                logger.info(f"[ibond_api] {api_url}: status={r.status_code}, len={len(r.text)}")
                if r.status_code == 200 and len(r.text) > 10:
                    logger.info(f"[ibond_api] response: {r.text[:200]}")
                    try:
                        data = r.json()
                        coupon = _extract_coupon_from_json(data)
                        if coupon != "-":
                            return coupon
                    except Exception:
                        pass
            except Exception:
                pass

    except Exception as e:
        logger.exception(f"[ibond_page] error: {e}")

    return "-"


def _extract_coupon_from_json(data):
    if not data:
        return "-"
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ["couponRate", "CouponRate", "rate", "Rate", "interestRate",
                    "InterestRate", "fixedRate", "FixedRate", "coupon", "Coupon"]:
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
                if 0.1 <= n <= 50:
                    if n < 1: n *= 100
                    return f"{n:.4f}".rstrip("0").rstrip(".") + "%"
            except ValueError:
                pass
    return "-"


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
        coupon = get_coupon_from_ibond_page(symbol, token, session)
        if coupon != "-":
            b["coupon_rate"] = coupon

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
