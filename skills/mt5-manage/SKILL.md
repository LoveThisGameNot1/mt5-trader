---
name: mt5-manage
description: Manage positions that are already open on a MetaTrader 5 account through the mt5-trader server: list them, move a stop or target, take break-even, trail a stop one step, close in part or in full, flatten everything, and cancel a pending order. Use it when the user says "what is open right now", "move my stop to break even", "trail this position", "close half of it", "close everything now", "cancel that pending order", or "get me flat".
argument-hint: <ticket> or a symbol filter
---

Manage open positions and pending orders on a live MT5 terminal.

1. Read before writing. Call `mt5_positions`, optionally with `symbol` to filter
   and `include_foreign=true` to also see positions carrying another magic. Each
   position carries `ticket`, `symbol`, `side`, `volume`, `price_open`,
   `price_current`, `sl`, `tp`, `profit`, `swap`, `magic`, `comment`, `opened`,
   `reason`, and, once the instrument is loaded, `risk_money` (money at risk from
   entry to stop), `risk_now` (current price to stop) and `r_now` (the R multiple
   now). The payload also has `mode` (netting or hedging) and `open_risk`. Foreign
   positions are separated into `foreign` so a manual trade is never mistaken for
   one of ours.

2. Change stops and targets with `mt5_modify` (`ticket`, optional `sl`, `tp`,
   `clear_sl`, `clear_tp`, `allow_foreign`). The modify is **absolute, not a
   patch**: whatever you leave out is carried over from the position as it is
   now, and clearing a level needs `clear_sl` or `clear_tp` explicitly. Sending
   only `sl` is safe, the take profit is preserved. Only the fields you asked to
   change are validated, so an existing target the market has already run past is
   legal to keep; refusing to move a stop because of it would block every trailing
   stop that ever worked. A stop is validated against the current price, not the
   entry, so a trailing stop above the entry is allowed. A stop inside the
   broker's `trade_freeze_level` is refused with the reason in `message`. The
   result is verified by reading the position back: if the broker accepted the
   change but the position reads back differently, `status` is `drifted`, not
   `done`, and `message` shows the landed vs requested levels. Other statuses are
   `refused`, `rejected`, `ambiguous` and `failed`.

3. Take break-even with `mt5_break_even` (`ticket`, `offset_points` default 0,
   `require_profit` default true). It moves the stop to the entry price plus the
   offset. It refuses while the position is not in profit, because a break-even
   stop on a loser is just a closer stop loss. It also refuses when the entry is
   inside the broker's minimum stop distance from the current price, and says so.

4. Trail with `mt5_trail` (`ticket`, `distance_points`, `step_points` default 0).
   It moves the stop to `distance_points` behind the current price, never
   backwards, and only when the improvement is at least `step_points`. It returns
   `no-change` when the candidate is not better than the current stop or the
   improvement is under the step. It moves **one step per call and does not
   loop**, deliberately: the caller decides the cadence so `orders_per_minute`
   stays a meaningful limit. A trailing loop hidden inside one tool call is a loop
   the rate limit cannot see.

5. Close with `mt5_close` (`ticket`, optional `volume`, `comment`,
   `allow_foreign`). It sends an opposite market order carrying `position=ticket`,
   which is what makes it a close rather than a hedge. `volume` defaults to the
   whole position. A partial close that would leave a remainder under the
   symbol's `volume_min` is closed in full instead, and `notes` says so. The
   result is verified by reading back; if the position still shows the full volume
   the status is `not_closed`. The answer carries `closed_volume`,
   `remaining_volume` and the `position` afterwards.

6. Read pending orders with `mt5_orders` (no arguments) before cancelling one, so
   the ticket and its trigger price come from the terminal rather than from memory.
   Cancel with `mt5_cancel` (`ticket`). The answer carries the `result` of the
   remove, the broker `comment`, and `still_there` after a fresh read. A pending
   order is not a position: `mt5_close` never touches one.

7. Flatten with `mt5_flatten` (optional `symbol`, `side`, `scope`). `scope=ours`
   (the default) closes only this kernel's positions. `scope=all` also closes
   anything else on the symbol and needs `MT5_ALLOW_FLATTEN=1` in the server's
   environment. The answer carries `requested`, `closed`, `remaining`, `details`
   and a `note`. This is the "stop now" button after a kill switch; the kill
   switch itself only blocks new orders and closes nothing.

8. Foreign guard. Every write tool refuses a ticket whose `magic` is not the
   kernel's unless `allow_foreign=true` is passed. Only `mt5_modify` and
   `mt5_close` expose that flag; `mt5_break_even` and `mt5_trail` always refuse a
   foreign position and cannot be told otherwise. The default is deliberate: a
   second EA or a manual trade must not be closed by accident.

Worked example: trail a long. Call `mt5_positions` with `symbol=GBPUSD` and read
the ticket and `sl`. Call `mt5_trail` with `ticket=12345`,
`distance_points=150`, `step_points=50`. Read the returned `status`; if it is
`done`, the new `position.sl` is the moved stop. If it is `no-change`, the stop
was already at least that close and nothing was sent.

What it never does:

- Never clears a stop or target you did not ask to clear. An omitted field is
  carried over, never zeroed.
- Never reports `done` without reading the position back; an unapplied change is
  `drifted`.
- Never trails in a loop, and never moves a stop backwards.
- Never closes a foreign position without `allow_foreign=true`, and never
  flattens everything without `MT5_ALLOW_FLATTEN=1`.
- Never closes a pending order; `mt5_close` acts on positions only.
