"""Scheduled jobs: run (bid day), remind (mandates + allotment news), sell (listing day), iplog."""
import logging
from datetime import date, time, timedelta

import requests

from . import gmp as G
from .planner import bday_add, chance_from_sub, plan, score
from .state import now_ist

log = logging.getLogger(__name__)
DEAD = {"application_deleted", "ipo_not_allotted"}


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _rs(x) -> str:
    return f"₹{x:,.0f}"


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa
        log.exception("%s failed", getattr(fn, "__name__", fn))
        return None


class Context:
    def __init__(self, cfg, upx, tg, state, live: bool):
        self.cfg, self.upx, self.tg, self.state, self.live = cfg, upx, tg, state, live

    @property
    def s(self):
        return self.cfg["strategy"]


# ---------------------------------------------------------------- bid day
def build_universe(ctx: Context, today: date, horizon_days=7) -> list:
    """Open + upcoming main-board IPOs closing within the horizon, enriched with details and market data."""
    ipos = (_safe(ctx.upx.list_ipos, "open", "regular") or []) + (_safe(ctx.upx.list_ipos, "upcoming", "regular") or [])
    universe = [i for i in ipos if _d(i.get("bidding_end_date")) and today <= _d(i["bidding_end_date"]) <= today + timedelta(days=horizon_days)]
    ig = _safe(G.fetch_investorgain, today)
    iw = _safe(G.fetch_ipowatch, today)
    subs = _safe(G.fetch_subscription, today.year) or {"_rows": []}
    infos = []
    for i in universe:
        d = _safe(ctx.upx.ipo_details, i["id"]) or {}
        name = d.get("name") or i.get("name") or i["id"]
        price = d.get("cut_off_price") or d.get("maximum_price") or i.get("maximum_price")
        lot = d.get("lot_size") or d.get("minimum_quantity")
        tl = d.get("timeline") or {}
        close = _d(d.get("bidding_end_date") or i.get("bidding_end_date"))
        info = {
            "id": i["id"], "name": name, "symbol": (i.get("symbol") or d.get("symbol") or "").upper(), "isin": d.get("isin"),
            "price": float(price) if price else None, "lot": int(lot) if lot else None, "close": close,
            "allot": _d(tl.get("allotment_date")) or bday_add(close, 1),
            "refund": _d(tl.get("refund_initiation_date")) or bday_add(close, 2),
            "listing": _d(tl.get("listing_date")) or bday_add(close, 3),
            "daily_end_time": d.get("daily_end_time"), "status": i.get("status"), "is_today": close == today,
        }
        info["price_src"] = "upstox"
        if not (info["price"] and info["lot"]):
            # Upstox fills the price band in only a day or two before opening; borrow it from the GMP sites
            for rows in (ig or [], iw or []):
                m, _ = G.best_match(name, [r for r in rows if not r["is_sme"]])
                if m and m.get("price") and m.get("lot"):
                    info["price"], info["lot"], info["price_src"] = float(m["price"]), int(m["lot"]), m["source"]
                    break
        info["amount"] = (info["lot"] or 0) * (info["price"] or 0) * ctx.s.get("lots", 1)
        if ig is None and iw is None:
            info["gmp"] = {"gmp": None, "gmp_pct": None, "note": "GMP sources unreachable", "values": {}}
        else:
            info["gmp"] = G.gmp_for(name, info["price"], ig or [], iw or [], ctx.s["gmp_disagreement_pct"])
        info["gmp_pct"] = info["gmp"]["gmp_pct"]
        sub = subs.get(info["symbol"]) if info["symbol"] else None
        if not sub:
            sub, _ = G.best_match(name, subs.get("_rows", []))
        info["retail_sub"] = sub["retail"] if sub else info["gmp"].get("retail_sub_iw")
        try:
            info["total_sub"] = float(str(i.get("total_subscription") or "").rstrip("xX")) or None
        except ValueError:
            info["total_sub"] = None
        basis = info["retail_sub"] if info["retail_sub"] is not None else info["total_sub"]
        info["chance"] = chance_from_sub(basis, ctx.s["default_chance"])
        infos.append(info)
        ctx.state.append("gmp_history.jsonl", {"id": info["id"], "name": name, "symbol": info["symbol"], "close": close,
                                               "price": info["price"], "gmp": info["gmp"]["values"], "gmp_pct": info["gmp_pct"],
                                               "retail_sub": info["retail_sub"], "total_sub": info["total_sub"]})
    return infos


def committed_funds(ctx: Context, today: date, details_cache: dict) -> tuple:
    """Returns (applied_ids, locked) where locked = [(amount, release_date)] from Upstox + dry-run records."""
    orders = _safe(ctx.upx.ipo_orders) or []
    holdings = None
    applied_ids, locked = set(), []
    for o in orders:
        st = o.get("status") or ""
        if st in DEAD:
            continue
        applied_ids.add(o.get("id"))
        amt = float(o.get("upi_amount_blocked") or 0) or sum(float(b.get("amount") or 0) for b in o.get("bids") or [])
        od = details_cache.get(o.get("id")) or _safe(ctx.upx.ipo_details, o["id"]) or {}
        tl = od.get("timeline") or {}
        close = _d(od.get("bidding_end_date"))
        refund = _d(tl.get("refund_initiation_date")) or (bday_add(close, 2) if close else today + timedelta(days=3))
        if st == "ipo_allotted" or o.get("order_status") == "allotted":
            if holdings is None:
                holdings = _safe(ctx.upx.holdings) or []
            if not any(h.get("isin") == od.get("isin") and (h.get("quantity") or 0) > 0 for h in holdings):
                continue  # already sold: proceeds assumed back in the account
            locked.append((amt, date.max))
        else:
            if o.get("payment_status") != "mandate_accepted" and close and today > close:
                continue  # mandate never approved; application lapsed
            if today > refund:
                continue
            locked.append((amt, refund))
    if not ctx.live:  # make dry-run budgets behave like live ones
        for a in ctx.state.applications().values():
            if a.get("mode") == "dry-run" and a["id"] not in applied_ids and _d(a["refund"]) and _d(a["refund"]) >= today:
                locked.append((a["amount"], _d(a["refund"])))
                applied_ids.add(a["id"])
    return applied_ids, locked


def job_run(ctx: Context) -> str:
    s, today, now = ctx.s, now_ist().date(), now_ist()
    infos = build_universe(ctx, today)
    applied_ids, locked = committed_funds(ctx, today, {})
    locked_on = lambda d: sum(a for a, rel in locked if rel >= d)  # noqa: E731
    thr = s["threshold_pct"]
    cands = []
    for x in infos:
        if x["id"] in applied_ids:
            x["decision"] = "already applied"
        elif not x["price"] or not x["lot"]:
            x["decision"] = "price band not announced yet"
        elif x["is_today"] and x["price_src"] != "upstox":
            x["decision"] = "Upstox has no price for it yet, apply manually if you want it"
        elif x["gmp_pct"] is None:
            x["decision"] = f"no GMP ({x['gmp']['note']})"
        elif x["gmp_pct"] < thr:
            x["decision"] = f"below threshold ({x['gmp_pct']:.1f}%)"
        else:
            ch = x["chance"] if x["is_today"] else s["default_chance"]
            x["score"] = score(x["gmp_pct"], ch, x["is_today"], s)
            cands.append({"day": x["close"], "refund": x["refund"], "amount": x["amount"], "score": x["score"], "info": x})
    todays = [c for c in cands if c["day"] == today]
    picks = []
    if todays:
        horizon = max(c["refund"] for c in todays)
        window = [c for c in cands if c["day"] <= horizon]
        picks = plan(today, window, locked_on, s["budget"])
    picked = {c["info"]["id"] for c in picks}
    for c in todays:
        if c["info"]["id"] not in picked:
            c["info"]["decision"] = "skipped: budget reserved for better IPOs closing soon"
    too_late = now.time() > time.fromisoformat(s.get("apply_deadline", "16:30"))
    applied = []
    for c in picks:
        x = c["info"]
        if too_late:
            x["decision"] = "NOT applied: past apply deadline, apply manually"
            continue
        qty = x["lot"] * s.get("lots", 1)
        order_id = None
        if ctx.live:
            try:
                res = ctx.upx.apply_ipo(x["id"], ctx.cfg["secrets"]["UPSTOX_UPI"], qty, x["price"])
                order_id = (res or {}).get("order_id")
                x["decision"] = f"APPLIED (order {order_id})"
            except Exception as e:  # noqa
                log.exception("apply failed for %s", x["id"])
                x["decision"] = f"APPLY FAILED: {e}"
                continue
        else:
            x["decision"] = "DRY-RUN: would apply"
        ctx.state.save_application({"key": x["id"], "id": x["id"], "name": x["name"], "symbol": x["symbol"], "isin": x["isin"],
                                    "order_id": order_id, "amount": x["amount"], "qty": qty, "price": x["price"],
                                    "applied_on": today, "close": x["close"], "refund": x["refund"], "listing": x["listing"],
                                    "allot": x["allot"], "gmp_pct": x["gmp_pct"], "retail_sub": x["retail_sub"],
                                    "mode": "live" if ctx.live else "dry-run", "status": "applied"})
        applied.append(x)
    ctx.state.append("decisions.jsonl", {"today": today, "live": ctx.live, "locked": locked_on(today),
                                         "ipos": [{k: v for k, v in x.items() if k != "gmp"} | {"gmp": x["gmp"]["values"]} for x in infos]})
    msg = format_run(ctx, today, now, infos, applied, locked_on(today))
    ctx.tg.send(msg)
    return msg


def format_run(ctx, today, now, infos, applied, locked_today) -> str:
    s = ctx.s
    head = f"IPO bidder · {now.strftime('%a %d %b %H:%M')} · {'LIVE' if ctx.live else 'DRY-RUN'}\n"
    head += f"Budget {_rs(s['budget'])} · locked {_rs(locked_today)} · free {_rs(s['budget'] - locked_today)}"
    todays = [x for x in infos if x["is_today"]]
    later = [x for x in infos if not x["is_today"]]
    lines = [head]
    if applied:
        lines.append("\nAPPLIED — approve the UPI request by 5:00 PM:" if ctx.live else "\nWOULD APPLY (dry-run):")
        for x in applied:
            lines.append(f"• {x['name']} — GMP {x['gmp_pct']:.1f}% (₹{x['gmp']['gmp']:.0f}) · retail {_x(x['retail_sub'])} · "
                         f"{x['lot']} sh @ ₹{x['price']:g} = {_rs(x['amount'])} · {x['decision']}")
    others = [x for x in todays if x not in applied]
    if others:
        lines.append("\nClosing today, not applied:")
        for x in others:
            lines.append(f"• {x['name']} — {_gmp(x)} · retail {_x(x['retail_sub'])} · {x['decision']}")
    if not todays:
        lines.append("No main-board IPO closes today.")
    if later:
        lines.append("\nClosing later:")
        for x in sorted(later, key=lambda x: x["close"]):
            lot = f"{_rs(x['amount'])}/lot" if x["amount"] else "price band not announced"
            lines.append(f"• {x['name']} ({x['close'].strftime('%a %d')}) — {_gmp(x)} · {lot}")
    return "\n".join(lines)


def _x(v):
    return f"{v:.1f}x" if v is not None else "n/a"


def _gmp(x):
    if x["gmp_pct"] is None:
        vals = x["gmp"].get("values") or {}
        if vals and not x["price"]:
            return f"GMP ₹{min(vals.values()):g} (no price yet for %)"
        return f"GMP n/a ({x['gmp']['note']})" if x["gmp"].get("note") else "GMP n/a"
    note = f" ({x['gmp']['note']})" if x["gmp"].get("note") else ""
    return f"GMP {x['gmp_pct']:.1f}%{note}"


# ---------------------------------------------------------------- reminders
def job_remind(ctx: Context) -> str:
    today = now_ist().date()
    orders = _safe(ctx.upx.ipo_orders) or []
    pending = [o for o in orders if o.get("status") == "awaiting_mandate" and o.get("payment_status") != "mandate_accepted"]
    lines = []
    if pending:
        lines.append("REMINDER — UPI mandates still not approved (deadline 5:00 PM on the closing day):")
        for o in pending:
            amt = sum(float(b.get("amount") or 0) for b in o.get("bids") or [])
            lines.append(f"• {o.get('symbol') or o.get('id')} — {_rs(amt)} · order {o.get('order_id')}")
    # allotment news for our applications, once
    notified = ctx.state.read("notified.json", {})
    news = []
    for o in orders:
        key = f"{o.get('order_id')}:{o.get('status')}"
        if o.get("status") in ("ipo_allotted", "ipo_not_allotted") and key not in notified:
            notified[key] = str(today)
            if o["status"] == "ipo_allotted":
                news.append(f"• ALLOTTED {o.get('symbol')}: {o.get('units_allotted')} shares. Listing day sell is "
                            f"{'automatic' if ctx.cfg.get('sell', {}).get('enabled') else 'manual'}.")
            else:
                news.append(f"• Not allotted {o.get('symbol')}; {_rs(float(o.get('upi_amount_blocked') or 0))} will be released.")
    if news:
        lines.append("\nAllotment results:" if lines else "Allotment results:")
        lines.extend(news)
        ctx.state.write("notified.json", notified)
    msg = "\n".join(lines)
    if msg:
        ctx.tg.send(msg)
    return msg or "nothing to remind"


# ---------------------------------------------------------------- listing day
def job_sell(ctx: Context) -> str:
    sell_cfg = ctx.cfg.get("sell", {})
    today = now_ist().date()
    orders = [o for o in (_safe(ctx.upx.ipo_orders) or []) if o.get("status") == "ipo_allotted" or o.get("order_status") == "allotted"]
    if not orders:
        return "no allotments"
    holdings = _safe(ctx.upx.holdings) or []
    sold = ctx.state.read("sells.json", {})
    lines = []
    for o in orders:
        if o.get("order_id") in sold:
            continue
        d = _safe(ctx.upx.ipo_details, o["id"]) or {}
        listing = _d((d.get("timeline") or {}).get("listing_date"))
        if not listing or not (listing <= today <= listing + timedelta(days=3)):
            continue
        h = next((h for h in holdings if h.get("isin") == d.get("isin") and (h.get("quantity") or 0) > 0), None)
        name = d.get("name") or o.get("symbol")
        if not h:
            lines.append(f"• {name}: allotted but shares not in holdings yet; will retry.")
            continue
        qty = min(int(h["quantity"]), int(o.get("units_allotted") or h["quantity"]))
        issue = float(d.get("cut_off_price") or d.get("maximum_price") or 0)
        ltp = float(h.get("last_price") or 0)
        if sell_cfg.get("policy", "always") == "only_if_gain" and issue and ltp < issue:
            lines.append(f"• {name}: trading at ₹{ltp:g} below issue ₹{issue:g}; policy is only_if_gain, holding. Sell manually if you prefer.")
            continue
        if not (ctx.live and sell_cfg.get("enabled")):
            lines.append(f"• {name}: {qty} shares listed, LTP ₹{ltp:g} vs issue ₹{issue:g}. Auto-sell is off: sell manually in the Upstox app.")
            continue
        exp_ip = sell_cfg.get("expected_public_ip")
        if exp_ip:
            cur = public_ip()
            if cur != exp_ip:
                lines.append(f"• {name}: public IP is {cur}, not the registered {exp_ip}. Sell manually and update the IP at Upstox.")
                continue
        try:
            res = ctx.upx.sell_market(h["instrument_token"], qty)
            oid = (res or {}).get("order_ids") or (res or {}).get("order_id")
            sold[o["order_id"]] = {"date": str(today), "qty": qty, "sell_order": oid, "ltp_at_order": ltp, "issue": issue}
            ctx.state.write("sells.json", sold)
            est = (ltp - issue) * qty
            lines.append(f"• SOLD {name}: {qty} shares at market (LTP ₹{ltp:g}, issue ₹{issue:g}, est. gain {_rs(est)}). Order {oid}")
        except Exception as e:  # noqa
            log.exception("sell failed")
            lines.append(f"• {name}: SELL FAILED ({e}). Sell manually in the Upstox app.")
    msg = "\n".join(["Listing day:"] + lines) if lines else ""
    if msg:
        ctx.tg.send(msg)
    return msg or "nothing listing today"


# ---------------------------------------------------------------- public IP log
def public_ip() -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            return requests.get(url, timeout=10).text.strip()
        except requests.RequestException:
            continue
    return None


def job_iplog(ctx: Context) -> str:
    ip = public_ip()
    hist = ctx.state.read_jsonl("ip_log.jsonl")
    last = hist[-1]["ip"] if hist else None
    ctx.state.append("ip_log.jsonl", {"ip": ip})
    if last and ip and ip != last:
        ctx.tg.send(f"Public IP changed: {last} → {ip}. If auto-sell is registered with Upstox, update the static IP there.")
    return f"public ip {ip} (previous {last})"
