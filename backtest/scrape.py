"""Download raw pages for the IPO backtest dataset.

Usage: python3 -I scrape.py <out_dir>
Writes: <out_dir>/raw/perf_<year>.html, chg_<year>.json, ig_<slug>.html, iw_<slug>.html, manifest.json
"""
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

OUT = sys.argv[1]
RAW = os.path.join(OUT, "raw")
os.makedirs(RAW, exist_ok=True)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
DELAY = 0.7
YEARS = [2024, 2025, 2026]


def get(url, referer=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Referer": referer} if referer else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "ignore"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, "", url
    except Exception as e:  # noqa
        return 0, str(e), url
    finally:
        time.sleep(DELAY)


def clean(c):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", c))).strip()


def save(name, text):
    with open(os.path.join(RAW, name), "w", encoding="utf-8") as f:
        f.write(text)


manifest = {"ipos": [], "misses": [], "chg": {}}

# 1. investorgain performance tracker per year -> universe + slugs
for y in YEARS:
    code, body, _ = get(f"https://www.investorgain.com/report/ipo-gmp-performance-tracker/377/ipo/?year={y}")
    print("perf", y, code, len(body), flush=True)
    if code != 200:
        continue
    save(f"perf_{y}.html", body)
    tables = re.findall(r"<table.*?</table>", body, re.S)
    if not tables:
        continue
    for r in re.findall(r"<tr.*?</tr>", tables[0], re.S)[1:]:
        cells = [clean(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", r, re.S)]
        link = re.findall(r'href="(/gmp/([^/"]+)/(\d+)/)"', r)
        if not cells or not link or "SME" in cells[0]:
            continue
        manifest["ipos"].append({"year": y, "cells": cells, "ig_path": link[0][0], "slug": link[0][1], "ig_id": link[0][2]})

print("universe:", len(manifest["ipos"]), flush=True)

# 2. chittorgarh subscription API per year (paginated for past years)
for y in YEARS:
    fy = f"{y}-{str(y + 1)[2:]}"
    seen, recs = set(), []
    for page in range(1, 60):
        url = f"https://webnodejs.chittorgarh.com/cloud/report/data-read/21/{page}/11/{y}/{fy}/0/mainboard/0?search=&v=11-50"
        code, body, _ = get(url, referer="https://www.chittorgarh.com/")
        try:
            d = json.loads(body)
            rows = d.get("reportTableData") or []
        except Exception:
            rows = []
        new = [r for r in rows if r.get("~id") not in seen]
        if not new:
            break
        for r in new:
            seen.add(r.get("~id"))
        recs.extend(new)
        if d.get("totalPages", 1) <= page and d.get("totalRecords", 0) > 5:
            break
    print("chg", y, "records", len(recs), flush=True)
    save(f"chg_{y}.json", json.dumps(recs, ensure_ascii=False))
    manifest["chg"][y] = len(recs)

# 3. per-IPO pages: investorgain GMP page + ipowatch GMP page
for i, ipo in enumerate(manifest["ipos"]):
    slug = ipo["slug"]
    code, body, _ = get("https://www.investorgain.com" + ipo["ig_path"])
    ipo["ig_status"] = code
    if code == 200:
        save(f"ig_{slug}.html", body)
    url = f"https://ipowatch.in/{slug}-gmp/"
    code, body, final = get(url)
    if code != 200:
        # fall back to site search
        name = re.sub(r"\s*(IPO|Ltd\.?|Limited)\s*$", "", ipo["cells"][0]).strip()
        scode, sbody, _ = get("https://ipowatch.in/?s=" + urllib.parse.quote(name))
        cands = re.findall(r'href="(https://ipowatch\.in/[^"]*-ipo-gmp[^"]*/)"', sbody)
        if cands:
            url = cands[0]
            code, body, final = get(url)
    ipo["iw_status"] = code
    ipo["iw_url"] = final if code == 200 else None
    if code == 200:
        save(f"iw_{slug}.html", body)
    else:
        manifest["misses"].append(slug)
    if i % 20 == 0:
        print(f"{i}/{len(manifest['ipos'])} {slug} ig={ipo['ig_status']} iw={code}", flush=True)

with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, ensure_ascii=False, indent=1)
print("done. misses:", len(manifest["misses"]), manifest["misses"][:20], flush=True)
