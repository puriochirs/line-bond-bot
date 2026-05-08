import requests
from bs4 import BeautifulSoup
import logging
import re
import time
import json

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.thaibma.or.th/EN/Issuer/IssuerDetail.aspx",
}

BASE_URL      = "https://www.thaibma.or.th"
REGISSUE_URL  = f"{BASE_URL}/issuer/regissue"          # JSON API
BOND_INFO_URL = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"
ISSUER_DETAIL = f"{BASE_URL}/EN/Issuer/IssuerDetail.aspx"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: ดึง bond list จาก JSON API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bond_list(abbr_name: str, session: requests.Session) -> list[dict]:
    """
    เรียก API:
      GET /issuer/regissue?abbrName={TICKER}&term=long   → Long Term Debenture
      GET /issuer/regissue?abbrName={TICKER}&term=short  → Short Term Debenture
    คืน list ของ bond dicts พร้อม symbol, dates, term, secured, etc.
    """
    all_bonds = []
    ref = f"{ISSUER_DETAIL}?issuer={abbr_name.lower()}"

    for term in ["long", "short"]:
        url = f"{REGISSUE_URL}?abbrName={abbr_name}&term={term}"
        try:
            resp = session.get(
                url,
                headers={**HEADERS, "Referer": ref},
                timeout=20,
            )
            resp.raise_for_status()

            logger.info(f"[api] {term}: status={resp.status_code}, len={len(resp.text)}, ct={resp.headers.get('Content-Type','')}")

            # Parse JSON
            data = resp.json()
            logger.info(f"[api] {term}: got {len(data) if isinstance(data, list) else type(data)} items")

            if isinstance(data, list):
                for item in data:
                    bond = _item_to_bond(item, term)
                    if bond:
                        all_bonds.append(bond)
            elif isinstance(data, dict):
                # อาจอยู่ใน key เช่น "data", "result", "bonds"
                for key in ["data", "result", "bonds", "items", "records", "value"]:
                    if key in data and isinstance(data[key], list):
                        for item in data[key]:
                            bond = _item_to_bond(item, term)
                            if bond:
                                all_bonds.append(bond)
                        break
                # ถ้าไม่มี key ที่รู้จัก log raw data
                if not all_bonds:
                    logger.info(f"[api] {term}: dict keys = {list(data.keys())[:10]}")

        except json.JSONDecodeError as e:
            logger.warning(f"[api] {term}: JSON decode error: {e}, raw={resp.text[:200]}")
        except Exception as e:
            logger.exception(f"[api] {term}: error: {e}")

    logger.info(f"[api] Total bonds from API: {len(all_bonds)}")
    return all_bonds


def _item_to_bond(item: dict, term_type: str) -> dict | None:
    """แปลง JSON item เป็น bond dict — ใช้ key จริงจาก ThaiBMA API"""
    if not isinstance(item, dict):
        return None

    def g(*keys):
        for k in keys:
            if k in item and item[k] is not None:
                v = str(item[k]).strip()
                if v and v not in ["", "null", "None", "0"]:
                    return v
        return "-"

    symbol = g("Symbol", "symbol", "ThaiBMASymbol")
    if symbol == "-":
        return None

    # SecureCode: FASSET = มีหลักประกัน, UNSECURE = ไม่มี
    secure_code = g("SecureCode", "securedType", "SecuredType")
    sc_lower = secure_code.lower()
    if "unsecure" in sc_lower:
        secured_label = "🔓 ไม่มีหลักประกัน (Unsecured)"
    elif secure_code != "-":
        secured_label = f"🔒 มีหลักประกัน ({secure_code})"
    else:
        secured_label = "🔓 ไม่มีหลักประกัน"

    bond = {
        "symbol":           symbol,
        "term_type":        "Long Term" if term_type == "long" else "Short Term",
        "issue_date":       g("IssuedDate", "IssueDate", "issueDate"),
        "maturity_date":    g("MaturityDate", "maturityDate"),
        "tenor":            g("Term", "term", "tenor"),
        "coupon_rate":      g("MarketYield", "CouponRate", "couponRate", "Coupon"),
        "issue_size":       g("IssueSize", "issueSize"),
        "outstanding_size": g("CurrentOutstanding", "IssueOutstanding", "outstanding"),
        "secured_type":     secure_code,
        "secured_label":    secured_label,
        "registrar":        g("Registrar", "registrar"),
        "bondholder_rep":   g("BondholderRepresentative", "bondholderRep"),
        "underwriters":     g("Underwriter", "underwriter"),
        "issue_rating":     g("IssueRating", "issueRating"),
        "issuer_rating":    g("CompanyRating", "issuerRating"),
        "distribution":     g("DistributionDisplay", "distribution"),
        "attribute":        g("AttributeDisplay", "attribute"),
        "esg":              g("ESGDisplay", "esg"),
        "isin":             g("IssueLegacyID", "isinCode", "ISIN"),
    }

    # หา detail URL จาก IssueID หรือ GUID
    issue_id = g("IssueID", "issueId", "id", "Id")
    if issue_id != "-" and "-" in issue_id:  # UUID pattern
        bond["detail_url"] = f"{BOND_INFO_URL}?symbol={issue_id}"
    elif issue_id != "-":
        bond["detail_url"] = f"{BOND_INFO_URL}?symbol={issue_id}"

    return bond


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: ดึง detail จากหน้า Bond Detail (optional — ถ้า API ไม่มีข้อมูลครบ)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_bond_detail(detail_url: str, session: requests.Session) -> dict:
    detail = {}
    if not detail_url:
        return detail
    try:
        resp = session.get(detail_url, headers={**HEADERS, "Accept": "text/html,*/*"}, timeout=20)
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
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน (Unsecured)"
        elif "secure" in st or "fasset" in st or "secure" in bt:
            detail["secured_label"] = "🔒 มีหลักประกัน (Secured)"

    except Exception as e:
        logger.exception(f"[detail] {detail_url}: {e}")
    return detail


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name: str) -> list[dict]:
    session = requests.Session()
    abbr = company_name.strip().upper()
    logger.info(f"[main] === Searching: '{abbr}' ===")

    # Step 1: ดึงจาก JSON API
    bonds = fetch_bond_list(abbr, session)

    if not bonds:
        logger.info(f"[main] API returned 0 bonds for '{abbr}'")
        return []

    # Step 2: ถ้ามี detail_url และ API ให้ข้อมูลไม่ครบ ดึงเพิ่ม (optional)
    results = []
    for b in bonds[:15]:
        detail_url = b.get("detail_url", "")
        missing = b.get("coupon_rate", "-") == "-" or b.get("underwriters", "-") == "-"

        if detail_url and missing:
            time.sleep(0.3)
            detail = fetch_bond_detail(detail_url, session)
            # เติมเฉพาะ field ที่ยังขาด
            for k, v in detail.items():
                if k not in b or b[k] == "-":
                    b[k] = v

        results.append(b)

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

    # แยก short/long term
    long_bonds  = [b for b in bonds if "Long" in b.get("term_type", "")]
    short_bonds = [b for b in bonds if "Short" in b.get("term_type", "")]

    lines = [f"📋 หุ้นกู้ {company_name.upper()} ({len(bonds)} รุ่น)", "─" * 28]

    def add_bonds(bond_list: list[dict], label: str):
        if not bond_list:
            return
        lines.append(f"\n{label} ({len(bond_list)} รุ่น)")
        for b in bond_list:
            sym   = b.get("symbol", "-")
            issue = b.get("issue_date", "-")
            mat   = b.get("maturity_date", "-")
            tenor = b.get("tenor", "-")
            cpn   = b.get("coupon_rate", "-")
            out   = b.get("outstanding_size", b.get("issue_size", "-"))
            sec   = b.get("secured_label", "🔓 ไม่มีหลักประกัน")
            irat  = b.get("issue_rating", "-")
            erat  = b.get("issuer_rating", "-")
            reg   = b.get("registrar", "-")
            bh    = b.get("bondholder_rep", "-")
            uw    = b.get("underwriters", "-")
            dist  = b.get("distribution", "-")

            lines.extend([
                f"\n🔹 {sym}",
                f"  📅 ออก: {issue}",
                f"  📅 ครบกำหนด: {mat}",
                f"  ⏳ อายุ: {tenor}",
                f"  💰 ดอกเบี้ย: {cpn}",
                f"  💵 Outstanding: {out}",
                f"  {sec}",
                f"  📢 ขายให้: {dist}",
                f"  📊 Issue Rating: {irat}",
                f"  📊 Issuer Rating: {erat}",
                f"  🏦 Registrar: {reg}",
                f"  👤 BH Rep: {bh}",
                f"  📢 Underwriter: {uw}",
            ])

    add_bonds(long_bonds, "📌 Long Term Debenture")
    add_bonds(short_bonds, "📌 Short Term Debenture")

    lines += ["", "─" * 28, "📌 ข้อมูลจาก ThaiBMA"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _assign(d: dict, label: str, value: str):
    v = value.strip()
    if not label or not v or v in ["-", "null"]:
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
