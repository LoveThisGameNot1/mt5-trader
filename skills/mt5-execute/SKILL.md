---
name: mt5-execute
description: Place exactly one MetaTrader 5 order and handle an unclear answer without doubling a position. Use when asked to "place the order", "send it", "execute this plan", "put the trade on", or "go ahead and buy" after a plan exists, and whenever a send comes back ambiguous and something must be reconciled. It runs the risk kernel, places one order through the broker's own pre-flight, and on any unconfirmed result reconciles instead of resending. It never places a second order from one decision.
argument-hint: "[plan_id and confirm, or the full order fields]"
---

Place one order, verify it, and never resolve an unclear answer by sending again.

1. Establish the environment before anything. Call `mt5_status` once: module
   importable, terminal connected, Algo Trading on, account demo or real, and the
   `gates` block that shows autonomy, `can_place_orders` and whether live orders
   are allowed. Then call `mt5_risk_status` for the limits in force, the kill
   switch state and any unresolved intent. If an intent is unresolved, the kernel
   blocks new exposure on purpose, so execution starts with `mt5_reconcile`.

2. Re-read the market. Call `mt5_quote` or `mt5_symbol` for a fresh price. A plan
   built minutes ago can be priced through a moved spread or a stop now inside
   the broker's stop level; the kernel re-reads metadata per order and will block,
   but pricing the order against the current tick is what makes the fill land.

3. Plan or validate, then order. If no stored plan exists, call `mt5_plan` to
   validate and store one. With a plan in hand call `mt5_order` with its
   `plan_id` and the order fields. The order tool runs the kernel again, then
   builds the request, calls the broker's own `order_check` pre-flight, and only
   then sends. The order of operations is fixed: status, risk status, market data,
   plan or validate, order.

4. Know what autonomy requires. Under `propose` the order tool refuses outright -
   there is no way to send, and the workflow ends at planning, which still works
   and still produces a stored plan. Under `auto` each
   call needs `confirm: true` and, for a stored plan, a `plan_id` whose
   `fingerprint` still matches the order being sent; a mismatch returns
   `sent: false` with a message about the changed fingerprint and requires a new
   plan. Under `full` a passing risk check alone is enough, with no per-order
   confirmation.

5. Treat a BLOCK as final. If the kernel returns BLOCK, `mt5_order` returns
   `sent: false` with `failed_checks` and the message "a block is final: there is
   no override". No argument changes that. Read the failing check, change the plan
   (smaller volume, different stop, a different hour) and call `mt5_plan` again.

6. Read the pre-flight result. Before sending, the tool calls the broker's
   `order_check` with the exact request. When `preflight.available` is true and
   `preflight.ok` is false, the order was refused before it was sent and `sent`
   is false with "Nothing was placed". That is a definite no: no position exists,
   and the request or the current market must change. When `order_check` returns
   nothing (`available: false`) the check is skipped and the send proceeds.

7. Classify the send from `errors.py`. Every `order_send` result falls into one of
   four buckets. `ok` (10008 placed, 10009 done, 10010 partial) means the broker
   accepted; confirm the position with `mt5_positions` before reporting success.
   `retry` (10004 requote, 10020, 10021, 10024, 10030, 10031) means the request
   was cleanly rejected and the tool re-prices or rotates the filling mode and
   sends again itself; 10030 in particular rotates to the symbol's next accepted
   filling mode, which is the most common MT5 rejection after sizing. `fatal`
   (everything else) means the request or market is wrong; stop and report the
   broker's reason, do not resend. `ambiguous` is handled next.

8. An ambiguous answer means the order may be live. Retcode 10012 (timeout) and an
   empty `order_send` result both mean the terminal never confirmed the outcome.
   MT5 has no client order id, so there is no server-side key to look the order up
   by; the tool wrote an intent record to disk before sending precisely so this can
   be resolved afterwards. The rule is reconcile, never resend: resending is the
   single most expensive mistake here, because it silently doubles a position.

9. Reconcile immediately. Call `mt5_reconcile` (default `all: true`, which walks
   every unresolved intent; `all: false` reconciles only the newest). It searches
   the deal history for a matching entry deal, then open positions, then pending
   orders. `executed` means a deal was found. `maybe_executed` means no deal yet
   but a position exists with this magic and symbol, so treat it as executed until
   the next check. `placed` means a pending order exists. `not_executed` means
   nothing matched and the order never reached the market. Only after a resolution
   that is clearly `not_executed` may a fresh order be considered.

10. Confirm the outcome, not the retcode. Call `mt5_positions` (optionally
    `symbol`) after an accepted send. A `10009` means the request was accepted,
    not that the position looks the way it was asked to; the position list is the
    evidence. One tool call equals one order, so a strategy that wants many orders
    must be driven by the caller - a model repeating `mt5_order` in a loop is what
    `orders_per_minute` and the duplicate window exist to catch.

Worked example. `mt5_order` with `{"plan_id": "plan_ab12", "confirm": true}`
returns `status: "ambiguous"`, `result.retcode: 10012`. Stop. Call
`mt5_reconcile`; it returns one result with `status: "maybe_executed"` and a
position on the symbol. The order is live; do not send again. A second order
would be a doubled position.

What it never does:
- Never sends a second order to resolve an ambiguous one; it reconciles first.
- Never retries after a BLOCK or a fatal retcode, and never uses an override
  argument - none exists.
- Never treats an accepted retcode as proof of a position without reading it back
  with `mt5_positions`.
- Never touches positions carrying another magic number unless `allow_foreign` is
  explicitly set on the managing tool.
- Never drives a loop of orders from inside the model; each call is one order.
