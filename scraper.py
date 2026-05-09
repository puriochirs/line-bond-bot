import requests
from bs4 import BeautifulSoup
import logging
import re
import time
import json
from datetime import datetime

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.thaibma.or.th/EN/Issuer/IssuerDetail.aspx",
}

BASE_URL      = "https://www.thaibma.or.th"
REGISSUE_URL  = f"{BASE_URL}/issuer/regissue"
BOND_INFO_URL = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"
ISSUER_DETAIL = f"{BASE_URL}/EN/Issuer/IssuerDetail.aspx"

# คำที่ไม่ควรอยู่ใน UWs (garbage values จาก API)
UW_BLACKLIST = ["financial advisor", "remark", "note", "หมายเหตุ", "-"]


def fmt_date(raw: str) -> str:
    if not raw or raw in ["-", "null", "None"]:
        return "-"
    raw = raw.split("T")[0].strip()
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"]:
        try:
            return datetime.strptime(raw, fmt).strftime("%-d %b %Y")
        except ValueError:
            continue
    return raw


def fmt_number(raw: str) -> str:
    if not raw or raw == "-":
        return "-"
    try:
        n = float(raw)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except Exception:
        return raw


def is_valid_uw(val: str) -> bool:
    """ตรวจสอบว่า val เป็น underwriter จริงๆ ไม่ใช่ garbage"""
    if not val or val == "-":
        return False
    v_lower = val.lower()
    return not any(b in v_lower for b in UW_BLACKLIST)


# ─── STEP 1: Bond List from JSON API ─────────────────────────────────────────

def fetch_bond_list(abbr_name: str, session: requests.Session) -> list:
    all_bonds = []
    ref = f"{ISSUER_DETAIL}?issuer={abbr_name.lower()}"
    for term in ["long", "short"]:
        url = f"{REGISSUE_URL}?abbrName={abbr_name}&term={term}"
        try:
            resp = session.get(url, headers={**HEADERS, "Referer": ref}, timeout=20)
            resp.raise_for_status()
            data = resp.json()
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
        except Exception as e:
            logger.exception(f"[api] {term}: {e}")
    logger.info(f"[api] Total: {len(all_bonds)} bonds")
    return all_bonds


def _item_to_bond(item: dict, term_type: str):
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

    # UWs จาก API — กรองก่อน ถ้า garbage ให้เป็น "-" แล้วจะดึงจาก detail page
    api_uw = g("Underwriter", "underwriter")
    uw_value = api_uw if is_valid_uw(api_uw) else "-"

    bond = {
        "symbol":           symbol,
        "term_type":        "Long Term" if term_type == "long" else "Short Term",
        "issue_date":       fmt_date(g("IssuedDate", "IssueDate", "issueDate")),
        "maturity_date":    fmt_date(g("MaturityDate", "maturityDate")),
        "tenor":            g("Term", "term", "tenor"),
        "coupon_rate":      "-",
        "issue_size":       fmt_number(g("IssueSize", "issueSize")),
        "outstanding_size": fmt_number(g("CurrentOutstanding", "IssueOutstanding", "outstanding")),
        "secured_type":     secure_code,
        "secured_label":    secured_label,
        "registrar":        g("Registrar", "registrar"),
        "bondholder_rep":   g("BondholderRepresentative", "bondholderRep"),
        "underwriters":     uw_value,
        "issue_rating":     g("IssueRating", "issueRating"),
        "issuer_rating":    g("CompanyRating", "issuerRating"),
        "distribution":     g("DistributionDisplay", "distribution"),
        "isin":             g("IssueLegacyID", "isinCode", "ISIN"),
    }

    issue_id = g("IssueID", "issueId", "id", "Id")
    if issue_id != "-":
        bond["detail_url"] = f"{BOND_INFO_URL}?symbol={issue_id}"

    return bond


# ─── STEP 2: Bond Detail ─────────────────────────────────────────────────────

def fetch_bond_detail(detail_url: str, session: requests.Session) -> dict:
    detail = {}
    if not detail_url:
        return detail
    try:
        logger.info(f"[detail] GET {detail_url}")
        resp = session.get(detail_url, headers={**HEADERS, "Accept": "text/html,*/*"}, timeout=20)
        resp.raise_for_status()
        raw_html = resp.text
        soup = BeautifulSoup(raw_html, "lxml")

        # ── 1. Coupon Rate ───────────────────────────────────────────────────
        # ดึง section หลัง "Coupon Payment" เท่านั้น เพื่อป้องกันการ match ผิด
        coupon_idx = raw_html.lower().find("coupon payment")
        if coupon_idx >= 0:
            # หา "Fixed: X.X%" ในส่วนหลัง "Coupon Payment" (400 chars)
            coupon_section = raw_html[coupon_idx: coupon_idx + 400]
            m = re.search(r"Fixed\s*:?\s*([\d]+\.?[\d]*)\s*%", coupon_section, re.I)
            if m:
                detail["coupon_rate"] = m.group(1) + "%"
                logger.info(f"[detail] coupon fixed: {detail['coupon_rate']}")
            else:
                # FRN / Floating
                m2 = re.search(r"\b(FRN)\b|(Floating\s*Rate)|(TBR|MLR|MOR)\s*[+\-]\s*[\d.]+", coupon_section, re.I)
                if m2:
                    detail["coupon_rate"] = m2.group(0).strip()
                    logger.info(f"[detail] coupon FRN: {detail['coupon_rate']}")
                else:
                    logger.info(f"[detail] coupon not found. section: {coupon_section[:100]}")
        else:
            logger.info(f"[detail] 'Coupon Payment' not found in HTML")

        # ── 2. Underwriter ───────────────────────────────────────────────────
        for table in soup.find_all("table"):
            found = False
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if not cells:
                    continue
                first_lower = cells[0].get_text(strip=True).lower()
                if "underwriter" in first_lower:
                    # เอาแค่ cell[1] = value จริงๆ
                    if len(cells) >= 2:
                        uw_val = cells[1].get_text(strip=True)
                        if is_valid_uw(uw_val):
                            detail["underwriters"] = uw_val
                            logger.info(f"[detail] UW: {uw_val[:60]}")
                    found = True
                    break
            if found:
                break

        # ── 3. Secured Label ─────────────────────────────────────────────────
        if "[ Senior ][ Unsecured ]" in raw_html:
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน"
        elif "[ Senior ][ Secured ]" in raw_html or "[ Secured ]" in raw_html:
            detail["secured_label"] = "🔒 มีหลักประกัน"

    except Exception as e:
        logger.exception(f"[detail] error: {e}")
    return detail


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name: str) -> list:
    session = requests.Session()
    abbr = company_name.strip().upper()
    logger.info(f"[main] === Searching: '{abbr}' ===")
    bonds = fetch_bond_list(abbr, session)
    if not bonds:
        return []
    results = []
    for b in bonds[:15]:
        detail_url = b.get("detail_url", "")
        if detail_url:
            time.sleep(0.3)
            detail = fetch_bond_detail(detail_url, session)
            for k, v in detail.items():
                if k not in b or b[k] == "-":
                    b[k] = v
        results.append(b)
    logger.info(f"[main] Done: {len(results)} bonds")
    return results


# ─── FORMAT ───────────────────────────────────────────────────────────────────

def format_bond_message(bonds: list, company_name: str) -> str:
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
