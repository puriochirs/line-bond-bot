"""
SEC Filing Scraper — market.sec.or.th
ค้นหา bond offerings ที่กำลังขาย/จะขาย + ดึง coupon rate จาก PDF Fact Sheet
"""
import io
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


# ─── PDF coupon extraction ─────────────────────────────────────────────────────

def extract_coupon_from_pdf_url(pdf_url: str, session: requests.Session) -> str:
    """Download PDF และ parse หา coupon rate — คืน '-' ถ้าไม่เจอ"""
    try:
        logger.info(f"[pdf] downloading: {pdf_url}")
        resp = session.get(pdf_url, headers=HTML_HEADERS, timeout=60, stream=True)
        if resp.status_code != 200:
            return "-"

        # อ่านแค่ 500KB แรก (ข้อมูลส่วนใหญ่อยู่ด้านต้น)
        pdf_bytes = b""
        for chunk in resp.iter_content(chunk_size=65536):
            pdf_bytes += chunk
            if len(pdf_bytes) > 200_000:
                break

        return parse_coupon_from_pdf_bytes(pdf_bytes)

    except Exception as e:
        logger.warning(f"[pdf] download error: {e}")
        return "-"


def parse_coupon_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Parse PDF bytes หา coupon rate"""
    try:
        from pdfminer.high_level import extract_text
        text = extract_text(io.BytesIO(pdf_bytes))
    except Exception as e:
        logger.warning(f"[pdf] pdfminer error: {e}")
        return "-"

    if not text:
        return "-"

    logger.info(f"[pdf] extracted {len(text)} chars")

    # pattern ที่พบบ่อยใน Fact Sheet ไทย
    patterns = [
        r"อัตราดอกเบี้ย[^\d]*([\d]+\.[\d]+)\s*%?\s*ต่อปี",
        r"อัตราดอกเบี้ยคงที่[^\d]*([\d]+\.[\d]+)\s*%",
        r"Fixed\s*(?:Rate)?\s*[:\s]*([\d]+\.[\d]+)\s*%",
        r"Coupon\s*Rate\s*[:\s]*([\d]+\.[\d]+)\s*%",
        r"Interest\s*Rate\s*[:\s]*([\d]+\.[\d]+)\s*%",
        r"ดอกเบี้ย\s*([\d]+\.[\d]+)\s*%\s*ต่อปี",
        r"([\d]+\.[\d]+)\s*%\s*(?:per\s*annum|ต่อปี)",
    ]

    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                n = float(m.group(1))
                if 0.1 <= n <= 30:
                    logger.info(f"[pdf] coupon found: {n}%")
                    return f"{n:.2f}%".rstrip('0').rstrip('.')  + "%"
            except Exception:
                pass

    logger.info(f"[pdf] coupon not found in PDF")
    return "-"


def get_pdf_link_from_filing_page(filing_url: str, session: requests.Session) -> str:
    """เข้าหน้า filing → หา PDF link ที่เป็น Fact Sheet / Prospectus"""
    try:
        resp = session.get(filing_url, headers=HTML_HEADERS, timeout=60)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "lxml")

        # หา link ที่ชี้ไปยัง PDF
        pdf_keywords = ["fact", "prospectus", "หนังสือชี้ชวน", "term", "factsheet"]
        for a in soup.find_all("a", href=True):
            href  = a["href"]
            text  = a.get_text(strip=True).lower()
            href_l = href.lower()
            if ".pdf" in href_l or any(k in text for k in pdf_keywords) or any(k in href_l for k in pdf_keywords):
                if href.startswith("http"):
                    return href
                elif href.startswith("/"):
                    return "https://market.sec.or.th" + href
                else:
                    return href

        # Fallback: หา link ทั้งหมดที่มี .pdf
        for a in soup.find_all("a", href=True):
            if ".pdf" in a["href"].lower():
                href = a["href"]
                if href.startswith("http"):
                    return href
                elif href.startswith("/"):
                    return "https://market.sec.or.th" + href

    except Exception as e:
        logger.warning(f"[pdf] filing page error: {e}")
    return ""


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


def parse_debenture_table(html: str, soup: BeautifulSoup,
                          session: requests.Session) -> list:
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

        # ดึง filing link จากแถวนั้น → ไปหา PDF
        filing_link = ""
        coupon_rate = "-"
        last_td = tds[-1] if tds else None
        if last_td:
            a = last_td.find("a", href=True)
            if a:
                filing_link = a["href"]
                if not filing_link.startswith("http"):
                    filing_link = "https://market.sec.or.th" + filing_link

        if filing_link:
            logger.info(f"[sec] filing link: {filing_link}")
            # ลอง parse PDF แบบ best-effort ถ้า timeout ก็ข้ามไป
            try:
                pdf_url = get_pdf_link_from_filing_page(filing_link, session)
                if pdf_url:
                    coupon_rate = extract_coupon_from_pdf_url(pdf_url, session)
                else:
                    logger.info(f"[sec] no PDF found")
            except Exception as pdf_err:
                logger.warning(f"[sec] PDF skip: {pdf_err}")

        results.append({
            "bond_name":   shorten_bond_name(bond_name),
            "amount_mln":  amount,
            "dist_type":   dist_type,
            "start_date":  fmt_date(start_date),
            "end_date":    fmt_date(end_date),
            "condition":   condition[:40] if condition else "",
            "coupon_rate": coupon_rate,
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
        cpn   = o.get("coupon_rate", "-")
        lines.extend([
            f"\n{icon} {label}",
            f"  📄 {o['bond_name']}",
            f"  💰 ดอกเบี้ย: {cpn}",
            f"  💵 วงเงิน: {o['amount_mln']} ลบ.",
            f"  📢 เสนอขายให้: {o['dist_type']}",
            f"  📅 {o['start_date']} → {o['end_date']}{cond}",
        ])

    return "\n".join(lines)
