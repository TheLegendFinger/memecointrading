# memebot

A Solana memecoin trading bot. It discovers new pairs on DexScreener, filters
out the obvious traps, scores what is left on a momentum/volume model, sizes
positions against your bankroll, and manages exits with stops, trailing stops
and time limits.

**It trades real money.** There is no practice mode and no dry run: every order
is a real swap signed with your wallet and broadcast through the
[Jupiter](https://station.jup.ag/) aggregator.

Runs on **Windows, macOS and Linux**.

---

## Quick start

**macOS / Linux**

```bash
git clone https://github.com/TheLegendFinger/memecointrading.git
cd memecointrading
./scripts/start.sh
```

**Windows**

```powershell
git clone https://github.com/TheLegendFinger/memecointrading.git
cd memecointrading
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

That is the only command you need on either. It sets the project up the first
time, then opens a menu — everything is a number:

```
  ╋╋╋╋╋╋╋╋╋╋┏┓╋╋┏┓
  ┏━━┳━┳━━┳━┫┗┳━┫┗┓
  ┃┃┃┃┻┫┃┃┃┻┫╋┃╋┃┏┫   v1.6.0
  ┗┻┻┻━┻┻┻┻━┻━┻━┻━┛   LIVE - real money

   $1,043.18 · $812.40 cash · 2 open · +4.32%

   TRADE
    1  Start trading         REAL money on Solana
    2  Close all positions   sell everything at market

   LOOK
    3  Portfolio             equity, open positions, win rate
    4  Trade history         recent fills with fees and P&L
    5  Scan the market       what the bot sees right now

   SETUP
    6  Wallet                create, fund, back up, withdraw
    7  Health check          are the market feeds reachable?

    0  Quit

   › Type a number:
```

On Windows, `start.bat` does the same if you would rather double-click. Every
individual script exists in both flavours — `scripts/run.sh` and
`scripts\run.ps1`, `scripts/doctor.sh` and `scripts\doctor.ps1`, and so on —
and the CLI works directly too. The menu is a front door, not a replacement.

**Before risking anything**, look at what it would buy: menu **5** scans the
live market and prints the scored candidates without trading. Menu **7** checks
the feeds are reachable.

<details>
<summary>Without the scripts</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-live.txt
cp config.example.yaml config.yaml

python -m memebot           # the same menu
python -m memebot doctor    # or go straight to a command
python -m memebot scan      # what it would consider buying
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
  [PASS] candidate pipeline      2914ms  318 scanned -> 24 passed filters -> 3 above min_score 0.55 | found by: search 190, trending 55, busiest 43, boosts 20, profiles 10
  [PASS] execution                 96ms  live via Jupiter | wallet 7xKX...9Fda | 0.42 SOL | arms itself when you start trading
```

The `execution` line answers three questions: is there a wallet, is the RPC up,
and is there enough SOL to pay for a swap. It does not care whether live
trading is *armed* — that acknowledgement is made when you start trading, and
the menu and scripts make it for you.

If the pipeline line says *"filters are rejecting everything"* or *"best score
was 0.41; lower min_score to trade"*, that is a tuning problem, not a bug — see
[Configuration](#configuration).

---

## Where to run it

**On your own machine.** The wallet key lives there, you can see what it is
doing, and Ctrl+C stops it. Windows, macOS and Linux all work the same way.

Nothing else can trade: the optional web dashboard is **read-only**, and there
is deliberately no endpoint anywhere that can place an order. A cloud function
holding a wallet key, timing out mid-swap, is not a trade-off worth taking.

To keep it running after you log out:

* **macOS** — `caffeinate -i ./scripts/run.sh` stops the machine sleeping while
  it runs, or use a `launchd` plist for a proper background job.
* **Windows** — Task Scheduler: Create Task, "Run whether user is logged on or
  not", trigger At startup, action `scripts\run.ps1`, and untick "Stop the task
  if it runs longer than...".
* **Linux** — a `systemd --user` service, or `tmux`.

The bot is safe to kill and restart at any time — its state is in the database
and its bankroll is read from the chain, not from memory.

## Deploying to Vercel

Optional, and **read-only** — it shows what the bot is doing so you can check
from your phone. It cannot place, cancel or change a trade, and needs no wallet
key. The repo is a working Vercel project as-is: a static dashboard in `public/`
and Python serverless functions in `api/`.

1. **Import the repo** at [vercel.com/new](https://vercel.com/new). Framework
   preset: **Other**. No build command, no output directory — `vercel.json`
   covers it.

2. **Add environment variables** (Project → Settings → Environment Variables):

   | Name | Value | Why |
   | --- | --- | --- |
   | `POSTGRES_URL` *or* `DATABASE_URL` | your Postgres connection string | **Required.** Serverless has no disk; without this every trade is discarded. |
   | `MEMEBOT_STARTING_CASH` | `1000` | Paper bankroll. |

   If you add Vercel's own Postgres integration it sets `POSTGRES_URL` for you.

3. **Deploy**, then open `/api/health` — it runs the same checks as `doctor` and
   reports what the deployment can reach.

The dashboard is public once deployed. It shows your equity, positions and
trades to anyone with the URL, so treat the URL as private (or put Vercel
Authentication in front of the project). No endpoint can trade, move funds or
change anything — there is no wallet key in the deployment at all.

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

---

## Commands

| Command | What it does |
| --- | --- |
| *(none)* | Opens the numbered menu. Same as `menu`. |
| `doctor` | Check every API, the database, and the candidate funnel. Start here. |
| `wallet` | The wallet: `--new` (with a seed phrase), `--phrase`, `--import`, `--withdraw --to ADDR --amount 0.1\|all`, or bare to show address and balance. |
| `run` | The trading loop, with the live display. `--cycles N` to stop after N passes, `--interval S` for the cadence, `--plain` for log lines instead. |
| `once` | A single cycle, then exit. Good for cron. |
| `scan` | Score the live market and print the table — never trades. |
| `status` | Portfolio summary (`--json` for scripts). |
| `trades` | Recent fills with fees, slippage and realized P&L. |
| `liquidate` | Close every open position at market. |
| `reset` | Wipe the bot's trade history. Moves no funds. |
| `learn` | What the bot has concluded from its own closed trades, bucket by bucket (`--json` for scripts). |
| `config` | Print the effective configuration after file + env + flags. `--reset` restores the recommended `config.yaml`. |

Global flags: `--config`, `--db`, `--log-level`. `--db` takes a SQLite path
*or* a `postgresql://` URL.

`python scripts/dev_server.py` serves the read-only dashboard locally.

## Watching it trade

The terminal is the display. Start it (menu **1** or **2**) and it redraws after
every scan — no browser, no second window:

```
╭────────────────────────────────────────────────────────────────────────╮
│ memebot                                      LIVE · cycle 14 · 16:43:07│
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

  next scan in ~30s  ·  Ctrl+C to stop  ·  real funds
```

Each holding shows its live price, P&L, average entry, how long it has been
held, and a sparkline of its recent prices. **WATCHING** is what scored highest
this scan without being bought. **ACTIVITY** is every buy, sell, failed order
and halt, with the reason attached.

Log lines go to the log file while this is up, so nothing scribbles over the
frame. Pipe the output somewhere (a file, CI) and it switches back to plain log
lines automatically; `--plain` forces that. On a console that cannot render box
characters or colour, it falls back to ASCII rather than failing.

There is also a **read-only web dashboard** — candlestick charts with entry and
exit markers, and the same activity feed — for the
[Vercel deployment](#deploying-to-vercel), so you can check the bot from your
phone. Locally, `python scripts/dev_server.py` serves it if you want it.

## How a cycle works

```
   five feeds, two kinds (see "How coins are found" below)
                    │
                    ▼
        [1] discover up to 400 candidate pairs (~35 requests)
            de-duplicated by token, capped per ticker family
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
        [5] on-chain check ─────────────► refused (mint authority live, freeze
                    │                     authority live, holders concentrated)
                    ▼
        [6] execute the swap through Jupiter
                    │
                    ▼
        [7] manage open positions every cycle:
            stop loss · trailing stop · take profit · time stop ·
            liquidity-drain exit · stale-data exit · momentum reversal
```

Steps [1]–[6] run every `poll_interval_seconds` (20s). Step [7] does **not** wait
for them: coins already held are re-priced and their exits checked every
`position_poll_seconds` (5s), because a stop loss is only ever as good as the
last price it saw. The fast tick costs one batched request however many
positions are open; a full scan costs about 35, which is what the rate limits
care about. Together that is ~117 requests a minute against DexScreener's
published ~300.

The live display counts down to the next scan, and typing **STOP** then Enter
stops the bot (Ctrl+C still works):

```
  next scan in  12s  ·  2 held, re-checked every 5s  ·  real funds
  type STOP then Enter to stop  ·  Ctrl+C also works
```

Stopping is not selling — open positions stay open. Use `liquidate` (menu
option **2**) to close them.

### How coins are found

Step [1] is worth spelling out, because it is where a scan goes wrong in a way
that is hard to see from the outside.

| Feed | What it ranks by | Source |
| --- | --- | --- |
| `search_terms` | **name** — text match on symbol and name | DexScreener `/latest/dex/search` |
| `use_boosted_feed` | what someone is paying to promote | DexScreener boost leaderboards |
| `use_token_profiles` | newest tokens to publish a profile | DexScreener token profiles |
| `use_trending_pools` | **trading activity** right now | GeckoTerminal trending pools |
| `use_top_pools` | 24h volume | GeckoTerminal pools |
| `use_new_pools` | pool creation time (off by default) | GeckoTerminal new pools |

Search is the trap. It matches *text*, so asking for twenty fashionable words
returns whatever happens to be called those words — and when one ticker
catches, twenty near-identical copycats launch within the hour and every one of
them matches. A scan can come back looking like a single coin's fan club.

Two things stop that. The pool feeds rank by what is actually being traded and
do not care what anything is called. And `data.max_per_symbol` (default 2) caps
how many coins from one ticker family — STONK, STONKS, $STONK, STONK2 all
collapse to the same family — can be considered in a cycle, keeping the busiest.

Feeds are interleaved rather than concatenated, so a long one cannot crowd out
the rest, and every candidate is then priced by DexScreener alone: GeckoTerminal
only ever contributes *names to look at*, never a number the bot trades on.

`scan` shows which feed found each coin in its `FOUND BY` column, and `doctor`
prints the per-feed breakdown. If everything says `search`, the pool feeds are
unreachable and discovery has quietly narrowed to name matching.

### What the on-chain check looks at

Steps [1]–[4] read the *market*: price, volume, who is buying. None of that can
see the two setups that empty a wallet fastest, because both live on the
token's mint account rather than in its chart. Step [5] reads the chain through
the same RPC the bot trades with, for the one coin it is about to buy:

| Check | Why it matters |
| --- | --- |
| **mint authority** revoked | If it is still live, whoever deployed the token can print more supply whenever they like and dilute you to nothing in a single transaction. |
| **freeze authority** revoked | If it is still live, they can freeze your token account. You hold the coin and can never sell it — the chart looks perfect right up to the moment you try to exit. |
| **holder concentration** | The largest account, and the largest ten, as a share of supply. If ten accounts hold most of it, the exit is theirs and not yours. |

The pool's own account is excluded from the concentration numbers — a
constant-product pool holds most of the supply by design, and counting it as a
whale makes every healthy token look captured. It is recognised by size:
DexScreener reports total pool liquidity, so one side is about half of it.

Three RPC calls per token, cached for ten minutes, and only for a coin actually
about to be bought — not for all four hundred in a scan. If the chain cannot be
read, the token is treated as having **failed**, not passed
(`safety.allow_unverified` if you disagree). `scan` shows the verdict in its
`CHAIN` column for the candidates above your entry threshold.

**What this is not.** It is not a full rug scanner, and passing it does not mean
a coin is safe. It cannot see whether the LP is locked or burned — finding the
LP mint reliably needs per-DEX pool layouts. It reads token *accounts*, not
owners, so one person spread across five accounts reads as five holders: it
undercounts concentration, though it never invents it. And nothing here can
tell you whether the team will simply sell.

### Learning from its own trades

At every entry the bot banks what it knew at that moment — which feed found the
coin, how deep the pool was, how old it was, how hard it was moving, how lopsided
the buying was, which part of the day it is — and, when the position closes, what
the trade returned. `python -m memebot learn` (menu option **6**) prints the
whole table.

Those recordings are written from the very first trade. What they are *used* for
starts later, and cautiously, because a few dozen memecoin trades is a violent
sample and the dangerous failure is not "learns nothing" but "learns a lucky
streak and bets the wallet on it":

- **Nothing is applied until `min_trades` (30) have closed.** Before that it
  only records.
- **Every edge is shrunk towards the overall average** by how thin the evidence
  is — `n / (n + k)`. Five trades in a bucket barely move; fifty move most of
  the way. A bucket under `min_bucket_trades` (4) is ignored outright.
- **An edge is relative.** A bucket is only good if it beat this bot's own
  average; a history where every trade won equally says nothing about which
  entries were better, and tilts nothing.
- **The total tilt is hard-capped** at `max_adjustment` (0.10 on a 0–1 score).
  However lopsided the history, this leans on the strategy — it never replaces
  it.
- **Nothing is hidden.** Every bucket's sample size, raw win rate, return and
  final adjustment are in the `learn` output, and the reason on each buy shows
  the tilt that was applied.

```
  BUCKET             N   WIN%  RETURN   VS AVG  ADJUST
  -----------------  --  ----  -------  ------  ------
  found by trending  22   59%    +8.4%   +6.1%  +0.042
  liq<50k             9   22%   -11.2%  -13.5%  -0.063
  age<2h              6   33%    -4.0%   -6.3%  -0.023
```

It is a tilt learned from experience, not a model. It can only weigh the
buckets someone wrote down, and if the market regime changes underneath it, it
will keep believing the old one for a while — which is exactly why the cap
exists. `reset` clears this history along with the rest of the book.

## What an order actually does

Per order, live mode requests a Jupiter route → rejects it if price impact
exceeds your slippage tolerance → builds the swap → signs it locally (your key
never leaves the machine, and a transaction your wallet should not sign is never
sent) → broadcasts → polls to confirmation → books the **actual settled
balances** from the confirmed transaction rather than the quote's estimate.

Failures are returned as unfilled orders and logged; they never corrupt the
position book. An order that reverts is skipped until the next cycle.

## The wallet

Menu option **7** does all of it — the bot trades from one wallet and only that
one.

| | |
| --- | --- |
| **Create** | Generates a wallet and a **12 or 24 word seed phrase**, and saves it to `.env`. The phrase is the backup: it restores the same wallet in Phantom or Solflare (`Import wallet` → paste → first account). |
| **Fund** | Send SOL to the address it shows. Choose the **Solana network** when withdrawing from an exchange. |
| **Back up** | Shows the seed phrase again, behind a confirmation. Write it on paper — losing the project folder without a written copy loses the funds. |
| **Restore** | Paste a phrase to move an existing wallet in. Checksum-validated, so a mistyped word is rejected rather than silently becoming a different empty wallet. |
| **Withdraw** | Sends SOL to any address, an amount or `all`. It refuses to send to itself, to overdraw, or to send at all when the wallet cannot cover the fee. |

Keys are derived on `m/44'/501'/0'/0'` — the path Phantom, Solflare and the
Solana CLI use — with SLIP-0010 ed25519, checked against the specification's own
test vectors. A wrong derivation would produce a phrase that restores nothing,
so that is a test rather than an assumption.

**Withdrawing moves SOL only.** Memecoins the bot is holding stay where they
are, so close positions first (menu **3**) if you want the whole balance out.

## Going live

**[LIVE_TRADING.md](LIVE_TRADING.md) is the full step-by-step** — wallet
creation, funding, and the first run, written for Windows PowerShell. The short
version:

```bash
./scripts/wallet.sh --new --save    # create a burner        (macOS)
./scripts/wallet.sh                 # check it is funded
./scripts/run.sh                    # trade for real
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1 -New -Save   # Windows
powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1
powershell -ExecutionPolicy Bypass -File scripts\run.ps1
```

`run.sh` / `run.ps1` install the Solana packages, show the wallet, and make you
type `LIVE` before anything trades. They arm the interlock **for that process
only** — nothing written to disk can trade, so no other command can start
trading by accident.

> Real money. Memecoins go to zero routinely, and a bot will find the ones that
> do faster than you will. Use a burner funded with an amount you are prepared
> to lose entirely, and watch the first sessions.

Per order, live mode requests a Jupiter route → rejects it if price impact
exceeds tolerance → builds the swap → signs locally (your key never leaves the
machine, and a transaction your wallet should not sign is never sent) → polls to
confirmation → books the **actual settled balances** from the confirmed
transaction rather than the quote's estimate.

**The bankroll is the wallet, and only the wallet.** There is no starting-cash
setting: at the start of every cycle the bot reads the balance from the chain
and sizes against that, minus a SOL fee reserve it never spends. Send more SOL
in and it trades bigger; take some out and it trades smaller. Before the first
successful read it has nothing to spend, which is the safe direction to be
wrong in.

**Run live trading on your own machine, not on Vercel or GitHub Actions.** A
wallet key in a cloud environment variable is a wallet key you have handed to a
platform, and a cycle that times out mid-swap is a position you did not intend.

## Configuration

`config.example.yaml` documents every knob. Precedence is
**CLI flags > environment (`MEMEBOT_*`) > config file > defaults**, and unknown
keys are rejected at startup rather than silently ignored.

Settings that used to exist and no longer do - `mode`, `dry_run`,
`risk.starting_cash_usd`, the `execution.paper_*` group - are the exception:
older versions of this project wrote them into your `config.yaml` themselves,
so they are quietly deleted from the file and the bot starts anyway.

Worth tuning first:

| Setting | Default | Why you would change it |
| --- | --- | --- |
| `strategy.min_score` | `0.55` | The main dial. `0.65` is a coin clearly running (+3-4% on 5m, +18% on 1h, 3x its own volume, 2:1 buys); `0.45` is decent; `0.30` is warming up; under `0.10` is flat or falling. |
| `data.search_terms` | 20 terms | How wide the net is by *name*. Each term is one request returning ~30 pairs. |
| `data.max_per_symbol` | `2` | How many coins from one ticker family get through per cycle. `0` turns the cap off. |
| `data.use_trending_pools` | `true` | Discovery by trading activity rather than by name. Turning it off narrows the scan to text matching. |
| `data.max_candidates` | `400` | How many pairs a cycle scores. |
| `poll_interval_seconds` | `20` | How often the market is scanned for something new. The rate-limit dial. |
| `position_poll_seconds` | `5` | How often coins already held are re-priced and their exits checked. |
| `risk.position_size_pct` | `0.08` | Fraction of equity per position. |
| `risk.min_position_usd` | `1` | Smallest position worth opening. Swap costs are mostly flat, so at $1 a round trip is most of the trade — `doctor` prints the number. |
| `risk.stop_loss_pct` | `0.20` | Too tight and you get stopped out of every winner. |
| `filters.min_liquidity_usd` | `25000` | The single most effective rug filter. |
| `safety.max_top10_holder_pct` | `0.40` | Share of supply the ten biggest non-pool accounts may hold. |
| `safety.allow_unverified` | `false` | Whether a token whose chain data could not be read is bought anyway. |
| `learning.min_trades` | `30` | Closed trades before anything learned is applied. |
| `learning.max_adjustment` | `0.10` | The hard cap on the whole learned tilt. |
| `filters.min_age_minutes` | `20` | Lower to catch launches earlier, at much higher risk. |
| `execution.slippage_bps` | `150` | Too low and orders revert; too high and you get sandwiched. |

`config.yaml` is your copy of `config.example.yaml`, and while you have not
edited it, it is kept in step with the recommended settings automatically -
otherwise an improved default would never reach anyone who had already run
setup. The moment you change a single value it is yours and is left alone.
`python -m memebot config --reset` puts the recommended settings back.

If `doctor` reports plenty of pairs but nothing tradable it names the score
that would have worked, so you can decide between lowering `strategy.min_score`
and waiting for a better market. If it says only a few dozen coins were seen,
that is `data.search_terms` being too short - each term is one request worth
about 30 pairs, and four terms is not a market.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 248 tests, no network access required
```

The suite fakes DexScreener, Jupiter, the Solana RPC and Postgres, so it runs
offline and deterministically. `tests/fakes.py` holds a simulated executor -
a test double, not a trading mode - so the engine, risk and portfolio tests can
run without a wallet or a network. It covers the fill model, portfolio accounting
(including partial exits and cost basis), every risk rule, the filters, the
scoring model, full engine cycles, the serverless handlers (including that
nothing in the deployment can trade), the storage dialects, and the live
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
  execution/       base.py (interface), live.py (Jupiter swaps)
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
  has not been backtested against historical data. Look at what `scan` turns up
  for a while first, and start with an amount you would not miss.
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
