# Tool reference

Generated from the server's own registry by `python tools/render_docs.py`. Do not
edit by hand: the descriptions and the argument list come from the code, which is
the point of generating it.

`read` tools only look at the terminal. `WRITE` tools change something on it, and
every one of them runs the risk kernel first or refuses to act on a position that
carries another magic number.


25 tools: 15 read, 10 write.


## Read


### `mt5_status`

Diagnose the whole stack before anything else: is the MetaTrader5 python module importable, is the terminal running and connected, is Algo Trading on, is the account demo or real, what is the server time offset, which symbols does it expose, and which environment switches gate trading. Answers even when the terminal is closed. Call this first in every session.


No arguments.


### `mt5_clock`

The broker's clock. MT5 times are server time presented as UTC, and a daily loss limit has to be measured against server midnight. Reports the measured offset, how it was measured, and the current server, UTC and local time side by side.


No arguments.


### `mt5_account`

Account balance, equity, margin, free margin, margin level, leverage, netting or hedging mode, currency, server name, and today's anchored numbers (day start equity, day PnL in money and percent, orders sent today).


No arguments.


### `mt5_symbols`

List or search the terminal's symbols. Use it to find the broker's real name for an instrument: brokers decorate symbols (EURUSD.pro, XAUUSD.m, US30.cash) and the decoration cannot be guessed.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `search` | string |  | substring, case insensitive |

| `limit` | int |  | default 100 |


### `mt5_symbol`

Full trading metadata for one symbol, read fresh: digits, point, tick size, tick value for loss and profit, contract size, volume minimum, maximum and step, the broker's stop and freeze levels in points, trade mode, execution mode, the filling modes this symbol accepts, swap, and live bid/ask/spread. Every sizing and stop decision must be based on this, never on a pip assumption.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbol` | string | yes | symbol or alias |


### `mt5_quote`

Live bid, ask, spread in points and the tick timestamp for several symbols at once. Cheaper than calling mt5_symbol per instrument when only prices are needed.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbols` | string[] | yes |  |


### `mt5_rates`

OHLC candles for a symbol. Timeframes: M1, M2, M3, M4, M5, M6, M10, M12, M15, M20, M30, H1, H2, H3, H4, H6, H8, H12, D1, W1, MN1. Give either count (most recent) or from/to ISO timestamps.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbol` | string | yes |  |

| `timeframe` | string |  | default H1 |

| `count` | int |  | default 100, max 5000 |

| `from` | string |  | ISO timestamp, server time |

| `to` | string |  | ISO timestamp, server time |


### `mt5_positions`

Open positions with the kernel's own numbers attached: the money at risk to the stop, the risk left now, and the current R multiple. Separates this kernel's positions from anything else on the account so a manual trade or a second EA is never mistaken for ours.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbol` | string |  | optional filter |

| `include_foreign` | bool |  | also list positions with another magic |


### `mt5_orders`

Pending orders, with type, trigger price, stop loss, take profit and expiry.


No arguments.


### `mt5_history`

Closed deals in a window, summed properly: profit, swap, commission and fee as four separate columns plus the total. Also the round trips (position by position) and the close reason, so a stop-out is distinguishable from a take profit. Defaults to the current server day.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `days` | number |  | look back N days, default 1, max 365 |

| `symbol` | string |  |  |

| `magic_only` | bool |  | default true |


### `mt5_stats`

Performance over a window: closed positions, win rate, profit factor, expectancy, average win and loss, best and worst, the longest losing streak, average holding time, a breakdown by close reason, and a realised equity curve. Built from round trips, not from raw deals.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `days` | number |  | default 30, max 365 |

| `symbol` | string |  |  |

| `magic_only` | bool |  | default true |


### `mt5_journal`

The kernel's own audit trail: intents, sends, rejections, ambiguous results, reconciliations, kill switch events, limit breaches. Read this when something is unexplained - it records what was attempted, not just what succeeded.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `limit` | int |  | default 100 |

| `kind` | string |  | filter by event kind |


### `mt5_risk_status`

The complete risk picture and the limits actually in force: every effective limit with the hard ceiling it was clamped against, money at risk now across all positions, positions with no stop loss, the day's numbers, kill switch state, unresolved intents, and the autonomy level with what it permits. Call this before planning anything.


No arguments.


### `mt5_size`

Size a position from money at risk and never round up: the largest valid volume whose loss at the stop stays inside the allowance. Works for forex, indices, gold and crypto because it uses the symbol's own tick size and tick value. If the smallest tradeable size is already too big, it says so and reports what that minimum would cost.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbol` | string | yes |  |

| `side` | string | yes | buy or sell |

| `entry` | number |  | defaults to the current price |

| `stop` | number | yes | required: risk cannot be priced without it |

| `target` | number |  |  |

| `risk_money` | number |  | money to risk |

| `risk_percent` | number |  | percent of equity to risk |


### `mt5_validate`

Run the full risk kernel on a plan without sending anything: returns PASS or BLOCK, every single check with the value it measured and the limit it compared against, the normalised order, and a fingerprint. In propose mode this is the finish line - hand the result to the human. Never conclude an order is fine without this.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbol` | string | yes | symbol or alias |

| `side` | string | yes | buy or sell |

| `kind` | string |  | market (default) or pending |

| `type` | string |  | limit, stop or stop_limit for pending orders |

| `volume` | number |  | lots; omit to size from risk |

| `risk_money` | number |  | money to risk, alternative to volume |

| `risk_percent` | number |  | percent of equity to risk |

| `price` | number |  | entry for a pending order, otherwise the current price is used |

| `stop` | number |  | stop loss price |

| `target` | number |  | take profit price |

| `stop_limit_price` | number |  | for stop_limit orders only |

| `deviation` | int |  | slippage tolerance in points |

| `filling` | string |  | auto (default), fok, ioc or return |

| `comment` | string |  |  |

| `time_in_force` | string |  | gtc (default), day or specified |

| `allow_no_sl` | bool |  | explicitly accept a position with no stop; guards.require_sl overrides |


## Write


### `mt5_reconcile`

Resolve intents whose outcome MT5 never confirmed, by searching the deal history, the open positions and the pending orders for a match. Run this whenever a send came back ambiguous, and before opening new exposure. Unresolved intents block new orders on purpose.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `all` | bool |  | default true; false reconciles only the newest intent |


### `mt5_plan`

Validate a plan and store it, so the human can read it, or so it can be executed later by plan_id. Returns the plan id and the full verdict. Stored plans are immutable: any edit changes the fingerprint and invalidates the plan.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbol` | string | yes | symbol or alias |

| `side` | string | yes | buy or sell |

| `kind` | string |  | market (default) or pending |

| `type` | string |  | limit, stop or stop_limit for pending orders |

| `volume` | number |  | lots; omit to size from risk |

| `risk_money` | number |  | money to risk, alternative to volume |

| `risk_percent` | number |  | percent of equity to risk |

| `price` | number |  | entry for a pending order, otherwise the current price is used |

| `stop` | number |  | stop loss price |

| `target` | number |  | take profit price |

| `stop_limit_price` | number |  | for stop_limit orders only |

| `deviation` | int |  | slippage tolerance in points |

| `filling` | string |  | auto (default), fok, ioc or return |

| `comment` | string |  |  |

| `time_in_force` | string |  | gtc (default), day or specified |

| `allow_no_sl` | bool |  | explicitly accept a position with no stop; guards.require_sl overrides |


### `mt5_order`

Place an order. Runs the risk kernel first and refuses on any BLOCK reason; there is no override. With MT5_AUTONOMY=auto each call needs confirm=true and a plan_id whose fingerprint still matches; with full autonomy a passing kernel is enough. On demo unless the server was started with MT5_ALLOW_LIVE=1. If the result comes back ambiguous, stop and call mt5_reconcile - do not send again.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbol` | string | yes | symbol or alias |

| `side` | string | yes | buy or sell |

| `kind` | string |  | market (default) or pending |

| `type` | string |  | limit, stop or stop_limit for pending orders |

| `volume` | number |  | lots; omit to size from risk |

| `risk_money` | number |  | money to risk, alternative to volume |

| `risk_percent` | number |  | percent of equity to risk |

| `price` | number |  | entry for a pending order, otherwise the current price is used |

| `stop` | number |  | stop loss price |

| `target` | number |  | take profit price |

| `stop_limit_price` | number |  | for stop_limit orders only |

| `deviation` | int |  | slippage tolerance in points |

| `filling` | string |  | auto (default), fok, ioc or return |

| `comment` | string |  |  |

| `time_in_force` | string |  | gtc (default), day or specified |

| `allow_no_sl` | bool |  | explicitly accept a position with no stop; guards.require_sl overrides |

| `plan_id` | string |  | a plan from mt5_plan; its fingerprint must still match |

| `confirm` | bool |  | required when autonomy=auto |


### `mt5_modify`

Change the stop loss and/or take profit of an open position. MT5's modify is absolute, so whatever you leave out is preserved from the position; clearing needs clear_sl or clear_tp explicitly. The result is verified by reading the position back, and a change the broker accepted but did not apply is reported as drifted, not as done.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `ticket` | int | yes |  |

| `sl` | number |  |  |

| `tp` | number |  |  |

| `clear_sl` | bool |  |  |

| `clear_tp` | bool |  |  |

| `allow_foreign` | bool |  | default false |


### `mt5_break_even`

Move the stop to the entry price, optionally offset by points. Refuses while the position is not in profit, because a break-even stop on a loser is just a closer stop loss.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `ticket` | int | yes |  |

| `offset_points` | number |  | default 0 |

| `require_profit` | bool |  | default true |


### `mt5_trail`

One trailing step: move the stop to distance_points behind the current price, never backwards, and only if it improves by at least step_points. Deliberately one step per call, so the kernel's rate limits stay meaningful - calling this in a loop from inside the model is what the limits exist to catch.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `ticket` | int | yes |  |

| `distance_points` | number | yes | gap in points |

| `step_points` | number |  | minimum improvement, default 0 |


### `mt5_close`

Close a position, fully or in part, with an opposite market order that carries position=ticket. A partial close that would leave a remainder under the symbol's minimum volume is closed in full instead, and told to you as such. Verified by reading the position back.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `ticket` | int | yes |  |

| `volume` | number |  | default: the whole position |

| `comment` | string |  |  |

| `allow_foreign` | bool |  | default false |


### `mt5_cancel`

Cancel a pending order by ticket.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `ticket` | int | yes |  |


### `mt5_flatten`

Close everything and verify it is flat. scope=ours closes only this kernel's positions; scope=all also closes anything else on the symbol and needs MT5_ALLOW_FLATTEN=1 in the server environment. This is the 'stop now' button: after a kill switch, this is what you call.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `symbol` | string |  | optional, default every symbol |

| `side` | string |  | buy or sell, optional |

| `scope` | string |  | ours (default) or all |


### `mt5_kill_switch`

Trip or read the kill switch. Tripping is always allowed and blocks every future order including a passing risk check. Clearing it needs MT5_ALLOW_KILL_RELEASE=1 in the server environment, because stopping is a decision the model may take and resuming is the human's.


| argument | type | required | meaning |

| --- | --- | --- | --- |

| `action` | string | yes | trip, status or release |

| `reason` | string |  |  |


## The two gates every write tool sits behind

1. `MT5_AUTONOMY` in the server's environment. `propose` (the default) makes the
   order tools refuse outright. `auto` additionally needs `confirm=true` and a
   `plan_id` whose fingerprint still matches.
2. The risk kernel. A `BLOCK` is final; no argument overrides it. Read the
   `failed_checks` array, change the plan, and check again.

`MT5_ALLOW_LIVE=1` is required for any real account, `MT5_ALLOW_FLATTEN=1` to
close positions this kernel does not own, and `MT5_ALLOW_KILL_RELEASE=1` to clear
the kill switch.

