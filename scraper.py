import requests
from bs4 import BeautifulSoup
from datetime import datetime
import logging
import re
import time

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

BASE_URL = "https://www.thaibma.or.th"
ISSUER_SEARCH_URL = f"{BASE_URL}/EN/Issuer/IssuerSearch.aspx"
ISSUER_DETAIL_BASE = f"{BASE_URL}/EN/Issuer/IssuerDetail.aspx"
BOND_DETAIL_BASE   = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"


# ── Step 1: หา issuer code จากชื่อบริษัท ──────────────────────────────────

def find_issuer_code(company_name: str, session: requests.Session) -> str | None:
    """
    ค้นหา issuer abbreviation (เช่น 'ci', 'ptt') จากชื่อหรือชื่อย่อที่ user พิมพ์
    """
    try:
        resp = session.get(ISSUER_SEARCH_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # หา ViewState fields
        fields = _get_viewstate(soup)

        # หาชื่อ input field สำหรับค้นหา
        search_field = None
        for inp in soup.find_all("input"):
            name = inp.get("name", "")
            if "issuer" in name.lower() or "search" in name.lower() or "txt" in name.lower():
                if inp.get("type", "text") in ["text", "search", ""]:
                    search_field = name
                    break

        if not search_field:
            # ลองใช้ชื่อที่คาดว่าจะใช้
            search_field = "ctl00$ContentPlaceHolder1$txtIssuerSearch"

        fields[search_field] = company_name

        # หาปุ่ม Submit
        for inp in soup.find_all("input", {"type": "submit"}):
            fields[inp["name"]] = inp.get("value", "Search")
            break

        resp2 = session.post(
            ISSUER_SEARCH_URL,
            data=fields,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": ISSUER_SEARCH_URL},
            timeout=25,
        )
        resp2.raise_for_status()
        soup2 = BeautifulSoup(resp2.text, "lxml")

        # หา link ที่ไปยัง IssuerDetail?issuer=xxx
        for a in soup2.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"[Ii]ssuer[Dd]etail\.aspx\?issuer=([^&\"']+)", href)
            if m:
                code = m.group(1).strip().lower()
                logger.info(f"[find_issuer] found code: {code} for '{company_name}'")
                return code

        # ถ้าหาไม่เจอ ลองใช้ชื่อย่อโดยตรง
        logger.info(f"[find_issuer] not found via search, trying direct: {company_name.lower()}")
        return company_name.lower().strip()

    except Exception as e:
        logger.exception(f"[find_issuer] error: {e}")
        return company_name.lower().strip()


# ── Step 2: ดึงรายการหุ้นกู้จาก Issuer Detail page ──────────────────────────

def get_bond_list(issuer_code: str, session: requests.Session) -> list[dict]:
    """
    ดึงรายการหุ้นกู้จากหน้า IssuerDetail แบบ Current Bond
    คืนค่า list ของ {symbol, detail_url}
    """
    url = f"{ISSUER_DETAIL_BASE}?issuer={issuer_code}"
    bonds = []

    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # ตรวจว่าเจอหน้าบริษัทจริงๆ
        title = soup.find("h1") or soup.find("h2") or soup.find("h3")
        logger.info(f"[get_bond_list] page title: {title.get_text(strip=True) if title else 'N/A'}")

        # หา link ที่ชี้ไปยัง BondFeature/Issue.aspx
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "BondFeature" in href or "Issue.aspx" in href:
                full_url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
                if full_url not in seen:
                    seen.add(full_url)
                    symbol_text = a.get_text(strip=True)
                    bonds.append({"symbol": symbol_text, "detail_url": full_url})

        logger.info(f"[get_bond_list] found {len(bonds)} bond links on issuer page")

        # ถ้าหน้า Current Bond โหลด via AJAX อาจไม่เจอ link
        # ลองหาจาก table ทุกอันในหน้านั้น
        if not bonds:
            for table in soup.find_all("table"):
                for row in table.find_all("tr")[1:]:
                    cells = row.find_all("td")
                    link = row.find("a", href=True)
                    if link and ("Issue" in link.get("href", "") or "Bond" in link.get("href", "")):
                        href = link["href"]
                        full_url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
                        bonds.append({"symbol": link.get_text(strip=True), "detail_url": full_url})

    except Exception as e:
        logger.exception(f"[get_bond_list] error: {e}")

    return bonds


# ── Step 3: ดึงรายละเอียดจากหน้าหุ้นกู้แต่ละตัว ────────────────────────────

def get_bond_detail(bond_url: str, session: requests.Session) -> dict:
    """
    ดึงข้อมูลละเอียดจากหน้า BondFeature/Issue.aspx?symbol=xxx
    """
    detail = {}
    try:
        resp = session.get(bond_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # ดึงข้อมูลจาก definition list หรือ table ที่มี label:value
        # หน้านี้มักเป็น table 2 column (label | value)
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True).lower()
                    value = cells[1].get_text(" ", strip=True)

                    if "symbol" in label:
                        detail["symbol"] = value.split()[0] if value else "-"
                    elif "issue date" in label:
                        detail["issue_date"] = value
                    elif "maturity date" in label:
                        detail["maturity_date"] = value
                    elif "issue term" in label or "issue term / ttm" in label:
                        detail["tenor"] = value.split("/")[0].strip() if "/" in value else value
                    elif "coupon payment" in label:
                        # ดึงอัตราดอกเบี้ย fixed rate
                        detail["coupon_raw"] = value
                        rate_match = re.search(r"Fixed:\s*([\d.]+)%?", value, re.IGNORECASE)
                        if rate_match:
                            detail["coupon_rate"] = rate_match.group(1) + "%"
                        else:
                            detail["coupon_rate"] = value[:50] if value else "-"
                    elif "bond type" in label:
                        detail["bond_type"] = value
                    elif "secured" in label or "collateral" in label:
                        detail["secured"] = value[:200] if value else "-"
                    elif "registrar" in label and "co-registrar" not in label:
                        detail["registrar"] = value
                    elif "debenture holder" in label or "bondholder representative" in label:
                        detail["bondholder_rep"] = value
                    elif "underwriter" in label:
                        detail["underwriters"] = value
                    elif "financial advisor" in label:
                        detail["financial_advisor"] = value
                    elif "issue rating" in label:
                        detail["issue_rating"] = value
                    elif "issuer rating" in label and "issue rating" not in label:
                        # ดึงเฉพาะ rating value
                        rating_cells = row.find_all("td")
                        if len(rating_cells) >= 3:
                            detail["issuer_rating"] = rating_cells[2].get_text(strip=True)
                    elif "isin" in label and "local" in label:
                        detail["isin"] = value
                    elif "issue size" in label and "outstanding" not in label:
                        detail["issue_size"] = value
                    elif "outstanding size" in label:
                        detail["outstanding_size"] = value
                    elif "distribution" in label:
                        detail["distribution"] = value

        # ดึง Secured Type จาก "Bond Type" field
        if "bond_type" in detail:
            bt = detail["bond_type"].lower()
            if "secured" in bt or "fasset" in detail.get("secured", "").lower():
                detail["secured_label"] = "🔒 มีหลักประกัน (Secured)"
            elif "unsecure" in bt or "unsecure" in detail.get("secured", "").lower():
                detail["secured_label"] = "🔓 ไม่มีหลักประกัน (Unsecured)"
            else:
                detail["secured_label"] = "🔓 ไม่มีหลักประกัน"

        logger.info(f"[get_bond_detail] parsed: symbol={detail.get('symbol')}, coupon={detail.get('coupon_rate')}")

    except Exception as e:
        logger.exception(f"[get_bond_detail] error for {bond_url}: {e}")

    return detail


# ── Main Entry Point ─────────────────────────────────────────────────────────

def search_bonds_by_company(company_name: str) -> list[dict]:
    """
    Main function: ค้นหาหุ้นกู้ทั้งหมดของบริษัทพร้อมข้อมูลละเอียด
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    logger.info(f"[main] Searching: {company_name}")

    # Step 1: หา issuer code
    issuer_code = find_issuer_code(company_name, session)
    logger.info(f"[main] issuer_code: {issuer_code}")

    # Step 2: ดึงรายการหุ้นกู้ (พร้อม detail URLs)
    bond_list = get_bond_list(issuer_code, session)

    # ถ้าหน้า issuer ไม่มี bond links (เพราะโหลดด้วย JS)
    # ลอง fallback ไปหาจาก bond search page
    if not bond_list:
        logger.info(f"[main] No bonds from issuer page, trying bond search fallback")
        bond_list = search_via_bond_info_page(company_name, session)

    if not bond_list:
        logger.info(f"[main] No bonds found for: {company_name}")
        return []

    # Step 3: ดึง detail ของแต่ละหุ้นกู้ (จำกัด 15 รุ่น)
    results = []
    for b in bond_list[:15]:
        detail_url = b.get("detail_url", "")
        if not detail_url:
            continue
        time.sleep(0.3)  # หน่วงเวลาเล็กน้อยเพื่อไม่ให้ถูก block
        detail = get_bond_detail(detail_url, session)
        if detail:
            # ถ้า symbol ไม่มีจาก detail ให้ใช้จาก list
            if not detail.get("symbol"):
                detail["symbol"] = b.get("symbol", "-")
            results.append(detail)

    logger.info(f"[main] Final: {len(results)} bonds with details")
    return results


def search_via_bond_info_page(company_name: str, session: requests.Session) -> list[dict]:
    """
    Fallback: ค้นหาจากหน้า Bond Information โดยตรง แล้วดึง links
    """
    BOND_INFO_URL = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"
    bonds = []
    try:
        resp = session.get(BOND_INFO_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        fields = _get_viewstate(soup)

        # ใส่ชื่อบริษัทในทุก text input
        for inp in soup.find_all("input", {"type": "text"}):
            name = inp.get("name", "")
            if name:
                fields[name] = company_name

        for inp in soup.find_all("input", {"type": "submit"}):
            fields[inp.get("name", "search")] = inp.get("value", "Search")
            break

        resp2 = session.post(
            BOND_INFO_URL, data=fields,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=25,
        )
        soup2 = BeautifulSoup(resp2.text, "lxml")

        # หา links ไปยัง bond detail pages
        seen = set()
        for a in soup2.find_all("a", href=True):
            href = a["href"]
            if "Issue.aspx?symbol=" in href:
                full_url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
                if full_url not in seen:
                    seen.add(full_url)
                    bonds.append({"symbol": a.get_text(strip=True), "detail_url": full_url})

        logger.info(f"[bond_info_fallback] found {len(bonds)} bond links")
    except Exception as e:
        logger.exception(f"[bond_info_fallback] error: {e}")
    return bonds


def _get_viewstate(soup: BeautifulSoup) -> dict:
    fields = {}
    for name in ["__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR"]:
        el = soup.find("input", {"name": name})
        if el:
            fields[name] = el.get("value", "")
    return fields


# ── Format Message ────────────────────────────────────────────────────────────

def format_bond_message(bonds: list[dict], company_name: str) -> str:
    if not bonds:
        return (
            f"❌ ไม่พบข้อมูลหุ้นกู้ของ \"{company_name}\"\n\n"
            "💡 ลองพิมพ์ด้วย:\n"
            "  • ชื่อย่อ เช่น PTT, CPALL, CI, ASW\n"
            "  • ชื่อเต็มภาษาอังกฤษ\n\n"
            "📌 ข้อมูลจาก ThaiBMA"
        )

    lines = [
        f"📋 หุ้นกู้ของ {company_name.upper()}",
        f"พบ {len(bonds)} รุ่น (Current Bond)",
        "─" * 30,
    ]

    for b in bonds:
        symbol       = b.get("symbol", "-")
        issue_date   = b.get("issue_date", "-")
        maturity     = b.get("maturity_date", "-")
        coupon       = b.get("coupon_rate", "-")
        tenor        = b.get("tenor", "-")
        secured      = b.get("secured_label") or _secured_label(b)
        registrar    = b.get("registrar", "-")
        bh_rep       = b.get("bondholder_rep", "-")
        underwriters = b.get("underwriters", "-")
        fin_adv      = b.get("financial_advisor", "-")
        issue_rating = b.get("issue_rating", "-")
        issuer_rating= b.get("issuer_rating", "-")
        outstanding  = b.get("outstanding_size", "-")

        lines.append(
            f"\n🔹 {symbol}\n"
            f"  📅 วันที่ออก: {issue_date}\n"
            f"  📅 ครบกำหนด: {maturity}\n"
            f"  ⏳ อายุ: {tenor}\n"
            f"  💰 ดอกเบี้ย: {coupon}\n"
            f"  💵 Outstanding: {outstanding}\n"
            f"  {secured}\n"
            f"  📊 Issue Rating: {issue_rating}\n"
            f"  📊 Issuer Rating: {issuer_rating}\n"
            f"  🏦 Registrar: {registrar}\n"
            f"  👤 BH Representative: {bh_rep}\n"
            f"  📢 Underwriter(s): {underwriters}\n"
            f"  💼 Financial Advisor: {fin_adv}"
        )

    lines.append("\n" + "─" * 30)
    lines.append("📌 ข้อมูลจาก ThaiBMA (www.thaibma.or.th)")
    return "\n".join(lines)


def _secured_label(b: dict) -> str:
    secured = b.get("secured", "").lower()
    bond_type = b.get("bond_type", "").lower()
    if "unsecure" in secured or "unsecure" in bond_type:
        return "🔓 ไม่มีหลักประกัน (Unsecured)"
    if "secure" in secured or "fasset" in secured or "secure" in bond_type:
        return "🔒 มีหลักประกัน (Secured)"
    return "🔓 ไม่มีหลักประกัน"
