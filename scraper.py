import requests
from bs4 import BeautifulSoup
import logging
import re
import time

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

BASE_URL          = "https://www.thaibma.or.th"
BOND_INFO_URL     = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"
ISSUER_DETAIL_BASE = f"{BASE_URL}/EN/Issuer/IssuerDetail.aspx"


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 1: ดึงข้อมูลผ่านตาราง Issuer List บน Bond Info page
# ─────────────────────────────────────────────────────────────────────────────

def find_bonds_via_issuer_link(company_name: str, session: requests.Session) -> list[dict]:
    """
    1. GET Bond Info page → เจอ issuer list table (abbr, name eng, name th)
    2. หาแถวที่ abbreviation ตรงกับ company_name
    3. ตาม link ไปยังหน้า bond list ของบริษัทนั้น
    4. Parse bond data
    """
    prefix = company_name.strip().upper()

    try:
        resp = session.get(BOND_INFO_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # log ทุก select element เพื่อ debug
        selects = soup.find_all("select")
        logger.info(f"[v6] selects found: {len(selects)}")
        for s in selects[:3]:
            opts = [o.get_text(strip=True) for o in s.find_all("option")[:5]]
            logger.info(f"[v6] select name={s.get('name')} opts={opts}")

        # หา link ที่เกี่ยวกับ issuer จาก issuer list table
        issuer_link = None
        for table in soup.find_all("table"):
            headers_row = table.find("tr")
            if not headers_row:
                continue
            headers = [th.get_text(strip=True).lower() for th in headers_row.find_all(["th", "td"])]

            # ตาราง issuer list มี column "abbr" หรือ "symbol" หรือ "name"
            if not any(k in " ".join(headers) for k in ["abbr", "symbol", "name"]):
                continue

            logger.info(f"[v6] scanning table headers: {headers}")

            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["td", "th"])
                if not cells:
                    continue

                row_text = " ".join(c.get_text(strip=True) for c in cells).upper()

                # ตรวจว่าแถวนี้ตรงกับ company ที่ค้นหา
                if prefix not in row_text:
                    continue

                # หา link ในแถวนี้
                for a in row.find_all("a", href=True):
                    href = a["href"]
                    if any(k in href for k in ["Issuer", "Bond", "Issue", "Detail"]):
                        issuer_link = _full_url(href)
                        logger.info(f"[v6] found issuer link: {issuer_link}")
                        break
                if issuer_link:
                    break
            if issuer_link:
                break

        if not issuer_link:
            logger.info(f"[v6] no issuer link found for '{prefix}'")
            return []

        # ตาม link ไปหน้า issuer/bond list
        time.sleep(0.5)
        resp2 = session.get(issuer_link, headers={**HEADERS, "Referer": BOND_INFO_URL}, timeout=20)
        resp2.raise_for_status()
        soup2 = BeautifulSoup(resp2.text, "lxml")

        logger.info(f"[v6] issuer page url: {issuer_link}")
        logger.info(f"[v6] issuer page tables: {len(soup2.find_all('table'))}, links: {len(soup2.find_all('a', href=True))}")

        bonds = _parse_bond_links_and_table(soup2, prefix)
        logger.info(f"[v6] found {len(bonds)} bonds from issuer page")
        return bonds

    except Exception as e:
        logger.exception(f"[v6] issuer_link error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 2: Issuer Detail page + UpdatePanel (trigger Current Bond tab)
# ─────────────────────────────────────────────────────────────────────────────

def find_bonds_via_updatepanel(company_name: str, session: requests.Session) -> list[dict]:
    """
    ไปหน้า IssuerDetail แล้ว trigger Current Bond tab ด้วย ASP.NET UpdatePanel partial postback
    """
    issuer_code = company_name.strip().lower()
    url = f"{ISSUER_DETAIL_BASE}?issuer={issuer_code}"

    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # ตรวจว่าเจอหน้า issuer จริงๆ
        page_text = soup.get_text(strip=True)
        logger.info(f"[v6] issuer detail page text len: {len(page_text)}")

        # ดูว่ามี bond data ใน initial HTML เลยหรือเปล่า
        direct_bonds = _parse_bond_links_and_table(soup, company_name.strip().upper())
        if direct_bonds:
            logger.info(f"[v6] found {len(direct_bonds)} bonds in initial HTML")
            return direct_bonds

        # ลอง trigger Current Bond tab ผ่าน UpdatePanel
        fields = _get_viewstate(soup)

        # ค้นหา tab control IDs จาก HTML
        tab_targets = []
        for el in soup.find_all(attrs={"id": True}):
            eid = el.get("id", "")
            if any(k in eid.lower() for k in ["tab", "container"]):
                # แปลง id เป็น __EVENTTARGET format (replace _ with $)
                target = "ctl00$ContentPlaceHolder1$" + eid.replace("ctl00_ContentPlaceHolder1_", "").replace("_", "$")
                tab_targets.append(target)

        # เพิ่ม common patterns
        tab_targets += [
            "ctl00$ContentPlaceHolder1$TabContainer1",
            "ctl00$ContentPlaceHolder1$tcMain",
            "ctl00$ContentPlaceHolder1$TabPanel2",
        ]

        for target in tab_targets[:5]:
            fields_copy = {**fields}
            fields_copy["__EVENTTARGET"] = target
            fields_copy["__EVENTARGUMENT"] = "1"  # tab index 1 = Current Bond
            fields_copy["__ASYNCPOST"] = "true"

            resp2 = session.post(
                url, data=fields_copy,
                headers={
                    **HEADERS,
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                    "X-MicrosoftAjax": "Delta=true",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": url,
                },
                timeout=25,
            )

            # UpdatePanel response format: "length|type|id|content|..."
            raw = resp2.text
            logger.info(f"[v6] UpdatePanel target={target} resp len={len(raw)}")

            # Extract HTML from Delta response
            html_parts = re.findall(r"\d+\|updatePanel\|[^|]+\|(.*?)(?=\d+\|(?:updatePanel|hiddenField|asyncPostBackControlIDs)|$)", raw, re.DOTALL)
            if not html_parts:
                # ลอง parse ตรงๆ
                html_parts = [raw]

            for html_part in html_parts:
                part_soup = BeautifulSoup(html_part, "lxml")
                bonds = _parse_bond_links_and_table(part_soup, company_name.strip().upper())
                if bonds:
                    logger.info(f"[v6] UpdatePanel found {len(bonds)} bonds!")
                    return bonds

    except Exception as e:
        logger.exception(f"[v6] updatepanel error: {e}")

    return []


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 3: GET Issuer Detail + parse ทุกอย่างที่มี
# ─────────────────────────────────────────────────────────────────────────────

def find_bonds_direct(company_name: str, session: requests.Session) -> list[dict]:
    """ลอง GET หน้า Issuer Detail หลายรูปแบบ แล้ว parse สิ่งที่มี"""
    codes_to_try = [
        company_name.strip().lower(),
        company_name.strip().upper(),
    ]
    for code in codes_to_try:
        url = f"{ISSUER_DETAIL_BASE}?issuer={code}"
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            bonds = _parse_bond_links_and_table(soup, company_name.strip().upper())
            if bonds:
                logger.info(f"[v6] direct found {len(bonds)} bonds for code '{code}'")
                return bonds
        except Exception as e:
            logger.warning(f"[v6] direct error for code '{code}': {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS: Parse bond links and table from any soup
# ─────────────────────────────────────────────────────────────────────────────

def _parse_bond_links_and_table(soup: BeautifulSoup, prefix: str) -> list[dict]:
    """
    Parse bond data จาก soup — รองรับทั้ง link-based และ table-based
    Filter เฉพาะ bond ที่ symbol ขึ้นต้นด้วย prefix
    """
    bonds = []
    seen_symbols = set()

    # 1) หา bond detail links (Issue.aspx?symbol=uuid)
    detail_map = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        onclick = a.get("onclick", "")
        sym = a.get_text(strip=True).split()[0].upper() if a.get_text(strip=True) else ""

        url = None
        if "Issue.aspx?symbol=" in href:
            url = _full_url(href)
        elif "Issue.aspx" in href and "symbol=" in href.lower():
            url = _full_url(href)

        # onclick UUID
        if not url:
            m = re.search(r"['\"]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})['\"]", onclick)
            if m:
                url = f"{BOND_INFO_URL}?symbol={m.group(1)}"

        if url and sym and sym.startswith(prefix):
            detail_map[sym] = url

    logger.info(f"[parse] detail_map size: {len(detail_map)}, prefix='{prefix}'")

    # 2) Parse ตาราง
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_cells = rows[0].find_all(["th", "td"])
        headers = [c.get_text(strip=True).lower() for c in header_cells]
        joined = " ".join(headers)

        if not any(k in joined for k in ["symbol", "maturity", "issue", "term", "coupon", "ttm"]):
            continue

        logger.info(f"[parse] bond table headers: {headers}")
        col = _map_cols(headers)

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if not cells or all(c.get_text(strip=True) == "" for c in cells):
                continue

            cell_texts = [c.get_text(strip=True) for c in cells]

            sym_idx = col.get("symbol", 0)
            raw_sym = cell_texts[sym_idx] if sym_idx < len(cell_texts) else cell_texts[0]
            sym = raw_sym.split()[0].upper() if raw_sym else ""

            if not sym or not sym.startswith(prefix):
                continue
            if sym in seen_symbols:
                continue
            seen_symbols.add(sym)

            bond = {"symbol": sym}
            for field, idx in col.items():
                if field != "symbol" and idx < len(cell_texts) and cell_texts[idx]:
                    bond[field] = cell_texts[idx]

            # ลอง get detail URL จาก link ใน row
            detail_url = detail_map.get(sym, "")
            if not detail_url:
                row_links = row.find_all("a", href=True)
                for a in row_links:
                    href = a["href"]
                    if "symbol=" in href.lower():
                        detail_url = _full_url(href)
                        break
                    onclick = a.get("onclick", "")
                    m = re.search(r"['\"]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})['\"]", onclick)
                    if m:
                        detail_url = f"{BOND_INFO_URL}?symbol={m.group(1)}"
                        break

            bond["detail_url"] = detail_url
            logger.info(f"[parse] ✓ bond: {sym}, url={'yes' if detail_url else 'no'}")
            bonds.append(bond)

    # 3) ถ้าหาจาก table ไม่ได้ แต่มี detail_map → ใช้จาก detail_map
    if not bonds and detail_map:
        for sym, url in detail_map.items():
            bonds.append({"symbol": sym, "detail_url": url})

    return bonds


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: ดึง detail จากหน้า Bond Detail
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bond_detail(detail_url: str, session: requests.Session) -> dict:
    detail = {}
    if not detail_url:
        return detail
    try:
        resp = session.get(detail_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                pairs = [(cells[i].get_text(strip=True).lower(),
                          cells[i+1].get_text(" ", strip=True))
                         for i in range(0, len(cells)-1, 2)]
                if len(cells) == 4:
                    pairs += [(cells[2].get_text(strip=True).lower(),
                               cells[3].get_text(" ", strip=True))]
                for label, value in pairs:
                    _assign(detail, label, value)

        st = detail.get("secured_type", "").lower()
        bt = detail.get("bond_type", "").lower()
        if "unsecure" in st or "unsecure" in bt:
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน"
        elif "secure" in st or "fasset" in st or "secure" in bt:
            detail["secured_label"] = "🔒 มีหลักประกัน"
        else:
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน"

        logger.info(f"[detail] {detail.get('symbol')} coupon={detail.get('coupon_rate')} uw={str(detail.get('underwriters',''))[:40]}")

    except Exception as e:
        logger.exception(f"[detail] error {detail_url}: {e}")
    return detail


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name: str) -> list[dict]:
    session = requests.Session()
    session.headers.update(HEADERS)
    name = company_name.strip()
    logger.info(f"[main] === Searching: '{name}' ===")

    # Strategy 1: Follow issuer link from Bond Info page
    bond_list = find_bonds_via_issuer_link(name, session)

    # Strategy 2: Issuer Detail page + UpdatePanel
    if not bond_list:
        bond_list = find_bonds_via_updatepanel(name, session)

    # Strategy 3: Direct GET Issuer Detail page
    if not bond_list:
        bond_list = find_bonds_direct(name, session)

    if not bond_list:
        logger.info(f"[main] No bonds found for '{name}'")
        return []

    # Fetch details
    results = []
    for b in bond_list[:10]:
        if b.get("detail_url"):
            time.sleep(0.3)
            detail = fetch_bond_detail(b["detail_url"], session)
            merged = {**b, **detail}
        else:
            merged = b
        if not merged.get("symbol"):
            merged["symbol"] = b.get("symbol", "-")
        results.append(merged)

    logger.info(f"[main] Done: {len(results)} bonds")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# FORMAT
# ─────────────────────────────────────────────────────────────────────────────

def format_bond_message(bonds: list[dict], company_name: str) -> str:
    if not bonds:
        return (
            f"❌ ไม่พบข้อมูลหุ้นกู้ของ \"{company_name}\"\n\n"
            "💡 ลองพิมพ์ชื่อย่อ:\n"
            "  เช่น PTT, CPALL, CI, ASW, KBANK\n\n"
            "📌 ข้อมูลจาก ThaiBMA"
        )

    lines = [f"📋 หุ้นกู้ {company_name.upper()} ({len(bonds)} รุ่น)", "─" * 28]

    for b in bonds:
        sym   = b.get("symbol", "-")
        issue = b.get("issue_date", "-")
        mat   = b.get("maturity_date", "-")
        tenor = b.get("tenor", "-")
        cpn   = b.get("coupon_rate", "-")
        out   = b.get("outstanding_size") or b.get("issue_size", "-")
        sec   = b.get("secured_label", "🔓 ไม่มีหลักประกัน")
        irat  = b.get("issue_rating", "-")
        erat  = b.get("issuer_rating", "-")
        reg   = b.get("registrar", "-")
        bh    = b.get("bondholder_rep", "-")
        uw    = b.get("underwriters", "-")
        fa    = b.get("financial_advisor", "-")

        lines += [
            f"\n🔹 {sym}",
            f"  📅 ออก: {issue}",
            f"  📅 ครบกำหนด: {mat}",
            f"  ⏳ อายุ: {tenor}",
            f"  💰 ดอกเบี้ย: {cpn}",
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


def _map_cols(headers: list[str]) -> dict:
    col = {}
    kw = {
        "symbol":         ["symbol", "series", "thaibma symbol"],
        "issue_date":     ["issue date"],
        "maturity_date":  ["maturity date", "maturity"],
        "coupon_rate":    ["coupon", "rate"],
        "tenor":          ["term", "tenor", "ttm"],
        "secured_type":   ["secured type", "secured"],
        "registrar":      ["registrar"],
        "outstanding_size": ["outstanding"],
        "issue_size":     ["issue size"],
    }
    for field, keywords in kw.items():
        for i, h in enumerate(headers):
            if any(k in h for k in keywords) and field not in col:
                col[field] = i
    return col


def _assign(d: dict, label: str, value: str):
    v = value.strip()
    if not label or not v or v == "-":
        return
    if "symbol" in label and not d.get("symbol"):
        d["symbol"] = v.split()[0]
    elif "issue date" in label and "registration" not in label and not d.get("issue_date"):
        d["issue_date"] = v
    elif "maturity date" in label and not d.get("maturity_date"):
        d["maturity_date"] = v
    elif "issue term" in label:
        d["tenor"] = v.split("/")[0].strip()
    elif "coupon payment" in label:
        m = re.search(r"Fixed:\s*([\d.]+)", v, re.I)
        d["coupon_rate"] = (m.group(1) + "%") if m else v[:80]
    elif "bond type" in label:
        d["bond_type"] = v
    elif "secured type" in label or (label == "collateral"):
        d["secured_type"] = v
    elif "registrar" in label and "co-" not in label and not d.get("registrar"):
        d["registrar"] = v
    elif "debenture holder" in label or "bondholder rep" in label:
        d["bondholder_rep"] = v
    elif "underwriter" in label:
        d["underwriters"] = v
    elif "financial advisor" in label:
        d["financial_advisor"] = v
    elif "isin" in label and "local" in label:
        d["isin"] = v
    elif "issue size" in label and "outstanding" not in label:
        d["issue_size"] = v
    elif "outstanding size" in label:
        d["outstanding_size"] = v
    elif "issue rating" in label:
        d["issue_rating"] = v
    elif "issuer rating" in label and "issue " not in label:
        d["issuer_rating"] = v
    elif "distribution" in label:
        d["distribution"] = v


def _full_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return BASE_URL + "/" + href.lstrip("/")
