"""Backtest of the IPO auto-bidder decision logic on historical main-board IPOs.

Usage: python3 -I backtest.py <dataset_dir>/ipos.json [--json out.json]
"""
import itertools
import json
import random
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

DEFAULT = dict(
    budget=105_000,
    threshold=12.0,          # auto-apply if GMP% >= this
    penalty_below=15.0,      # GMP% below this gets score * penalty
    penalty=0.6,
    future_discount=0.85,    # score multiplier for IPOs not closing today
    default_chance=0.20,     # allotment chance assumed when subscription unknown
    seen_sub_fraction=0.6,   # retail sub visible at 12:30 PM on close day, as fraction of final
    lookahead=True,
    rank="score",            # "score" or "gmp"
    sell_cost_fixed=40.0,    # brokerage + DP charge per sell
    sell_cost_pct=0.0012,    # STT + GST + txn charges approx on sell value
    lots=1,
)


def bday_add(d, n):
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def load(path):
    rows = json.load(open(path))
    ipos = []
    for r in rows:
        if re.search(r"reit|invit|fpo|-scheme-", r["slug"]) or re.search(r"\b(REIT|InvIT|FPO)\b", r["name"]):
            continue  # not an equity IPO; huge lots, outside the strategy
        if not (r.get("close_date") and r.get("lot") and r.get("issue_price") and r.get("listing_open") is not None):
            continue
        gmp, src = None, None
        for k in ("gmp_close_ig", "gmp_close_iw", "gmp_close_ipc"):
            if r.get(k) is not None:
                gmp, src = r[k], k.split("_")[-1]
                break
        if gmp is None:
            continue
        cd = date.fromisoformat(r["close_date"])
        ld = date.fromisoformat(r["listing_date"]) if r.get("listing_date") else bday_add(cd, 3)
        rd = date.fromisoformat(r["refund_date"]) if r.get("refund_date") else bday_add(cd, 2)
        ad = date.fromisoformat(r["allot_date"]) if r.get("allot_date") else bday_add(cd, 1)
        ipos.append(dict(
            slug=r["slug"], name=r["name"], year=r["year"], close=cd, allot=ad, refund=rd, listing=ld,
            price=float(r["issue_price"]), lot=int(r["lot"]), gmp=float(gmp),
            gmp_pct=100.0 * float(gmp) / float(r["issue_price"]),
            gmp_by_date={date.fromisoformat(k): v for k, v in (r.get("gmp_by_date") or {}).items()},
            retail_sub=float(r["retail_sub"]) if r.get("retail_sub") else None,
            listing_open=float(r["listing_open"]), listing_close=r.get("listing_close"),
            gmp_final=r.get("gmp_final"), gmp_src=src, lot_src=r.get("lot_source", "iw"),
        ))
    ipos.sort(key=lambda x: x["close"])
    return ipos


def gmp_seen_on(ipo, d):
    """GMP of `ipo` as visible on day d (latest quote dated <= d)."""
    if d >= ipo["close"]:
        return ipo["gmp"]
    cands = [k for k in ipo["gmp_by_date"] if k <= d]
    return ipo["gmp_by_date"][max(cands)] if cands else None


def score(ipo, gmp_pct, chance, today, cfg):
    s = gmp_pct * chance if cfg["rank"] == "score" else gmp_pct
    if gmp_pct < cfg["penalty_below"]:
        s *= cfg["penalty"]
    if ipo["close"] != today:
        s *= cfg["future_discount"]
    return s


class Sim:
    def __init__(self, ipos, cfg, rng):
        self.ipos, self.cfg, self.rng = ipos, cfg, rng
        self.active = []   # dict(ipo, amount, release, allotted, pnl)
        self.log = []      # decisions
        self.skips = []
        self.peak = 0.0    # peak capital locked at any point

    def locked_on(self, d):
        # funds are usable only from the business day AFTER the refund/credit date
        return sum(a["amount"] for a in self.active if a["release"] >= d)

    def chance_today(self, ipo):
        if ipo["retail_sub"] is None:
            return self.cfg["default_chance"]
        seen = ipo["retail_sub"] * self.cfg["seen_sub_fraction"]
        return min(1.0, 1.0 / seen) if seen > 0 else 1.0

    def plan(self, today, closers, upcoming_all):
        cfg = self.cfg
        cands = []
        for ipo in closers:
            if ipo["gmp_pct"] >= cfg["threshold"]:
                cands.append(dict(ipo=ipo, day=today, amount=ipo["lot"] * ipo["price"] * cfg["lots"],
                                  s=score(ipo, ipo["gmp_pct"], self.chance_today(ipo), today, cfg),
                                  gmp_pct=ipo["gmp_pct"]))
        if not cands:
            return [], []
        horizon = max(c["ipo"]["refund"] for c in cands)
        if cfg["lookahead"]:
            for ipo in upcoming_all:
                if today < ipo["close"] <= horizon:
                    g = gmp_seen_on(ipo, today)
                    if g is None:
                        continue
                    gp = 100.0 * g / ipo["price"]
                    if gp >= cfg["threshold"]:
                        cands.append(dict(ipo=ipo, day=ipo["close"], amount=ipo["lot"] * ipo["price"] * cfg["lots"],
                                          s=score(ipo, gp, cfg["default_chance"], today, cfg), gmp_pct=gp))
        cands = sorted(cands, key=lambda c: -c["s"])[:12]
        best, best_s = [], -1.0
        days = sorted({c["day"] for c in cands})
        for r in range(1, len(cands) + 1):
            for combo in itertools.combinations(cands, r):
                ok = True
                for d in days:
                    need = sum(c["amount"] for c in combo if c["day"] <= d and c["ipo"]["refund"] >= d)
                    if need + self.locked_on(d) > cfg["budget"] + 1e-6:
                        ok = False
                        break
                if ok:
                    tot = sum(c["s"] for c in combo)
                    if tot > best_s:
                        best, best_s = list(combo), tot
        picks = [c for c in best if c["day"] == today]
        return picks, cands

    def run(self):
        by_close = defaultdict(list)
        for ipo in self.ipos:
            by_close[ipo["close"]].append(ipo)
        for today in sorted(by_close):
            closers = by_close[today]
            picks, cands = self.plan(today, closers, self.ipos)
            chosen = {c["ipo"]["slug"] for c in picks}
            for c in cands:
                if c["day"] == today and c["ipo"]["slug"] not in chosen:
                    self.skips.append(dict(day=today, slug=c["ipo"]["slug"], gmp_pct=c["gmp_pct"], reason="budget/lookahead"))
            for c in picks:
                ipo = c["ipo"]
                p = min(1.0, 1.0 / ipo["retail_sub"]) if ipo["retail_sub"] else self.cfg["default_chance"]
                allotted = self.rng.random() < p
                if allotted:
                    sale = ipo["lot"] * self.cfg["lots"] * ipo["listing_open"]
                    pnl = sale - c["amount"] - self.cfg["sell_cost_fixed"] - self.cfg["sell_cost_pct"] * sale
                    release = bday_add(ipo["listing"], 1)
                else:
                    pnl, release = 0.0, ipo["refund"]
                self.active.append(dict(ipo=ipo, amount=c["amount"], release=release, allotted=allotted, pnl=pnl,
                                        p=p, day=today, gmp_pct=c["gmp_pct"]))
            self.peak = max(self.peak, self.locked_on(today))
        return self


def summarize(sims, ipos, cfg):
    """sims: list of Sim (Monte Carlo runs)."""
    apps = [a for a in sims[0].active]  # decisions identical across seeds? no: allotment affects budget. use per-sim stats
    years = sorted({i["year"] for i in ipos})
    out = dict(cfg=cfg, years=years, n_ipos=len(ipos), runs=len(sims))
    pnl = [sum(a["pnl"] for a in s.active) for s in sims]
    napps = [len(s.active) for s in sims]
    nallot = [sum(1 for a in s.active if a["allotted"]) for s in sims]
    ev = [sum(a["p"] * (a["ipo"]["lot"] * cfg["lots"] * (a["ipo"]["listing_open"] - a["ipo"]["price"]) - cfg["sell_cost_fixed"]) for a in s.active) for s in sims]
    out.update(
        applications=statistics.mean(napps), allotments=statistics.mean(nallot),
        pnl_mean=statistics.mean(pnl), pnl_p5=sorted(pnl)[len(pnl) // 20], pnl_p50=statistics.median(pnl),
        pnl_p95=sorted(pnl)[-max(1, len(pnl) // 20)], pnl_min=min(pnl), pnl_max=max(pnl), ev_mean=statistics.mean(ev),
    )
    # quality of picks from first sim
    s0 = sims[0]
    picked = [a["ipo"] for a in s0.active]
    if picked:
        out["hit_rate"] = sum(1 for i in picked if i["listing_open"] > i["price"]) / len(picked)
        out["avg_listing_gain_pct"] = statistics.mean(100 * (i["listing_open"] - i["price"]) / i["price"] for i in picked)
        out["avg_gmp_pct_picked"] = statistics.mean(a["gmp_pct"] for a in s0.active)
        out["avg_chance"] = statistics.mean(a["p"] for a in s0.active)
    out["skipped_for_budget"] = statistics.mean(len(s.skips) for s in sims)
    # per-year pnl
    per_year = defaultdict(list)
    for s in sims:
        y = defaultdict(float)
        for a in s.active:
            y[a["ipo"]["year"]] += a["pnl"]
        for yr in years:
            per_year[yr].append(y[yr])
    out["pnl_by_year"] = {yr: statistics.mean(v) for yr, v in per_year.items()}
    return out


def run_cfg(ipos, cfg, runs=300, seed=1):
    sims = [Sim(ipos, cfg, random.Random(seed + k)).run() for k in range(runs)]
    return summarize(sims, ipos, cfg), sims


def fmt(r):
    return (f"apps={r['applications']:.1f} allot={r['allotments']:.1f} hit={r.get('hit_rate', 0):.0%} "
            f"avgGain={r.get('avg_listing_gain_pct', 0):.1f}% avgGMP={r.get('avg_gmp_pct_picked', 0):.1f}% "
            f"chance={r.get('avg_chance', 0):.0%} skipped={r['skipped_for_budget']:.1f} | "
            f"P&L mean=₹{r['pnl_mean']:,.0f} p5=₹{r['pnl_p5']:,.0f} p50=₹{r['pnl_p50']:,.0f} p95=₹{r['pnl_p95']:,.0f} "
            f"| by year " + ", ".join(f"{y}:₹{v:,.0f}" for y, v in sorted(r['pnl_by_year'].items())))


if __name__ == "__main__":
    ipos = load(sys.argv[1])
    print(f"loaded {len(ipos)} IPOs with complete data; years:", sorted({i['year'] for i in ipos}))
    base = dict(DEFAULT)
    variants = [
        ("baseline: 12% auto, penalty<15, lookahead, score rank", base),
        ("threshold 15%", {**base, "threshold": 15.0}),
        ("threshold 20%", {**base, "threshold": 20.0}),
        ("threshold 10%", {**base, "threshold": 10.0}),
        ("no penalty below 15", {**base, "penalty": 1.0}),
        ("no lookahead", {**base, "lookahead": False}),
        ("rank by plain GMP%", {**base, "rank": "gmp"}),
        ("budget 75k", {**base, "budget": 75_000}),
        ("budget 150k", {**base, "budget": 150_000}),
        ("unlimited budget (what selectivity alone gives)", {**base, "budget": 10_000_000}),
        ("apply to everything, unlimited budget", {**base, "threshold": 0.0, "budget": 10_000_000}),
    ]
    results = {}
    for name, cfg in variants:
        r, _ = run_cfg(ipos, cfg)
        results[name] = r
        print(f"\n## {name}\n   {fmt(r)}")
    if "--json" in sys.argv:
        json.dump(results, open(sys.argv[sys.argv.index("--json") + 1], "w"), indent=1, default=str)
    if "--csv" in sys.argv:
        # per-IPO decision log for the baseline config (expected values, no allotment randomness)
        import csv
        s = Sim(ipos, base, random.Random(0)).run()
        applied = {a["ipo"]["slug"]: a for a in s.active}
        skipped = {k["slug"]: k for k in s.skips}
        with open(sys.argv[sys.argv.index("--csv") + 1], "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["close_date", "name", "year", "issue_price", "lot", "amount", "gmp_close", "gmp_pct", "gmp_src",
                        "retail_sub_x", "allot_chance", "listing_open", "listing_gain_pct", "decision",
                        "ev_rupees", "lot_src"])
            for i in ipos:
                p = min(1.0, 1.0 / i["retail_sub"]) if i["retail_sub"] else base["default_chance"]
                gain = i["lot"] * (i["listing_open"] - i["price"])
                dec = "APPLIED" if i["slug"] in applied else ("SKIPPED(budget)" if i["slug"] in skipped else
                                                              ("below threshold" if i["gmp_pct"] < base["threshold"] else "-"))
                w.writerow([i["close"], i["name"], i["year"], i["price"], i["lot"], round(i["lot"] * i["price"]),
                            i["gmp"], round(i["gmp_pct"], 1), i["gmp_src"], i["retail_sub"], round(p, 3),
                            i["listing_open"], round(100 * (i["listing_open"] - i["price"]) / i["price"], 1), dec,
                            round(p * gain - base["sell_cost_fixed"]) if dec == "APPLIED" else "", i["lot_src"]])
