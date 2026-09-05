# memebot

A Solana memecoin trading bot. It discovers new pairs on DexScreener, filters
out the obvious traps, scores what is left on a momentum/volume model, sizes
positions against your bankroll, and manages exits with stops, trailing stops
and time limits.

**It paper trades the real Solana market by default** — live DexScreener prices,
live liquidity, live order flow, real Jupiter routes — with fills simulated
locally so no funds move. The live path is fully implemented: flip `mode: live`,
arm the safety interlock, and the same pipeline signs real swaps through the
[Jupiter](https://station.jup.ag/) aggregator.

It ships with a web dashboard you can deploy to Vercel in a couple of minutes.

---

## Quick start on Windows

```powershell
git clone https://github.com/TheLegendFinger/memecointrading.git
cd memecointrading
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

That is the only command you need. It sets the project up the first time, then
opens a menu — everything is a number:

```
 ╭────────────────────────────────────────────────────────────────╮
 │ memebot 1.0.0                                            PAPER │
 ╰────────────────────────────────────────────────────────────────╯

   $1,043.18 · $812.40 cash · 2 open · +4.32%

   TRADE
    1  Paper trade           practice on the real market, no real money
    2  Live trade            REAL money on Solana
    3  Close all positions   sell everything at market

   LOOK
    4  Portfolio             equity, open positions, win rate
    5  Trade history         recent fills with fees and P&L
    6  Scan the market       what the bot sees right now

   SETUP
    7  Wallet                address, balance, or create a burner
    8  Health check          are the market feeds reachable?

    0  Quit

   › Type a number:
```

`start.bat` in the project folder does the same thing if you would rather
double-click than type. Every individual script still exists
(`scripts\run.ps1`, `scripts\doctor.ps1`, ...) and the CLI is unchanged — the
menu is a front door, not a replacement.

<details>
<summary>macOS / Linux</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml

python -m memebot               # the same menu
python -m memebot doctor        # or go straight to a command
python -m memebot run --config config.yaml
```
</details>

**Start with `doctor`.** It pings DexScreener and Jupiter, runs the full
discovery → filter → score funnel against the live market, and tells you exactly
what is or isn't working:

```
  [PASS] dexscreener search       231ms  184 solana pairs; e.g. WIF at $1.9042
  [PASS] dexscreener boosts       142ms  10 boosted token(s)
  [PASS] jupiter price            180ms  SOL = $198.44
  [PASS] jupiter routing          301ms  0.1 SOL routes to 19.81 USDC via Whirlpool (impact 0.004%)
  [PASS] candidate pipeline      2914ms  118 scanned -> 24 passed filters -> 3 above min_score 0.55
```

If the pipeline line says *"filters are rejecting everything"* or *"best score
was 0.41; lower min_score to trade"*, that is a tuning problem, not a bug — see
[Configuration](#configuration).

---

## Where to actually run this

**A trading bot needs to run continuously. Vercel cannot do that** — its
functions are short-lived (10–60s), it has no persistent disk, and on the Hobby
plan its cron jobs fire **once per day**. Deploying the repo to Vercel gets you a
first-class dashboard and an on-demand `/api/cycle` endpoint; it does not get you
a bot that trades every 30 seconds.

So pick a setup:

| Setup | Trading runs on | Costs | Cycle interval | Good for |
| --- | --- | --- | --- | --- |
| **A. Local + Vercel dashboard** *(recommended)* | your Windows PC | free | 30s, whatever you set | Watching it properly. Your PC has to be on. |
| **B. GitHub Actions + Vercel dashboard** | GitHub's runners | free | ~10 min, often late | No PC required, no card on file. |
| **C. All-Vercel** | Vercel Cron | needs **Pro** ($20/mo) | 1/min on Pro, **1/day on Hobby** | Everything in one place. |
| **D. Local only** | your PC | free | 30s | Trying it out. No cloud account at all. |

All four run the same bot with the same code. They differ only in what triggers
a cycle and where the state lives.

### D — Local only (no cloud, start here)

Nothing to configure. `scripts\run.ps1` (or menu option 1) keeps state in
`data/memebot.sqlite3` and shows everything in the terminal as it trades.

### A — Your PC trades, Vercel shows the dashboard

The PC runs the loop; Vercel serves a dashboard you can open from your phone.
They share one Postgres database.

1. **Create a free Postgres database.** [Neon](https://neon.tech) or
   [Supabase](https://supabase.com) both have a free tier that is plenty. Copy
   the connection string (`postgresql://user:pass@host/db`).

2. **Point the bot at it.** Add this line to `.env` in the project folder:

   ```
   DATABASE_URL=postgresql://user:pass@host/dbname
   ```

   Verify with `python -m memebot doctor` — the state line should now say
   `postgres (...)` rather than `sqlite`.

3. **Deploy the dashboard** — see [Deploying to Vercel](#deploying-to-vercel)
   below, and set the same `DATABASE_URL` there.

4. **Run the bot** with `scripts\run.ps1`. Trades appear on the Vercel dashboard
   within seconds.

To keep it running after you log out, use Task Scheduler:

```
Task Scheduler > Create Task
  General : "memebot", tick "Run whether user is logged on or not"
  Triggers: At startup  (+ tick "Repeat task every 5 minutes" as a restart net)
  Actions : Start a program
            Program : C:\path\to\memecointrading\scripts\run.ps1
            Start in: C:\path\to\memecointrading
  Settings: untick "Stop the task if it runs longer than..."
```

The bot is safe to kill and restart at any time — its state lives in the
database, not in memory.

### B — GitHub Actions trades, Vercel shows the dashboard

No PC required. `.github/workflows/trade.yml` runs a cycle every 10 minutes on
GitHub's runners, for free.

1. Create the Postgres database as above.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   `DATABASE_URL` = your connection string.
3. Enable Actions on the repo. The schedule takes it from there, and
   **Actions → trading cycle → Run workflow** triggers one by hand.
4. Deploy the dashboard with the same `DATABASE_URL`.

GitHub's minimum interval is 5 minutes and scheduled runs are queued rather than
punctual, so expect ~10–15 minutes between cycles in practice. Keep this one on
paper mode — a repository secret is not the place for a wallet key.

### C — Everything on Vercel

Deploy as below, then set the cron in `vercel.json`. It ships with a **daily**
schedule because that is Hobby's limit:

```json
"crons": [{ "path": "/api/cycle", "schedule": "0 12 * * *" }]
```

On Pro, change it to `*/1 * * * *` for a cycle every minute. On Hobby, leave it —
and understand that one cycle a day is a demonstration, not a trading strategy.

---

## Deploying to Vercel

The repo is a working Vercel project as-is: a static dashboard in `public/` and
Python serverless functions in `api/`.

1. **Import the repo** at [vercel.com/new](https://vercel.com/new). Framework
   preset: **Other**. No build command, no output directory — `vercel.json`
   covers it.

2. **Add environment variables** (Project → Settings → Environment Variables):

   | Name | Value | Why |
   | --- | --- | --- |
   | `POSTGRES_URL` *or* `DATABASE_URL` | your Postgres connection string | **Required.** Serverless has no disk; without this every trade is discarded. |
   | `CRON_SECRET` | any long random string | Required to call `/api/cycle`. Without it that endpoint refuses everything. |
   | `MEMEBOT_MODE` | `paper` | Explicit is better. |
   | `MEMEBOT_STARTING_CASH` | `1000` | Paper bankroll. |

   If you add Vercel's own Postgres integration it sets `POSTGRES_URL` for you.

3. **Deploy**, then open `/api/health` — it runs the same checks as `doctor` and
   reports what the deployment can reach.

The dashboard is public once deployed. It shows your equity, positions and
trades to anyone with the URL, so treat the URL as private (or put Vercel
Authentication in front of the project). `/api/cycle` is the only endpoint that
changes anything, and it needs the `CRON_SECRET` bearer token:

```bash
curl -X POST "https://your-app.vercel.app/api/cycle?key=YOUR_CRON_SECRET"
```

### What the deployment serves

| Route | What it does |
| --- | --- |
| `/` | The dashboard. **Live** tab: candlesticks with this bot's entries and exits, and a feed of what it is doing. **Overview** tab: equity curve, positions, trade log. |
| `/api/status` | Portfolio summary as JSON. |
| `/api/positions` | Open positions. |
| `/api/trades?limit=50` | Trade history. |
| `/api/equity?limit=300` | Equity curve points. |
| `/api/scan?limit=25` | Live scored candidates — read-only, never trades. |
| `/api/candles?address=…` | OHLC candles plus this bot's fills as markers. |
| `/api/events?since_id=…` | The action feed, incrementally. |
| `/api/health` | Connectivity and configuration diagnostics. |
| `/api/cycle` | **Protected.** Runs one trading cycle. |

---

## Commands

| Command | What it does |
| --- | --- |
| *(none)* | Opens the numbered menu. Same as `menu`. |
| `doctor` | Check every API, the database, and the candidate funnel. Start here. |
| `wallet` | Create or inspect the live trading wallet: address, balance, position size. |
| `run` | The trading loop, with the live display. `--cycles N` to stop after N passes, `--interval S` for the cadence, `--plain` for log lines instead. |
| `once` | A single cycle, then exit. Good for cron. |
| `scan` | Score the live market and print the table — never trades. |
| `status` | Portfolio summary (`--json` for scripts). |
| `trades` | Recent fills with fees, slippage and realized P&L. |
| `liquidate` | Close every open position at market. |
| `reset` | Wipe paper state and start the bankroll over (refuses in live mode). |
| `config` | Print the effective configuration after file + env + flags. |

Global flags: `--config`, `--mode {paper,live}`, `--db`, `--log-level`,
`--dry-run`. `--db` takes a SQLite path *or* a `postgresql://` URL.

`python scripts/dev_server.py` serves the dashboard locally, and
`python scripts/demo_session.py` runs a full session against a synthetic market
with no network at all — useful for seeing the machinery work end to end.

## Watching it trade

The terminal is the display. Start it (menu **1** or **2**) and it redraws after
every scan — no browser, no second window:

```
╭────────────────────────────────────────────────────────────────────────╮
│ memebot                                     PAPER · cycle 14 · 16:43:07│
╰────────────────────────────────────────────────────────────────────────╯

  $945.27 · $786.19 cash · -5.47% · 2 open · 2W/5L

  HOLDING
   MOODENG          0.0767  ▲   3.1%    $78.19  avg 0.0738     4m  ▁▁▂▃▃▃▄▇██
   POPCAT           0.0271  ▲   9.2%    $80.88  avg 0.0246    11m  ▁▁▄▃▃▅▅▇▇██

  WATCHING            score      5m       1h   liquidity
   MEW                  0.49     5.1%    16.9%    $454,601
   BONK                 0.21     0.5%     1.3%    $120,075

  ACTIVITY
   16:43  BUY   Bought MOODENG for $75.22 @ 0.073807399
           score 0.70 | slip 50bps | fee $0.59
   16:43  SELL  Sold BONK for $121.36 (+49.46)
           take profit +72.0%

  next scan in ~30s  ·  Ctrl+C to stop
```

Each holding shows its live price, P&L, average entry, how long it has been
held, and a sparkline of its recent prices. **WATCHING** is what scored highest
this scan without being bought. **ACTIVITY** is every buy, sell, failed order
and halt, with the reason attached.

Log lines go to the log file while this is up, so nothing scribbles over the
frame. Pipe the output somewhere (a file, CI) and it switches back to plain log
lines automatically; `--plain` forces that. On a console that cannot render box
characters or colour, it falls back to ASCII rather than failing.

There is also a **web dashboard** — candlestick charts with entry and exit
markers, and the same activity feed — but it is for the
[Vercel deployment](#deploying-to-vercel), so you can check the bot from your
phone. Locally, `python scripts/dev_server.py` serves it if you want it.

## How a cycle works

```
   DexScreener: search + boost leaderboards + newest token profiles
                    │
                    ▼
        [1] discover ~120 candidate pairs
                    │
                    ▼
        [2] hard filters  ──────────────► rejected (liquidity, age, rug ratio,
                    │                     sell pressure, already parabolic…)
                    ▼
        [3] momentum score 0..1           0.30 · 5m price change
                    │                     0.25 · 1h price change
                    │                     0.20 · volume surge vs its own 24h avg
                    │                     0.15 · buy/sell flow
                    │                     0.10 · liquidity depth
                    ▼
        [4] risk sizing  ───────────────► position size = min(equity %, max $,
                    │                     free cash, 0.5% of pool liquidity)
                    ▼
        [5] execute (paper simulator | Jupiter swap)
                    │
                    ▼
        [6] manage open positions every cycle:
            stop loss · trailing stop · take profit · time stop ·
            liquidity-drain exit · stale-data exit · momentum reversal
```

## The paper fill model

Paper mode is not "assume you get the mid price" — that flatters a strategy into
looking profitable when it isn't. Each simulated swap applies:

* **price impact** from a constant-product curve using the pool's real
  liquidity (a $500 order into a $50k pool costs ~2%, not 0),
* **base slippage** for latency and competing flow, with jitter,
* **pool fee** (bps) plus network and priority fees in dollars,
* **failed transactions** at a configurable rate (2% by default), and
* **reverts** when modelled slippage exceeds the order's tolerance — exactly
  what the on-chain minimum-out check does.

Set `execution.paper_use_live_quotes: true` to price paper fills off **real
Jupiter routes** instead of the built-in curve — the most realistic setting, at
the cost of two extra API calls per order. Set `execution.paper_random_seed` for
reproducible runs.

## Going live

**[LIVE_TRADING.md](LIVE_TRADING.md) is the full step-by-step** — wallet
creation, funding, and the first run, written for Windows PowerShell. The short
version:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1 -New -Save   # create a burner
powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1              # check it is funded
powershell -ExecutionPolicy Bypass -File scripts\live.ps1                # trade for real
```

`live.ps1` installs the Solana packages, creates `config.live.yaml` from the
small-wallet template, shows the wallet and the market feeds, and makes you type
`LIVE` before anything trades. It arms the interlock **for that window only** —
nothing written to disk can trade for real, so no other command can start live
trading by accident.

> Real money. Memecoins go to zero routinely, and a bot will find the ones that
> do faster than you will. Use a burner funded with an amount you are prepared
> to lose entirely, and watch the first sessions.

Per order, live mode requests a Jupiter route → rejects it if price impact
exceeds tolerance → builds the swap → signs locally (your key never leaves the
machine, and a transaction your wallet should not sign is never sent) → polls to
confirmation → books the **actual settled balances** from the confirmed
transaction rather than the quote's estimate.

The bankroll is the wallet. At the start of every live cycle the bot reads the
wallet's balance from the chain and sizes against that, minus a SOL fee reserve
it never spends — `risk.starting_cash_usd` is a paper-only number.

**Run live trading on your own machine, not on Vercel or GitHub Actions.** A
wallet key in a cloud environment variable is a wallet key you have handed to a
platform, and a cycle that times out mid-swap is a position you did not intend.

## Configuration

`config.example.yaml` documents every knob. Precedence is
**CLI flags > environment (`MEMEBOT_*`) > config file > defaults**, and unknown
keys are rejected at startup rather than silently ignored.

Worth tuning first:

| Setting | Default | Why you would change it |
| --- | --- | --- |
| `strategy.min_score` | `0.55` | The main dial. Raise for fewer, higher-conviction entries; lower to trade more. |
| `risk.position_size_pct` | `0.08` | Fraction of equity per position. |
| `risk.stop_loss_pct` | `0.20` | Too tight and you get stopped out of every winner. |
| `filters.min_liquidity_usd` | `25000` | The single most effective rug filter. |
| `filters.min_age_minutes` | `20` | Lower to catch launches earlier, at much higher risk. |
| `execution.slippage_bps` | `150` | Too low and orders revert; too high and you get sandwiched. |

If `doctor` reports plenty of pairs but nothing tradable, loosen
`strategy.min_score` first, then `filters.min_volume_h1_usd`.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 248 tests, no network access required
```

The suite fakes DexScreener, Jupiter, the Solana RPC and Postgres, so it runs
offline and deterministically. It covers the fill model, portfolio accounting
(including partial exits and cost basis), every risk rule, the filters, the
scoring model, full engine cycles, the serverless handlers (including that
`/api/cycle` fails closed without a secret), the storage dialects, and the live
executor's safety envelope. With the live extras installed, a further set signs
real transactions with real solders cryptography and asserts they verify, that
the message Jupiter built is unchanged, and that a transaction the wallet should
not sign is never broadcast.

```
memebot/
  config.py        dataclass config + file/env/flag merging and validation
  models.py        Token, PairSnapshot, Signal, Order, Fill, Position
  http.py          retrying, rate-limited HTTP client
  storage.py       SQLite + Postgres behind one interface
  portfolio.py     cash, positions, cost basis, P&L
  risk.py          sizing, entry gates, circuit breakers, exit rules
  engine.py        the cycle
  doctor.py        dependency and configuration diagnostics
  wallet.py        burner wallet creation and inspection
  menu.py          the numbered menu
  console_view.py  the live trading display in the terminal
  ui.py            terminal colour and box drawing, with ASCII fallbacks
  cli.py           command line interface
  data/            dexscreener.py (discovery/prices), jupiter.py (routing)
  execution/       base.py (interface), paper.py (simulator), live.py (Jupiter)
  strategy/        filters.py (safety gates), momentum.py (scoring)
api/               Vercel serverless functions (status, trades, cycle, scan…)
public/            the dashboard
scripts/           Windows setup/run scripts, dev server, offline demo
```

Adding a strategy: subclass `Strategy`, implement `score` and
`generate_entries`, register it in `memebot/strategy/__init__.py`, then select it
with `strategy.name` in the config.

## Limitations, honestly

* The momentum model is a reasonable starting point, **not** a proven edge. It
  has not been backtested against historical data — paper trade it long enough to
  form your own view before risking anything.
* Discovery depends on DexScreener's free API. When it rate limits or lags, the
  bot sees a stale market. `doctor` will tell you when that is happening.
* There is no honeypot/mint-authority check yet. The liquidity, age and
  liquidity-to-FDV filters catch a lot, but not a token that simply disables
  selling. Adding a token-security API call in `strategy/filters.py` is the
  highest-value next step.
* Live mode does not split large orders or retry a failed swap at a wider
  tolerance; an order that reverts is skipped until the next cycle.
* Third-party endpoints move. If Jupiter or DexScreener change a URL, `doctor`
  will say so and `data.jupiter_price_url` / `data.dexscreener_base_url` in the
  config are how you point at the new one.

## License

MIT — see [LICENSE](LICENSE). Nothing here is financial advice, and running this
in live mode can lose every dollar in the connected wallet.
