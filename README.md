# Pump.fun Meme-Coin Scalper

Watches **every new meme coin on Pump.fun** as it launches. For each one it
**checks the social links** (no website, X or Instagram means no buy),
**screens for rug-pull signs**, and **scores the coin's potential** from live
trading activity. It scalps the strongest coins, the ones with **lots of
trades** from many real wallets, and it does this as often as the risk limits
allow.

The previous strategy, copying the top traders, is still available with
`STRATEGY=copy` (see [Copy strategy](#copy-strategy-strategycopy)).

> ⚠️ **Risk warning.** Meme coins are extremely volatile; most lose nearly all
> their value within hours. Rug checks reduce the risk but can't remove it.
> A determined scammer can pass every check. You can lose everything you put
> in. The bot runs in **paper mode by default**. Run it on paper for days
> before you risk real SOL, and only use a dedicated hot wallet holding money
> you can afford to lose. None of this is financial advice.

## How the scalper works

```
new coin launched ──► fetch metadata ──► website / X / Instagram?  ── no ──► never buy
       │                                        │ yes
       ▼                                        ▼
 every trade updates:  holders · dev bag · activity     rug flags ──► never buy (+ blacklist dev)
       │
       ▼   every 2s, for coins aged 20s – 30min
 hard checks: socials · rug risk · ≥30 trades/60s · ≥15 traders · ≥60% buying
              · net inflow ≥2 SOL · mcap 30–350 SOL · not already +150%
       │ all pass
       ▼
 potential score 0–100  (trades 30 · traders 20 · buy pressure 20 · inflow 15 · acceleration 15)
       │ ≥ 60
       ▼
 BUY ──► exits: −12% stop · 3 min max · momentum fades · dev sells · whale dumps
     └─ hits +20% ──► RUNNER: no cap, trail 10% below peak, never below +20%
 then the same coin can be scalped again after 60s (max 3 times)
```

### 1. Socials: no website, X or Instagram means no buy (`socials.py`)
- **Where links come from:** each coin's metadata file on IPFS. The bot tries
  several IPFS gateways because public ones rate-limit.
- **Which links count:** the website, X and Telegram fields, plus links in the
  description, since Instagram has no field of its own. You need at least one
  of website, X or Instagram (`SOCIALS_MODE=any`), or every one of them with
  `SOCIALS_MODE=all`.
- **Junk links are ignored:**
  - pump.fun, chart sites and explorers;
  - a bare `x.com`, an X search, or **someone else's tweet**;
  - an Instagram post instead of a profile.

  Only the coin's own X profile or community counts.
- **Reused links:** if another coin already used the same website, X or
  Instagram, this coin is flagged. Scammers often copy a real project's links
  onto a fake coin.
- **Missing metadata:** if the metadata can't be fetched, the coin isn't bought.

### 2. Rug-pull protection (`potential.py`, `scalper.py`)

Pump.fun coins can't have their liquidity pulled or holders frozen. Rugs
happen when the **creator or insiders dump** their tokens. So the bot watches
who holds what and how they behave:

| Check | Action |
|---|---|
| Dev sells **any** tokens | Coin flagged forever, **creator blacklisted** (saved across restarts). If held, sell immediately. |
| Creator is blacklisted, or launched >3 coins in the last hour | Never buy their coins |
| >25% of supply bought in the first 3 seconds (bundled launch) | Never buy |
| Dev holds >10%, top-10 wallets hold >35%, or one wallet holds >8% | Skip (re-checked later) |
| Few wallets making many trades (diversity <25%) | Skip: fake volume / wash trading |
| One wallet sells ≥3% of supply in one trade while we hold | Sell immediately |
| Links reused from another coin | Never buy |

### 3. Potential: focus on high trading activity (`potential.py`, `tokens.py`)
Measured over the last 60 seconds:
- **Number of trades:** the biggest weight in the score.
- **Unique traders:** many different wallets, not a few bots.
- **Buy pressure:** the share of SOL volume that's buying.
- **Net inflow:** more SOL coming in than going out.
- **Acceleration:** trading speeding up versus the previous 30 seconds.
- **Not already pumped:** coins already up more than 150% in the window are
  skipped, so the bot doesn't buy the top.

### 4. Scalping
- **Exits:**
  - stop loss at −12%;
  - a 3-minute maximum hold;
  - **momentum fade**, when ≥65% of the last 15 seconds' volume is selling;
  - the dev selling, or a whale dumping.
- **Winners:** at +20% a trade becomes a runner. It trails 10% below the peak
  and never closes below +20%.
- **Repeat scalps:** the bot can scalp the same coin again 60 seconds later,
  up to 3 times, with up to 5 positions open at once.
- **Status line:** every minute the bot logs how many coins it saw, how many
  had socials, how many were rug-flagged, how many trades it entered, and the
  most common reasons it skipped coins.

## Execution (both strategies)
- **Live:**
  - PumpPortal's `trade-local` API builds the transaction. The bot signs it
    **locally** (your key never leaves your machine) and sends it through your
    RPC.
  - It **waits for on-chain confirmation** and reads the **real** token balance
    and SOL change from the chain. A buy that fails or never lands doesn't
    become a position.
  - A sell that fails is retried every second.
- **Paper:**
  - **Delay:** fills happen 1.5 s after the decision, at the market price at
    that moment.
  - **Price limit:** if price moved more than `SLIPPAGE_PCT` in that time, the
    order fails, like a real one would.
  - **Costs:** every fill also costs 3% price impact plus fees.
- **Limits:** a daily loss cap (`MAX_DAILY_LOSS_SOL`) stops new trades for the
  day once it's hit.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit .env
python -m pumpbot
```

### Required: PumpPortal API key
PumpPortal streams **trade** data only when you connect with an API key
funded with **at least 0.02 SOL**. Without one, the bot gets new-coin events
but can't see any trades (it warns you at startup). Create a key at
<https://pumpportal.fun>, fund its linked wallet, and set `PUMPPORTAL_API_KEY`
in `.env`. Treat the key like a password: it can trade with that wallet's funds.

### Going live
1. Run in paper mode (`DRY_RUN=true`) until you've seen a few dozen closed
   trades in the `status:` lines. Check the win rate and PnL.
2. Create a **new** wallet just for the bot. Fund it with a small amount.
3. Set `PRIVATE_KEY` (base58), a fast paid `RPC_URL`, and `DRY_RUN=false`.
   Start with a small `BUY_AMOUNT_SOL` such as 0.02.

## Tuning

Every setting is in `.env.example`. The most important ones for the scalper:

| Setting | Default | Meaning |
|---|---|---|
| `REQUIRED_SOCIALS` / `SOCIALS_MODE` | website,twitter,instagram / any | which links a coin must have |
| `SCALP_MIN_TRADES` | 30 | minimum trades in the last 60s |
| `SCALP_MIN_UNIQUE_TRADERS` | 15 | minimum different wallets in the last 60s |
| `SCALP_MIN_SCORE` | 60 | potential score needed to buy |
| `RUG_MAX_DEV_PCT` | 10 | maximum creator share of supply |
| `TAKE_PROFIT_PCT` | 20 | gain where a trade becomes a runner (lower it, e.g. 10, for quicker scalps) |
| `STOP_LOSS_PCT` | 12 | cut losers |
| `SCALP_MAX_HOLD_SEC` | 180 | maximum time in a scalp before +TP |
| `SCALP_MAX_OPEN_POSITIONS` | 5 | positions open at once |
| `BUY_AMOUNT_SOL` | 0.1 | size per trade |
| `MAX_DAILY_LOSS_SOL` | 0.5 | stop opening trades for the day after this loss |

The +20% is measured from your **real cost per token**, which includes the
buy fee, slippage and priority fee. A trade that closes at the +20% floor
still pays the sell fee and slippage, so it nets roughly **+15–18%**.

## Copy strategy (`STRATEGY=copy`)
The original strategy:
- **Ranking:** the bot ranks wallets by realised profit on new coins over the
  last 24 hours. Scalpers that flip in under 2 minutes are skipped.
- **Following:** it follows the top 25. Each copy is scored on the trader's
  measured hit rate for the +20% strategy, how far price has already moved,
  whether other top traders are buying the same coin, and buy size.
- **Exits:** it uses the same +20% runner exits.

Its settings are in the `COPY STRATEGY` section of `.env.example`.

## Known limits
- **Late subscription:** the bot subscribes to a coin's trades right after its
  create event. Trades in the first fraction of a second can be missed, so
  holder numbers are close but not exact.
- **Metadata from datacenters:** IPFS gateways rate-limit, and Pump.fun's own
  API blocks datacenter IPs. Coins whose metadata can't be fetched are skipped.
- **Unconfirmed live buys:** if a live buy isn't confirmed within
  `CONFIRM_TIMEOUT_SEC` and no tokens have arrived, the bot treats it as failed.
  It logs a warning, because the transaction could still land later.
- **Stops aren't guaranteed prices:** the stop loss and the +20% floor are
  triggers. When a coin crashes in one trade, the sell fills at the next
  available price.

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
