"""Parse raw pages into dataset/ipos.json.

Usage: python3 -I parse.py <dataset_dir>            # full run
       python3 -I parse.py --sample <ig.html> <iw.html> <chg.json>   # smoke test
"""
import html
import json
import os
import re
import sys
from datetime import datetime, date, time

MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
MONTHS.update({"sept": 9})


def clean(c):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()


def tables(body):
    out = []
    for t in re.findall(r"<table.*?</table>", body, re.S):
        rows = [[clean(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
                for r in re.findall(r"<tr.*?</tr>", t, re.S)]
        out.append([r for r in rows if r])
    return out


def num(s):
    if s is None:
        return None
    m = re.search(r"-?[\d,]*\.?\d+", s.replace("₹", ""))
    return float(m.group(0).replace(",", "")) if m else None


def month_num(name):
    return MONTHS.get(name[:3].lower()) or MONTHS.get(name.lower())


def parse_long_date(s):
    """'September 9, 2026' or '9th Sep 2026' or '02-Nov-2026' -> date"""
    s = s.strip()
    m = re.match(r"([A-Za-z]+)\.? (\d{1,2}),? (\d{4})", s)
    if m:
        return date(int(m.group(3)), month_num(m.group(1)), int(m.group(2)))
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)? ([A-Za-z]+),? (\d{4})", s)
    if m:
        return date(int(m.group(3)), month_num(m.group(2)), int(m.group(1)))
    m = re.match(r"(\d{1,2})-([A-Za-z]+)-(\d{2,4})", s)
    if m:
        y = int(m.group(3)); y = y + 2000 if y < 100 else y
        return date(y, month_num(m.group(2)), int(m.group(1)))
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


# ---------- investorgain performance tracker row ----------
def parse_perf_cells(cells):
    name = cells[0]
    sym = cells[1].split(",")[0].strip() if len(cells) > 1 else ""
    bse = (cells[1].split(",")[1].strip() if len(cells) > 1 and "," in cells[1] else None)
    pct = lambda s: (num(re.search(r"\(([-\d.]+)%\)", s).group(1)) if re.search(r"\(([-\d.]+)%\)", s) else None)
    return {
        "name": name, "symbol": sym, "bse_code": bse,
        "listing_date": parse_long_date(cells[2]).isoformat() if parse_long_date(cells[2]) else None,
        "issue_size_cr": num(cells[3]), "total_sub_ig": num(cells[4]), "gmp_final": num(cells[5]),
        "issue_price": num(cells[6]),
        "listing_open": num(cells[8].split("(")[0]) if len(cells) > 8 else None,
        "listing_open_pct": pct(cells[8]) if len(cells) > 8 else None,
        "listing_close": num(cells[9].split("(")[0]) if len(cells) > 9 and cells[9] else None,
        "listing_close_pct": pct(cells[9]) if len(cells) > 9 else None,
        "ltp": num(cells[10].split("(")[0]) if len(cells) > 10 and cells[10] else None,
    }


# ---------- ipowatch per-IPO page ----------
def parse_iw(body):
    out = {}
    txt = clean(re.sub(r"<script.*?</script>|<style.*?</style>", "", body, flags=re.S))
    m = re.search(r"market lot of ([\d,]+) Shares", txt, re.I)
    out["lot"] = int(m.group(1).replace(",", "")) if m else None
    gmp_rows = []
    for t in tables(body):
        hdr = [h.lower() for h in t[0]]
        if any("date" in h for h in hdr) and any("gmp" in h for h in hdr) and len(t) > 1:
            for r in t[1:]:
                if len(r) >= 2 and re.match(r"\d{1,2} [A-Za-z]+", r[0]):
                    gmp_rows.append((r[0], num(r[1]), r[-1] if re.match(r"\d{1,2}:\d{2}", r[-1] or "") else None))
        for r in t:
            if len(r) >= 2:
                k = r[0].lower().rstrip(":")
                v = r[1]
                if k in ("ipo open date",):
                    out["open_date"] = parse_long_date(v)
                elif k in ("ipo close date",):
                    out["close_date"] = parse_long_date(v)
                elif k == "basis of allotment":
                    out["allot_date"] = parse_long_date(v)
                elif k == "refunds":
                    out["refund_date"] = parse_long_date(v)
                elif k == "ipo listing date":
                    out["listing_date_iw"] = parse_long_date(v)
                elif k == "ipo price band":
                    nums = re.findall(r"[\d,]+(?:\.\d+)?", v)
                    if nums:
                        out["price_low"] = float(nums[0].replace(",", ""))
                        out["price_high"] = float(nums[-1].replace(",", ""))
    out["gmp_rows"] = gmp_rows
    for k in ("open_date", "close_date", "allot_date", "refund_date", "listing_date_iw"):
        if isinstance(out.get(k), date):
            out[k] = out[k].isoformat()
    return out


def iw_gmp_by_date(gmp_rows, close_date):
    """ipowatch day-wise rows (dates lack year) -> {iso_date: gmp}."""
    if not close_date:
        return {}
    cd = date.fromisoformat(close_date)
    out = {}
    for d, g, t in gmp_rows:
        m = re.match(r"(\d{1,2}) ([A-Za-z]+)", d)
        if not m or g is None:
            continue
        mo = month_num(m.group(2))
        if not mo:
            continue
        yr = cd.year if mo <= cd.month + 1 else cd.year - 1
        try:
            dd = date(yr, mo, int(m.group(1)))
        except ValueError:
            continue
        out[dd.isoformat()] = g
    return out


def iw_gmp_on(gmp_rows, close_date):
    """GMP as of close date (latest row dated <= close date)."""
    by = iw_gmp_by_date(gmp_rows, close_date)
    cands = [d for d in by if d <= close_date]
    if not cands:
        return None, None
    d = max(cands)
    return by[d], d


# ---------- investorgain per-IPO page ----------
def parse_ig(body):
    out = {}
    txt = clean(re.sub(r"<script.*?</script>|<style.*?</style>", "", body, flags=re.S))
    m = re.search(r"Opened - (\d{1,2}(?:st|nd|rd|th)? [A-Za-z]+ \d{4})Closed - (\d{1,2}(?:st|nd|rd|th)? [A-Za-z]+ \d{4})(?:Listed - (\d{1,2}(?:st|nd|rd|th)? [A-Za-z]+ \d{4}))?", txt)
    if m:
        out["open_date"] = parse_long_date(m.group(1)).isoformat()
        out["close_date"] = parse_long_date(m.group(2)).isoformat()
        if m.group(3):
            out["listing_date"] = parse_long_date(m.group(3)).isoformat()
    log = []
    for t in tables(body):
        if len(t) > 1 and "time" in [h.lower() for h in t[0]]:
            for r in t[1:]:
                if len(r) >= 3:
                    log.append((num(r[1]), r[2]))
    out["log"] = log
    return out


def ig_gmp_at(log, close_date, cutoff=time(12, 30)):
    """Latest log entry at/before <close_date 12:30>. Entries like '11 Sept, 12:02 pm'."""
    if not close_date or not log:
        return None, None
    cd = date.fromisoformat(close_date)
    limit = datetime.combine(cd, cutoff)
    best = None
    for g, ts in log:
        m = re.match(r"(\d{1,2}) ([A-Za-z]+),? (\d{1,2}):(\d{2}) ?(am|pm)", ts, re.I)
        if not m or g is None:
            continue
        mo = month_num(m.group(2))
        if not mo:
            continue
        yr = cd.year if mo <= cd.month + 1 else cd.year - 1
        hh = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
        try:
            dt = datetime(yr, mo, int(m.group(1)), hh, int(m.group(4)))
        except ValueError:
            continue
        if dt <= limit and (best is None or dt > best[0]):
            best = (dt, g)
    return (best[1], best[0].isoformat()) if best else (None, None)


# ---------- chittorgarh ----------
def parse_chg(recs):
    out = {}
    for r in recs:
        rec = {
            "retail_sub": r.get("Retail (x)") or None, "total_sub": r.get("Total (x)") or None,
            "qib_sub": r.get("QIB (x)") or None, "nii_sub": r.get("NII (x)") or None,
            "close_date": r.get("~Issue_Close_Date"), "open_date": r.get("~Issue_Open_Date"),
            "applications": r.get("Applications"), "isin": r.get("~isin"),
            "name_chg": clean(r.get("Company", "")),
        }
        for v in (rec["retail_sub"], rec["total_sub"], rec["qib_sub"], rec["nii_sub"]):
            pass
        key_sym = (r.get("~nse_symbol") or "").strip().upper()
        if key_sym:
            out[("sym", key_sym)] = rec
        if r.get("~bse_script_code"):
            out[("bse", str(r["~bse_script_code"]))] = rec
    return out


def main_full(ds):
    man = json.load(open(os.path.join(ds, "manifest.json")))
    raw = os.path.join(ds, "raw")
    chg = {}
    for f in os.listdir(raw):
        if f.startswith("chg_") and f.endswith(".json"):
            chg.update(parse_chg(json.load(open(os.path.join(raw, f)))))
    rows, problems = [], []
    for ipo in man["ipos"]:
        rec = parse_perf_cells(ipo["cells"])
        rec["slug"] = ipo["slug"]; rec["year"] = ipo["year"]
        igp = os.path.join(raw, f"ig_{ipo['slug']}.html")
        iwp = os.path.join(raw, f"iw_{ipo['slug']}.html")
        ig_body = open(igp, encoding="utf-8").read() if os.path.exists(igp) else ""
        ig = parse_ig(ig_body) if ig_body else {}
        iw = {}
        if os.path.exists(iwp):
            body = open(iwp, encoding="utf-8").read()
            t = re.search(r"<title>(.*?)</title>", body, re.S)
            if t and "IPO GMP Today" in t.group(1):   # generic list page returned by site search
                rec["iw_bad"] = True
            else:
                iw = parse_iw(body)
        # lot fallback: investorgain daily table has "Est. Profit per lot" = GMP x lot
        if not iw.get("lot") and ig_body:
            for t in tables(ig_body):
                if t and t[0] and "GMP Date" in t[0][0]:
                    for r in t[1:4]:
                        if len(r) >= 4 and num(r[1]) and num(r[3]):
                            lot = round(num(r[3]) / num(r[1]))
                            if 1 <= lot <= 5000:
                                iw["lot"] = lot; rec["lot_source"] = "ig_profit"
                                break
                    break
        if not iw.get("lot") and rec.get("issue_price"):
            iw["lot"] = int(-(-14000 // rec["issue_price"]))   # SEBI min application ~Rs 14-15k
            rec["lot_source"] = "approx"
        rec["open_date"] = iw.get("open_date") or ig.get("open_date")
        rec["close_date"] = iw.get("close_date") or ig.get("close_date")
        rec["allot_date"] = iw.get("allot_date"); rec["refund_date"] = iw.get("refund_date")
        rec["lot"] = iw.get("lot")
        rec["price_low"] = iw.get("price_low"); rec["price_high"] = iw.get("price_high")
        rec["gmp_close_iw"], rec["gmp_close_iw_date"] = iw_gmp_on(iw.get("gmp_rows", []), rec["close_date"])
        rec["gmp_by_date"] = iw_gmp_by_date(iw.get("gmp_rows", []), rec["close_date"])
        rec["gmp_close_ig"], rec["gmp_close_ig_ts"] = ig_gmp_at(ig.get("log", []), rec["close_date"])
        rec["ig_log_entries"] = len(ig.get("log", []))
        c = chg.get(("sym", rec["symbol"].upper())) or chg.get(("bse", rec["bse_code"] or ""))
        if c:
            rec.update({k: c[k] for k in ("retail_sub", "total_sub", "qib_sub", "nii_sub", "applications", "isin", "name_chg")})
            rec["close_date"] = rec["close_date"] or c["close_date"]
        missing = [k for k in ("close_date", "lot", "issue_price", "gmp_close_iw", "retail_sub", "listing_open") if rec.get(k) in (None, "")]
        if missing:
            problems.append((rec["slug"], missing))
        rows.append(rec)
    json.dump(rows, open(os.path.join(ds, "ipos.json"), "w"), indent=1, default=str)
    print("rows", len(rows), "with problems", len(problems))
    for p in problems[:40]:
        print("  ", p)


if __name__ == "__main__":
    if sys.argv[1] == "--sample":
        ig = parse_ig(open(sys.argv[2], encoding="utf-8").read())
        iw = parse_iw(open(sys.argv[3], encoding="utf-8").read())
        print("IG:", {k: v for k, v in ig.items() if k != "log"}, "log entries", len(ig["log"]))
        print("IW:", {k: v for k, v in iw.items() if k != "gmp_rows"}, "gmp rows", len(iw["gmp_rows"]), iw["gmp_rows"][:3])
        cd = iw.get("close_date") or ig.get("close_date")
        print("close", cd, "iw gmp on close:", iw_gmp_on(iw["gmp_rows"], cd), "ig gmp @12:30:", ig_gmp_at(ig["log"], cd))
        cj = json.load(open(sys.argv[4]))
        chg = parse_chg(cj["reportTableData"] if isinstance(cj, dict) else cj)
        print("CHG keys", len(chg), list(chg.items())[0])
    else:
        main_full(sys.argv[1])
