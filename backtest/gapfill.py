"""Fill close-day GMP / lot gaps from ipocentral.in day-wise tables.

Usage: python3 -I gapfill.py <dataset_dir>
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse as P  # noqa: E402

DS = sys.argv[1]
RAW = os.path.join(DS, "raw")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "ignore"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, "", url
    except Exception as e:  # noqa
        return 0, str(e), url
    finally:
        time.sleep(1.0)


def parse_ipc(body):
    out = {"gmp_by_date": {}, "lot": None, "close_date": None}
    for t in P.tables(body):
        if not t:
            continue
        hdr = " ".join(t[0]).lower()
        if "day-wise" in hdr and "gmp" in hdr:
            for r in t[1:]:
                d = P.parse_long_date(r[0]) if r else None
                if d and len(r) > 1:
                    g = P.num(r[1])
                    if g is not None:
                        out["gmp_by_date"][d.isoformat()] = g
        for r in t:
            if len(r) >= 2:
                k = r[0].lower()
                if "lot size" in k or "minimum bid" in k:
                    m = re.search(r"([\d,]+)\s*shares", r[1], re.I)
                    if m:
                        out["lot"] = int(m.group(1).replace(",", ""))
                elif k.endswith("ipo dates") or k == "ipo dates":
                    m = re.search(r"(\d{1,2})\s*[–-]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", r[1])
                    if m:
                        out["close_date"] = date(int(m.group(4)), P.month_num(m.group(3)), int(m.group(2))).isoformat()
                elif "closing date" in k or k == "ipo close date":
                    d = P.parse_long_date(r[1])
                    if d:
                        out["close_date"] = d.isoformat()
    return out


rows = json.load(open(os.path.join(DS, "ipos.json")))
targets = [r for r in rows if (r.get("gmp_close_ig") is None and r.get("gmp_close_iw") is None) or r.get("lot_source") == "approx"]
print("targets:", len(targets), flush=True)
results = {}
for i, r in enumerate(targets):
    slug = r["slug"]
    cache = os.path.join(RAW, f"ipc_{slug}.html")
    body = ""
    if os.path.exists(cache):
        body = open(cache, encoding="utf-8").read()
    else:
        name = re.sub(r"\b(IPO|Ltd\.?|Limited|FPO|InvIT|REIT)\b", "", r["name"], flags=re.I).strip()
        words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", name)][:2]
        code, sbody, _ = get("https://ipocentral.in/?s=" + urllib.parse.quote(name + " IPO"))
        cands = re.findall(r'href="(https://ipocentral\.in/[^"#?]+/)"', sbody)
        cands = [c for c in cands if "ipo" in c and not re.search(r"/(category|tag|author|page|wp-|feed)/", c)]
        named = [c for c in cands if all(w in c for w in words)] or [c for c in cands if words and words[0] in c]
        gmp_first = [c for c in named if "gmp" in c] + [c for c in named if "gmp" not in c]
        for attempt, url in enumerate(gmp_first[:3]):
            code, body, final = get(url)
            if code != 200:
                body = ""
                continue
            if parse_ipc(body)["gmp_by_date"]:
                r["ipc_url"] = final
                break
            # not the GMP page: look for a name-matching GMP link on it
            inner = [l for l in re.findall(r'href="(https://ipocentral\.in/[^"#?]+/)"', body)
                     if "gmp" in l and all(w in l for w in words[:1])]
            if inner:
                code, body2, final2 = get(inner[0])
                if code == 200 and parse_ipc(body2)["gmp_by_date"]:
                    body, r["ipc_url"] = body2, final2
                    break
            body = ""
        if body:
            open(cache, "w", encoding="utf-8").write(body)
    if body:
        p = parse_ipc(body)
        results[slug] = p
        cd = r.get("close_date") or p["close_date"]
        if p["gmp_by_date"] and cd:
            ds = [d for d in p["gmp_by_date"] if d <= cd]
            if ds:
                r["gmp_close_ipc"] = p["gmp_by_date"][max(ds)]
                r["gmp_close_ipc_date"] = max(ds)
            for d, g in p["gmp_by_date"].items():
                r.setdefault("gmp_by_date", {}).setdefault(d, g)
        if p["lot"] and r.get("lot_source") == "approx":
            r["lot"] = p["lot"]; r["lot_source"] = "ipc"
    print(f"{i}/{len(targets)} {slug}: gmp={r.get('gmp_close_ipc')} lot={r.get('lot')} ({r.get('lot_source','iw')}) url={r.get('ipc_url')}", flush=True)

json.dump(rows, open(os.path.join(DS, "ipos.json"), "w"), indent=1, default=str)
json.dump(results, open(os.path.join(DS, "ipocentral.json"), "w"), indent=1)
still = [r["slug"] for r in rows if r.get("gmp_close_ig") is None and r.get("gmp_close_iw") is None and r.get("gmp_close_ipc") is None]
print("done. still missing GMP:", len(still), still, flush=True)
