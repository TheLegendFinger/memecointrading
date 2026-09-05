# memebot

A Solana memecoin trading bot. It discovers new pairs on DexScreener, filters
out the obvious traps, scores what is left on a momentum/volume model, sizes
positions against your bankroll, and manages exits with stops, trailing stops
and time limits.

**It runs in paper mode by default** — fills are simulated locally and no funds
move. The live path is fully implemented: flip `mode: live`, arm the safety
interlock, and the same pipeline signs real swaps through the
[Jupiter](https://station.jup.ag/) aggregator and broadcasts them to mainnet.

---

## Quick start (paper trading)

```bash
git clone https://github.com/TheLegendFinger/memecointrading.git
cd memecointrading
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml     # optional; defaults are sane

python scripts/demo_session.py         # watch a full session on a fake market
python -m memebot scan                 # what does the bot see right now?
python -m memebot once --dry-run       # one cycle, decisions logged, no trades
python -m memebot run --config config.yaml
```

`demo_session.py` needs no network at all — it drives the real engine against a
synthetic market so you can see entries, stops, liquidity-drain exits and the
fee model working before pointing anything at mainnet.

Then, in another shell:

```bash
python -m memebot status               # equity, open positions, win rate
python -m memebot trades               # trade log
```

## Commands

| Command | What it does |
| --- | --- |
| `run` | The trading loop. `--cycles N` to stop after N passes, `--interval S` to change the cadence. |
| `once` | A single cycle, then exit. Good for cron and for testing config changes. |
| `scan` | Score the current market and print the table — never trades. |
| `status` | Portfolio summary (`--json` for machine-readable output). |
| `trades` | Recent fills with fees, slippage and realized P&L. |
| `liquidate` | Close every open position at market. |
| `reset` | Wipe paper state and start the bankroll over (refuses in live mode). |
| `config` | Print the effective configuration after file + env + flags. |

Global flags: `--config`, `--mode {paper,live}`, `--db`, `--log-level`,
`--dry-run`.

## How a cycle works

```
       DexScreener search + boost feed
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

Everything is persisted to SQLite (`data/memebot.sqlite3`), so stopping and
restarting the bot resumes the same book.

## The paper fill model

Paper mode is not "assume you get the mid price" — that flatters a strategy
into looking profitable when it isn't. Each simulated swap applies:

* **price impact** from a constant-product curve using the pool's real
  liquidity (a $500 order into a $50k pool costs ~2%, not 0),
* **base slippage** for latency and competing flow, with jitter,
* **pool fee** (bps) plus network and priority fees in dollars,
* **failed transactions** at a configurable rate (2% by default), and
* **reverts** when modelled slippage exceeds the order's tolerance — exactly
  what the on-chain minimum-out check does.

Set `execution.paper_use_live_quotes: true` to price paper fills off real
Jupiter routes instead of the built-in curve, and
`execution.paper_random_seed` for reproducible runs.

## Going live

> Real money. Memecoins go to zero routinely, and a bot will find the ones that
> do faster than you will. Use a burner wallet funded with an amount you are
> prepared to lose entirely, and watch the first sessions.

1. Install the Solana extras:

   ```bash
   pip install -r requirements-live.txt
   ```

2. Put a wallet key in `.env` (copy from `.env.example`) — either
   `SOLANA_PRIVATE_KEY` (base58) or `SOLANA_KEYPAIR_PATH` (a Solana CLI JSON
   keypair). Fund it with SOL: your trading capital plus a little for fees.

3. Arm the interlock. This is separate from the config on purpose — nothing
   trades for real without it:

   ```bash
   echo 'LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISK' >> .env
   ```

4. Use a private RPC. The public endpoint drops transactions under load:

   ```bash
   echo 'MEMEBOT_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...' >> .env
   ```

5. Start small and watch it:

   ```bash
   python -m memebot run --mode live --config config.yaml
   ```

   The CLI prints the wallet, the RPC and the size limits, then asks you to
   type `trade` before the first order.

What live mode does per order: request a Jupiter route → reject it if price
impact exceeds your tolerance → build the swap transaction → sign it locally
(your key never leaves the machine) → send it → poll until confirmed → read the
**actual settled balances** from the confirmed transaction and book those, not
the quote's estimate. Failures are returned as unfilled orders and logged; they
never corrupt the position book.

Live mode reads your bankroll from config the same way paper does, so set
`risk.max_position_usd` and `risk.max_open_positions` to what your wallet can
actually cover.

## Configuration

`config.example.yaml` documents every knob. Precedence is
**CLI flags > environment (`MEMEBOT_*`) > config file > defaults**, and unknown
keys are rejected at startup rather than silently ignored.

The settings worth tuning first:

| Setting | Default | Why you would change it |
| --- | --- | --- |
| `strategy.min_score` | `0.55` | Raise for fewer, higher-conviction entries; lower to trade more. |
| `risk.position_size_pct` | `0.08` | Fraction of equity per position. |
| `risk.stop_loss_pct` | `0.20` | Memecoins are volatile; too tight and you get stopped out of every winner. |
| `filters.min_liquidity_usd` | `25000` | The single most effective rug filter. |
| `filters.min_age_minutes` | `20` | Lower to catch launches earlier, at much higher risk. |
| `execution.slippage_bps` | `150` | Too low and orders revert; too high and you get sandwiched. |

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # 145 tests, no network access required
```

The test suite fakes DexScreener, Jupiter and the Solana RPC, so it runs
offline and deterministically. It covers the fill model, portfolio accounting
(including partial exits and cost basis), every risk rule, the filters, the
scoring model, full engine cycles, and the live executor's safety envelope.

Layout:

```
memebot/
  config.py        dataclass config + file/env/flag merging and validation
  models.py        Token, PairSnapshot, Signal, Order, Fill, Position
  http.py          retrying, rate-limited HTTP client
  storage.py       SQLite schema and queries
  portfolio.py     cash, positions, cost basis, P&L
  risk.py          sizing, entry gates, circuit breakers, exit rules
  engine.py        the cycle
  cli.py           command line interface
  data/            dexscreener.py (discovery/prices), jupiter.py (routing)
  execution/       base.py (interface), paper.py (simulator), live.py (Jupiter)
  strategy/        filters.py (safety gates), momentum.py (scoring)
```

Adding a strategy: subclass `Strategy`, implement `score` and
`generate_entries`, register it in `memebot/strategy/__init__.py`, then select
it with `strategy.name` in the config.

## Limitations, honestly

* The momentum model is a reasonable starting point, **not** a proven edge. It
  has not been backtested against historical data — paper trade it long enough
  to form your own view before risking anything.
* Discovery depends on DexScreener's free API. When it rate limits or lags, the
  bot sees a stale market.
* There is no honeypot/mint-authority check yet. The liquidity, age and
  liquidity-to-FDV filters catch a lot, but not a token that simply disables
  selling. Adding a token-security API call in `strategy/filters.py` is the
  highest-value next step.
* Live mode does not currently split large orders or retry a failed swap at a
  wider tolerance; an order that reverts is simply skipped until the next cycle.

## License

MIT
