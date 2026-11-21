"""CLI entry point: python -m ipo_bidder <command>"""
import argparse
import logging
import sys
from datetime import date

from . import gmp as G
from .config import load_config, missing_secrets
from .jobs import Context, job_iplog, job_remind, job_run, job_sell, public_ip
from .notify import Telegram
from .state import State, now_ist
from .upstox import Upstox


def make_ctx(cfg, live_override=None) -> Context:
    state = State(cfg["_state_dir"])
    sec = cfg["secrets"]
    upx = Upstox(sec["UPSTOX_API_KEY"], sec["UPSTOX_TOTP_SECRET"], sec["UPSTOX_PIN"], state)
    tg = Telegram(sec["TELEGRAM_BOT_TOKEN"], sec.get("TELEGRAM_CHAT_ID") or cfg.get("telegram", {}).get("chat_id", ""))
    live = cfg["general"].get("mode") == "live" if live_override is None else live_override
    return Context(cfg, upx, tg, state, live)


def cmd_gmp(cfg, args):
    today = now_ist().date()
    ig = G.fetch_investorgain(today)
    iw = G.fetch_ipowatch(today)
    subs = G.fetch_subscription(today.year)
    print(f"{'investorgain':<40} {'GMP':>7} {'%':>6} {'price':>7} {'close':>10}   ipowatch match")
    for r in sorted(ig, key=lambda r: (r["close_date"] or date.max)):
        if r["is_sme"] and not args.sme:
            continue
        m, sc = G.best_match(r["name"], [x for x in iw if not x["is_sme"]])
        sub = None
        for row in subs["_rows"]:
            if G.similarity(r["name"], row["name"]) >= 0.6:
                sub = row
                break
        print(f"{r['name'][:40]:<40} {str(r['gmp']):>7} {str(r['gmp_pct_site']):>6} {str(r['price']):>7} {str(r['close_date']):>10}   "
              f"{(m['name'][:28] + ' ₹' + str(m['gmp'])) if m else '-':<38} retail {sub['retail'] if sub else 'n/a'}")


def cmd_test_auth(cfg, args):
    ctx = make_ctx(cfg)
    p = ctx.upx.profile()
    print(f"OK: {p.get('user_name')} ({p.get('user_id')}) · ddpi={p.get('ddpi')} · active={p.get('is_active')} · products={p.get('products')}")
    print(f"open main-board IPOs visible via API: {len(ctx.upx.list_ipos('open', 'regular'))}")


def cmd_chat_id(cfg, args):
    tg = Telegram(cfg["secrets"]["TELEGRAM_BOT_TOKEN"], "")
    ids = tg.get_chat_ids()
    if not ids:
        print("No messages yet. Send any message to your bot in Telegram, then rerun.")
    for cid, who in ids:
        print(f"chat_id = {cid}   ({who})  -> put TELEGRAM_CHAT_ID={cid} in .env")


def cmd_status(cfg, args):
    ctx = make_ctx(cfg)
    apps = ctx.state.applications()
    print(f"mode: {'LIVE' if ctx.live else 'DRY-RUN'} · state dir: {ctx.state.dir}")
    print(f"recorded applications: {len(apps)}")
    for a in sorted(apps.values(), key=lambda a: str(a["applied_on"])):
        print(f"  {a['applied_on']} {a['name'][:30]:<30} {a['mode']:<7} ₹{a['amount']:,.0f} gmp {a['gmp_pct']}% order {a.get('order_id')}")
    if args.upstox:
        for o in ctx.upx.ipo_orders():
            print(f"  upstox: {o.get('symbol'):<12} {o.get('status'):<20} {o.get('payment_status'):<16} blocked ₹{o.get('upi_amount_blocked')} allotted {o.get('units_allotted')}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ipo_bidder")
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gmp", help="show scraped GMP/subscription tables (no Upstox needed)").add_argument("--sme", action="store_true")
    sub.add_parser("test-auth", help="log in to Upstox via TOTP and print the profile")
    sub.add_parser("chat-id", help="discover your Telegram chat id")
    p = sub.add_parser("run", help="bid-day job (12:30 PM)")
    p.add_argument("--live", action="store_true", help="force live even if config says dry-run")
    p.add_argument("--dry-run", action="store_true", help="force dry-run")
    sub.add_parser("remind", help="mandate reminders + allotment news (3:30 PM)")
    sub.add_parser("sell", help="listing-day sell job (10:01 AM)")
    sub.add_parser("iplog", help="log the public IP, alert on change")
    sub.add_parser("ip", help="print the public IP")
    sub.add_parser("status", help="show recorded applications").add_argument("--upstox", action="store_true")
    sub.add_parser("notify-test", help="send a test Telegram message")
    sub.add_parser("notify", help="send an arbitrary Telegram message").add_argument("text")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr),
                                  logging.FileHandler(cfg["_state_dir"] / "ipo_bidder.log")])
    if args.cmd == "gmp":
        return cmd_gmp(cfg, args)
    if args.cmd == "ip":
        print(public_ip())
        return
    if args.cmd == "chat-id":
        return cmd_chat_id(cfg, args)
    upstox_keys = ("UPSTOX_API_KEY", "UPSTOX_TOTP_SECRET", "UPSTOX_PIN")
    required = {"test-auth": upstox_keys, "status": upstox_keys, "remind": upstox_keys, "sell": upstox_keys,
                "run": upstox_keys + ("UPSTOX_UPI",), "notify-test": ("TELEGRAM_BOT_TOKEN",)}
    need = missing_secrets(cfg, required.get(args.cmd, ()))
    if need:
        sys.exit(f"missing in .env: {', '.join(need)}")
    if args.cmd == "test-auth":
        return cmd_test_auth(cfg, args)
    if args.cmd == "status":
        return cmd_status(cfg, args)
    live = True if getattr(args, "live", False) else (False if getattr(args, "dry_run", False) else None)
    ctx = make_ctx(cfg, live)
    if args.cmd == "notify-test":
        ok = ctx.tg.send(f"IPO bidder test message · {now_ist().strftime('%d %b %H:%M')} · mode {'LIVE' if ctx.live else 'DRY-RUN'}")
        print("sent" if ok else "not sent (check TELEGRAM_BOT_TOKEN and [telegram] chat_id)")
        return
    if args.cmd == "notify":
        print("sent" if ctx.tg.send(args.text) else "not sent")
        return
    job = {"run": job_run, "remind": job_remind, "sell": job_sell, "iplog": job_iplog}[args.cmd]
    try:
        print(job(ctx))
    except Exception as e:  # noqa: BLE001 - report crashes to Telegram, then fail the unit
        import traceback
        tb = "".join(traceback.format_exception(e)).strip().splitlines()[-6:]
        ctx.tg.send(f"IPO bidder: the '{args.cmd}' job crashed at {now_ist().strftime('%d %b %H:%M')}:\n"
                    f"{type(e).__name__}: {e}\n\n" + "\n".join(tb) + "\n\nCheck manually today if an IPO closes.")
        raise


if __name__ == "__main__":
    main()
