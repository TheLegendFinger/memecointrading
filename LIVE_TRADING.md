# Live trading — step by step (Windows PowerShell)

This walks you from a fresh clone to a bot placing real swaps on Solana with
real money.

> **Read this once first.** The bot buys brand-new memecoins. A large share of
> them go to zero, some cannot be sold at all, and it will keep trading until
> you stop it. Fund it with an amount you would be genuinely fine losing in
> full — treat it as gone the moment you send it. Nothing here is financial
> advice, and the strategy has not been backtested.

---

## 0. What you need

* Windows with **Python 3.9+** — check with `python --version`. If it is
  missing: `winget install Python.Python.3.12`
* **Git** — `winget install Git.Git`
* Some **SOL**. $30–100 is plenty to start. You can buy it on any exchange
  (Coinbase, Kraken, Binance) and withdraw it to the wallet this bot creates.

Open **PowerShell** (press Start, type `powershell`, hit Enter) and work
through the steps below in order.

---

## 1. Get the code

```powershell
cd ~
git clone https://github.com/TheLegendFinger/memecointrading.git
cd memecointrading
```

## 2. Install

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

This creates a private Python environment inside the folder and installs
everything. It takes a minute. `-ExecutionPolicy Bypass` is needed because
Windows blocks unsigned scripts by default; it applies to this one command
only.

## 3. Paper trade first — for at least a day

Do not skip this. It is the same code path, the same live market data, with
simulated fills. It costs nothing and it is how you find out whether the
settings suit you.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1   # is the market data reachable?
powershell -ExecutionPolicy Bypass -File scripts\run.ps1      # paper trading; Ctrl+C to stop
```

In a second PowerShell window:

```powershell
cd ~\memecointrading
powershell -ExecutionPolicy Bypass -File scripts\dashboard.ps1   # opens http://localhost:8000
```

Let it run. If it never trades, the market is quiet or your settings are
strict — `doctor.ps1` will tell you which, and the
[README](README.md#configuration) says what to loosen.

## 4. Create the wallet

```powershell
powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1 -New -Save
```

This prints an **address** and a **private key**, and saves the key to `.env`
(which git ignores).

* **Copy the private key somewhere safe now.** It is shown once. Anyone who has
  it can take everything in the wallet.
* This is a **burner**. Never use a wallet that holds anything else — the bot
  can spend everything in it.

## 5. Fund it

Send SOL from your exchange to the address from step 4. Choose the **Solana
network** when withdrawing — sending on the wrong network loses the funds.

Check it arrived:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1
```

It shows the balance and what each trade would be worth at your settings:

```
  Address   4Nzz5f1qub5YNCUJyMjg7sNEzoi3YKnQrfF8CZhTDYDN
  SOL       0.520000  ($103.22)
  Tradable  $98.26  (after a 0.025 SOL fee reserve)

  At the current settings each position would be about $19.65, up to 3 at once.

  Live trading not armed (set LIVE_TRADING_CONFIRM in .env to enable)
```

"Live trading not armed" is expected — `scripts\live.ps1` arms it, for that one
window only.

## 6. Go live

```powershell
powershell -ExecutionPolicy Bypass -File scripts\live.ps1
```

It installs the Solana packages, creates `config.live.yaml` from the
small-wallet template, shows your wallet, checks the market feeds, then asks you
to type `LIVE` in capitals. Nothing is traded until you do.

Then it runs. Leave the window open — closing it stops the bot.

## 7. Watch it

In a second window:

```powershell
cd ~\memecointrading
.\.venv\Scripts\python.exe -m memebot status --config config.live.yaml
.\.venv\Scripts\python.exe -m memebot trades --config config.live.yaml
```

Or the dashboard:

```powershell
.\.venv\Scripts\python.exe scripts\dev_server.py --db data\memebot-live.sqlite3
```

## 8. Stopping

**Ctrl+C** in the bot's window stops it. Open positions stay open — the bot is
just no longer managing them.

To sell everything at market:

```powershell
.\.venv\Scripts\python.exe -m memebot liquidate --config config.live.yaml
```

To take the money out entirely, send the SOL from the wallet back to your
exchange using any Solana wallet app (Phantom, Solflare) with the private key
you saved in step 4.

---

## What it does with your money

Every 45 seconds it scans the market, and for anything it likes:

* sizes the position at **20% of your wallet balance**, capped at **$25**, and
  never more than 0.2% of the pool's liquidity,
* holds at most **3 positions** at once,
* sells at **-25%** (stop loss), **+50%** (take profit), after a **20% pullback**
  from the high once up 25%, after **6 hours**, or immediately if the pool's
  liquidity drains,
* stops trading for the day after a **-20%** day, and stops entirely after
  **-35%** from its peak.

All of that is in `config.live.yaml` — edit it and restart to change it. Raise
`max_position_usd` only after you have watched it work.

The bankroll is read from the chain at the start of every cycle, so it always
sizes against what the wallet really holds. Send more SOL in and it uses it;
the balance drops and it trades smaller.

## Safety rails

| Rail | What it does |
| --- | --- |
| `LIVE_TRADING_CONFIRM` | Set only by `live.ps1`, only for that window. Nothing else can trade for real. |
| Typing `LIVE` | The last manual gate before any order. |
| Fee reserve | 0.025 SOL is never traded, so the wallet can always pay to sell. |
| Price impact check | An order is dropped if the route's impact exceeds your slippage tolerance. |
| Signature check | If the transaction is not the one your wallet should sign, nothing is sent. |
| Settled amounts | Positions are booked from what actually landed on chain, not the quote. |
| Circuit breakers | Daily loss and drawdown limits halt new entries. |

## When something goes wrong

**`NativeCommandError` / a red `python.exe :` block** — you are on an older
version of these scripts. Windows PowerShell treats anything a program writes to
its error stream as fatal, and Python writes perfectly normal messages there.
Update and try again:

```powershell
cd ~\memecointrading
git pull
```

**"No wallet configured"** — run step 4.

**"wallet ... holds 0.0000 SOL, below the fee reserve"** — the SOL has not
arrived, or went to a different address. Check the address on
[solscan.io](https://solscan.io).

**"no route found"** — Jupiter cannot trade that token right now. The bot skips
it and moves on.

**"price impact X% exceeds tolerance"** — the order was too big for the pool.
It is dropped on purpose. Lower `max_position_usd` if it happens constantly.

**Orders keep failing to confirm** — the public RPC is overloaded. Get a free
key from [Helius](https://helius.dev) and put it in `.env`:

```
MEMEBOT_RPC_URL=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
```

**It never buys anything** — run
`.\.venv\Scripts\python.exe -m memebot doctor --config config.live.yaml`. The
pipeline line tells you whether the filters or the score threshold are the
blocker.

**You want it to stop right now** — Ctrl+C, then `liquidate` as in step 8.

---

## Do not run live trading in the cloud

Not on Vercel, not in GitHub Actions. A wallet key in a cloud environment
variable is a key you have handed to a platform, and a function that times out
mid-swap leaves you holding a position nothing is managing. Live trading runs on
your machine, where you can see it and stop it.
