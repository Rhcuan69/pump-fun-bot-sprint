"""Pump.fun replay bot: patient early-momentum rider.

Strategy, in one paragraph: watch every new bonding curve from its CreateEvent and
keep a short tape of its trades. Enter only once the coin has proven it is not an
instant bundle-and-dump: at least 5 trades and 10 slots since creation, real SOL reserves of 1-100 SOL, a
price up 30-60% over its last 10 trades while staying within 15% of the window high,
net buy flow of at least 0.05 SOL with at least 6 of the last 10 trades buys from at
least 4 distinct wallets, and the creator not having sold. Buy a fixed 0.03 SOL (one
shot, no pyramiding). Exit the whole position on the first of: +30% take-profit, a
trailing stop 10% below the peak once the position is +15% up, a -25% stop-loss, a
1500-slot time stop, the creator selling, or a sudden sell-off (5 straight sells
totalling 0.5 SOL) in the tape. One entry per coin
at a time, at most 5 open positions, at most 3 entries per coin per run.

Costs that shape the numbers: a round trip pays 95 bps protocol + 30 bps creator
fees on each leg plus 105,000 lamports of tx fees per order, so a 0.03 SOL stake
needs roughly a 3-4% move just to break even; the +30% / -25% exits are set well
outside that band. The per-reply size cap is 1% of a curve's real SOL reserves,
which is why entries wait for 1 SOL of real reserves.

Determinism: a pure function of the batches the harness sends. No clock, no
randomness, no environment reads, no I/O. Iteration is over insertion-ordered
dicts only; sets are used for membership/size, never iterated.

Wire protocol: agent-fun/replay/v2, spoken through the vendored SDK shim
(vendor/makefun_bot.py, the kit's own Python shim, standard library only).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot

LAMPORTS_PER_SOL = 1_000_000_000

# --- entry parameters -------------------------------------------------------
MIN_TRADES = 5            # trades seen before an entry is allowed
MIN_AGE_SLOTS = 10        # slots since the CreateEvent
MIN_REAL_SOL = 1_000_000_000      # 1 SOL of real reserves (1% cap >= 0.01 SOL)
MAX_REAL_SOL = 100_000_000_000    # 100 SOL; bigger curves have little upside left
MOM_WINDOW = 10           # trades of lookback for the entry signal
MIN_RUN = 1.30            # price now vs price MOM_WINDOW trades ago, at least +30%
MAX_RUN = 1.60            # ... and at most +60% (do not chase the top)
NEAR_HIGH = 0.85          # price now at least 85% of the window high
MIN_NET_FLOW_SOL = 50_000_000     # net buy SOL over the window, at least 0.05 SOL
MIN_BUYS = 6              # buys among the last MOM_WINDOW trades
MIN_BUYERS = 4            # distinct buying wallets in the window
STAKE = 30_000_000        # 0.03 SOL per position, one shot
MIN_BUY = 10_000_000       # smallest buy worth sending (round-trip guardrail)
MAX_ENTRIES_PER_MINT = 3
COOLDOWN_SLOTS = 300

# --- exit parameters --------------------------------------------------------
TAKE_PROFIT = 1.30        # sell when position value reaches 130% of cost
TRAIL_ARM = 1.15          # trailing stop arms once value reaches 115% of cost
TRAIL = 0.90              # ... then sell at 10% below the peak value
STOP_LOSS = 0.75          # sell when value falls to 75% of cost
MAX_HOLD_SLOTS = 1500     # sell after this many slots held, whatever the price
MAX_HOLD_TRADES = 0       # sell after this many trades on the mint since entry; 0 = off
SELL_OFF_TRADES = 5       # a run of this many straight sells in the tape ...
SELL_OFF_MIN_SOL = 500_000_000  # ... totalling at least 0.5 SOL exits the position
EXIT_ON_CREATOR_SELL = True  # exit at once when the creator sells
EXIT_ON_SELL_OFF = True      # exit on a sudden sell-off in the tape

BUY_SLIPPAGE_BPS = 1000   # 10% on buys; fills land 2 slots after the decision
SELL_SLIPPAGE_BPS = 10000  # sells always go out; never get stuck in a dead coin


class Tape:
    """One curve's recent trades, newest last, bounded to the last 40."""

    __slots__ = ("trades", "creator", "creator_sold", "created_slot")

    def __init__(self, created_slot, creator):
        self.trades = []          # (slot, price, is_buy, sol_lamports, user)
        self.creator = creator
        self.creator_sold = False
        self.created_slot = created_slot

    def add(self, slot, price, is_buy, sol, user):
        self.trades.append((slot, price, is_buy, sol, user))
        if len(self.trades) > 40:
            del self.trades[: len(self.trades) - 40]
        if user == self.creator and not is_buy:
            self.creator_sold = True


class Position:
    """Bookkeeping for one open position, synced to the harness account."""

    __slots__ = ("entry_slot", "peak_value", "entries", "trades")

    def __init__(self, entry_slot, value):
        self.entry_slot = entry_slot
        self.peak_value = value
        self.entries = 1
        self.trades = 0


class Rider:
    def __init__(self):
        self.tapes = {}        # mint -> Tape, insertion order
        self.positions = {}    # mint -> Position, synced to state.positions
        self.entries = {}      # mint -> entries used this run
        self.last_exit = {}    # mint -> slot of last exit (cooldown)
        self.max_open = 5

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def price_of(curve):
        vt = curve.virtual_token_reserves
        if vt <= 0:
            return 0.0
        return curve.virtual_quote_reserves / vt

    def value_of(self, state, mint):
        holding = state.positions.get(mint)
        curve = state.curves.get(mint)
        if holding is None or curve is None:
            return 0
        price = self.price_of(curve)
        if price <= 0.0:
            return 0
        return int(holding.token_amount * price)

    def entry_ok(self, state, mint, tape, curve):
        """All entry filters; returns the buy size in lamports, or 0."""
        if len(tape.trades) < MIN_TRADES:
            return 0
        if state.slot - tape.created_slot < MIN_AGE_SLOTS:
            return 0
        if tape.creator_sold:
            return 0
        if curve.complete:
            return 0
        if self.entries.get(mint, 0) >= MAX_ENTRIES_PER_MINT:
            return 0
        last_exit = self.last_exit.get(mint)
        if last_exit is not None and state.slot - last_exit < COOLDOWN_SLOTS:
            return 0
        real_sol = curve.real_quote_reserves
        if real_sol < MIN_REAL_SOL or real_sol > MAX_REAL_SOL:
            return 0
        window = tape.trades[-MOM_WINDOW:]
        price_now = self.price_of(curve)
        price_then = window[0][1]
        if price_then <= 0.0 or price_now <= 0.0:
            return 0
        run = price_now / price_then
        if run < MIN_RUN or run > MAX_RUN:
            return 0
        high = 0.0
        for t in window:
            if t[1] > high:
                high = t[1]
        if high > 0.0 and price_now < NEAR_HIGH * high:
            return 0
        buys = 0
        net_flow = 0
        buyers = set()
        for t in window:
            if t[2]:
                buys += 1
                net_flow += t[3]
                buyers.add(t[4])
            else:
                net_flow -= t[3]
        if buys < MIN_BUYS:
            return 0
        if net_flow < MIN_NET_FLOW_SOL:
            return 0
        if len(buyers) < MIN_BUYERS:
            return 0
        size = STAKE
        cap = state.max_buy_lamports(mint)
        if cap < size:
            size = cap
        if size < MIN_BUY:
            return 0
        return size

    def exit_reason(self, state, mint, tape, curve, pos):
        """Return a reason string when the position should be sold whole, else None."""
        holding = state.positions.get(mint)
        if holding is None:
            return None
        value = self.value_of(state, mint)
        cost = holding.cost_lamports
        if value <= 0 or cost <= 0:
            return None
        if value > pos.peak_value:
            pos.peak_value = value
        ratio = value / cost
        if ratio >= TAKE_PROFIT:
            return "take-profit"
        if ratio <= STOP_LOSS:
            return "stop-loss"
        if pos.peak_value >= TRAIL_ARM * cost and value <= TRAIL * pos.peak_value:
            return "trailing-stop"
        if state.slot - pos.entry_slot >= MAX_HOLD_SLOTS:
            return "time-stop"
        if MAX_HOLD_TRADES > 0 and pos.trades >= MAX_HOLD_TRADES:
            return "trade-stop"
        if tape is not None and tape.creator_sold and EXIT_ON_CREATOR_SELL:
            return "creator-sold"
        if tape is not None and EXIT_ON_SELL_OFF and len(tape.trades) >= SELL_OFF_TRADES:
            tail = tape.trades[-SELL_OFF_TRADES:]
            sold = 0
            all_sells = True
            for t in tail:
                if t[2]:
                    all_sells = False
                    break
                sold += t[3]
            if all_sells and sold >= SELL_OFF_MIN_SOL:
                return "sell-off"
        return None


def on_hello(state):
    state.bot = Rider()
    state.bot.max_open = state.hello["caps"]["max_open_positions"]


def on_batch(state, batch):
    bot = state.bot
    orders = []
    slot = state.slot

    # Fold this batch's events into the tapes; remember which mints moved.
    moved = []
    seen_moved = set()
    for e in batch["events"]:
        kind = e["event"]
        mint = e["mint"]
        if kind == "CreateEvent":
            if mint not in bot.tapes:
                bot.tapes[mint] = Tape(e["slot"], e["creator"])
        elif kind == "TradeEvent":
            tape = bot.tapes.get(mint)
            if tape is None:
                tape = Tape(e["slot"], e["creator"])
                bot.tapes[mint] = tape
            vt = int(e["virtual_token_reserves"])
            vq = int(e["virtual_quote_reserves"])
            price = (vq / vt) if vt > 0 else 0.0
            tape.add(e["slot"], price, bool(e["is_buy"]), int(e["sol_amount"]), e["user"])
            if mint not in seen_moved:
                seen_moved.add(mint)
                moved.append(mint)
        else:  # CompleteEvent
            tape = bot.tapes.get(mint)
            if tape is None:
                bot.tapes[mint] = Tape(e["slot"], "")

    # Sync position bookkeeping with the harness account.
    for mint in list(bot.positions.keys()):
        if mint not in state.positions:
            bot.last_exit[mint] = slot
            del bot.positions[mint]
    # Count this batch's trades per mint for the trade-based hold.
    batch_trades = {}
    for e in batch["events"]:
        if e["event"] == "TradeEvent":
            batch_trades[e["mint"]] = batch_trades.get(e["mint"], 0) + 1
    for mint, n in batch_trades.items():
        pos = bot.positions.get(mint)
        if pos is not None:
            pos.trades += n
    for mint in state.positions:
        if mint not in bot.positions:
            value = bot.value_of(state, mint)
            bot.positions[mint] = Position(slot, value)
            bot.entries[mint] = bot.entries.get(mint, 0) + 1

    # Exits first: sell whole positions that hit an exit rule.
    pending_sells = set()
    for p in state.pending.values():
        if p.order["side"] == "sell":
            pending_sells.add(p.order["mint"])
    for mint in list(state.positions.keys()):
        if mint in pending_sells:
            continue
        holding = state.positions[mint]
        tape = bot.tapes.get(mint)
        curve = state.curves.get(mint)
        if curve is None:
            continue
        pos = bot.positions.get(mint)
        if pos is None:
            continue
        reason = bot.exit_reason(state, mint, tape, curve, pos)
        if reason is not None:
            orders.append(
                {
                    "side": "sell",
                    "mint": mint,
                    "token_amount": holding.token_amount,
                    "max_slippage_bps": SELL_SLIPPAGE_BPS,
                }
            )

    # Entries: only mints whose tape moved in this batch can newly qualify.
    pending_buys = set()
    for p in state.pending.values():
        if p.order["side"] == "buy":
            pending_buys.add(p.order["mint"])
    if state.committed_position_count() < bot.max_open:
        free = state.free_cash_lamports()
        buys_added = 0
        for mint in moved:
            if mint in state.positions or mint in pending_buys:
                continue
            if state.committed_position_count() + buys_added >= bot.max_open:
                break
            tape = bot.tapes.get(mint)
            curve = state.curves.get(mint)
            if tape is None or curve is None:
                continue
            size = bot.entry_ok(state, mint, tape, curve)
            if size <= 0:
                continue
            if free < size + state.tx_fee_lamports():
                continue
            free -= size + state.tx_fee_lamports()
            buys_added += 1
            orders.append(
                {
                    "side": "buy",
                    "mint": mint,
                    "quote_lamports": size,
                    "max_slippage_bps": BUY_SLIPPAGE_BPS,
                }
            )
            pending_buys.add(mint)

    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch, on_hello=on_hello)
