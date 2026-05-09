"""
SEC Filing Scraper — market.sec.or.th
ค้นหา bond offerings ที่กำลังขาย/จะขาย + ดึง coupon rate จาก PDF Fact Sheet
"""
import re
import logging
import requests
from datetime import date
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SEC_BASE   = "https://market.sec.or.th/public/idisc"
FILING_URL = f"{SEC_BASE}/api/product/GetViewFiling"

SEC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "th,en;q=0.9",
    "Content-Type": "application/json",
    "Referer": "https://market.sec.or.th/public/idisc/th/ViewMore/filing-debt",
    "X-Requested-With": "XMLHttpRequest",
}

HTML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,*/*",
    "Accept-Language": "th,en;q=0.9",
}


# ─── Date helpers ─────────────────────────────────────────────────────────────

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
    return m.group(1)[:60] if m else name[:60]


# ─── (PDF parsing removed — ใช้ link แทน) ────────────────────────────────────


# ─── SEC API ──────────────────────────────────────────────────────────────────

def _call_filing_api(search_symbol: str, search_company: str,
                     session: requests.Session) -> tuple:
    """Return (html_content, soup_with_links)"""
    payload = {
        "UniqueIdReference": "",
        "SearchCompany":     search_company,
        "SearchSymbol":      search_symbol,
        "SecuTypeCode":      "ALL",
        "OfferType":         None,
        "OfferTypeCode":     None,
        "ProjType":          None,
        "ProjRetailType":    None,
        "MarketTypeCode":    "",
        "OfferDateFrom": "", "OfferDateto": "",
        "EfftDateFrom":  "", "EfftDateTo":  "",
        "DbenEfftDateFrom": "", "DbenEfftDateTo": "",
        "DbenFlag": "", "CountDateFrom": "", "CountDateTo": "",
        "FundAbbrName": "", "FilingData": 0,
        "PolicyCode": None, "SpecCode": None,
        "Gain": None, "InvestCountryFlag": None,
        "Lang": "th",
    }
    resp = session.post(FILING_URL, json=payload, headers=SEC_HEADERS, timeout=25)
    logger.info(f"[sec] sym={search_symbol!r} co={search_company!r}: "
                f"status={resp.status_code}, len={len(resp.text)}")
    if resp.status_code != 200:
        return "", None
    data    = resp.json()
    content = data.get("content", "") or ""
    logger.info(f"[sec] content len={len(content)}")
    return content, BeautifulSoup(content, "lxml") if content else None


def search_sec_offerings(abbr: str) -> list:
    session = requests.Session()
    abbr_up = abbr.strip().upper()

    content, soup = None, None
    for sym, comp in [(abbr_up, ""), ("", abbr_up)]:
        try:
            content, soup = _call_filing_api(sym, comp, session)
            if content and len(content) > 100:
                break
        except Exception as e:
            logger.warning(f"[sec] api error sym={sym!r}: {e}")

    if not content or not soup:
        return []

    return parse_debenture_table(content, soup, session)


def parse_debenture_table(html: str, soup: BeautifulSoup, session=None) -> list:
    today   = date.today()
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
        logger.info("[sec] no debenture table")
        return []

    rows = table.find_all("tr")[1:]
    logger.info(f"[sec] rows={len(rows)}")

    for row in rows:
        tds       = row.find_all("td")
        cols      = [c.get_text(strip=True) for c in tds]
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

        logger.info(f"[sec] row: start={start_date} end={end_date} today={today}")

        if end_date and end_date < today:
            continue

        status = "upcoming" if (start_date and start_date > today) else "active"

        # ดึง filing link จากแถวนั้น
        filing_link = ""
        last_td = tds[-1] if tds else None
        if last_td:
            a = last_td.find("a", href=True)
            if a:
                filing_link = a["href"]
                if filing_link and not filing_link.startswith("http"):
                    filing_link = "https://market.sec.or.th" + filing_link

        results.append({
            "bond_name":   shorten_bond_name(bond_name),
            "amount_mln":  amount,
            "dist_type":   dist_type,
            "start_date":  fmt_date(start_date),
            "end_date":    fmt_date(end_date),
            "condition":   condition[:40] if condition else "",
            "filing_link": filing_link,
            "status":      status,
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
        link  = f"\n  🔗 {o['filing_link']}" if o.get("filing_link") else ""
        lines.extend([
            f"\n{icon} {label}",
            f"  📄 {o['bond_name']}",
            f"  💵 วงเงิน: {o['amount_mln']} ลบ.",
            f"  📢 เสนอขายให้: {o['dist_type']}",
            f"  📅 {o['start_date']} → {o['end_date']}{cond}{link}",
        ])

    return "\n".join(lines)
