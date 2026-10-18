# The risk model

Read this before pointing the pack at anything but a demo. It describes what is
enforced, where the enforcement lives, and what it cannot protect you from.

## Two layers, and why the split matters

**The config file** holds the working limits. It lives next to the server and is
easy to read and change. It can only ever make the kernel *tighter*.

**`HARD_CEILINGS` in `mt5trader/config.py`** is compiled into the source. Every
config value is clamped against it when the process starts, and the clamp is
reported rather than applied silently:

```
$ python -m mt5trader config
notes
  - config risk.risk_per_trade_pct=999 clamped to the hard ceiling 2.0
```

| ceiling | value | what it bounds |
| --- | --- | --- |
| `risk_per_trade_pct` | 2.0 % | equity lost if one stop is hit |
| `risk_per_trade_usd` | 2000 | the same, in money, for large accounts |
| `total_risk_pct` | 6.0 % | everything open at once, stops included |
| `daily_loss_pct` | 5.0 % | equity against equity at server midnight |
| `daily_loss_usd` | 5000 | the same, in money |
| `max_drawdown_pct` | 20.0 % | equity against the peak for the day |
| `max_open_positions` | 20 | positions this kernel owns |
| `orders_per_day` | 50 | orders sent in one server day |
| `orders_per_minute` | 6 | the runaway-loop brake |
| `max_lots_per_order` | 10.0 | one order's volume |
| `max_spread_points` | 200 | entry cost |
| `max_slippage_points` | 50 | deviation tolerance |
| `max_quote_age_seconds` | 3600 | how stale a quote may be and still count as a market |
| `loss_cooldown_seconds` | 900 | nothing after a loss for this long |
| `duplicate_window_seconds` | 300 | re-entry on the same symbol |

## Autonomy is not in the config file

This is the important design decision. Autonomy comes from the **process
environment**, set by whoever launched the server:

| `MT5_AUTONOMY` | what the order tools do |
| --- | --- |
| `propose` (default) | refuse. Planning and validation only. |
| `auto` | send, but only with `confirm=true` and a `plan_id` whose fingerprint still matches |
| `full` | send on a passing risk check alone |

A file the model can edit is not a safety boundary, because the model can edit
files. An environment variable read once at process start is: the model has no
tool that changes it, and a config edit plus a server restart still cannot raise a
hard ceiling.

On top of that, **any account whose `trade_mode` is real is read-only** unless the
process was started with `MT5_ALLOW_LIVE=1`. Demo accounts are the default
because a demo is the only environment where "the model placed an order I did not
expect" is cheap.

Two more switches, both asymmetric on purpose:

* `MT5_ALLOW_KILL_RELEASE` – tripping the kill switch is always allowed; clearing
  it needs this. Stopping is a decision the model may take. Resuming is yours.
* `MT5_ALLOW_FLATTEN` – closing positions this kernel does not own. Your manual
  trades and every other EA stay out of reach by default.

## What the kernel checks, in order

Every order goes through `RiskKernel.check` and gets `PASS` or `BLOCK` with each
check's measured value next to its limit. A `BLOCK` is final: no tool overrides
it, and the way to proceed is to change the plan.

The autonomy level is **not** one of the checks. It is reported as an
`execution_gate` on the verdict (`can_send`, `needs_confirm`) and enforced by the
order tools, because it decides who may *send*, not whether the plan is sound.
Making it a blocking check would leave `propose` mode unable to validate anything,
which is the one mode whose whole job is validating.

**Environment** – real-account switch, the terminal's Algo Trading button, whether
the account permits trading at all.

**State** – the kill switch, and unresolved intents. An MT5 timeout may or may
not have placed an order, so until it is reconciled against the deal history, no
new exposure opens. Closing is always allowed.

**The instrument** – the symbol is tradeable, the side is permitted by the broker
(`trade_mode` long-only, short-only, close-only), the side is permitted by you,
the symbol is not on the block list, the spread is inside the limit, the hour is
not in `blocked_server_hours`, and we are not inside a `news_blackout` window.
For a market order, the quote has to exist *and* be current: a Friday tick still
carries a bid, an ask and a one-point spread, so a closed market looks open
unless freshness is checked against the measured clock
(`guards.max_quote_age_seconds`, default 15 minutes, ceiling one hour). Pending
orders skip both, because resting until the price arrives is what they are for.

**Size** – the volume is normalised to the symbol's own `volume_min + n *
volume_step` sequence, then bounded by the per-order lot limit, and the money at
risk against the stop is compared with the per-trade allowance.

**Portfolio** – the position count, the count on this symbol, the sum of open
risk including this order, and positions with no stop at all.

**The day** – equity against the equity at server midnight, the drawdown from the
day's peak, the order count, the per-minute rate, the duplicate window, and the
cooldown after a loss.

**Margin** – the terminal's own margin figure, with a quarter of free margin kept
in reserve.

Checks that trip the daily loss or drawdown limit **also trip the kill switch**.
That is deliberate: a limit that only blocks one order and then forgets is a
limit a loop can walk past.

## Four decisions worth arguing about

**Fail closed.** If a number the kernel needs is missing, the order is blocked.
"I could not verify this is safe" and "this is safe" must never produce the same
verdict, so a missing margin figure or a missing quote is a block.

**A stop-less position is not a small risk.** By default, a position without a
stop loss is treated as consuming the entire remaining daily allowance, so the
next order is blocked until it has one. You cannot bound a risk nobody has
bounded, and pretending otherwise silently disables the total-risk limit.
`guards.treat_stopless_as_full_risk` turns that off if you want the other
opinion.

**The day is measured in equity, not realised profit.** Equity against equity at
server midnight already contains every open position, so a floating loss counts
before it becomes a realised one. The alternative — summing closed deals — is a
limit that only fires after the damage is booked.

**Money is four columns.** `profit`, `swap`, `commission` and `fee` are separate
fields in MT5. A loss limit that reads only `profit` misses overnight swap and
per-lot commission, which on a CFD position can exceed the profit.

## What this cannot protect you from

* **A bad strategy, executed perfectly.** Every limit here bounds *how much* you
  can lose, not whether the edge exists. A 0.5 % risk per trade on a
  negative-expectancy system loses 0.5 % per trade, on schedule.
* **A broker that changes the rules mid-trade.** Stop levels widen, spreads
  triple, the symbol goes close-only. The kernel re-reads metadata per order, so
  it will notice and block, but it cannot make the fill happen.
* **Slippage beyond the deviation.** A stop order is a request, not a contract.
  Gap risk on a Sunday open or a news spike lands outside every limit here.
* **A wrong server clock reading.** If no fresh tick is available the offset is
  unmeasurable and declared as such, which can shift a daily boundary. It is
  reported in `mt5_clock` and in every verdict.
* **The model itself.** Prompt injection, a misread chart, a hallucinated level:
  the kernel bounds the *volume* of that mistake, not its *direction*.
* **Correlation.** Five positions across EURUSD, GBPUSD, EURGBP and two DAX
  CFDs are one trade. The kernel counts risk, not correlation, and by default
  allows only one position per symbol anyway.

## Operating it

```
python -m mt5trader config      # what is actually in force, and every clamp applied
python -m mt5trader doctor      # is the terminal reachable, is Algo Trading on
python -m mt5trader selftest    # offline: floors clamp, sizing honours its budget
```

Start on a demo with `MT5_AUTONOMY=propose`. Read the verdicts for a week.
Then `auto` with a per-order confirmation. `full` is a decision about trusting
the strategy, not about trusting the software.
