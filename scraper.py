import requests
import logging
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
}

BASE         = "https://www.thaibma.or.th"
REGISSUE_URL = f"{BASE}/issuer/regissue"
ISSUE_URL    = f"{BASE}/issue"
ISSUER_URL   = f"{BASE}/EN/Issuer/IssuerDetail.aspx"
BOND_PAGE    = f"{BASE}/EN/BondInfo/BondFeature/Issue.aspx"


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


def warm_session(session):
    """เข้าหน้าเว็บก่อนเพื่อได้ session cookie"""
    try:
        session.get(
            BOND_PAGE,
            headers={**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"},
            timeout=15
        )
        logger.info("[session] warmed up")
    except Exception as e:
        logger.warning(f"[session] warm failed: {e}")


def api_get(url, session, ref=None):
    try:
        r = session.get(
            url,
            headers={**HEADERS, "Referer": ref or BOND_PAGE},
            timeout=15
        )
        if r.status_code == 401 or "Authorization has been denied" in r.text:
            logger.warning(f"[api_get] auth denied: {url} — retrying after warm")
            warm_session(session)
            time.sleep(0.5)
            r = session.get(url, headers={**HEADERS, "Referer": ref or BOND_PAGE}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"[api_get] {url}: {e}")
        return None


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


def parse_coupon(data):
    if not data:
        return "-"
    logger.info(f"[coupon] raw: {str(data)[:200]}")
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ["CouponRate", "couponRate", "Rate", "rate", "Value", "value",
                    "Reference", "reference", "CouponValue", "couponValue"]:
            val = item.get(key)
            if val is None or str(val).strip() in ["", "null", "None", "-"]:
                continue
            v = str(val).strip()
            m = re.search(r"Fixed\s*:?\s*([\d.]+)", v, re.I)
            if m:
                n = float(m.group(1))
                if n < 1:
                    n *= 100
                return f"{n:.4f}".rstrip("0").rstrip(".") + "%"
            try:
                n = float(v)
                if 0 < n <= 50:
                    if n < 1:
                        n *= 100
                    return f"{n:.4f}".rstrip("0").rstrip(".") + "%"
            except ValueError:
                pass
            if any(k in v.upper() for k in ["FRN", "FLOAT", "MLR", "MOR", "TBR"]):
                return v[:40]
    return "-"


# ─── STEP 1: Bond List ────────────────────────────────────────────────────────

def fetch_bond_list(abbr_name, session):
    all_bonds = []
    ref = f"{ISSUER_URL}?issuer={abbr_name.lower()}"
    for term in ["long", "short"]:
        url = f"{REGISSUE_URL}?abbrName={abbr_name}&term={term}"
        data = api_get(url, session, ref)
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
        "secured_type":     secure_code,
        "secured_label":    secured_label,
        "registrar":        g("Registrar", "registrar"),
        "bondholder_rep":   g("BondholderRepresentative"),
        "underwriters":     "-",
        "issue_rating":     g("IssueRating", "issueRating"),
        "issuer_rating":    g("CompanyRating", "issuerRating"),
        "distribution":     g("DistributionDisplay", "distribution"),
        "isin":             g("IssueLegacyID", "isinCode"),
    }


# ─── STEP 2: Coupon + Participants ────────────────────────────────────────────

def fetch_bond_apis(symbol, session):
    detail = {}
    ref = BOND_PAGE

    # Coupon
    coupon_data = api_get(f"{ISSUE_URL}/couponpaymentreference?Symbol={symbol}", session, ref)
    coupon = parse_coupon(coupon_data)
    if coupon != "-":
        detail["coupon_rate"] = coupon
    time.sleep(0.2)

    # Underwriter
    uw_data = api_get(f"{ISSUE_URL}/participant?Symbol={symbol}&InstitutionRole=UDW", session, ref)
    uw = parse_participant(uw_data, "UDW")
    if uw != "-":
        detail["underwriters"] = uw
    time.sleep(0.2)

    # BH Rep
    rept_data = api_get(f"{ISSUE_URL}/participant?Symbol={symbol}&InstitutionRole=REPT", session, ref)
    rept = parse_participant(rept_data, "REPT")
    if rept != "-":
        detail["bondholder_rep"] = rept
    time.sleep(0.2)

    # Registrar
    regt_data = api_get(f"{ISSUE_URL}/participant?Symbol={symbol}&InstitutionRole=REGT", session, ref)
    regt = parse_participant(regt_data, "REGT")
    if regt != "-":
        detail["registrar"] = regt

    return detail


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name):
    session = requests.Session()
    abbr = company_name.strip().upper()
    logger.info(f"[main] === Searching: '{abbr}' ===")

    # warm session ก่อน
    warm_session(session)

    bonds = fetch_bond_list(abbr, session)
    if not bonds:
        return []

    results = []
    for b in bonds[:15]:
        symbol = b.get("symbol", "")
        if symbol:
            time.sleep(0.2)
            detail = fetch_bond_apis(symbol, session)
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
