---
name: mt5-plan
description: Turn a trading idea into an exact, validated order without sending anything. Use when asked to "plan a trade", "size this position", "how many lots for a 0.5 percent risk", "check this order before I place it", "what stop should I use", or "validate this entry" on a MetaTrader 5 account. It reads the symbol's own metadata, sizes from money at risk rather than from lots, runs the full risk kernel on the result, and returns PASS or BLOCK with every measured value next to the limit it was compared against. It never sends an order.
argument-hint: "[symbol] [side] [stop price] [risk_percent or risk_money]"
---

Turn an idea into an exact order and stop at a verdict. Nothing here places a trade.

1. Read the limits before planning. Call `mt5_risk_status`. It returns every
   effective limit with the hard ceiling it was clamped against, the autonomy
   level and what it permits, open risk, positions with no stop, the day's
   counters, and any unresolved intent. A plan is only meaningful against the
   limits actually in force: planning first and reading later means re-planning
   after a surprise.

2. Get the symbol's real metadata. Call `mt5_symbol` with `symbol`. Brokers
   decorate names (`EURUSD.pro`, `XAUUSD.m`, `US30.cash`) and the decoration
   cannot be guessed, so use `mt5_symbols` with `search` if the name is unknown.
   Take `tick_size`, `tick_value_loss`, `volume_min`, `volume_step`,
   `volume_max`, `trade_stops_level_points`, the accepted filling modes and
   live bid/ask from this call. Every stop and size decision uses these, not a
   pip assumption, because the same code has to size forex, gold, an index and a
   crypto contract.

3. Size from money at risk, never from lots. Call `mt5_size` with `symbol`,
   `side`, `stop` and either `risk_money` or `risk_percent`. `entry` defaults to
   the current price when omitted; `target` is optional and only adds a
   `reward_risk` figure. `risk_percent` is a fraction of equity, so it needs no
   balance figure from you. The result is the largest valid volume whose loss at
   the stop stays inside the allowance.

4. Expect the floor. `mt5_size` normalises the volume to the symbol's own
   `volume_min + n * volume_step` sequence and always rounds down, never up:
   putting on 0.04 lots when the plan authorised 0.037 puts on more risk than was
   agreed, and that difference compounds. When even the minimum tradeable size is
   too big it returns `lots: null`, `needed_lots` and `minimum_possible_risk`, so
   the human is told what the smallest trade would cost instead of "too small".

5. A plan with no stop cannot be sized from risk at all, and `mt5_size` requires
   `stop` for exactly that reason. There is no distance to divide the allowance
   by. Such a plan needs an explicit `volume`, and the kernel will block it
   unless `guards.require_sl` is off or `allow_no_sl` is passed.

6. Validate before believing anything. Call `mt5_validate` with `symbol`, `side`
   and the rest of the order fields (`volume` or `risk_money` or `risk_percent`,
   `stop`, `target`, `kind`, `type`, `price`, `deviation`, `filling`,
   `time_in_force`). It sends nothing and returns PASS or BLOCK, the normalised
   order, and a `fingerprint`. The `checks` array carries every check with the
   value it measured (`risk`, `volume`, `spread_points`, `projected`,
   `drawdown_pct`, and so on) next to the `limit` it compared against, so a
   refusal is read, not guessed.

7. Read a BLOCK from `failed_checks`, then change the input and check again.
   A BLOCK is final: no argument overrides it, so retrying the same plan is
   guaranteed to fail the same way. Reduce the volume, widen the stop, move the
   target so the reward-to-risk clears `guards.min_reward_risk` (default 1.0; the
   `reward_risk` check compares the R multiple against it), or wait out the
   spread or hour that failed. Re-run `mt5_validate` with the changed fields.

8. Store the plan when it matters. Call `mt5_plan` with the same fields. It runs
   the identical kernel and, on PASS, stores the plan and returns a `plan_id` and
   `stored: true`. On BLOCK it returns `stored: false`, so there is nothing to
   execute. A stored plan is immutable: its `fingerprint` is a hash of symbol,
   side, kind, type, volume, price, stop, target, magic, deviation, filling and
   expiration, and any edit to one of those changes the hash. The executor refuses
   a plan whose fingerprint no longer matches.

9. Read `execution_gate` on the verdict. Autonomy decides who may send, not whether
   the plan is sound: `can_send` is false under `propose`, and `needs_confirm` is
   true under `auto`. So in propose mode planning is the finish line - the plan is
   still validated and stored, `mt5_order` refuses to send it, and the correct
   action is to hand the verdict, the order and the `plan_id` to the human. Do not
   report a propose-mode plan as if it had been executed.

Worked example. "Plan a EURUSD buy risking 0.5 percent, stop 30 points below."
`mt5_symbol` gives the real name, digits and tick values. `mt5_size` with
`{"symbol": "EURUSD", "side": "buy", "stop": 1.08200, "risk_percent": 0.5}`
returns `lots` floored to the step and `risk_money` at or under the budget.
`mt5_plan` with those fields returns PASS, `plan_id`, `fingerprint` and the full
`checks` list. Show the order, the `risk_money`, `reward_risk` and the
`fingerprint` to the human.

What it never does:
- Never calls `mt5_order`, `mt5_close`, `mt5_flatten` or any other write tool.
- Never rounds a volume up, and never derives a size from a lot count the human
  stated without converting that to money at risk against a real stop.
- Never edits a stored plan; a changed field is a new plan with a new id.
- Never retries a BLOCK unchanged, and never treats a missing measurement
  (no quote, no margin, no metadata) as a pass - the kernel fails closed.
- Never uses a pip value, a contract-size constant or a name the broker did not
  return from `mt5_symbol`.
