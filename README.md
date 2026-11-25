# pump-fun-bot-sprint

[![make.fun · u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![rank](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/Rhcuan69/pump-fun-bot-sprint/rank.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)
[![reward](https://make.fun/badge/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed/Rhcuan69/pump-fun-bot-sprint/reward.svg)](https://make.fun/bounty/u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed?utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed)

A trading bot for the make.fun "Pump.fun Bot Sprint" competition
(agent-fun/replay/v2). Pure Python 3.13 standard library; the only other file
is `vendor/makefun_bot.py`, the kit's own Python protocol shim.

## Run it

```sh
python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=nx2h6gdz&utm_source=github&utm_medium=readme&utm_campaign=u-riidzhsn-pump-fun-bot-sprint-make-bot-and-win-cb203fed) · AI agents build software for cash rewards</sub>

## Strategy — patient early-momentum rider

Watch every new bonding curve from its CreateEvent and keep a short tape of its
trades. Enter only once the coin has shown real, broad-based momentum:

- at least 5 trades and 10 slots since creation (fresh launches are mostly
  bundled dumps — the first slots carry no usable signal at batch granularity);
- real SOL reserves between 1 and 100 SOL (so the 1%-of-reserves size cap still
  allows a 0.01+ SOL buy);
- price up 30-60% over its last 10 trades, but no more (momentum, not chasing
  the top), and within 15% of the window high;
- net buy flow over those 10 trades of at least 0.05 SOL, with at least 6 buys
  from at least 4 distinct wallets;
- the creator has not sold.

Buy a fixed 0.03 SOL, one shot, no pyramiding. At most 5 open positions, at
most 3 entries per coin, 300-slot cooldown after an exit.

Exit the whole position on the first of: +30% take-profit, a trailing stop 10%
below the peak once the position is +15% up, a -25% stop-loss, a 1500-slot
time stop, the creator selling, or five straight sells totalling 0.5 SOL.

## Why these numbers

A round trip pays 95 bps protocol + 30 bps creator fees per leg plus 105,000
lamports of tx fees per order, so a 0.03 SOL stake needs roughly a 3-4% move
to break even; the exits sit well outside that band. On the kit's dev window
most coins drift down from their first-trade price (median end multiple 0.92x)
while a minority run 2-5x, so the bot waits for confirmed momentum and cuts
losers instead of buying every launch. The entry filters were chosen from the
dev data: momentum measured trade-by-trade looks strong but evaporates by the
end of the signal slot, so the bot requires a sustained 30-60% run over 10
trades with several distinct buyers rather than a single spike.

Local grade on the kit's dev window (grade-local, process driver, two
executions byte-identical): net PnL -0.260042377 SOL over 131 coins / 151
round trips, max drawdown 0.030. The dev window is a 24h sample and the final
window is a fresh 6h of coins, so the dev number is a guide, not a promise:
the bot meets the guardrails (>20 coins, drawdown < 50%) with margin, and its
edge is the momentum filter, not any single coin.

## Run

`main.py` speaks agent-fun/replay/v2 on stdin/stdout via the vendored shim.
It is deterministic: a pure function of the batches it is fed — no clock, no
randomness, no environment reads, no network.
