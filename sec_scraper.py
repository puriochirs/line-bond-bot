"""
SEC Filing Scraper — ใช้ GetViewFiling ค้นหาด้วยชื่อบริษัทโดยตรง
"""
import requests
import logging
import re
from datetime import date
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SEC_BASE    = "https://market.sec.or.th/public/idisc"
FILING_URL  = f"{SEC_BASE}/api/product/GetViewFiling"

SEC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "th,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": "https://market.sec.or.th/public/idisc/th/ViewMore/filing-debt",
    "X-Requested-With": "XMLHttpRequest",
}


def thai_to_date(s: str):
    if not s or s.strip() in ["-", "", "\xa0"]:
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s.strip())
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
    return d.strftime("%-d %b %Y") if d else "-"


def shorten_bond_name(name: str) -> str:
    name = name.strip().strip('"')
    m = re.search(r'(ครั้งที่\s*\d+/\d+.*?)$', name)
    if m:
        return m.group(1)[:60]
    return name[:60]


def search_sec_offerings(abbr: str) -> list:
    """ค้นหา bond offerings จาก SEC โดยใช้ SearchSymbol"""
    session = requests.Session()

    # ลองค้นด้วย SearchSymbol ก่อน (ชื่อย่อ เช่น CI, SIRI)
    for search_field, search_val in [
        ("SearchSymbol", abbr.upper()),
        ("SearchCompany", abbr.upper()),
    ]:
        payload = {
            "UniqueIdReference": "",
            "SearchCompany": search_val if search_field == "SearchCompany" else "",
            "SearchSymbol":  search_val if search_field == "SearchSymbol"  else "",
            "SecuTypeCode": "PL",      # PL = หุ้นกู้ที่ขออนุญาต
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
            resp = session.post(FILING_URL, json=payload, headers=SEC_HEADERS, timeout=20)
            logger.info(f"[sec] {search_field}={search_val}: status={resp.status_code}, len={len(resp.text)}")

            if resp.status_code != 200:
                continue

            data = resp.json()
            content = data.get("content", "") or ""
            logger.info(f"[sec] content len={len(content)}")

            if not content or len(content) < 100:
                continue

            results = parse_debenture_table(content)
            logger.info(f"[sec] found {len(results)} active/upcoming offerings")
            if results:
                return results

        except Exception as e:
            logger.warning(f"[sec] {search_field} error: {e}")

    return []


def parse_debenture_table(html: str) -> list:
    soup   = BeautifulSoup(html, "lxml")
    today  = date.today()
    results = []

    # หาตาราง ตราสารหนี้
    table = soup.find("table", {"id": "gPP02T03"})
    if not table:
        for heading in soup.find_all(class_="card-heading"):
            if "ตราสารหนี้" in heading.get_text():
                card = heading.find_parent(class_="card")
                if card:
                    table = card.find("table")
                    break

    if not table:
        logger.info("[sec] no debenture table in HTML")
        return []

    rows = table.find_all("tr")[1:]
    logger.info(f"[sec] rows={len(rows)}")

    for row in rows:
        cols = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cols) < 8:
            continue

        dist_type = cols[2] if len(cols) > 2 else "-"
        bond_name = cols[3] if len(cols) > 3 else "-"
        amount    = cols[4] if len(cols) > 4 else "-"
        start_raw = cols[6] if len(cols) > 6 else ""
        end_raw   = cols[7] if len(cols) > 7 else ""
        condition = cols[8] if len(cols) > 8 else ""

        start_date = thai_to_date(start_raw)
        end_date   = thai_to_date(end_raw)

        # กรองที่หมดอายุแล้วออก
        if end_date and end_date < today:
            continue

        status = "upcoming" if (start_date and start_date > today) else "active"

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


def format_sec_section(offerings: list) -> str:
    if not offerings:
        return ""

    lines = ["\n🏛 หุ้นกู้ที่กำลัง/จะเสนอขาย (SEC)", "─" * 28]
    for o in offerings:
        icon  = "🟢" if o["status"] == "active" else "🔜"
        label = "กำลังขายอยู่" if o["status"] == "active" else "เร็วๆ นี้"
        cond  = f"\n  ⚠️ {o['condition']}" if o["condition"] else ""
        lines.extend([
            f"\n{icon} {label}",
            f"  📄 {o['bond_name']}",
            f"  💰 วงเงิน: {o['amount_mln']} ลบ.",
            f"  📢 เสนอขายให้: {o['dist_type']}",
            f"  📅 {o['start_date']} → {o['end_date']}{cond}",
        ])

    return "\n".join(lines)
