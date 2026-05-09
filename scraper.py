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
        r = session.get(url, headers={**HEADERS, "Referer": ref or BOND_PAGE}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"[api_get] {url}: {e}")
        return None


def parse_coupon_from_feature(data):
    """ดึง coupon จาก feature API response"""
    if not data:
        return "-"
    logger.info(f"[feature] keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
    logger.info(f"[feature] raw: {str(data)[:300]}")

    if isinstance(data, dict):
        # หา coupon จาก field ต่างๆ
        for key in ["CouponRate", "couponRate", "Coupon", "coupon", "Rate", "rate",
                    "InterestRate", "interestRate", "CouponPayment"]:
            val = data.get(key)
            if val is not None and str(val).strip() not in ["", "null", "None", "0", "0.0"]:
                v = str(val).strip()
                try:
                    n = float(v)
                    if 0 < n <= 50:
                        if n < 1: n *= 100
                        return f"{n:.4f}".rstrip("0").rstrip(".") + "%"
                except ValueError:
                    if any(k in v.upper() for k in ["FRN", "FLOAT", "MLR", "MOR", "TBR", "FIXED"]):
                        return v[:40]
    return "-"


def clean_uw(val):
    """กรอง garbage values ออกจาก Underwriter"""
    if not val or val in ["-", ""]:
        return "-"
    blacklist = ["financial advisor", "remark", "note", "หมายเหตุ",
                 "n/a", "none", "null", "-"]
    if any(b in val.lower() for b in blacklist):
        return "-"
    return val


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

    # เก็บ IssueID (UUID) สำหรับเรียก feature API
    issue_id = g("IssueID", "issueId", "id", "Id")

    return {
        "symbol":           symbol,
        "issue_id":         issue_id,
        "term_type":        "Long Term" if term_type == "long" else "Short Term",
        "issue_date":       fmt_date(g("IssuedDate", "IssueDate")),
        "maturity_date":    fmt_date(g("MaturityDate", "maturityDate")),
        "tenor":            g("Term", "term", "tenor"),
        "coupon_rate":      "-",
        "issue_size":       fmt_number(g("IssueSize", "issueSize")),
        "outstanding_size": fmt_number(g("CurrentOutstanding", "IssueOutstanding")),
        "secured_type":     secure_code,
        "secured_label":    secured_label,
        # ดึงจาก regissue API ตรงๆ (มีอยู่แล้ว)
        "registrar":        g("Registrar", "registrar"),
        "bondholder_rep":   g("BondholderRepresentative"),
        "underwriters":     clean_uw(g("Underwriter", "underwriter")),
        "issue_rating":     g("IssueRating", "issueRating"),
        "issuer_rating":    g("CompanyRating", "issuerRating"),
        "distribution":     g("DistributionDisplay", "distribution"),
        "isin":             g("IssueLegacyID", "isinCode"),
    }


# ─── STEP 2: Feature API (public — ใช้ UUID) ─────────────────────────────────

def fetch_feature(issue_id, symbol, session):
    """ลอง feature API ด้วย UUID เพื่อดึง coupon rate"""
    if not issue_id or issue_id == "-":
        return {}

    # ลอง feature API ด้วย UUID (เห็นใน Network tab ว่าใช้ UUID uppercase)
    uuid_upper = issue_id.upper()
    urls_to_try = [
        f"{BASE}/issue/feature?Symbol={uuid_upper}",
        f"{BASE}/issue/feature?Symbol={issue_id}",
        f"{BASE}/bondinfo/feature?Symbol={uuid_upper}",
        f"{BASE}/EN/BondInfo/feature?Symbol={uuid_upper}",
    ]

    for url in urls_to_try:
        data = api_get(url, session, BOND_PAGE)
        if data:
            logger.info(f"[feature] success: {url}")
            coupon = parse_coupon_from_feature(data)
            if coupon != "-":
                return {"coupon_rate": coupon}

    logger.info(f"[feature] all URLs failed for {issue_id}")
    return {}


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name):
    session = requests.Session()
    abbr = company_name.strip().upper()
    logger.info(f"[main] === Searching: '{abbr}' ===")

    bonds = fetch_bond_list(abbr, session)
    if not bonds:
        return []

    results = []
    for b in bonds[:15]:
        issue_id = b.get("issue_id", "-")
        symbol   = b.get("symbol", "")

        # ลองดึง coupon จาก feature API
        if b.get("coupon_rate", "-") == "-" and issue_id != "-":
            time.sleep(0.3)
            feature_detail = fetch_feature(issue_id, symbol, session)
            for k, v in feature_detail.items():
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
