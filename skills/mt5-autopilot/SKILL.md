---
name: mt5-autopilot
description: The contract for running a MetaTrader 5 account unattended with MT5_AUTONOMY=full. Use when asked to "run it on autopilot", "trade unattended", "keep it running", "manage the positions for me", or "let it run all session" on a demo or a funded account. It defines the once-per-session pre-flight, the one-order-per-decision rule, the mandatory stop on every position, how to handle an ambiguous send and a kernel block, when to trip the kill switch, and what ends the session.
argument-hint: "[session brief: symbols, session length, when to stop]"
---

Run a session without a human watching each call, and stop on a defined trigger.

1. Pre-flight once, and report it to the human. At session start call
   `mt5_status` and `mt5_risk_status`, and read the result into the brief: is the
   terminal connected, is Algo Trading on, is the account demo or real, is
   `MT5_ALLOW_LIVE` set, what is the autonomy level and what it permits, what are
   the effective limits and their clamp notes, is the kill switch already tripped,
   are there unresolved intents. Write this summary out before the first order.
   A session that starts without knowing which gates are open is a session that
   will hit one unexpectedly and stop mid-trade.

2. Reconcile before opening exposure. Call `mt5_reconcile`. Any intent whose
   outcome MT5 never confirmed blocks all new orders on purpose, because an order
   may already be live. `executed` and `maybe_executed` mean the position exists;
   `placed` means a pending order exists; `not_executed` means it never reached
   the market. New exposure is only allowed once this comes back clean.

3. One order per decision. Build the decision (symbol, side, stop, size), then
   call `mt5_order` once. Never loop the model on `mt5_order` or `mt5_trail`.
   `orders_per_minute` exists precisely to stop a model talking to itself: it
   counts orders in the last minute and blocks past the limit, and the duplicate
   window blocks a re-entry on a symbol that just traded. If a strategy needs many
   orders, the caller drives them one decision at a time; the model does not
   repeat calls on its own.

4. Read the kernel block and change the plan, or stop. When `mt5_order` returns
   BLOCK, its `failed_checks` names what failed and the measured value next to the
   limit. A block is final and there is no override. Either change the plan - a
   smaller volume, a wider stop, a different hour, a target that clears
   `guards.min_reward_risk` - and go through `mt5_plan` again, or end the session
   for that setup. Retrying the same plan is guaranteed to fail the same way. The
   daily-loss and drawdown checks do not merely block one order: they trip the
   kill switch, so a limit breach ends the session.

5. Every position carries a stop. `guards.require_sl` is on by default, and a
   position with no stop has no computable risk, so it is treated as consuming the
   entire remaining allowance and blocks the next order until it has one. Read
   `mt5_positions`, which attaches the money at risk to the stop and the current R
   multiple. A position reported in `open_risk.unbounded` has no stop: fix it with
   `mt5_modify`, `mt5_break_even` or `mt5_trail`, or close it.

6. Handle an ambiguous send first, every time. If `mt5_order` returns
   `status: "ambiguous"` (retcode 10012, or an empty result), the order may be
   live. Call `mt5_reconcile` before anything else, and do not send again. New
   exposure stays blocked until the intent is resolved. This is the failure that
   would silently double a position if the answer were guessed.

7. Manage open positions with one call each. The manage tools are `mt5_modify`
   (set `sl` and/or `tp`; it is absolute, so clearing needs `clear_sl` or
   `clear_tp`), `mt5_break_even` (moves the stop to entry, refuses while not in
   profit), `mt5_trail` (one trailing step, never backwards, only if it improves
   by at least `step_points`) and `mt5_close` (full or partial, read back). Each
   returns a verified state, reporting `drifted` when the broker accepted a change
   it did not apply - a stop that silently failed to move is the most dangerous
   state, so never treat "accepted" as "applied".

8. Trip the kill switch on exactly these conditions. Call `mt5_kill_switch` with
   `action: "trip"` and a `reason` when a limit was hit, when the account behaves
   unexpectedly (equity or a position the kernel cannot explain), when a
   reconciliation cannot be resolved, or when a position exists that the model
   cannot account for. Tripping is always allowed and blocks every future order
   including one that would pass the kernel. Clearing it needs
   `MT5_ALLOW_KILL_RELEASE=1` and an operator: stopping is a decision the model may
   take, resuming is the human's.

9. Going flat is separate from the kill switch. Tripping the switch closes
   nothing; it only blocks sends. To close positions call `mt5_flatten`, with
   `scope: "ours"` (the default, only this kernel's positions) or `scope: "all"`
   (needs `MT5_ALLOW_FLATTEN=1`). After a kill switch, this is what is called to
   reach flat.

10. Review, then decide to continue or end. Call `mt5_journal` for the kernel's
    own trail of intents, sends, rejections, ambiguous results, reconciliations
    and limit events, and `mt5_positions` for the current book. End the session
    when any of these hold: the kill switch tripped, the daily loss or drawdown
    limit was reached, an intent stays unresolved after a reconcile, the model
    cannot explain a position, or the session's own time or loss budget is spent.
    Ending means flat positions and the kill switch tripped or the reason written
    to the human.

Session checklist: pre-flight (`mt5_status`, `mt5_risk_status`) reported once;
`mt5_reconcile` clean; decide; one `mt5_order`; confirm with `mt5_positions`;
manage with one call per position; `mt5_reconcile` after any ambiguous result;
review with `mt5_journal`; end on the first of the stop conditions above.

What it never does:
- Never sends more than one order per decision, and never loops a manage tool.
- Never opens new exposure while an intent is unresolved, and never resolves an
  ambiguous result by resending.
- Never holds a position without a stop.
- Never releases the kill switch; that needs `MT5_ALLOW_KILL_RELEASE=1` and a human.
- Never treats the kill switch as a close: it trips the switch and then calls
  `mt5_flatten` if flat is wanted.
