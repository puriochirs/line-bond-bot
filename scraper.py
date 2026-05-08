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

BASE_URL      = "https://www.thaibma.or.th"
BOND_INFO_URL = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: ค้นหารายการ bond จาก Bond Info page และ filter ด้วย symbol prefix
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bond_list(company_name: str, session: requests.Session) -> list[dict]:
    """
    POST ไปที่ Bond Info page ด้วยชื่อบริษัท
    แล้ว parse ตาราง → filter เฉพาะ bond ที่ symbol ขึ้นต้นด้วยชื่อบริษัท
    คืน list ของ {symbol, issue_date, maturity_date, tenor, secured_type,
                  registrar, detail_url}
    """
    prefix = company_name.strip().upper()

    try:
        # GET page ก่อนเพื่อดึง ViewState
        resp = session.get(BOND_INFO_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        fields = _get_viewstate(soup)

        # log ทุก input field เพื่อ debug
        text_inputs = soup.find_all("input", {"type": "text"})
        logger.info(f"[fetch_list] text inputs found: {[i.get('name') for i in text_inputs]}")

        # ใส่ชื่อบริษัทในทุก text input
        for inp in text_inputs:
            iname = inp.get("name", "")
            if iname:
                fields[iname] = company_name

        # ปุ่ม submit
        for inp in soup.find_all("input", {"type": "submit"}):
            fields[inp.get("name", "")] = inp.get("value", "Search")
            break

        # POST
        resp2 = session.post(
            BOND_INFO_URL, data=fields,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": BOND_INFO_URL},
            timeout=30,
        )
        resp2.raise_for_status()
        soup2 = BeautifulSoup(resp2.text, "lxml")

        # log จำนวน table และ link ทั้งหมดที่เจอ (เพื่อ debug)
        all_tables = soup2.find_all("table")
        all_links  = soup2.find_all("a", href=True)
        logger.info(f"[fetch_list] tables={len(all_tables)}, links={len(all_links)}")

        # หา bond detail links ทุกรูปแบบที่เป็นไปได้
        detail_map = {}  # symbol -> url
        for a in all_links:
            href    = a.get("href", "")
            onclick = a.get("onclick", "")
            sym_text = a.get_text(strip=True).split()[0] if a.get_text(strip=True) else ""

            # รูปแบบ 1: href มี symbol= (UUID)
            if "symbol=" in href.lower():
                full = _full_url(href)
                if sym_text:
                    detail_map[sym_text.upper()] = full
                # ดึง UUID แล้วเก็บด้วย
                m = re.search(r"symbol=([a-f0-9\-]{30,})", href, re.I)
                if m:
                    detail_map[m.group(0)] = full

            # รูปแบบ 2: onclick มี UUID
            m = re.search(r"['\"]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})['\"]", onclick)
            if m:
                uuid = m.group(1)
                full = f"{BOND_INFO_URL}?symbol={uuid}"
                if sym_text:
                    detail_map[sym_text.upper()] = full

        logger.info(f"[fetch_list] detail_map size: {len(detail_map)}")

        # parse ตาราง
        bonds = []
        for table in all_tables:
            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # ตรวจ header
            header_cells = rows[0].find_all(["th", "td"])
            headers_text = [c.get_text(strip=True).lower() for c in header_cells]
            joined = " ".join(headers_text)
            if not any(k in joined for k in ["symbol", "maturity", "coupon", "issue", "term"]):
                continue

            logger.info(f"[fetch_list] found bond table, headers: {headers_text}")

            col = _map_cols(headers_text)

            for row in rows[1:]:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                if not cells or all(c == "" for c in cells):
                    continue

                # symbol อยู่ column แรก หรือ index จาก col_map
                sym_idx = col.get("symbol", 0)
                raw_sym = cells[sym_idx] if sym_idx < len(cells) else cells[0]
                # ตัดเฉพาะชื่อ symbol (ตัวแรก)
                sym = raw_sym.split()[0].upper() if raw_sym else ""

                if not sym:
                    continue

                # filter ด้วย prefix
                if not sym.startswith(prefix):
                    continue

                bond = {"symbol": sym}

                # map fields จาก column
                for field, idx in col.items():
                    if field == "symbol":
                        continue
                    if idx < len(cells):
                        bond[field] = cells[idx]

                # หา detail URL
                detail_url = detail_map.get(sym, "")
                if not detail_url:
                    # ลองหา link ใน row นั้น
                    row_el = table.find_all("tr")[rows.index(row) + 1] if False else None
                    for a in table.find_all("tr")[list(range(len(rows)))[rows.index(row) if row in rows else 0]].find_all("a", href=True):
                        href = a.get("href", "")
                        if "symbol=" in href.lower():
                            detail_url = _full_url(href)
                            break
                        onclick = a.get("onclick", "")
                        m = re.search(r"['\"]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})['\"]", onclick)
                        if m:
                            detail_url = f"{BOND_INFO_URL}?symbol={m.group(1)}"
                            break

                bond["detail_url"] = detail_url
                bonds.append(bond)
                logger.info(f"[fetch_list] matched bond: {sym}, detail_url={detail_url[:60] if detail_url else 'N/A'}")

        logger.info(f"[fetch_list] total matched: {len(bonds)} bonds for prefix '{prefix}'")
        return bonds

    except Exception as e:
        logger.exception(f"[fetch_list] error: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: ดึงรายละเอียดจาก Bond Detail page
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bond_detail(detail_url: str, session: requests.Session) -> dict:
    """ดึงข้อมูลละเอียดจาก Issue.aspx?symbol=xxx"""
    detail = {}
    if not detail_url:
        return detail
    try:
        resp = session.get(detail_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Parse ทุก label-value pair จาก table
        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                # ดึง pair: (label, value) จากทุก 2 cells
                for i in range(0, len(cells) - 1, 2):
                    label = cells[i].get_text(strip=True).lower()
                    value = cells[i+1].get_text(" ", strip=True)
                    _assign_field(detail, label, value)

                # บางหน้ามี 4 cells ต่อ row (2 pairs)
                if len(cells) == 4:
                    label2 = cells[2].get_text(strip=True).lower()
                    value2 = cells[3].get_text(" ", strip=True)
                    _assign_field(detail, label2, value2)

        # สร้าง secured label
        st = detail.get("secured_type", "").lower()
        bt = detail.get("bond_type", "").lower()
        if "unsecure" in st or "unsecure" in bt:
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน (Unsecured)"
        elif "secure" in st or "fasset" in st or "secure" in bt:
            detail["secured_label"] = "🔒 มีหลักประกัน (Secured)"
        else:
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน"

        logger.info(f"[detail] symbol={detail.get('symbol')}, coupon={detail.get('coupon_rate')}, uw={str(detail.get('underwriters',''))[:40]}")

    except Exception as e:
        logger.exception(f"[detail] error {detail_url}: {e}")

    return detail


def _assign_field(d: dict, label: str, value: str):
    if not label or not value:
        return
    v = value.strip()
    if not v or v == "-":
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
    elif label in ["secured type", "collateral"] or ("secured" in label and "co-" not in label):
        d["secured_type"] = v
    elif "registrar" in label and "co-registrar" not in label and not d.get("registrar"):
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


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name: str) -> list[dict]:
    session = requests.Session()
    session.headers.update(HEADERS)
    logger.info(f"[main] === Searching: '{company_name}' ===")

    # Step 1: หารายการ bond จาก search page
    bond_list = fetch_bond_list(company_name.strip(), session)

    if not bond_list:
        logger.info(f"[main] No bonds found for '{company_name}'")
        return []

    # Step 2: ดึง detail (สูงสุด 10 รุ่น)
    results = []
    for b in bond_list[:10]:
        detail_url = b.get("detail_url", "")
        if detail_url:
            time.sleep(0.3)
            detail = fetch_bond_detail(detail_url, session)
            merged = {**b, **detail}
        else:
            merged = b

        if not merged.get("symbol"):
            merged["symbol"] = b.get("symbol", "-")
        results.append(merged)

    logger.info(f"[main] Done: {len(results)} bonds")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# FORMAT MESSAGE
# ─────────────────────────────────────────────────────────────────────────────

def format_bond_message(bonds: list[dict], company_name: str) -> str:
    if not bonds:
        return (
            f"❌ ไม่พบข้อมูลหุ้นกู้ของ \"{company_name}\"\n\n"
            "💡 ลองพิมพ์ใหม่ด้วยชื่อย่อ:\n"
            "  เช่น PTT, CPALL, CI, ASW, KBANK\n\n"
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
        out    = b.get("outstanding_size") or b.get("issue_size", "-")
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


def _map_cols(headers: list[str]) -> dict:
    col = {}
    for i, h in enumerate(headers):
        if any(k in h for k in ["symbol", "series", "bond name"]) and "symbol" not in col:
            col["symbol"] = i
        if "issue date" in h and "issue_date" not in col:
            col["issue_date"] = i
        if "maturity" in h and "maturity_date" not in col:
            col["maturity_date"] = i
        if ("coupon" in h or "rate" in h) and "coupon_rate" not in col:
            col["coupon_rate"] = i
        if ("term" in h or "tenor" in h) and "tenor" not in col:
            col["tenor"] = i
        if "secured" in h and "secured_type" not in col:
            col["secured_type"] = i
        if "registrar" in h and "registrar" not in col:
            col["registrar"] = i
        if "outstanding" in h and "outstanding_size" not in col:
            col["outstanding_size"] = i
    return col


def _full_url(href: str) -> str:
    if href.startswith("http"):
        return href
    return BASE_URL + "/" + href.lstrip("/")
