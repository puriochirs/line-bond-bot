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

BASE_URL         = "https://www.thaibma.or.th"
ISSUER_SEARCH_URL = f"{BASE_URL}/EN/Issuer/IssuerSearch.aspx"
ISSUER_DETAIL_BASE = f"{BASE_URL}/EN/Issuer/IssuerDetail.aspx"
BOND_INFO_URL    = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: หา issuer code จากชื่อบริษัท
# ─────────────────────────────────────────────────────────────────────────────

def find_issuer_code(company_name: str, session: requests.Session) -> str:
    """ค้นหา issuer abbreviation เช่น 'ci', 'ptt' จาก Issuer Search page"""
    name_clean = company_name.strip().upper()
    try:
        resp = session.get(ISSUER_SEARCH_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        fields = _get_viewstate(soup)

        # หา input field ชื่อค้นหา
        for inp in soup.find_all("input"):
            iname = inp.get("name", "")
            itype = inp.get("type", "text").lower()
            if itype in ["text", "search", ""] and ("search" in iname.lower() or "issuer" in iname.lower() or "txt" in iname.lower()):
                fields[iname] = company_name
                logger.info(f"[issuer_search] using input: {iname}")
                break

        for inp in soup.find_all("input", {"type": "submit"}):
            fields[inp["name"]] = inp.get("value", "Search")
            break

        resp2 = session.post(
            ISSUER_SEARCH_URL, data=fields,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Referer": ISSUER_SEARCH_URL},
            timeout=25,
        )
        soup2 = BeautifulSoup(resp2.text, "lxml")

        # หา link ไปยัง IssuerDetail?issuer=xxx
        for a in soup2.find_all("a", href=True):
            m = re.search(r"[Ii]ssuer[Dd]etail\.aspx\?issuer=([^&\"'\s]+)", a["href"])
            if m:
                code = m.group(1).lower().strip()
                logger.info(f"[issuer_search] found code: '{code}'")
                return code

    except Exception as e:
        logger.exception(f"[issuer_search] error: {e}")

    # fallback: ใช้ชื่อที่ user พิมพ์เป็น code โดยตรง
    return company_name.lower().strip()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: ดึงรายการหุ้นกู้จาก Issuer Detail page
# ─────────────────────────────────────────────────────────────────────────────

def get_bond_links_from_issuer_page(issuer_code: str, session: requests.Session) -> list[dict]:
    """
    ดึง link ของหุ้นกู้แต่ละตัวจากหน้า IssuerDetail
    คืนค่า list ของ {symbol, detail_url}
    """
    url = f"{ISSUER_DETAIL_BASE}?issuer={issuer_code}"
    bonds = []
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # ตรวจว่าเจอ issuer จริง
        page_text = soup.get_text()
        if "no data" in page_text.lower() and len(page_text) < 2000:
            logger.info(f"[issuer_page] Page too short or no data for '{issuer_code}'")
            return []

        # หา link ที่ไปยัง BondFeature/Issue.aspx?symbol=xxx
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "Issue.aspx?symbol=" in href:
                full_url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
                if full_url not in seen:
                    seen.add(full_url)
                    symbol = a.get_text(strip=True).split()[0] if a.get_text(strip=True) else ""
                    bonds.append({"symbol": symbol, "detail_url": full_url})

        logger.info(f"[issuer_page] Found {len(bonds)} bond links for '{issuer_code}'")

    except Exception as e:
        logger.exception(f"[issuer_page] error: {e}")

    return bonds


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2b: Fallback — ค้นหาจาก Bond Info page และ filter ด้วย symbol prefix
# ─────────────────────────────────────────────────────────────────────────────

def get_bond_links_from_search(company_name: str, session: requests.Session) -> list[dict]:
    """
    ค้นหาหุ้นกู้จาก Bond Info search page
    แล้ว filter เฉพาะ bond ที่ symbol ขึ้นต้นด้วยชื่อบริษัท
    """
    prefix = company_name.upper().strip()
    bonds = []

    try:
        resp = session.get(BOND_INFO_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        fields = _get_viewstate(soup)

        # ใส่ชื่อบริษัทใน text inputs ทุกตัว
        for inp in soup.find_all("input", {"type": "text"}):
            iname = inp.get("name", "")
            if iname:
                fields[iname] = company_name
                logger.info(f"[bond_search] setting {iname} = {company_name}")

        for inp in soup.find_all("input", {"type": "submit"}):
            fields[inp.get("name", "")] = inp.get("value", "Search")
            break

        resp2 = session.post(
            BOND_INFO_URL, data=fields,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded", "Referer": BOND_INFO_URL},
            timeout=25,
        )
        soup2 = BeautifulSoup(resp2.text, "lxml")

        # หา bond links ที่ symbol match กับ company prefix
        seen = set()
        for a in soup2.find_all("a", href=True):
            href = a["href"]
            if "Issue.aspx?symbol=" in href:
                symbol_text = a.get_text(strip=True).split()[0]
                # Filter: symbol ต้องขึ้นต้นด้วยชื่อบริษัท เช่น CI269A ขึ้นต้นด้วย CI
                if symbol_text.upper().startswith(prefix):
                    full_url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
                    if full_url not in seen:
                        seen.add(full_url)
                        bonds.append({"symbol": symbol_text, "detail_url": full_url})

        logger.info(f"[bond_search] Found {len(bonds)} bonds matching prefix '{prefix}'")

        # ถ้ายังไม่เจอ ให้ลอง parse table แล้ว filter ด้วย symbol
        if not bonds:
            bonds = _parse_table_with_prefix_filter(soup2, prefix)

    except Exception as e:
        logger.exception(f"[bond_search] error: {e}")

    return bonds


def _parse_table_with_prefix_filter(soup: BeautifulSoup, prefix: str) -> list[dict]:
    """Parse table rows และ filter เฉพาะ row ที่ symbol ขึ้นต้นด้วย prefix"""
    bonds = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        if not any(k in " ".join(headers) for k in ["symbol", "maturity", "coupon", "issue"]):
            continue
        col_map = _map_columns(headers)
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if not cells:
                continue
            sym_idx = col_map.get("symbol", 0)
            symbol = cells[sym_idx] if sym_idx < len(cells) else cells[0]
            if not symbol.upper().startswith(prefix):
                continue
            # หา detail link
            link = row.find("a", href=True)
            detail_url = ""
            if link and "Issue.aspx" in link["href"]:
                href = link["href"]
                detail_url = href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
            bond = {"symbol": symbol, "detail_url": detail_url}
            for field, idx in col_map.items():
                if idx < len(cells):
                    bond[field] = cells[idx]
            bonds.append(bond)
    logger.info(f"[table_filter] Found {len(bonds)} bonds with prefix '{prefix}'")
    return bonds


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: ดึงข้อมูลละเอียดจากหน้า Bond Detail
# ─────────────────────────────────────────────────────────────────────────────

def get_bond_detail(bond_url: str, session: requests.Session) -> dict:
    """ดึงข้อมูลละเอียดจาก BondFeature/Issue.aspx?symbol=xxx"""
    detail = {}
    if not bond_url:
        return detail
    try:
        resp = session.get(bond_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            for row in rows:
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue

                # วนจับ label-value pairs (อาจมี 2 หรือ 4 cells ต่อ row)
                pairs = []
                for i in range(0, len(cells) - 1, 2):
                    label = cells[i].get_text(strip=True).lower()
                    value = cells[i+1].get_text(" ", strip=True)
                    pairs.append((label, value))

                for label, value in pairs:
                    if not label or not value:
                        continue

                    if "symbol" in label and not detail.get("symbol"):
                        detail["symbol"] = value.split()[0]
                    elif "issue date" in label and "registration" not in label:
                        detail["issue_date"] = value
                    elif "maturity date" in label:
                        detail["maturity_date"] = value
                    elif "issue term" in label:
                        detail["tenor"] = value.split("/")[0].strip()
                    elif "coupon payment" in label:
                        rate_m = re.search(r"Fixed:\s*([\d.]+)", value, re.I)
                        detail["coupon_rate"] = (rate_m.group(1) + "%") if rate_m else value[:80]
                    elif "bond type" in label:
                        detail["bond_type"] = value
                    elif label == "secured type" or (("secured" in label or "collateral" in label) and "co-" not in label):
                        detail["secured_type"] = value
                    elif "registrar" in label and "co-registrar" not in label:
                        detail["registrar"] = value
                    elif "debenture holder" in label or "bondholder rep" in label:
                        detail["bondholder_rep"] = value
                    elif "underwriter" in label:
                        detail["underwriters"] = value
                    elif "financial advisor" in label:
                        detail["financial_advisor"] = value
                    elif "isin" in label and "local" in label:
                        detail["isin"] = value
                    elif "issue size" in label and "outstanding" not in label:
                        detail["issue_size"] = value
                    elif "outstanding size" in label:
                        detail["outstanding_size"] = value
                    elif "issue rating" in label:
                        detail["issue_rating"] = value if value.strip() else "-"
                    elif "issuer rating" in label and "issue " not in label:
                        detail["issuer_rating"] = value if value.strip() else "-"
                    elif "distribution" in label:
                        detail["distribution"] = value

        # สร้าง secured label
        st = detail.get("secured_type", "").lower()
        bt = detail.get("bond_type", "").lower()
        if "unsecure" in st or "unsecure" in bt:
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน"
        elif "secure" in st or "fasset" in st or "secure" in bt:
            detail["secured_label"] = "🔒 มีหลักประกัน"
        else:
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน"

        logger.info(f"[bond_detail] symbol={detail.get('symbol')}, coupon={detail.get('coupon_rate')}, underwriter={detail.get('underwriters','')[:40]}")

    except Exception as e:
        logger.exception(f"[bond_detail] error for {bond_url}: {e}")

    return detail


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name: str) -> list[dict]:
    session = requests.Session()
    session.headers.update(HEADERS)
    company_clean = company_name.strip()
    logger.info(f"[main] === Start search: '{company_clean}' ===")

    # Step 1: หา issuer code
    issuer_code = find_issuer_code(company_clean, session)
    logger.info(f"[main] issuer_code = '{issuer_code}'")

    # Step 2a: ดึง bond links จาก Issuer Detail page
    bond_list = get_bond_links_from_issuer_page(issuer_code, session)

    # Step 2b: fallback — ค้นหาจาก Bond Info page
    if not bond_list:
        logger.info(f"[main] No links from issuer page, trying bond search...")
        bond_list = get_bond_links_from_search(company_clean, session)

    if not bond_list:
        logger.info(f"[main] No bonds found for '{company_clean}'")
        return []

    logger.info(f"[main] Processing {len(bond_list)} bonds...")

    # Step 3: ดึง detail ของแต่ละหุ้นกู้ (จำกัด 10 รุ่น)
    results = []
    for b in bond_list[:10]:
        detail_url = b.get("detail_url", "")
        if detail_url:
            time.sleep(0.4)
            detail = get_bond_detail(detail_url, session)
        else:
            detail = {}

        # merge ข้อมูลจาก list + detail
        merged = {**b, **detail}
        if not merged.get("symbol"):
            merged["symbol"] = b.get("symbol", "-")
        results.append(merged)

    logger.info(f"[main] Done: {len(results)} bonds with details")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# FORMAT
# ─────────────────────────────────────────────────────────────────────────────

def format_bond_message(bonds: list[dict], company_name: str) -> str:
    if not bonds:
        return (
            f"❌ ไม่พบข้อมูลหุ้นกู้ของ \"{company_name}\"\n\n"
            "💡 ลองพิมพ์ใหม่:\n"
            "  • ชื่อย่อ เช่น PTT, CPALL, CI, ASW\n"
            "  • ต้องเป็นตัวอักษรภาษาอังกฤษ\n\n"
            "📌 ข้อมูลจาก ThaiBMA"
        )

    lines = [
        f"📋 หุ้นกู้ {company_name.upper()} ({len(bonds)} รุ่น)",
        "─" * 28,
    ]

    for b in bonds:
        sym    = b.get("symbol", "-")
        issue  = b.get("issue_date", "-")
        mat    = b.get("maturity_date", "-")
        tenor  = b.get("tenor", "-")
        coupon = b.get("coupon_rate", "-")
        out    = b.get("outstanding_size", "-")
        sec    = b.get("secured_label", "🔓 ไม่มีหลักประกัน")
        irat   = b.get("issue_rating", "-")
        erat   = b.get("issuer_rating", "-")
        reg    = b.get("registrar", "-")
        bh     = b.get("bondholder_rep", "-")
        uw     = b.get("underwriters", "-")
        fa     = b.get("financial_advisor", "-")

        lines += [
            f"\n🔹 {sym}",
            f"  📅 ออก: {issue}",
            f"  📅 ครบกำหนด: {mat}",
            f"  ⏳ อายุ: {tenor}",
            f"  💰 ดอกเบี้ย: {coupon}",
            f"  💵 Outstanding: {out}",
            f"  {sec}",
            f"  📊 Issue Rating: {irat}",
            f"  📊 Issuer Rating: {erat}",
            f"  🏦 Registrar: {reg}",
            f"  👤 BH Rep: {bh}",
            f"  📢 Underwriter: {uw}",
            f"  💼 FA: {fa}",
        ]

    lines += ["", "─" * 28, "📌 ข้อมูลจาก ThaiBMA"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_viewstate(soup: BeautifulSoup) -> dict:
    fields = {}
    for name in ["__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR"]:
        el = soup.find("input", {"name": name})
        if el:
            fields[name] = el.get("value", "")
    return fields


def _map_columns(headers: list[str]) -> dict:
    col_map = {}
    for i, h in enumerate(headers):
        if any(k in h for k in ["symbol", "series"]):
            col_map.setdefault("symbol", i)
        if "issue date" in h:
            col_map["issue_date"] = i
        if "maturity" in h:
            col_map["maturity_date"] = i
        if "coupon" in h or "rate" in h:
            col_map["coupon_rate"] = i
        if "term" in h and "tenor" not in col_map:
            col_map["tenor"] = i
        if "secured" in h:
            col_map["secured_type"] = i
        if "underwriter" in h:
            col_map["underwriters"] = i
        if "registrar" in h:
            col_map["registrar"] = i
    return col_map
