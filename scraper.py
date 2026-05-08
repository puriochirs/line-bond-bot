import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "th,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.thaibma.or.th/",
}

BASE_URL = "https://www.thaibma.or.th"

def search_bonds_by_company(company_name: str) -> list[dict]:
    """
    Search for bonds issued by a given company name from ThaiBMA.
    Returns a list of bond records.
    """
    try:
        # ThaiBMA bond search endpoint (corporate bonds)
        search_url = f"{BASE_URL}/EN/Market/Primary/CorporateBond.aspx"
        
        session = requests.Session()
        
        # First, get the page to obtain ASP.NET viewstate
        resp = session.get(search_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        viewstate = soup.find("input", {"name": "__VIEWSTATE"})
        eventvalidation = soup.find("input", {"name": "__EVENTVALIDATION"})
        viewstategenerator = soup.find("input", {"name": "__VIEWSTATEGENERATOR"})

        payload = {
            "__VIEWSTATE": viewstate["value"] if viewstate else "",
            "__EVENTVALIDATION": eventvalidation["value"] if eventvalidation else "",
            "__VIEWSTATEGENERATOR": viewstategenerator["value"] if viewstategenerator else "",
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "ctl00$ContentPlaceHolder1$txtIssuerName": company_name,
            "ctl00$ContentPlaceHolder1$btnSearch": "Search",
        }

        resp2 = session.post(search_url, headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}, data=payload, timeout=20)
        resp2.raise_for_status()

        return parse_bond_results(resp2.text, company_name)

    except Exception as e:
        print(f"[scraper] Error: {e}")
        # Fallback: try alternative endpoint
        return search_via_bond_info(company_name)


def search_via_bond_info(company_name: str) -> list[dict]:
    """Alternative search using bond info page."""
    try:
        url = f"{BASE_URL}/EN/Market/Primary/CorporateBond.aspx"
        session = requests.Session()
        resp = session.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try to find search form inputs
        form = soup.find("form")
        if not form:
            return []

        inputs = {}
        for inp in form.find_all("input"):
            name = inp.get("name", "")
            val = inp.get("value", "")
            if name:
                inputs[name] = val

        # Find the company name input field
        for key in inputs:
            if "issuer" in key.lower() or "company" in key.lower() or "name" in key.lower():
                inputs[key] = company_name
                break

        # Find search button
        for inp in form.find_all("input", {"type": "submit"}):
            inputs[inp.get("name", "btnSearch")] = inp.get("value", "Search")

        resp2 = session.post(url, data=inputs, headers=HEADERS, timeout=20)
        return parse_bond_results(resp2.text, company_name)

    except Exception as e:
        print(f"[scraper fallback] Error: {e}")
        return []


def parse_bond_results(html: str, company_name: str) -> list[dict]:
    """Parse HTML table from ThaiBMA search results into bond records."""
    soup = BeautifulSoup(html, "html.parser")
    bonds = []

    # Find all tables that could contain bond data
    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Try to detect header row
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        if not any(keyword in " ".join(headers).lower() for keyword in ["symbol", "maturity", "coupon", "issuer", "isin", "rate", "หุ้นกู้"]):
            continue

        col_map = map_columns(headers)

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if not cells or len(cells) < 3:
                continue

            bond = extract_bond(cells, col_map, headers)
            if bond:
                bonds.append(bond)

    # Filter by company name if possible
    if company_name and bonds:
        filtered = [b for b in bonds if company_name.upper() in (b.get("issuer", "") + b.get("symbol", "")).upper()]
        if filtered:
            return filtered

    return bonds


def map_columns(headers: list[str]) -> dict:
    """Map column indices to bond field names."""
    col_map = {}
    for i, h in enumerate(headers):
        h_lower = h.lower()
        if any(k in h_lower for k in ["symbol", "series", "รุ่น"]):
            col_map["symbol"] = i
        elif any(k in h_lower for k in ["issuer", "company", "บริษัท"]):
            col_map["issuer"] = i
        elif any(k in h_lower for k in ["issue date", "วันที่ออก"]):
            col_map["issue_date"] = i
        elif any(k in h_lower for k in ["maturity", "ครบกำหนด", "due date"]):
            col_map["maturity_date"] = i
        elif any(k in h_lower for k in ["coupon", "rate", "อัตรา", "ดอกเบี้ย"]):
            col_map["coupon_rate"] = i
        elif any(k in h_lower for k in ["tenor", "term", "อายุ"]):
            col_map["tenor"] = i
        elif any(k in h_lower for k in ["secured", "collateral", "ประกัน"]):
            col_map["secured"] = i
        elif any(k in h_lower for k in ["underwriter", "ผู้จัดจำหน่าย", "arranger"]):
            col_map["underwriter"] = i
        elif any(k in h_lower for k in ["isin"]):
            col_map["isin"] = i
    return col_map


def extract_bond(cells: list[str], col_map: dict, headers: list[str]) -> dict | None:
    """Extract a bond dict from a table row."""
    if all(c == "" for c in cells):
        return None

    bond = {}
    for field, idx in col_map.items():
        if idx < len(cells):
            bond[field] = cells[idx]

    # Fallback: assign by position if col_map is empty
    if not bond and len(cells) >= 4:
        bond = {
            "symbol": cells[0] if len(cells) > 0 else "-",
            "issuer": cells[1] if len(cells) > 1 else "-",
            "issue_date": cells[2] if len(cells) > 2 else "-",
            "maturity_date": cells[3] if len(cells) > 3 else "-",
            "coupon_rate": cells[4] if len(cells) > 4 else "-",
        }

    return bond if bond else None


def compute_tenor(issue_date_str: str, maturity_date_str: str) -> str:
    """Compute tenor in years from issue and maturity dates."""
    try:
        for fmt in ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%B %d, %Y"]:
            try:
                issue = datetime.strptime(issue_date_str.strip(), fmt)
                maturity = datetime.strptime(maturity_date_str.strip(), fmt)
                years = (maturity - issue).days / 365.25
                return f"{years:.1f} ปี"
            except ValueError:
                continue
    except Exception:
        pass
    return "-"


def format_bond_message(bonds: list[dict], company_name: str) -> str:
    """Format bond list into a Line message string."""
    if not bonds:
        return (
            f"❌ ไม่พบข้อมูลหุ้นกู้ของ \"{company_name}\"\n\n"
            "กรุณาตรวจสอบชื่อบริษัทและลองใหม่อีกครั้ง\n"
            "หรือลองพิมพ์ชื่อย่อ เช่น PTT, CPALL, TRUE"
        )

    lines = [f"📋 หุ้นกู้ของ {company_name.upper()}", f"พบทั้งหมด {len(bonds)} รุ่น", "─" * 30]

    for i, b in enumerate(bonds, 1):
        symbol = b.get("symbol") or b.get("isin") or f"รุ่นที่ {i}"
        issue_date = b.get("issue_date", "-")
        maturity = b.get("maturity_date", "-")
        coupon = b.get("coupon_rate", "-")
        tenor = b.get("tenor") or compute_tenor(issue_date, maturity)
        secured = b.get("secured", "-")
        underwriter = b.get("underwriter", "-")

        # Secured label
        if secured and secured != "-":
            secured_label = "🔒 มีหลักประกัน" if any(w in secured.lower() for w in ["secure", "collateral", "มีหลัก"]) else "🔓 ไม่มีหลักประกัน"
        else:
            secured_label = "🔓 ไม่มีหลักประกัน"

        lines.append(
            f"\n🔹 {symbol}\n"
            f"  📅 ออก: {issue_date}\n"
            f"  📅 ครบกำหนด: {maturity}\n"
            f"  💰 อัตราดอกเบี้ย: {coupon}%\n"
            f"  ⏳ อายุ: {tenor}\n"
            f"  {secured_label}\n"
            f"  🏦 ผู้จัดจำหน่าย: {underwriter}"
        )

    lines.append("\n─" * 30)
    lines.append("📌 ข้อมูลจาก ThaiBMA (www.thaibma.or.th)")
    return "\n".join(lines)
