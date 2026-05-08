import requests
from bs4 import BeautifulSoup
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

BASE_URL = "https://www.thaibma.or.th"
ISSUER_SEARCH_URL = f"{BASE_URL}/EN/Issuer/IssuerSearch.aspx"
BOND_INFO_URL = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"


def get_viewstate(session, url):
    """Fetch a page and extract ASP.NET form fields."""
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")
    fields = {}
    for name in ["__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED"]:
        el = soup.find("input", {"name": name})
        if el:
            fields[name] = el.get("value", "")
    return fields, soup


def search_issuer(company_name: str) -> list[dict]:
    """Search ThaiBMA Issuer Search page for the company."""
    session = requests.Session()
    try:
        fields, soup = get_viewstate(session, ISSUER_SEARCH_URL)

        # Find the search input name
        search_input = None
        for inp in soup.find_all("input", {"type": ["text", "search"]}):
            name = inp.get("name", "")
            if any(k in name.lower() for k in ["issuer", "company", "name", "search", "txt"]):
                search_input = name
                break

        if not search_input:
            # Try to find any text input
            for inp in soup.find_all("input", {"type": "text"}):
                search_input = inp.get("name", "")
                if search_input:
                    break

        logger.info(f"[issuer_search] input field: {search_input}")

        # Find submit button
        submit_btn = None
        for inp in soup.find_all("input", {"type": "submit"}):
            submit_btn = inp.get("name", "")
            break
        if not submit_btn:
            for btn in soup.find_all("button", {"type": "submit"}):
                submit_btn = btn.get("name", "")
                break

        payload = {**fields}
        if search_input:
            payload[search_input] = company_name
        if submit_btn:
            payload[submit_btn] = "Search"

        resp2 = session.post(
            ISSUER_SEARCH_URL,
            data=payload,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Referer": ISSUER_SEARCH_URL},
            timeout=25,
        )
        resp2.raise_for_status()

        return parse_issuer_results(resp2.text, company_name)

    except Exception as e:
        logger.exception(f"[issuer_search] error: {e}")
        return []


def search_bonds_by_issuer_code(issuer_code: str, session: requests.Session) -> list[dict]:
    """After finding issuer code, search for their bonds."""
    try:
        fields, soup = get_viewstate(session, BOND_INFO_URL)

        # Look for issuer dropdown or input
        payload = {**fields}

        # Try to set issuer code
        for select in soup.find_all("select"):
            name = select.get("name", "")
            if any(k in name.lower() for k in ["issuer", "company"]):
                payload[name] = issuer_code
                break

        # Find search button
        for inp in soup.find_all("input", {"type": "submit"}):
            payload[inp.get("name", "")] = inp.get("value", "Search")
            break

        resp = session.post(
            BOND_INFO_URL,
            data=payload,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Referer": BOND_INFO_URL},
            timeout=25,
        )
        return parse_bond_table(resp.text)
    except Exception as e:
        logger.exception(f"[bond_by_issuer] error: {e}")
        return []


def parse_issuer_results(html: str, company_name: str) -> list[dict]:
    """Parse issuer search results and find matching company links."""
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Look for table rows with company names
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows[1:]:  # skip header
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            row_text = " ".join(c.get_text(strip=True) for c in cells)
            if company_name.upper() in row_text.upper():
                # Find link to company detail
                link = row.find("a")
                href = link["href"] if link and link.get("href") else ""
                results.append({
                    "name": cells[0].get_text(strip=True) if cells else "",
                    "href": href,
                    "row_text": row_text,
                })

    logger.info(f"[parse_issuer] found {len(results)} matches for '{company_name}'")
    return results


def parse_bond_table(html: str) -> list[dict]:
    """Parse bond data from HTML table."""
    soup = BeautifulSoup(html, "lxml")
    bonds = []

    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        header_text = " ".join(headers).lower()

        # Check if this looks like a bond table
        if not any(k in header_text for k in ["symbol", "maturity", "coupon", "rate", "issue", "isin", "series"]):
            continue

        col_map = map_columns(headers)
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if not cells or len(cells) < 3 or all(c == "" for c in cells):
                continue
            bond = {}
            for field, idx in col_map.items():
                if idx < len(cells):
                    bond[field] = cells[idx]
            # Fallback positional assignment
            if not bond:
                bond = {
                    "symbol": cells[0] if len(cells) > 0 else "-",
                    "issue_date": cells[1] if len(cells) > 1 else "-",
                    "maturity_date": cells[2] if len(cells) > 2 else "-",
                    "coupon_rate": cells[3] if len(cells) > 3 else "-",
                    "tenor": cells[4] if len(cells) > 4 else "-",
                    "secured": cells[5] if len(cells) > 5 else "-",
                    "underwriter": cells[6] if len(cells) > 6 else "-",
                }
            bonds.append(bond)

    return bonds


def map_columns(headers: list[str]) -> dict:
    col_map = {}
    for i, h in enumerate(headers):
        h_lower = h.lower()
        if any(k in h_lower for k in ["symbol", "series", "bond"]):
            col_map.setdefault("symbol", i)
        if any(k in h_lower for k in ["isin"]):
            col_map["isin"] = i
        if any(k in h_lower for k in ["issue date", "issue_date", "issued"]):
            col_map["issue_date"] = i
        if any(k in h_lower for k in ["maturity", "due", "redemption", "expire"]):
            col_map["maturity_date"] = i
        if any(k in h_lower for k in ["coupon", "rate", "interest"]):
            col_map["coupon_rate"] = i
        if any(k in h_lower for k in ["tenor", "term", "year"]):
            col_map["tenor"] = i
        if any(k in h_lower for k in ["secured", "collateral", "security"]):
            col_map["secured"] = i
        if any(k in h_lower for k in ["underwriter", "lead", "arranger"]):
            col_map["underwriter"] = i
    return col_map


def compute_tenor(issue_str: str, maturity_str: str) -> str:
    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%m/%d/%Y"]:
        try:
            issue = datetime.strptime(issue_str.strip(), fmt)
            maturity = datetime.strptime(maturity_str.strip(), fmt)
            years = (maturity - issue).days / 365.25
            return f"{years:.1f} ปี"
        except ValueError:
            continue
    return "-"


def search_bonds_by_company(company_name: str) -> list[dict]:
    """Main entry point: search bonds for a company name."""
    session = requests.Session()

    # Strategy 1: Search via Issuer Search page
    logger.info(f"[search] Trying issuer search for: {company_name}")
    issuer_results = search_issuer(company_name)

    if issuer_results:
        # Try to get bonds for first matching issuer
        first = issuer_results[0]
        href = first.get("href", "")
        if href:
            full_url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
            try:
                resp = session.get(full_url, headers=HEADERS, timeout=20)
                bonds = parse_bond_table(resp.text)
                if bonds:
                    logger.info(f"[search] Found {len(bonds)} bonds via issuer link")
                    return bonds
            except Exception as e:
                logger.exception(f"[search] issuer link error: {e}")

    # Strategy 2: Bond Information page with company name
    logger.info(f"[search] Trying bond info page direct search")
    try:
        fields, soup = get_viewstate(session, BOND_INFO_URL)
        payload = {**fields}

        # Find any text input and set company name
        for inp in soup.find_all("input", {"type": "text"}):
            name = inp.get("name", "")
            if name:
                payload[name] = company_name
                break

        # Submit
        for inp in soup.find_all("input", {"type": "submit"}):
            payload[inp.get("name", "")] = inp.get("value", "Search")
            break

        resp = session.post(
            BOND_INFO_URL,
            data=payload,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=25,
        )
        bonds = parse_bond_table(resp.text)
        if bonds:
            logger.info(f"[search] Found {len(bonds)} bonds via bond info page")
            return bonds

    except Exception as e:
        logger.exception(f"[search] bond info page error: {e}")

    logger.info(f"[search] No bonds found for: {company_name}")
    return []


def format_bond_message(bonds: list[dict], company_name: str) -> str:
    if not bonds:
        return (
            f"❌ ไม่พบข้อมูลหุ้นกู้ของ \"{company_name}\"\n\n"
            "💡 ลองพิมพ์ใหม่ด้วย:\n"
            "  • ชื่อย่อภาษาอังกฤษ เช่น PTT, CPALL, TRUE\n"
            "  • ชื่อเต็ม เช่น BANGKOK EXPRESSWAY\n"
            "  • ชื่อภาษาไทย เช่น ปตท\n\n"
            "📌 ข้อมูลจาก ThaiBMA (www.thaibma.or.th)"
        )

    lines = [
        f"📋 หุ้นกู้ของ {company_name.upper()}",
        f"พบทั้งหมด {len(bonds)} รุ่น",
        "─" * 28,
    ]

    for i, b in enumerate(bonds[:10], 1):  # แสดงสูงสุด 10 รุ่น
        symbol = b.get("symbol") or b.get("isin") or f"รุ่นที่ {i}"
        issue_date = b.get("issue_date", "-")
        maturity = b.get("maturity_date", "-")
        coupon = b.get("coupon_rate", "-")
        tenor = b.get("tenor") or compute_tenor(issue_date, maturity)
        secured = b.get("secured", "")
        underwriter = b.get("underwriter", "-")

        if secured and any(w in secured.lower() for w in ["secure", "collateral", "guaranteed"]):
            secured_label = "🔒 มีหลักประกัน"
        else:
            secured_label = "🔓 ไม่มีหลักประกัน"

        if coupon and coupon != "-" and "%" not in coupon:
            coupon = f"{coupon}%"

        lines.append(
            f"\n🔹 {symbol}\n"
            f"  📅 ออก: {issue_date}\n"
            f"  📅 ครบกำหนด: {maturity}\n"
            f"  💰 ดอกเบี้ย: {coupon}\n"
            f"  ⏳ อายุ: {tenor}\n"
            f"  {secured_label}\n"
            f"  🏦 ผู้จัดจำหน่าย: {underwriter}"
        )

    if len(bonds) > 10:
        lines.append(f"\n... และอีก {len(bonds)-10} รุ่น")

    lines.append("\n" + "─" * 28)
    lines.append("📌 ข้อมูลจาก ThaiBMA")
    return "\n".join(lines)
