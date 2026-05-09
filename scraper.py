import requests
from bs4 import BeautifulSoup
import logging
import re
import time
import json
from datetime import datetime

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
    "Referer": "https://www.thaibma.or.th/EN/Issuer/IssuerDetail.aspx",
}

BASE_URL      = "https://www.thaibma.or.th"
REGISSUE_URL  = f"{BASE_URL}/issuer/regissue"
BOND_INFO_URL = f"{BASE_URL}/EN/BondInfo/BondFeature/Issue.aspx"
ISSUER_DETAIL = f"{BASE_URL}/EN/Issuer/IssuerDetail.aspx"


def fmt_date(raw: str) -> str:
    if not raw or raw in ["-", "null", "None"]:
        return "-"
    raw = raw.split("T")[0].strip()
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"]:
        try:
            return datetime.strptime(raw, fmt).strftime("%-d %b %Y")
        except ValueError:
            continue
    return raw


def fmt_number(raw: str) -> str:
    if not raw or raw == "-":
        return "-"
    try:
        n = float(raw)
        if n == int(n):
            return f"{int(n):,}"
        return f"{n:,.2f}"
    except Exception:
        return raw


# ─── STEP 1: Bond List from JSON API ─────────────────────────────────────────

def fetch_bond_list(abbr_name: str, session: requests.Session) -> list:
    all_bonds = []
    ref = f"{ISSUER_DETAIL}?issuer={abbr_name.lower()}"
    for term in ["long", "short"]:
        url = f"{REGISSUE_URL}?abbrName={abbr_name}&term={term}"
        try:
            resp = session.get(url, headers={**HEADERS, "Referer": ref}, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            items = data if isinstance(data, list) else []
            if isinstance(data, dict):
                for key in ["data", "result", "bonds", "items", "records", "value"]:
                    if key in data and isinstance(data[key], list):
                        items = data[key]
                        break
            for item in items:
                bond = _item_to_bond(item, term)
                if bond:
                    all_bonds.append(bond)
            logger.info(f"[api] {term}: {len(items)} items")
        except Exception as e:
            logger.exception(f"[api] {term}: {e}")
    logger.info(f"[api] Total: {len(all_bonds)} bonds")
    return all_bonds


def _item_to_bond(item: dict, term_type: str):
    if not isinstance(item, dict):
        return None

    def g(*keys):
        for k in keys:
            if k in item and item[k] is not None:
                v = str(item[k]).strip()
                if v and v not in ["", "null", "None"]:
                    return v
        return "-"

    symbol = g("Symbol", "symbol", "ThaiBMASymbol")
    if symbol == "-":
        return None

    secure_code = g("SecureCode", "securedType", "SecuredType")
    if "unsecure" in secure_code.lower() or secure_code == "-":
        secured_label = "🔓 ไม่มีหลักประกัน"
    else:
        secured_label = "🔒 มีหลักประกัน"

    bond = {
        "symbol":           symbol,
        "term_type":        "Long Term" if term_type == "long" else "Short Term",
        "issue_date":       fmt_date(g("IssuedDate", "IssueDate", "issueDate")),
        "maturity_date":    fmt_date(g("MaturityDate", "maturityDate")),
        "tenor":            g("Term", "term", "tenor"),
        "coupon_rate":      "-",
        "issue_size":       fmt_number(g("IssueSize", "issueSize")),
        "outstanding_size": fmt_number(g("CurrentOutstanding", "IssueOutstanding", "outstanding")),
        "secured_type":     secure_code,
        "secured_label":    secured_label,
        "registrar":        g("Registrar", "registrar"),
        "bondholder_rep":   g("BondholderRepresentative", "bondholderRep"),
        "underwriters":     g("Underwriter", "underwriter"),
        "issue_rating":     g("IssueRating", "issueRating"),
        "issuer_rating":    g("CompanyRating", "issuerRating"),
        "distribution":     g("DistributionDisplay", "distribution"),
        "isin":             g("IssueLegacyID", "isinCode", "ISIN"),
    }

    issue_id = g("IssueID", "issueId", "id", "Id")
    if issue_id != "-":
        bond["detail_url"] = f"{BOND_INFO_URL}?symbol={issue_id}"

    return bond


# ─── STEP 2: Bond Detail Page ─────────────────────────────────────────────────

def fetch_bond_detail(detail_url: str, session: requests.Session) -> dict:
    """
    ดึงข้อมูลจากหน้า bond detail
    รองรับ multi-row tables เช่น Coupon Payment ที่ label/value อยู่คนละแถว
    """
    detail = {}
    if not detail_url:
        return detail
    try:
        logger.info(f"[detail] Fetching: {detail_url}")
        resp = session.get(detail_url, headers={**HEADERS, "Accept": "text/html,*/*"}, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            pending_label = None  # label ที่รอ value จากแถวถัดไป

            for row in rows:
                cells = row.find_all(["td", "th"])
                if not cells:
                    continue

                first_cell_text = cells[0].get_text(strip=True).lower()
                all_text = " ".join(c.get_text(strip=True) for c in cells)

                # ── กรณี: label อยู่ซ้าย value อยู่ขวา (2+ cells, same row) ──
                if len(cells) >= 2:
                    label = first_cell_text
                    value = cells[1].get_text(" ", strip=True)
                    _assign(detail, label, value)

                    # 4-cell row: 2 pairs
                    if len(cells) >= 4:
                        label2 = cells[2].get_text(strip=True).lower()
                        value2 = cells[3].get_text(" ", strip=True)
                        _assign(detail, label2, value2)

                # ── กรณีพิเศษ: "Coupon Payment" เป็น label ของ section ──
                # แถวนี้มี "coupon payment" และ sub-headers เช่น Reference/Max/Min/From/To
                if "coupon payment" in first_cell_text or "coupon payment" in all_text.lower():
                    pending_label = "coupon payment"
                    continue

                # ── ถ้ามี pending_label รอ value จากแถวนี้ ──
                if pending_label == "coupon payment" and detail.get("coupon_rate", "-") == "-":
                    # หา "Fixed: X.X%" ในแถวนี้
                    for cell in cells:
                        cell_text = cell.get_text(strip=True)
                        m = re.search(r"Fixed:\s*([\d.]+)\s*%?", cell_text, re.I)
                        if m:
                            detail["coupon_rate"] = m.group(1) + "%"
                            logger.info(f"[detail] coupon found (multi-row): {detail['coupon_rate']}")
                            pending_label = None
                            break
                        # FRN หรือ Floating rate
                        if any(k in cell_text.lower() for k in ["frn", "floating", "tbr", "mlr", "mor"]):
                            detail["coupon_rate"] = cell_text[:40]
                            pending_label = None
                            break

                # ── Underwriter(s): อาจอยู่หลายแถว ──
                if "underwriter" in first_cell_text:
                    # เก็บทุก cell ที่ไม่ใช่ label
                    uw_parts = [c.get_text(strip=True) for c in cells[1:] if c.get_text(strip=True)]
                    if uw_parts:
                        existing = detail.get("underwriters", "")
                        new_part = " / ".join(uw_parts)
                        detail["underwriters"] = (existing + " / " + new_part).strip(" /") if existing and existing != "-" else new_part

        # สร้าง secured label จาก detail
        st = detail.get("secured_type", "").lower()
        bt = detail.get("bond_type", "").lower()
        if "unsecure" in st or "unsecure" in bt:
            detail["secured_label"] = "🔓 ไม่มีหลักประกัน"
        elif "secure" in st or "fasset" in st or "secure" in bt:
            detail["secured_label"] = "🔒 มีหลักประกัน"

        logger.info(f"[detail] result: coupon={detail.get('coupon_rate','?')}, uw={detail.get('underwriters','?')[:50]}")

    except Exception as e:
        logger.exception(f"[detail] error: {e}")
    return detail


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def search_bonds_by_company(company_name: str) -> list:
    session = requests.Session()
    abbr = company_name.strip().upper()
    logger.info(f"[main] === Searching: '{abbr}' ===")
    bonds = fetch_bond_list(abbr, session)
    if not bonds:
        return []
    results = []
    for b in bonds[:15]:
        detail_url = b.get("detail_url", "")
        if detail_url:
            time.sleep(0.3)
            detail = fetch_bond_detail(detail_url, session)
            for k, v in detail.items():
                if k not in b or b[k] == "-":
                    b[k] = v
        results.append(b)
    logger.info(f"[main] Done: {len(results)} bonds")
    return results


# ─── FORMAT ───────────────────────────────────────────────────────────────────

def format_bond_message(bonds: list, company_name: str) -> str:
    if not bonds:
        return (
            f"❌ ไม่พบข้อมูลหุ้นกู้ของ \"{company_name}\"\n\n"
            "💡 ลองพิมพ์ชื่อย่อ:\n"
            "  เช่น PTT, CPALL, CI, ASW, KBANK\n\n"
            "📌 ข้อมูลจาก ThaiBMA"
        )
    long_bonds  = [b for b in bonds if "Long"  in b.get("term_type", "")]
    short_bonds = [b for b in bonds if "Short" in b.get("term_type", "")]
    lines = [f"📋 หุ้นกู้ {company_name.upper()} ({len(bonds)} รุ่น)", "─" * 28]

    def add_bonds(bond_list, label):
        if not bond_list:
            return
        lines.append(f"\n{label} ({len(bond_list)} รุ่น)")
        for b in bond_list:
            lines.extend([
                f"\n🔹 {b.get('symbol','-')}",
                f"  📅 ออก: {b.get('issue_date','-')}",
                f"  📅 ครบกำหนด: {b.get('maturity_date','-')}",
                f"  ⏳ อายุ: {b.get('tenor','-')}",
                f"  💰 ดอกเบี้ย: {b.get('coupon_rate','-')}",
                f"  💵 Outstanding: {b.get('outstanding_size', b.get('issue_size','-'))} ลบ.",
                f"  {b.get('secured_label','🔓 ไม่มีหลักประกัน')}",
                f"  📢 ขายให้: {b.get('distribution','-')}",
                f"  📊 Issue Rating: {b.get('issue_rating','-')}",
                f"  📊 Issuer Rating: {b.get('issuer_rating','-')}",
                f"  🏦 Registrar: {b.get('registrar','-')}",
                f"  👤 BH Rep: {b.get('bondholder_rep','-')}",
                f"  📢 UWs: {b.get('underwriters','-')}",
            ])

    add_bonds(long_bonds,  "📌 Long Term Debenture")
    add_bonds(short_bonds, "📌 Short Term Debenture")
    lines.extend(["", "─" * 28, "📌 ข้อมูลจาก ThaiBMA"])
    return "\n".join(lines)


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _assign(d: dict, label: str, value: str):
    v = value.strip()
    if not label or not v or v in ["-", "null"]:
        return
    if "symbol" in label and not d.get("symbol"):
        d["symbol"] = v.split()[0]
    elif "issue date" in label and "registration" not in label and not d.get("issue_date"):
        d["issue_date"] = fmt_date(v)
    elif "maturity date" in label and not d.get("maturity_date"):
        d["maturity_date"] = fmt_date(v)
    elif "issue term" in label:
        d["tenor"] = v.split("/")[0].strip()
    elif "coupon payment" in label:
        # same-row case
        m = re.search(r"Fixed:\s*([\d.]+)", v, re.I)
        if m:
            d["coupon_rate"] = m.group(1) + "%"
        elif any(k in v.lower() for k in ["frn", "floating", "tbr", "mlr", "mor"]):
            d["coupon_rate"] = v[:60]
    elif "bond type" in label:
        d["bond_type"] = v
    elif "secured type" in label or label == "collateral":
        d["secured_type"] = v
    elif "registrar" in label and "co-" not in label and not d.get("registrar"):
        d["registrar"] = v
    elif "debenture holder" in label or "bondholder rep" in label:
        d["bondholder_rep"] = v
    elif "underwriter" in label:
        existing = d.get("underwriters", "")
        if not existing or existing == "-":
            d["underwriters"] = v
        elif v not in existing:
            d["underwriters"] = existing + " / " + v
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
