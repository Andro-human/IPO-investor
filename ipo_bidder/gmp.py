"""Grey-market premium and subscription data from public sites.

Sources:
- investorgain.com live GMP table (primary)
- ipowatch.in GMP table (second source)
- chittorgarh.com subscription JSON API (retail subscription by NSE symbol)
"""
import html
import logging
import re
from datetime import date

import requests

log = logging.getLogger(__name__)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
INVESTORGAIN = "https://www.investorgain.com/report/live-ipo-gmp/331/ipo/"
IPOWATCH = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
CHITTORGARH = ("https://webnodejs.chittorgarh.com/cloud/report/data-read/21/{page}/11/{year}/{fy}/0/mainboard/0"
               "?search=&v=11-50")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS["sept"] = 9
STOP = {"ipo", "ltd", "limited", "india", "bse", "nse", "sme", "listed", "closed", "open", "upcoming", "the",
        "and", "of", "co", "company", "pvt", "private", "fpo", "l", "c", "o", "u"}


def _get(url: str, **kw) -> str:
    r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://www.google.com/", **kw.pop("headers", {})},
                     timeout=30, **kw)
    r.raise_for_status()
    return r.text


def clean(c: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()


def tables(body: str) -> list:
    out = []
    for t in re.findall(r"<table.*?</table>", body, re.S):
        rows = []
        for r in re.findall(r"<tr.*?</tr>", t, re.S):
            cells = [clean(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
            links = re.findall(r'href="([^"]+)"', r)
            if cells:
                rows.append((cells, links))
        out.append(rows)
    return out


def num(s):
    if s is None:
        return None
    m = re.search(r"-?[\d,]*\.?\d+", s.replace("₹", "").replace("--", ""))
    return float(m.group(0).replace(",", "")) if m else None


def normalize(name: str) -> set:
    toks = re.findall(r"[a-z0-9]+", name.lower().replace("&", " and "))
    return {t for t in toks if t not in STOP}


def similarity(a: str, b: str) -> float:
    ta, tb = normalize(a), normalize(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb)) * (0.5 + 0.5 * inter / len(ta | tb))


def best_match(name: str, candidates: list, key=lambda c: c["name"], threshold=0.6):
    best, score = None, 0.0
    for c in candidates:
        s = similarity(name, key(c))
        if s > score:
            best, score = c, s
    return (best, score) if score >= threshold else (None, score)


def clean_name(name: str) -> str:
    """'Bombay Coated and Special Steels IPO [email protected] (10.98%)' -> 'Bombay Coated and Special Steels'."""
    name = re.split(r"\s+(?:IPO\b|BSE SME|NSE SME|\[email|L@|LP@|LN@|\(\d)", name)[0]
    name = re.sub(r"\s+(?:[A-Z]|Allotted|Listed|Closed|Open|Upcoming)\s*$", "", name)
    return name.strip(" -–")


def _date_no_year(s: str, ref: date) -> date | None:
    """'5-Nov', '4-Nov GMP: 1', '29-2 Nov' (take the last day) -> date near ref."""
    m = re.search(r"\d{1,2}\s*[-–]\s*(\d{1,2})\s+([A-Za-z]{3,})", s)
    if not m:
        m = re.search(r"(\d{1,2})\s*[-–]\s*([A-Za-z]{3,})", s)
    if not m:
        m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,})", s)
    if not m:
        return None
    mo = MONTHS.get(m.group(2)[:3].lower())
    if not mo:
        return None
    d = int(m.group(1))
    for yr in (ref.year, ref.year + 1, ref.year - 1):
        try:
            cand = date(yr, mo, d)
        except ValueError:
            continue
        if abs((cand - ref).days) <= 120:
            return cand
    return None


# ---------------- investorgain ----------------
def fetch_investorgain(ref: date | None = None) -> list:
    ref = ref or date.today()
    body = _get(INVESTORGAIN)
    out = []
    for rows in tables(body):
        if not rows or "GMP" not in " ".join(rows[0][0]):
            continue
        hdr = [h.lower() for h in rows[0][0]]
        idx = {k: next((i for i, h in enumerate(hdr) if h.startswith(k)), None)
               for k in ("name", "gmp", "sub", "price", "lot", "open", "close", "listing", "updated")}
        for cells, links in rows[1:]:
            if len(cells) < 5 or idx["name"] is None:
                continue
            name = cells[idx["name"]]
            gmp_cell = cells[idx["gmp"]] if idx["gmp"] is not None else ""
            m = re.match(r"₹\s*(-?[\d.,]+|--|-)\s*\(([-\d.]*)%\)", gmp_cell)
            gmp = num(m.group(1)) if m and m.group(1) not in ("--", "-") else None
            try:
                pct = float(m.group(2)) if m else None
            except ValueError:
                pct = None
            out.append({
                "source": "investorgain", "name": clean_name(name), "raw_name": name,
                "is_sme": bool(re.search(r"\bSME\b", name)),
                "gmp": gmp, "gmp_pct_site": pct,
                "sub": num(cells[idx["sub"]]) if idx["sub"] is not None else None,
                "price": num(cells[idx["price"]]) if idx["price"] is not None else None,
                "lot": int(num(cells[idx["lot"]]) or 0) or None if idx["lot"] is not None else None,
                "close_date": _date_no_year(cells[idx["close"]], ref) if idx["close"] is not None else None,
                "updated": cells[idx["updated"]] if idx["updated"] is not None else None,
                "url": next((l for l in links if "/gmp/" in l), None),
            })
    log.info("investorgain: %d rows", len(out))
    return out


# ---------------- ipowatch ----------------
IPOWATCH_LIST = "https://ipowatch.in/server/api/ipo/GetIpoDataList"


def _signed(cell: str):
    """'+ ₹ 9' -> 9.0, '- ₹ 5' -> -5.0, '₹0' -> 0.0, '₹-' -> None"""
    v = num(cell)
    if v is None:
        return None
    return -abs(v) if re.match(r"\s*-", cell) else v


def fetch_ipowatch(ref: date | None = None) -> list:
    """Main-board IPOs from ipowatch: list endpoint -> one detail page per IPO."""
    import json
    import time as _t
    ref = ref or date.today()
    r = requests.get(IPOWATCH_LIST, headers={"User-Agent": UA, "Referer": IPOWATCH, "Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    result = (r.json().get("result") or [{}])[0]
    data = json.loads(result.get("JsonData") or "{}")
    slugs = {}
    for key in ("Mainline", "Mainboard", "Upcoming", "Open", "Current"):
        for it in data.get(key) or []:
            if it.get("slug"):
                slugs[it["slug"]] = it.get("ipo")
    out = []
    for slug, name in slugs.items():
        try:
            body = _get(f"https://ipowatch.in/{slug}/")
        except requests.RequestException as e:
            log.warning("ipowatch detail %s failed: %s", slug, e)
            continue
        rec = {"source": "ipowatch", "name": name or slug, "is_sme": False, "gmp": None, "price": None, "lot": None,
               "close_date": None, "retail_sub": None, "updated": None, "url": f"https://ipowatch.in/{slug}/"}
        for rows in tables(body):
            hdr = " ".join(rows[0][0]).lower()
            if "gmp" in hdr and "date" in hdr and len(rows) > 1:
                cells = rows[1][0]
                rec["gmp"], rec["updated"] = _signed(cells[1]), cells[0]
            elif "subscription" in hdr and "category" in hdr:
                for cells, _ in rows[1:]:
                    if cells and "retail" in cells[0].lower() and len(cells) > 1:
                        rec["retail_sub"] = num(cells[1])
            for cells, _ in rows:
                if len(cells) >= 2:
                    k = cells[0].lower()
                    if k == "price band":
                        rec["price"] = num(cells[1].split("-")[-1])
                    elif k == "lot size":
                        rec["lot"] = int(num(cells[1]) or 0) or None
                    elif k == "ipo date":
                        m = re.search(r"to\s+(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4})", cells[1])
                        if m:
                            d = re.match(r"(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})", m.group(1))
                            mo = MONTHS.get(d.group(2)[:3].lower())
                            if mo:
                                rec["close_date"] = date(int(d.group(3)), mo, int(d.group(1)))
                    elif k == "ipo type" or k == "issue type":
                        rec["is_sme"] = "sme" in cells[1].lower() or "emerge" in cells[1].lower()
        out.append(rec)
        _t.sleep(0.5)
    log.info("ipowatch: %d rows", len(out))
    return out


# ---------------- chittorgarh subscription ----------------
def fetch_subscription(year: int | None = None) -> dict:
    """Returns {NSE_SYMBOL: {...}} plus a list under key '_rows' for name matching."""
    year = year or date.today().year
    fy = f"{year}-{str(year + 1)[2:]}" if date.today().month >= 4 else f"{year - 1}-{str(year)[2:]}"
    # the API ignores FY for current-year queries in practice; try the natural one first
    recs, seen = [], set()
    for page in range(1, 40):
        try:
            j = requests.get(CHITTORGARH.format(page=page, year=year, fy=fy),
                             headers={"User-Agent": UA, "Referer": "https://www.chittorgarh.com/"}, timeout=30).json()
        except Exception as e:  # noqa
            log.warning("chittorgarh fetch failed: %s", e)
            break
        rows = j.get("reportTableData") or []
        new = [r for r in rows if r.get("~id") not in seen]
        if not new:
            break
        recs.extend(new)
        seen.update(r.get("~id") for r in new)
        if j.get("totalRecords", 0) > 5:
            break
    out = {"_rows": []}
    for r in recs:
        rec = {"name": clean(r.get("Company", "")), "symbol": (r.get("~nse_symbol") or "").upper(),
               "retail": _f(r.get("Retail (x)")), "total": _f(r.get("Total (x)")), "qib": _f(r.get("QIB (x)")),
               "nii": _f(r.get("NII (x)")), "close_date": r.get("~Issue_Close_Date"),
               "as_on": r.get("Subscription as on"), "isin": r.get("~isin")}
        out["_rows"].append(rec)
        if rec["symbol"]:
            out[rec["symbol"]] = rec
    log.info("chittorgarh: %d subscription rows", len(recs))
    return out


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------- combine ----------------
def gmp_for(name: str, price: float, ig_rows: list, iw_rows: list, max_disagree_pct: float) -> dict:
    """Combine the two GMP sources for one IPO. Returns gmp (rupees), gmp_pct, sources, note."""
    ig, s1 = best_match(name, [r for r in ig_rows if not r["is_sme"]])
    iw, s2 = best_match(name, [r for r in iw_rows if not r["is_sme"]])
    vals = {}
    if ig and ig["gmp"] is not None:
        vals["investorgain"] = ig["gmp"]
    if iw and iw["gmp"] is not None:
        vals["ipowatch"] = iw["gmp"]
    res = {"matches": {"investorgain": ig["name"] if ig else None, "ipowatch": iw["name"] if iw else None},
           "values": vals, "gmp": None, "gmp_pct": None, "note": "",
           "retail_sub_iw": iw.get("retail_sub") if iw else None, "lot_iw": iw.get("lot") if iw else None}
    if not vals:
        res["note"] = "no GMP quote found"
        return res
    if len(vals) == 2 and price:
        a, b = vals["investorgain"], vals["ipowatch"]
        if abs(a - b) / price * 100 > max_disagree_pct:
            res["note"] = f"sources disagree ({a:.0f} vs {b:.0f})"
            return res
    res["gmp"] = min(vals.values())
    res["gmp_pct"] = round(100 * res["gmp"] / price, 2) if price else None
    if not price:
        res["note"] = "no price yet"
        return res
    if len(vals) == 1:
        res["note"] = f"single source ({next(iter(vals))})"
    return res
