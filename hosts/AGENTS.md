# Operating contract: MetaTrader 5 via the mt5 MCP server

This file is the written form of the workflow. It exists for models that follow
prose better than tool schemas, and as a fallback when the MCP client is not
available. Everything here is enforced by the server; the point of reading it is
to not waste calls finding out.

## The one rule

**The risk kernel decides.** `mt5_validate`, `mt5_plan` and `mt5_order` all return
a verdict of `PASS` or `BLOCK`. A `BLOCK` is final. There is no override argument,
no `force`, no retry that changes it. Read `failed_checks`, change the plan, check
again.

## Tool groups

**Look** (no side effects): `mt5_status`, `mt5_clock`, `mt5_account`,
`mt5_symbols`, `mt5_symbol`, `mt5_quote`, `mt5_rates`, `mt5_positions`,
`mt5_orders`, `mt5_history`, `mt5_stats`, `mt5_journal`, `mt5_risk_status`,
`mt5_size`, `mt5_validate`.

**Act**: `mt5_plan`, `mt5_order`, `mt5_modify`, `mt5_break_even`, `mt5_trail`,
`mt5_close`, `mt5_cancel`, `mt5_flatten`, `mt5_kill_switch`, `mt5_reconcile`.

## The fixed order of operations

1. `mt5_status` once per session. Read `verdict`, `connected`, `gates` and
   `problems`. If `connected` is false, fix that and stop; nothing else works.
2. `mt5_risk_status`. Read the limits in force, the kill switch, and
   `unresolved_intents`. Any unresolved intent blocks new exposure.
3. `mt5_reconcile` if step 2 showed an unresolved intent.
4. `mt5_symbol` for the instrument. Every number about it comes from here, never
   from a pip assumption. Re-read it before each order: brokers widen stop levels
   and spreads intraday.
5. `mt5_size` to turn money at risk into a volume. `mt5_size` floors to the
   symbol's valid volume sequence and never rounds up.
6. `mt5_plan` (or `mt5_validate` when nothing needs storing).
7. `mt5_order` once. Then `mt5_positions` to confirm what actually exists.

## Invariants

* **One order per decision.** `mt5_order` places one order. Do not call it in a
  loop. `orders_per_minute` and the duplicate window exist to stop exactly that.
* **Every position has a stop.** A position without one has no computable risk, is
  treated as consuming the whole remaining allowance, and blocks the next order.
* **Never send twice after an unclear answer.** Retcode `10012` and an empty
  result are *ambiguous*: the order may be live. Call `mt5_reconcile`, then stop.
* **An accepted retcode is not proof.** `10009` means the request was accepted.
  `mt5_positions` is the evidence.
* **A stop is validated against the current price, not the entry.** A trailing stop
  above the entry is legal.
* **`mt5_modify` is absolute, not a patch.** Omitted fields are carried over;
  clearing needs `clear_sl` or `clear_tp`.
* **Only this kernel's positions are touched.** Every write tool refuses a ticket
  whose `magic` is not the kernel's, unless `allow_foreign=true` is passed
  explicitly. `mt5_flatten` with `scope=all` needs `MT5_ALLOW_FLATTEN=1`.
* **The kill switch blocks orders; it closes nothing.** Going flat is a separate
  `mt5_flatten` call. Tripping the switch is always allowed; clearing it needs
  `MT5_ALLOW_KILL_RELEASE=1` and a human.
* **Missing data fails closed.** No quote, no margin figure, no metadata: that is a
  block, not a pass.
* **A quote that exists is not a quote that is current.** A Friday tick still has a
  bid, an ask and a one-point spread. On a closed market a market order is blocked
  by `quote_freshness`; a pending order is not, and that is when a breakout order
  belongs in the market.

## Reading a verdict

```
{
  "verdict": "PASS" | "BLOCK",
  "blocking": ["check_name: what and why", ...],
  "failed_checks": ["risk_per_trade", ...],
  "checks": [{"check": "...", "ok": true, "detail": "...", "limit": ...}, ...],
  "order": {"symbol": "...", "side": "...", "volume": 0.5, "risk_money": 50.0, ...},
  "fingerprint": "9f2c...",
  "execution_gate": {"autonomy": "propose", "can_send": false, "needs_confirm": false},
  "day": {"day_pnl": 0.0, "orders_today": 0, ...},
  "limits": {"per_trade_money": 50.0, "hard_ceilings": {...}, ...}
}
```

`execution_gate.can_send` is false under `propose`, so validation works and sending
does not. That is a mode, not an error.

## Retcode classes

| class | codes | what to do |
| --- | --- | --- |
| ok | 10008, 10009, 10010 | record it; confirm with `mt5_positions` |
| retry | 10004, 10020, 10021, 10024, 10030, 10031 | the tool re-prices or rotates the filling mode itself |
| ambiguous | 10012, or no result | stop, `mt5_reconcile`, never resend |
| fatal | everything else | stop and report the broker's reason |

## When to trip the kill switch

Call `mt5_kill_switch` with `action: "trip"` and a `reason` when: a limit was hit,
the account or a position cannot be explained, a reconciliation cannot be resolved,
or the session's loss or time budget is spent. Then decide separately whether to
`mt5_flatten`.

The daily loss and drawdown limits trip the switch by themselves. A limit that only
blocked one order and then forgot is a limit a loop can walk past.

## Reporting

* Say what the kernel measured, not what you assume. Quote the failing check and
  both numbers.
* Report `ambiguous` as unresolved, never as placed and never as failed.
* Never describe a propose-mode plan as executed. It is a proposal.
* `mt5_journal` is the audit trail of what was attempted, not only what succeeded.
  Read it when something is unexplained.

## Not financial advice

This is software that talks to a trading terminal and does arithmetic. It bounds
how much a mistake can cost. It does not know whether the trade is a good idea.
