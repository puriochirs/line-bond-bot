import requests
from bs4 import BeautifulSoup
import logging
import re
import json
import time

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

BASE_URL           = "https://www.thaibma.or.th"
ISSUER_DETAIL_BASE = f"{BASE_URL}/EN/Issuer/IssuerDetail.aspx"
BOND_INFO_URL      = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 1: ดึง JS จากหน้า Issuer Detail แล้วหา AJAX endpoint ของ Current Bond
# ─────────────────────────────────────────────────────────────────────────────

def find_ajax_endpoint(issuer_code: str, session: requests.Session) -> list[dict]:
    url = f"{ISSUER_DETAIL_BASE}?issuer={issuer_code}"
    prefix = issuer_code.upper()
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        page_html = resp.text
        soup = BeautifulSoup(page_html, "lxml")

        # 1) หา JSON data ที่ embed ใน script tags
        for script in soup.find_all("script"):
            src = script.get_text()
            if not src:
                continue
            # หา JSON array ที่มีข้อมูลหุ้นกู้ (symbol pattern เช่น PTT236A)
            bond_sym_pattern = re.compile(rf'["\']({re.escape(prefix)}\d+[A-Z][^"\']*)["\']')
            if bond_sym_pattern.search(src):
                logger.info(f"[v7] Found bond symbols in script tag!")
                bonds = _extract_from_js(src, prefix)
                if bonds:
                    return bonds

        # 2) หา URL patterns ใน JavaScript
        js_url_patterns = [
            r'(?:url|src|href|dataSource|data-url)\s*[=:]\s*["\']([^"\']+)["\']',
            r'\.(?:ajax|get|post|load)\s*\(\s*["\']([^"\']+)["\']',
            r'fetch\s*\(\s*["\']([^"\']+)["\']',
            r'\.(?:read|query)\s*\(\s*\{[^}]*url\s*:\s*["\']([^"\']+)["\']',
            r'["\']url["\']:\s*["\']([^"\']+)["\']',
        ]
        candidate_urls = set()
        all_js = " ".join(s.get_text() for s in soup.find_all("script"))
        for pattern in js_url_patterns:
            for m in re.finditer(pattern, all_js, re.I):
                u = m.group(1)
                if any(k in u.lower() for k in ["bond", "current", "issue", "issuer", "aspx", "ashx", "asmx", "api"]):
                    if u.startswith("/") or u.startswith("http"):
                        candidate_urls.add(u)

        logger.info(f"[v7] Candidate AJAX URLs: {list(candidate_urls)[:10]}")

        # 3) ลอง GET/POST แต่ละ candidate URL
        for u in list(candidate_urls)[:10]:
            full_u = u if u.startswith("http") else BASE_URL + u
            # เพิ่ม issuer parameter
            sep = "&" if "?" in full_u else "?"
            test_urls = [
                full_u,
                f"{full_u}{sep}issuer={issuer_code}",
                f"{full_u}{sep}Issuer={issuer_code}",
            ]
            for tu in test_urls:
                try:
                    r = session.get(tu, headers={**HEADERS, "Referer": url, "X-Requested-With": "XMLHttpRequest"}, timeout=10)
                    if r.status_code == 200 and len(r.text) > 100:
                        bonds = _try_parse_response(r.text, r.headers.get("Content-Type", ""), prefix)
                        if bonds:
                            logger.info(f"[v7] AJAX endpoint found: {tu}")
                            return bonds
                except Exception:
                    pass

    except Exception as e:
        logger.exception(f"[v7] find_ajax_endpoint error: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 2: UpdatePanel ด้วย proper Delta parsing + ScriptManager
# ─────────────────────────────────────────────────────────────────────────────

def find_bonds_updatepanel(issuer_code: str, session: requests.Session) -> list[dict]:
    url = f"{ISSUER_DETAIL_BASE}?issuer={issuer_code}"
    prefix = issuer_code.upper()
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        fields = _get_viewstate(soup)

        # หา ScriptManager
        sm_id = None
        for inp in soup.find_all("input", {"id": re.compile("ScriptManager", re.I)}):
            sm_id = inp.get("name") or inp.get("id")
        for el in soup.find_all(attrs={"id": re.compile("ScriptManager", re.I)}):
            sm_id = el.get("id")
            break
        if not sm_id:
            sm_id = "ctl00$ScriptManager1"

        logger.info(f"[v7] ScriptManager id: {sm_id}")

        # หา UpdatePanel IDs
        up_ids = []
        for el in soup.find_all(attrs={"id": re.compile(r"UpdatePanel|upd|panel", re.I)}):
            eid = el.get("id", "")
            if eid:
                up_ids.append(eid.replace("_", "$"))

        # หา Tab control IDs
        tab_ids = []
        for el in soup.find_all(attrs={"id": re.compile(r"tab|Tab", re.I)}):
            eid = el.get("id", "")
            if eid and ("tab" in eid.lower()):
                tab_ids.append(eid)

        logger.info(f"[v7] UpdatePanel IDs: {up_ids[:5]}, Tab IDs: {tab_ids[:10]}")

        # ลอง trigger Current Bond tab (tab index 1)
        targets_to_try = []
        for tid in tab_ids:
            t = tid.replace("_", "$")
            targets_to_try.append(t)
        # เพิ่ม common patterns
        targets_to_try += [
            "ctl00$ContentPlaceHolder1$TabContainer1",
            "ctl00$ContentPlaceHolder1$tab",
            "ctl00$ContentPlaceHolder1$tcMain",
        ]

        for target in targets_to_try[:8]:
            for tab_idx in ["1", "0"]:  # 1 = Current Bond, 0 = Issuer Info
                f = {**fields}
                f["__EVENTTARGET"] = target
                f["__EVENTARGUMENT"] = tab_idx
                f["__ASYNCPOST"] = "true"
                # ScriptManager field: SM_id=UpdatePanelID|EventTarget
                for up_id in (up_ids[:1] or ["ctl00$ContentPlaceHolder1$UpdatePanel1"]):
                    f[sm_id] = f"{up_id}|{target}"
                    break

                r = session.post(
                    url, data=f,
                    headers={
                        **HEADERS,
                        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                        "X-MicrosoftAjax": "Delta=true",
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": url,
                    },
                    timeout=25,
                )

                raw = r.text
                # Parse Delta format properly
                panels = _parse_delta(raw)
                logger.info(f"[v7] Delta target={target}[{tab_idx}]: {len(panels)} panels")

                for ptype, pid, content in panels:
                    if ptype != "updatePanel":
                        continue
                    logger.info(f"[v7] Panel id={pid}, content len={len(content)}")
                    part_soup = BeautifulSoup(content, "lxml")
                    bonds = _parse_bond_table(part_soup, prefix)
                    if bonds:
                        logger.info(f"[v7] Found {len(bonds)} bonds in panel '{pid}'!")
                        return bonds

                    # ลองหา JSON ใน script
                    for script in part_soup.find_all("script"):
                        js = script.get_text()
                        b = _extract_from_js(js, prefix)
                        if b:
                            return b

    except Exception as e:
        logger.exception(f"[v7] updatepanel error: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# STRATEGY 3: ลองหา API endpoints ที่ทราบ patterns
# ─────────────────────────────────────────────────────────────────────────────

KNOWN_PATTERNS = [
    "/EN/Issuer/GetCurrentBond.aspx?issuer={code}",
    "/EN/Issuer/GetCurrentBond.ashx?issuer={code}",
    "/EN/Issuer/CurrentBond.aspx?issuer={code}",
    "/EN/BondInfo/BondFeature/Issue.aspx?issuer={code}",
    "/EN/BondInfo/BondFeature/Issue.aspx?Issuer={code}",
    "/EN/BondInfo/GetBond.aspx?issuer={code}",
    "/EN/Issuer/IssuerBond.aspx?issuer={code}",
    "/api/bond?issuer={code}",
    "/EN/Issuer/GetBondsByIssuer?issuer={code}",
]

def try_known_endpoints(issuer_code: str, session: requests.Session) -> list[dict]:
    prefix = issuer_code.upper()
    ref = f"{ISSUER_DETAIL_BASE}?issuer={issuer_code}"
    for pattern in KNOWN_PATTERNS:
        url = BASE_URL + pattern.format(code=issuer_code)
        try:
            r = session.get(url, headers={**HEADERS, "Referer": ref, "X-Requested-With": "XMLHttpRequest"}, timeout=10)
            if r.status_code != 200 or len(r.text) < 50:
                continue
            ct = r.headers.get("Content-Type", "")
            bonds = _try_parse_response(r.text, ct, prefix)
            if bonds:
                logger.info(f"[v7] Known endpoint worked: {url}")
                return bonds
            logger.info(f"[v7] Tried {url}: status={r.status_code} len={len(r.text)}")
        except Exception as e:
            logger.warning(f"[v7] Known endpoint {url}: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: ดึง bond detail
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
                pairs = []
                for i in range(0, len(cells)-1, 2):
                    pairs.append((cells[i].get_text(strip=True).lower(),
                                  cells[i+1].get_text(" ", strip=True)))
                if len(cells) == 4:
                    pairs.append((cells[2].get_text(strip=True).lower(),
                                  cells[3].get_text(" ", strip=True)))
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
    except Exception as e:
        logger.exception(f"[detail] {detail_url}: {e}")
    return detail


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name: str) -> list[dict]:
    session = requests.Session()
    session.headers.update(HEADERS)
    code = company_name.strip()
    logger.info(f"[main] === Searching: '{code}' ===")

    bond_list = []

    # Strategy 1: find hidden AJAX endpoint
    if not bond_list:
        bond_list = find_ajax_endpoint(code, session)

    # Strategy 2: UpdatePanel with proper Delta parsing
    if not bond_list:
        bond_list = find_bonds_updatepanel(code, session)

    # Strategy 3: try known endpoint patterns
    if not bond_list:
        bond_list = try_known_endpoints(code, session)

    if not bond_list:
        logger.info(f"[main] No bonds found for '{code}'")
        return []

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
# PARSE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _parse_delta(raw: str) -> list[tuple]:
    """Parse ASP.NET UpdatePanel Delta format: length|type|id|content|"""
    parts = []
    pos = 0
    while pos < len(raw):
        pipe1 = raw.find("|", pos)
        if pipe1 == -1:
            break
        try:
            length = int(raw[pos:pipe1])
        except ValueError:
            pos = pipe1 + 1
            continue
        pipe2 = raw.find("|", pipe1 + 1)
        if pipe2 == -1:
            break
        ptype = raw[pipe1+1:pipe2]
        pipe3 = raw.find("|", pipe2 + 1)
        if pipe3 == -1:
            break
        pid = raw[pipe2+1:pipe3]
        content_start = pipe3 + 1
        content = raw[content_start:content_start + length]
        parts.append((ptype, pid, content))
        pos = content_start + length + 1
    return parts


def _parse_bond_table(soup: BeautifulSoup, prefix: str) -> list[dict]:
    bonds = []
    seen = set()
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        joined = " ".join(headers)
        if not any(k in joined for k in ["symbol", "maturity", "issue", "term", "ttm", "coupon", "outstanding"]):
            continue
        logger.info(f"[parse_table] matching headers: {headers}")
        col = _map_cols(headers)
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if not cells or all(c == "" for c in cells):
                continue
            sym_idx = col.get("symbol", 0)
            sym = cells[sym_idx].split()[0].upper() if sym_idx < len(cells) else ""
            if not sym or not sym.startswith(prefix) or sym in seen:
                continue
            seen.add(sym)
            bond = {"symbol": sym}
            for f, i in col.items():
                if f != "symbol" and i < len(cells):
                    bond[f] = cells[i]
            # หา detail URL
            row_el = table.find_all("tr")[rows.index(row) + 1 if row in rows else 0]
            for a in row.find_all("a", href=True):
                href = a["href"]
                if "symbol=" in href.lower():
                    bond["detail_url"] = _full_url(href)
                    break
                onclick = a.get("onclick", "")
                m = re.search(r"['\"]([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})['\"]", onclick)
                if m:
                    bond["detail_url"] = f"{BOND_INFO_URL}?symbol={m.group(1)}"
                    break
            logger.info(f"[parse_table] ✓ {sym}")
            bonds.append(bond)
    return bonds


def _extract_from_js(js: str, prefix: str) -> list[dict]:
    """พยายามดึงข้อมูลจาก JSON ที่ embed ใน JavaScript"""
    bonds = []
    json_arrays = re.findall(r'\[(\{["\']symbol["\'].*?\})\]', js, re.DOTALL | re.I)
    for arr in json_arrays:
        try:
            data = json.loads(f"[{arr}]")
            for item in data:
                sym = str(item.get("symbol", "")).upper()
                if sym.startswith(prefix):
                    bonds.append({
                        "symbol":        sym,
                        "issue_date":    str(item.get("issueDate", item.get("issue_date", "-"))),
                        "maturity_date": str(item.get("maturityDate", item.get("maturity_date", "-"))),
                        "coupon_rate":   str(item.get("coupon", item.get("couponRate", "-"))),
                        "tenor":         str(item.get("term", item.get("tenor", "-"))),
                    })
        except Exception:
            pass
    return bonds


def _try_parse_response(text: str, content_type: str, prefix: str) -> list[dict]:
    """ลอง parse response ในทุก format ที่เป็นไปได้"""
    # JSON response
    if "json" in content_type.lower() or text.strip().startswith(("[", "{")):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [_item_to_bond(item, prefix) for item in data if _item_to_bond(item, prefix)]
            if isinstance(data, dict):
                for key in ["data", "bonds", "items", "result", "records"]:
                    if key in data and isinstance(data[key], list):
                        return [_item_to_bond(i, prefix) for i in data[key] if _item_to_bond(i, prefix)]
        except Exception:
            pass
    # HTML response
    soup = BeautifulSoup(text, "lxml")
    return _parse_bond_table(soup, prefix)


def _item_to_bond(item: dict, prefix: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    sym = ""
    for k in ["symbol", "Symbol", "ThaiBMASymbol", "bondSymbol"]:
        if k in item:
            sym = str(item[k]).upper()
            break
    if not sym or not sym.startswith(prefix):
        return None
    return {
        "symbol":         sym,
        "issue_date":     str(item.get("issueDate", item.get("IssueDate", "-"))),
        "maturity_date":  str(item.get("maturityDate", item.get("MaturityDate", "-"))),
        "coupon_rate":    str(item.get("coupon", item.get("Coupon", item.get("couponRate", "-")))),
        "tenor":          str(item.get("term", item.get("Term", "-"))),
        "secured_type":   str(item.get("securedType", item.get("SecuredType", "-"))),
        "outstanding_size": str(item.get("outstanding", item.get("Outstanding", "-"))),
    }


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
        lines += [
            f"\n🔹 {b.get('symbol','-')}",
            f"  📅 ออก: {b.get('issue_date','-')}",
            f"  📅 ครบกำหนด: {b.get('maturity_date','-')}",
            f"  ⏳ อายุ: {b.get('tenor','-')}",
            f"  💰 ดอกเบี้ย: {b.get('coupon_rate','-')}",
            f"  💵 Outstanding: {b.get('outstanding_size',b.get('issue_size','-'))}",
            f"  {b.get('secured_label','🔓 ไม่มีหลักประกัน')}",
            f"  📊 Issue Rating: {b.get('issue_rating','-')}",
            f"  📊 Issuer Rating: {b.get('issuer_rating','-')}",
            f"  🏦 Registrar: {b.get('registrar','-')}",
            f"  👤 BH Rep: {b.get('bondholder_rep','-')}",
            f"  📢 Underwriter: {b.get('underwriters','-')}",
            f"  💼 FA: {b.get('financial_advisor','-')}",
        ]
    lines += ["", "─" * 28, "📌 ข้อมูลจาก ThaiBMA"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# SHARED HELPERS
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
        if any(k in h for k in ["symbol", "thaibma symbol"]) and "symbol" not in col:
            col["symbol"] = i
        if "issue date" in h and "issue_date" not in col:
            col["issue_date"] = i
        if "maturity" in h and "maturity_date" not in col:
            col["maturity_date"] = i
        if "coupon" in h and "coupon_rate" not in col:
            col["coupon_rate"] = i
        if ("term" in h or "ttm" in h) and "tenor" not in col:
            col["tenor"] = i
        if "secured" in h and "secured_type" not in col:
            col["secured_type"] = i
        if "registrar" in h and "registrar" not in col:
            col["registrar"] = i
        if "outstanding" in h and "outstanding_size" not in col:
            col["outstanding_size"] = i
        if "issue size" in h and "issue_size" not in col:
            col["issue_size"] = i
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
    elif "secured type" in label or label == "collateral":
        d["secured_type"] = v
    elif "registrar" in label and "co-" not in label and not d.get("registrar"):
        d["registrar"] = v
    elif "debenture holder" in label or "bondholder rep" in label:
        d["bondholder_rep"] = v
    elif "underwriter" in label:
        d["underwriters"] = v
    elif "financial advisor" in label:
        d["financial_advisor"] = v
    elif "outstanding size" in label:
        d["outstanding_size"] = v
    elif "issue size" in label and "outstanding" not in label:
        d["issue_size"] = v
    elif "issue rating" in label:
        d["issue_rating"] = v
    elif "issuer rating" in label and "issue " not in label:
        d["issuer_rating"] = v


def _full_url(href: str) -> str:
    return href if href.startswith("http") else BASE_URL + "/" + href.lstrip("/")
