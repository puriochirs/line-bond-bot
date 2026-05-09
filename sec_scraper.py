"""
SEC Filing Scraper — market.sec.or.th
Flow:
  1. POST /api/company/valuebyuniqueid {lang:"th", content:"ABBR"} → UniqueIdReference
  2. POST /api/product/GetViewFiling {UniqueIdReference:..., ...} → HTML with bond table
  3. Parse HTML → กรองเฉพาะที่กำลังขาย/จะขาย
"""
import requests
import logging
import re
from datetime import date
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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


def thai_to_date(s: str):
    """DD/MM/YYYY พ.ศ. → date object"""
    if not s or s.strip() in ["-", "", "\xa0"]:
        return None
    s = s.strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y > 2500:
        y -= 543
    try:
        return date(y, mo, d)
    except Exception:
        return None


def fmt_date(d) -> str:
    if not d:
        return "-"
    return d.strftime("%-d %b %Y")


def shorten_bond_name(name: str) -> str:
    """ย่อชื่อหุ้นกู้ให้สั้นลง เก็บแค่ส่วนที่สำคัญ"""
    if not name:
        return "-"
    name = name.strip().strip('"')
    # ตัดชื่อบริษัทออก เก็บแค่ "ครั้งที่ X/YYYY ชุดที่ Y ..."
    m = re.search(r'(ครั้งที่\s*\d+/\d+.*?)$', name)
    if m:
        return m.group(1)[:60]
    return name[:60]


def get_unique_id(abbr: str, session: requests.Session) -> str:
    """Step 1: ได้ UniqueIdReference จากชื่อย่อบริษัท"""
    try:
        resp = session.post(
            COMPANY_URL,
            json={"lang": "th", "content": abbr.upper()},
            headers=SEC_HEADERS,
            timeout=15,
        )
        logger.info(f"[sec] valuebyuniqueid: status={resp.status_code}, len={len(resp.text)}")
        if resp.status_code != 200:
            return ""
        data = resp.json()
        logger.info(f"[sec] unique id raw type={type(data).__name__}: {str(data)[:400]}")

        # parse UniqueIdReference จาก response
        if isinstance(data, list) and data:
            first = data[0]
            uid = (first.get("UniqueIdReference") or first.get("uniqueIdReference") or
                   first.get("CompanyId") or first.get("companyId") or "")
            if uid:
                return str(uid).zfill(10)

        if isinstance(data, dict):
            uid = (data.get("UniqueIdReference") or data.get("uniqueIdReference") or
                   data.get("CompanyId") or "")
            if uid:
                return str(uid).zfill(10)

        # ลอง parse string ที่อาจมี UniqueIdReference
        text = resp.text
        m = re.search(r'"UniqueIdReference"\s*:\s*"?(\d+)"?', text, re.I)
        if m:
            return m.group(1).zfill(10)

    except Exception as e:
        logger.warning(f"[sec] get_unique_id error: {e}")
    return ""


def get_filings_html(unique_id: str, session: requests.Session) -> str:
    """Step 2: ได้ HTML content จาก GetViewFiling"""
    payload = {
        "UniqueIdReference": unique_id,
        "SearchCompany": "",
        "SearchSymbol": "",
        "SecuTypeCode": "ALL",
        "OfferType": None,
        "OfferTypeCode": None,
        "ProjType": None,
        "ProjRetailType": None,
        "MarketTypeCode": "",
        "OfferDateFrom": "",
        "OfferDateto": "",
        "EfftDateFrom": "",
        "EfftDateTo": "",
        "DbenEfftDateFrom": "",
        "DbenEfftDateTo": "",
        "DbenFlag": "",
        "CountDateFrom": "",
        "CountDateTo": "",
        "FundAbbrName": "",
        "FilingData": 0,
        "PolicyCode": None,
        "SpecCode": None,
        "Gain": None,
        "InvestCountryFlag": None,
        "Lang": "th",
    }
    try:
        resp = session.post(
            FILING_URL,
            json=payload,
            headers=SEC_HEADERS,
            timeout=20,
        )
        logger.info(f"[sec] GetViewFiling: status={resp.status_code}, len={len(resp.text)}")
        if resp.status_code != 200:
            return ""
        data = resp.json()
        content = data.get("content", "") or ""
        logger.info(f"[sec] content len={len(content)}, preview={content[:200]!r}")
        return content
    except Exception as e:
        logger.warning(f"[sec] get_filings_html error: {e}")
    return ""


def parse_debenture_table(html: str) -> list:
    """Parse ตารางตราสารหนี้จาก HTML"""
    soup = BeautifulSoup(html, "lxml")
    results = []
    today   = date.today()

    # หา section ตราสารหนี้ (table id = gPP02T03)
    table = soup.find("table", {"id": "gPP02T03"})
    if not table:
        # fallback: หาจาก heading
        for heading in soup.find_all(class_="card-heading"):
            if "ตราสารหนี้" in heading.get_text():
                card = heading.find_parent(class_="card")
                if card:
                    table = card.find("table")
                    break

    if not table:
        logger.info("[sec] no debenture table found")
        return []

    rows = table.find_all("tr")[1:]  # skip header
    logger.info(f"[sec] debenture table rows: {len(rows)}")

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 8:
            continue

        text_cols = [c.get_text(strip=True) for c in cols]

        # columns: issuer, type, dist_type, bond_name, amount, filing_eff, start, end, condition, note, filing
        dist_type  = text_cols[2] if len(text_cols) > 2 else "-"
        bond_name  = text_cols[3] if len(text_cols) > 3 else "-"
        amount     = text_cols[4] if len(text_cols) > 4 else "-"
        start_raw  = text_cols[6] if len(text_cols) > 6 else ""
        end_raw    = text_cols[7] if len(text_cols) > 7 else ""
        condition  = text_cols[8] if len(text_cols) > 8 else ""

        start_date = thai_to_date(start_raw)
        end_date   = thai_to_date(end_raw)

        # กรองเฉพาะที่ยังไม่หมดอายุ
        if end_date and end_date < today:
            continue

        if start_date and start_date > today:
            status = "upcoming"
        elif start_date and start_date <= today:
            status = "active"
        else:
            status = "unknown"

        results.append({
            "bond_name":  shorten_bond_name(bond_name),
            "amount_mln": amount,
            "dist_type":  dist_type,
            "start_date": fmt_date(start_date),
            "end_date":   fmt_date(end_date),
            "condition":  condition[:40] if condition else "",
            "status":     status,
        })

    return results


def search_sec_offerings(abbr: str) -> list:
    """ค้นหา bond offerings จาก SEC สำหรับ abbr name"""
    session = requests.Session()

    unique_id = get_unique_id(abbr, session)
    if not unique_id:
        logger.info(f"[sec] no unique_id for '{abbr}'")
        return []

    logger.info(f"[sec] unique_id for '{abbr}': {unique_id}")
    html = get_filings_html(unique_id, session)
    if not html:
        return []

    return parse_debenture_table(html)


def format_sec_section(offerings: list) -> str:
    """Format ข้อมูล SEC offering เป็นข้อความสำหรับ LINE"""
    if not offerings:
        return ""

    lines = ["\n🏛 หุ้นกู้ที่กำลัง/จะเสนอขาย (SEC)", "─" * 28]

    for o in offerings:
        icon = "🟢" if o["status"] == "active" else "🔜"
        label = "กำลังขายอยู่" if o["status"] == "active" else "เร็วๆ นี้"
        cond  = f"\n  ⚠️ {o['condition']}" if o["condition"] else ""
        lines.extend([
            f"\n{icon} {label}",
            f"  📄 {o['bond_name']}",
            f"  💰 วงเงิน: {o['amount_mln']} ลบ.",
            f"  📢 เสนอขายให้: {o['dist_type']}",
            f"  📅 เริ่มขาย: {o['start_date']}  →  สิ้นสุด: {o['end_date']}{cond}",
        ])

    return "\n".join(lines)
