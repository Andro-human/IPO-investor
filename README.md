# IPO auto-bidder

Applies to Indian main-board IPOs through the Upstox API when the grey-market premium (GMP) on the
closing day clears a threshold, ranks candidates by expected value, respects a fixed budget, and
tells you on Telegram when to approve the UPI mandate. Optionally sells the allotment on listing day.

The only manual step in live mode is approving the UPI request on your phone.

## How a day goes

| Time (IST) | Job | What it does |
|---|---|---|
| 12:30 | `run` | Lists open/upcoming main-board IPOs from Upstox, fetches GMP (investorgain + ipowatch, takes the lower) and retail subscription (chittorgarh / ipowatch), scores `GMP% × allotment chance`, plans against the budget (including IPOs closing in the next days), applies 1 lot at cut-off for today's picks, Telegrams you. |
| 15:30 | `remind` | Reminds you of unapproved mandates; reports allotment results. |
| 10:01 | `sell` | On listing day: market-sells allotted shares (if `[sell] enabled`), else tells you to. |
| 09:00 | `iplog` | Logs the public IP; alerts if it changed (matters only for auto-sell). |

Decision rules live in `config.toml` (`[strategy]`). Defaults: 12% GMP threshold, score penalty below
15%, 1 lot, budget ₹1,05,000, lookahead discount 0.85. They came out of the backtest in `backtest/`.

## Setup

1. Upstox account with UPI linked; TOTP enabled (Settings → Security; save the secret text); 6-digit PIN.
2. Upstox developer app (account.upstox.com → Developer): API key + secret. For auto-sell, also enable
   DDPI in the app and register your static IP against the app.
3. Telegram bot via @BotFather; send it one message.
4. `cp .env.example .env` and fill it in.
5. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
6. `.venv/bin/python -m ipo_bidder chat-id` → put the id in `config.toml` `[telegram] chat_id`.
7. `.venv/bin/python -m ipo_bidder test-auth` (TOTP login + profile) and `notify-test`.
8. `.venv/bin/python -m ipo_bidder run` → a dry-run decision message. Nothing is placed until
   `[general] mode = "live"`.

On the Linux box: `./deploy/install.sh` installs systemd user timers for the four jobs.

## Commands

```
python -m ipo_bidder gmp              # scraped GMP / subscription tables, no Upstox needed
python -m ipo_bidder test-auth        # Upstox TOTP login + profile
python -m ipo_bidder run [--dry-run|--live]
python -m ipo_bidder remind | sell | iplog | ip
python -m ipo_bidder status [--upstox]
python -m ipo_bidder notify-test | chat-id
```

## Safety rails

- Dry-run by default; live needs `mode = "live"` in config (or `run --live`).
- Never bids after `apply_deadline` (16:30 IST); UPI mandates must be approved by 17:00.
- Duplicate protection comes from Upstox's own order list, not a local file. Don't apply manually on
  another broker for the same IPO (one application per PAN).
- GMP: if the two sites disagree by more than `gmp_disagreement_pct` points, or none has a quote, the
  IPO is skipped and you're told.
- Sell job only ever touches ISINs of IPOs applied for through Upstox, and only within 3 days of listing.
- Secrets stay in `.env`; state (token, applications, GMP history, logs) in `state/`.

## Files

- `ipo_bidder/` — `upstox.py` (API client), `gmp.py` (market data), `planner.py` (scoring + plan),
  `jobs.py` (the four jobs), `notify.py` (Telegram), `state.py`, `config.py`, `__main__.py` (CLI).
- `deploy/` — systemd units + `install.sh`.
- `backtest/` — data collection and simulation used to choose the parameters (2024–2026, 272 IPOs).
