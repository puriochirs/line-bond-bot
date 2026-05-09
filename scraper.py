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


def api_get(url, session, ref=None):
    try:
        r = session.get(url, headers={**HEADERS, "Referer": ref or BASE}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"[api_get] FAIL {url}: {e}")
        return None


def fmt_coupon(val):
    """แปลงค่า coupon หลายรูปแบบเป็น X.XX%"""
    if val is None:
        return "-"
    v = str(val).strip()
    if not v or v in ["", "0", "0.0", "null", "None", "-"]:
        return "-"
    # Fixed: 7.5 หรือ Fixed:0.075
    m = re.search(r"Fixed\s*:?\s*([\d.]+)", v, re.I)
    if m:
        n = float(m.group(1))
        if n < 1: n *= 100
        return f"{n:.4f}".rstrip("0").rstrip(".") + "%"
    # FRN / Floating
    if any(k in v.upper() for k in ["FRN", "FLOAT", "MLR", "MOR", "TBR"]):
        return v[:40]
    # ตัวเลขล้วน
    try:
        n = float(v)
        if 0 < n <= 50:
            if n < 1: n *= 100
            return f"{n:.4f}".rstrip("0").rstrip(".") + "%"
    except ValueError:
        pass
    return "-"


def clean_uw(val):
    """กรอง garbage ออกจาก Underwriter"""
    if not val or val in ["-", ""]:
        return "-"
    blacklist = ["financial advisor", "remark", "note", "หมายเหตุ"]
    if any(b in val.lower() for b in blacklist):
        return "-"
    return val.strip()


# ─── STEP 1: Bond List from regissue API ─────────────────────────────────────

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

    # Log ALL fields for first bond to understand structure
    logger.info(f"[item] {symbol} ALL fields: { {k: str(v)[:40] for k, v in item.items()} }")

    # Check Rows field
    rows = item.get("Rows")
    if rows:
        logger.info(f"[item] {symbol} Rows: {str(rows)[:200]}")

    secure_code = g("SecureCode", "securedType", "SecuredType")
    secured_label = "🔒 มีหลักประกัน" if (secure_code != "-" and "unsecure" not in secure_code.lower()) else "🔓 ไม่มีหลักประกัน"

    # Try to get coupon from regissue directly
    coupon = "-"
    for ck in ["CouponRate", "couponRate", "Coupon", "coupon", "InterestRate",
               "interestRate", "FixedRate", "fixedRate", "MarketYield"]:
        raw = item.get(ck)
        if raw is not None:
            c = fmt_coupon(raw)
            if c != "-":
                coupon = c
                logger.info(f"[coupon] from regissue field '{ck}': {coupon}")
                break

    return {
        "symbol":           symbol,
        "issue_id":         g("IssueID", "issueId"),
        "issuer_id":        g("IssuerID", "issuerId"),
        "term_type":        "Long Term" if term_type == "long" else "Short Term",
        "issue_date":       fmt_date(g("IssuedDate", "IssueDate")),
        "maturity_date":    fmt_date(g("MaturityDate", "maturityDate")),
        "tenor":            g("Term", "term", "tenor"),
        "coupon_rate":      coupon,
        "issue_size":       fmt_number(g("IssueSize", "issueSize")),
        "outstanding_size": fmt_number(g("CurrentOutstanding", "IssueOutstanding")),
        "secured_type":     secure_code,
        "secured_label":    secured_label,
        "registrar":        g("Registrar", "registrar"),
        "bondholder_rep":   g("BondholderRepresentative"),
        "underwriters":     clean_uw(g("Underwriter", "underwriter")),
        "issue_rating":     g("IssueRating", "issueRating"),
        "issuer_rating":    g("CompanyRating", "issuerRating"),
        "distribution":     g("DistributionDisplay", "distribution"),
        "isin":             g("IssueLegacyID", "isinCode"),
    }


# ─── STEP 2: Try list2 API (might have coupon) ────────────────────────────────

def try_list2(abbr_name, issuer_id, session):
    """ลอง list2 API ที่เห็นใน Network tab"""
    ref = f"{ISSUER_URL}?issuer={abbr_name.lower()}"
    urls = [
        f"{BASE}/issuer/list2?abbrName={abbr_name}&startYear=2020&specialOnly=true",
        f"{BASE}/issuer/list2?issuerId={issuer_id}&startYear=2020&specialOnly=true",
        f"{BASE}/issuer/list2?startYear=2020&specialOnly=true&abbrName={abbr_name}",
    ]
    for url in urls:
        data = api_get(url, session, ref)
        if data:
            logger.info(f"[list2] SUCCESS {url}: {str(data)[:300]}")
            return data
        logger.info(f"[list2] fail: {url}")
    return None


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name):
    session = requests.Session()
    abbr = company_name.strip().upper()
    logger.info(f"[main] === Searching: '{abbr}' ===")

    bonds = fetch_bond_list(abbr, session)
    if not bonds:
        return []

    # ลอง list2 API ถ้า coupon ยังไม่ได้
    if bonds and bonds[0].get("coupon_rate", "-") == "-":
        issuer_id = bonds[0].get("issuer_id", "-")
        try_list2(abbr, issuer_id, session)

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
