---
name: mt5-review
description: Report what actually happened on a MetaTrader 5 account through the mt5-trader server: performance statistics, closed round trips, the kernel's own event journal, account and margin state, and the broker's clock. Use it when the user says "how did today go", "what is my win rate", "show me the closed trades", "what is my profit factor", "why was that order blocked", "what happened to that position", or "when does the broker's day roll over".
argument-hint: <days> or a symbol
---

Report realised results and the audit trail, honestly.

1. Set the window with `mt5_clock` first when a boundary matters. It reports the
   measured `server_offset_seconds`, the `source` it was measured from, and
   server, UTC and local time side by side. Every day boundary in the kernel is
   server midnight, so an order count or a daily loss number only means something
   once you know the offset. When no fresh tick is available the offset is
   declared unmeasurable (`stale_tick` or `fallback_local`) rather than guessed.

2. Read the account with `mt5_account`. It returns `money` (`balance`, `equity`,
   `profit`, `margin`, `margin_free`, `margin_level`, `margin_so_call`,
   `margin_so_so`), `mode` (netting or hedging, plus `fifo_close`), `today` (day
   start equity, day PnL in money and percent, orders sent today) and
   `live_account`. Equity, not realised profit, is the honest running number
   because it already contains every open position.

3. Get the closed deals with `mt5_history` (`days` default 1, max 365, optional
   `symbol`, `magic_only` default true). Accounting is done in **round trips, one
   entry per closed position, not per deal**, because a position can be filled and
   closed in pieces; deals are the wrong unit. `round_trips` groups deals by
   `position_id` and each entry carries `position_id`, `symbol`, `magic`,
   `volume`, `entry_price`, `entry_time`, `close_time`, `net`, `swap`,
   `commission` and `close_reason`. A position with no closing deal is skipped (it
   is still open), and a partly closed position is skipped too: it belongs to the
   open side until it is fully closed. The `summary` gives the four money columns
   separately (`profit_gross`, `swap`, `commission`, `fee`) plus `realized_net`.
   **Realised money is the sum of `profit`, `swap`, `commission` and `fee`**;
   summing only `profit` is wrong because overnight swap and per-lot commission on
   a CFD can exceed the profit, and a loss limit that reads one column under-counts.

4. Get the performance with `mt5_stats` (`days` default 30, max 365, optional
   `symbol`, `magic_only` default true). It is built from the same round trips.
   The fields and what each tells you:
   - `closed_positions`, `wins`, `losses`, `flat`: the sample size and its split.
   - `win_rate`: percentage of closed positions with positive net.
   - `gross_profit`, `gross_loss`: the sum of winning nets and the absolute sum of
     losing nets.
   - `net`: gross profit minus gross loss, the realised result of the window.
   - `profit_factor`: gross profit divided by gross loss; `null` when there were no
     losses, which is not the same as a perfect edge.
   - `average_win`, `average_loss`: mean of the winners and mean of the losers.
   - `expectancy`: net per closed position, the number to compare across systems.
   - `best`, `worst`: the largest win and the largest loss in the window.
   - `max_drawdown_realized`: the deepest peak-to-trough fall of the realised
     curve, in money.
   - `longest_loss_streak`: the most consecutive losing closes, in time order.
   - `avg_hold_minutes`: mean time between entry and close.
   - `close_reasons`: a tally of why positions closed (see step 5).
   - `per_symbol`: `trades` and `net` per symbol, so one bad instrument is visible.
   When the window has no closed positions it returns `closed_positions: 0` and a
   `note` instead of the rest.

5. Read `close_reasons` to separate a **stop-out from a take profit and from an
   ordinary stop loss**. `take-profit` means the target was reached, `stop-loss`
   means the protective stop was hit, and `stop-out` means the broker liquidated
   the position on margin. A run of `stop-out` is not the same result as a run of
   `stop-loss` even when both are losses: a stop-out says the size or the margin
   was wrong, and it will keep happening until that is fixed. This is the
   difference between a strategy losing on schedule and a strategy about to blow
   the account up.

6. Read the journal with `mt5_journal` (`limit` default 100, optional `kind`). It
   is the kernel's own audit trail of **what was attempted, not just what
   succeeded**: `order_intent` (written before the request goes out),
   `order_sent`, `order_ambiguous` (the terminal never confirmed, so the position
   may already exist), `order_rejected`, `intent_reconciled`, `plan_created`,
   `stop_modified`, `position_closed`, `kill_switch_tripped` and
   `kill_switch_released`. Read it when something is unexplained. An
   `order_ambiguous` with no matching `intent_reconciled` is why new orders are
   blocked, and is the first thing to chase.

7. Report the caveats with the numbers, not instead of them. MT5 keeps no equity
   history, so the curve (`equity_curve_realized`) is a **realised-equity walk**,
   the cumulative sum of closed round-trip net, the last 400 points; it is not a
   tick-level equity curve and it ignores open floating positions. The window in
   `mt5_history` and `mt5_stats` is UTC based (`days` back from UTC now), while
   MT5 timestamps are server time presented as UTC, so a reading near server
   midnight can be off by the broker's offset. Both tools report this in
   `caveat`, and `mt5_clock` shows the offset.

Worked example: "How did the last week go?" Call `mt5_clock` to fix the offset,
then `mt5_stats` with `days=7` and `magic_only=true`. Report `closed_positions`,
`net`, `win_rate`, `profit_factor` and `expectancy`, then read `close_reasons`:
if most closes are `stop-loss` the stops are doing their job, if they are
`stop-out` the size was too large. Only if a number looks wrong call `mt5_journal`
and filter by `kind=order_ambiguous`.

What it never does:

- Never counts a deal as a trade. The unit is the closed round trip.
- Never counts a partly closed position as closed; it stays on the open side.
- Never reports realised money from `profit` alone.
- Never treats `profit_factor: null` (no losses) as a proven edge.
- Never presents the realised curve as a true equity curve, and never hides the
  UTC-window versus server-time mismatch.
- Never reports only successful events from the journal; rejections, ambiguous
  sends and kill switch events are part of the record.
