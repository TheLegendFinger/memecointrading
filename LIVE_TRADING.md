# Trading — step by step

This walks you from a fresh clone to a bot placing real swaps on Solana with
real money.

> **Read this once first.** The bot buys brand-new memecoins. A large share of
> them go to zero, some cannot be sold at all, and it will keep trading until
> you stop it. Fund it with an amount you would be genuinely fine losing in
> full — treat it as gone the moment you send it. Nothing here is financial
> advice, and the strategy has not been backtested.

---

## 0. What you need

* **Python 3.9+**
  * Windows: `winget install Python.Python.3.12`
  * macOS: `brew install python` (or from python.org)
* **Git**
  * Windows: `winget install Git.Git`
  * macOS: comes with the Xcode command line tools, `xcode-select --install`
* Some **SOL**. $30–100 is plenty to start. You can buy it on any exchange
  (Coinbase, Kraken, Binance) and withdraw it to the wallet this bot creates.

Open a terminal — **PowerShell** on Windows (Start, type `powershell`),
**Terminal** on macOS — and work through the steps below in order.

Commands are shown for both. Use the one for your machine.

---

## 1. Get the code

```bash
# macOS
cd ~
git clone https://github.com/TheLegendFinger/memecointrading.git
cd memecointrading
```

```powershell
# Windows
cd ~
git clone https://github.com/TheLegendFinger/memecointrading.git
cd memecointrading
```

## 2. Install and open the menu

```bash
# macOS
./scripts/start.sh
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

The first run sets everything up (a private Python environment inside the
folder — it takes a minute), then opens the menu. On Windows
`-ExecutionPolicy Bypass` is needed because unsigned scripts are blocked by
default; it applies to that one command only.

**From here you can do the whole thing with numbers**: `8` for the wallet, `9`
to check the feeds, `5` to see the market, `1` to trade for real. The steps
below give the equivalent commands if you prefer typing them.

## 3. Look at what it would buy

Menu option **5** scans the live market and prints the candidates it would
consider, scored, without trading. Menu **7** checks the data feeds are working.

Do both before step 4. If the scan finds nothing, the market is quiet or your
settings are strict — the health check tells you which, and the
[README](README.md#configuration) says what to loosen.

## 4. Create the wallet

Menu option **6**, then **2**. (Or `./scripts/wallet.sh --new --save` /
`scripts\wallet.ps1 -New -Save`.)

It prints an **address** and a **12-word seed phrase**, and saves both to `.env`
(which git ignores).

* **Write the seed phrase down on paper, now.** Those words are the wallet.
  They restore it in Phantom or Solflare if this machine dies, and anyone who
  has them can take everything in it.
* Never type the phrase into a website. The only place it belongs is paper.
* This is a **burner**. Never use a wallet that holds anything else — the bot
  can spend everything in it.

## 5. Fund it

Send SOL from your exchange to the address from step 4. Choose the **Solana
network** when withdrawing — sending on the wrong network loses the funds.

Check it arrived:

```bash
./scripts/wallet.sh          # macOS
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\wallet.ps1   # Windows
```

It shows the balance and what each trade would be worth at your settings:

```
  Address   4Nzz5f1qub5YNCUJyMjg7sNEzoi3YKnQrfF8CZhTDYDN
  SOL       0.520000  ($103.22)
  Tradable  $98.26  (after a 0.025 SOL fee reserve)

  At the current settings each position would be about $19.65, up to 3 at once.

  Live trading not armed (set LIVE_TRADING_CONFIRM in .env to enable)
```

"Live trading not armed" is expected — starting the bot arms it, for that one
run only.

## 6. Start trading

Menu option **1**, or:

```bash
./scripts/run.sh             # macOS
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts\run.ps1   # Windows
```

It installs the Solana packages, creates `config.yaml` from the small-wallet
template, shows your wallet, then asks you to type `LIVE` in capitals. Nothing
is traded until you do.

Then it runs. Leave the window open — closing it stops the bot.

## 7. Watch it

In a second window:

```bash
./.venv/bin/python -m memebot status          # macOS
./.venv/bin/python -m memebot trades
```
```powershell
.\.venv\Scripts\python.exe -m memebot status   # Windows
.\.venv\Scripts\python.exe -m memebot trades
```

The running window already shows all of this live; these are for checking on it
from a second window without interrupting the bot.

## 8. Stopping

**Ctrl+C** in the bot's window stops it. Open positions stay open — the bot is
just no longer managing them.

To sell everything at market:

```bash
./.venv/bin/python -m memebot liquidate        # macOS
```
```powershell
.\.venv\Scripts\python.exe -m memebot liquidate  # Windows
```

To take the money out, use menu **6** then **5** (Withdraw) — it sends SOL to
any address you give it, an amount or `all`. Close positions first (menu **2**),
because withdrawing moves SOL and not the memecoins.

You can also import the seed phrase from step 4 into Phantom and move funds from
there.

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

All of that is in `config.yaml` — edit it and restart to change it. Raise
`max_position_usd` only after you have watched it work.

The bankroll is read from the chain at the start of every cycle, so it always
sizes against what the wallet really holds. Send more SOL in and it uses it;
the balance drops and it trades smaller.

## Safety rails

| Rail | What it does |
| --- | --- |
| `LIVE_TRADING_CONFIRM` | Set only when you start the bot, only for that process. Nothing else can trade. |
| Typing `LIVE` | The last manual gate before any order. |
| Fee reserve | 0.025 SOL is never traded, so the wallet can always pay to sell. |
| Price impact check | An order is dropped if the route's impact exceeds your slippage tolerance. |
| Signature check | If the transaction is not the one your wallet should sign, nothing is sent. |
| Settled amounts | Positions are booked from what actually landed on chain, not the quote. |
| Circuit breakers | Daily loss and drawdown limits halt new entries. |

## When something goes wrong

**`NativeCommandError` / a red `python.exe :` block** (Windows) — you are on an older
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

**It never buys anything** — run the health check (menu **7**). The pipeline
line tells you whether the filters or the score threshold are the blocker.

**You want it to stop right now** — Ctrl+C, then `liquidate` as in step 8.

---

## Do not run live trading in the cloud

Not on Vercel, not in GitHub Actions. A wallet key in a cloud environment
variable is a key you have handed to a platform, and a function that times out
mid-swap leaves you holding a position nothing is managing. There is
deliberately no endpoint anywhere in this project that can place an order — the
optional web dashboard only reads.
