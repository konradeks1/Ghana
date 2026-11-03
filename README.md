# Pump.fun Copy-Trade Bot

Finds the **25 most profitable meme-coin traders on Pump.fun**, watches their
entries live, **analyses each entry** to estimate whether copying it will make
money, and only goes in when the odds favour a **+20% take-profit**.

> ⚠️ **Risk warning.** Meme coins are extremely volatile; most lose nearly all
> their value. Copy trading always fills *after* the leader, often at a worse
> price. You can lose everything you put in. The bot runs in **paper mode by
> default**. Run it on paper for days before you risk real SOL, and only use a
> dedicated hot wallet holding money you can afford to lose. None of this is
> financial advice.

## How it works

```
PumpPortal websocket ──► new tokens ──► all trades on fresh tokens
                                            │
                                            ▼
                         Leaderboard: realised PnL per wallet (24h window)
                                            │  top 25 (min 3 closed trades, PnL > 0)
                                            ▼
                         Follow those wallets (account-trade stream)
                                            │  a top trader buys
                                            ▼
          Entry analyzer ── P(+20% before −SL), expected value after fees
                                            │  approved + risk checks pass
                                            ▼
                    BUY ──► −12% stop │ 20 min timeout │ leader sells
                         │
                         └─ hits +20% ──► RUNNER: no cap, trail 10% below peak,
                                          never sell below +20%, 60 min max
```

1. **Discovery (`leaderboard.py`)** – Subscribes to every new Pump.fun token
   (the newest 400 at a time) and records every buy and sell. Each wallet gets
   realised PnL on an average-cost basis, plus closed round trips with return
   and hold time. Wallets are ranked by realised profit over the last 24h. Only
   wallets with at least 3 closed trades and positive PnL qualify. The top 25
   are re-ranked every 5 minutes. While the board warms up, any wallets in
   `traders.txt` fill the empty slots.
2. **Entry back-testing (`outcomes.py`)** – Every buy by a followed trader is
   tracked: did price reach **+20% before −12%** within 30 minutes? That gives
   each trader a hit rate for *this exact strategy*, separate from their own
   PnL. (A trader can make money from a few 10x wins while most of their entries
   would fail a +20% scalp.)
3. **Entry analysis (`analyzer.py`)** – When a top trader buys, the bot works out:
   - their strategy hit rate (Beta-smoothed: unknown traders start at 40%) blended with their share of round trips that returned at least 20%
   - a **chase penalty**: if price is already above their entry, the move still needed is bigger. Above 8% the bot skips.
   - **confluence**: other top-25 wallets buying the same token in the last 5 minutes raise the probability
   - **conviction**: a buy much larger or smaller than the trader's usual size
   - **filters**: minimum leader buy (0.3 SOL) and a market-cap band (25–400 SOL)

   It then computes `EV = p·TP − (1−p)·SL − fees`. It enters only when
   `p ≥ 0.55` and `EV ≥ +3%`. Every decision is logged with its reasons.
4. **Execution (`executor.py`)** – Paper mode simulates fills with fees and
   slippage. Live mode asks PumpPortal's `trade-local` API to build the
   transaction, signs it **locally** (your key never leaves your machine) and
   sends it through your RPC.
5. **Exits and risk (`positions.py`)**
   - *Before +20%:* stop loss at −12%, close after 20 minutes, and exit if the
     leader sells.
   - *At +20%, the trade becomes a runner:* the bot doesn't sell, so the gain can
     keep climbing (+50%, +100%, …). The bot tracks the peak price and sells
     when price falls 10% below it. A **+20% floor** means a runner never closes
     below +20%. Leader sells no longer close runners, since leaders often take
     profit early. Runners close after 60 minutes at most.
   - Set `LET_WINNERS_RUN=false` to go back to selling at exactly +20%.
   Limits: at most 3 open positions, a daily loss cap, and a 30-minute
   cooldown before re-buying the same token.

State (leaderboard stats, positions, paper balance) is saved to `state.json`,
so a restart keeps everything the bot has learned.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit .env
cp traders.example.txt traders.txt   # optional bootstrap wallets
python -m pumpbot
```

### Required: PumpPortal API key
PumpPortal streams **trade** and **account** data only when you connect with
an API key funded with **at least 0.02 SOL**. Without one, you get new-token
events only, and the bot cannot rank or follow traders (it warns you at
startup). Create a key at <https://pumpportal.fun>, fund its linked wallet,
and set `PUMPPORTAL_API_KEY` in `.env`.

### Going live
1. Run in paper mode (`DRY_RUN=true`) for long enough to see a few dozen
   closed trades in the `status:` log lines. Check the win rate and PnL.
2. Create a **new** wallet just for the bot. Fund it with a small amount.
3. Set `PRIVATE_KEY` (base58), a fast paid `RPC_URL`, and `DRY_RUN=false`.

## Tuning

All knobs are in `.env.example`. The most important ones:

| Setting | Default | Meaning |
|---|---|---|
| `TOP_N_TRADERS` | 25 | how many top wallets to follow |
| `TAKE_PROFIT_PCT` | 20 | the gain where a trade becomes a runner, measured on price from your fill |
| `LET_WINNERS_RUN` | true | +20% is the minimum, not the cap |
| `TRAILING_STOP_PCT` | 10 | runner sells this far below its peak |
| `TP_FLOOR_PCT` | 20 | a runner never exits below this gain |
| `STOP_LOSS_PCT` | 12 | cut losers |
| `MIN_WIN_PROB` | 0.55 | minimum estimated chance of hitting TP first |
| `MIN_EXPECTED_VALUE_PCT` | 3 | minimum EV after fees |
| `MAX_CHASE_PCT` | 8 | skip if price already ran past the leader's entry |
| `BUY_AMOUNT_SOL` | 0.1 | size per trade |
| `MAX_DAILY_LOSS_SOL` | 0.5 | stop opening trades for the day after this loss |

The +20% is measured on token price. After Pump.fun's 1% fee on each side,
slippage and priority fees, a trade that closes at the +20% floor nets roughly
**+15–17%**. Raise `TP_FLOOR_PCT` to about 25 if you want **+20% net**.

## Known limits
- The leaderboard only sees trades on tokens tracked since the bot started. A
  wallet's sells of tokens bought earlier are ignored. Rankings get better the
  longer the bot runs.
- In live mode, token amounts and sale proceeds are **estimated** from price.
  Sells always send `100%` of the balance, so positions close fully.
- Copy trading has latency. The chase filter limits it but cannot remove it.

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
