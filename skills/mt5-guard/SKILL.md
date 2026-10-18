---
name: mt5-guard
description: Report and operate the risk limits, using `mt5_risk_status`, `mt5_kill_switch`, `mt5_flatten`, `mt5_reconcile`, `mt5_journal` and `mt5_positions`. Use when a user asks "what are my limits", "how much am I risking", "stop everything now", "go flat", "release the kill switch", "what happened to that order", or "why is my order blocked".
argument-hint: "[status|trip|release] [reason]"
---

Show the limits actually in force, and carry out the stop and recovery actions.

1. Call `mt5_risk_status` with no arguments before planning anything. It returns every effective limit with the hard ceiling it was clamped against, money at risk now, positions with no stop, the day's numbers, the kill switch state, unresolved intents, and the autonomy level with `autonomy_meaning` describing what that level permits.

2. Know the two layers. The config file holds the working limits and can only make the kernel tighter. `HARD_CEILINGS` live in `mt5trader/config.py` and cannot be raised by a config edit; every value is clamped against them at startup, and the clamp is reported in `settings_notes` (and by `python -m mt5trader config`), never applied silently. The reply also carries `limits.hard_ceilings`, so the ceiling a value was clamped to is visible next to the value.

3. Know that autonomy is not in the config file. It comes from `MT5_AUTONOMY` in the server's environment, set by whoever launched the server. A file the model can edit is not a safety boundary, while an environment variable read once at startup is. The reply reports `autonomy`, `allow_live`, `allow_flatten`, `allow_risk_edit` and `allow_kill_release` so all five switches are visible at once.

4. To stop everything, call `mt5_kill_switch` with `action` set to `trip` and a `reason`. Tripping is always allowed. Clearing it needs `action` set to `release`, which raises a permission error unless the server was started with `MT5_ALLOW_KILL_RELEASE=1`, because stopping is a decision the model may take and resuming is the human's. `action` set to `status` reads the current state and day counters without changing anything.

5. Remember the kill switch blocks new orders but closes nothing. After tripping it, read `open_positions` in the reply and call `mt5_flatten` to go flat. `mt5_flatten` takes optional `symbol`, `side` and `scope`; `scope` defaults to `ours`, and `scope=all` also closes anything else on the symbol, which needs `MT5_ALLOW_FLATTEN=1`. `mt5_flatten` verifies the account is flat afterwards.

6. When a send came back ambiguous, call `mt5_reconcile`. It searches the deal history, the open positions and the pending orders for a match to the unconfirmed intent. Unresolved intents block new exposure on purpose, and `mt5_reconcile` is the only way to clear them. Pass `all` (default true) to reconcile every intent, or `all=false` to reconcile only the newest.

7. Read `mt5_journal` with `limit` and optional `kind` to see what was attempted, not just what succeeded: intents, sends, rejections, ambiguous results, reconciliations, kill switch events and limit breaches. The reply also gives `journal_dir`.

8. Read `mt5_positions` with optional `symbol` and `include_foreign` to separate this kernel's positions from a manual trade or a second EA. The reply carries `open_risk`, whose `total` is the money at risk now and whose `unbounded` lists positions with no stop at all.

9. Read `day_counters` in `mt5_risk_status` for `orders_today`, `orders_last_minute`, `loss_streak` and the timestamps of the last loss and last order, which is what the cooldown, duplicate and rate checks are measured against.

10. Read `open_risk` closely. `total` is the money at risk now across every position with a stop, `per_symbol` breaks that down, and `unbounded` lists any position with no stop, whose risk cannot be measured at all. `kill_switch.since` gives the server time the switch was tripped.

11. The checks that can appear in `failed_checks` on a verdict, in the order the kernel records them: `live_account_guard`, `terminal_autotrading`, `account_trading`, `kill_switch`, `unresolved_intents`, `plan`, `market_open`, `side_allowed`, `config_side`, `blocked_symbol`, `quotes`, `quote_freshness`, `spread`, `server_hour`, `news_blackout`, `event_blackout`, `max_lots`, `risk_per_trade`, `reward_risk`, `open_positions`, `positions_per_symbol`, `total_risk`, `stopless_positions`, `portfolio_correlation`, `daily_loss`, `drawdown`, `orders_per_day`, `orders_per_minute`, `duplicate`, `loss_cooldown`, `margin`. Autonomy is deliberately not in that list: it is reported as `execution_gate` on the verdict, because it decides who may send rather than whether the plan is sound. A `BLOCK` is final: no tool overrides it, and the way forward is to change the plan. A check that trips `daily_loss` or `drawdown` also trips the kill switch. `quotes` and `quote_freshness` apply to market orders only; a pending order is not checked, because resting until the price arrives is what it is for. `event_blackout` and `portfolio_correlation` can be switched between blocking and warning, and in warn mode they still appear here, because a finding is a finding; read the check's `action` and its `detail` before assuming a block.

12. Read `limits` for the numbers in force: `per_trade_money`, `per_trade_pct`, `total_risk_money`, `daily_loss_money`, `max_drawdown_pct`, `max_open_positions`, `orders_per_day`, `orders_per_minute`, `max_lots_per_order`, `loss_cooldown_seconds` and `duplicate_window_seconds`. Each sits next to the ceiling it was clamped against in `limits.hard_ceilings`.

Worked example:

```
mt5_risk_status              -> autonomy propose, max_open_positions 5,
                                unresolved_intents 1, kill_switch tripped false
mt5_reconcile                -> reconciled 1
mt5_kill_switch action=trip reason="news spike"
                             -> open_positions 2, next "call mt5_flatten to go flat"
mt5_flatten scope=ours       -> closed 2, verified flat
mt5_journal limit=20         -> kill_switch_tripped, position_closed events
```

What it never does:

- It does not raise a hard ceiling or enable live trading. Both are set in the environment.
- It does not release the kill switch without `MT5_ALLOW_KILL_RELEASE=1`.
- It does not close anything when the kill switch trips; `mt5_flatten` is a separate call.
- It does not clear an unresolved intent except through `mt5_reconcile`.
- It does not override a `BLOCK`. A block is final, and the plan must change.
- It does not close positions this kernel does not own unless `scope=all` and `MT5_ALLOW_FLATTEN=1` are both present.
- It does not edit the config file. `allow_risk_edit` reports whether a risk edit is permitted at all, and no tool here writes a limit.
- It does not turn a stop-less position into a bounded one. `mt5_flatten` or a stop set by hand is the only way to bound it.
- It does not restart the day counters. They roll over at server midnight, from the measured clock.
- It does not clear the kill switch on a restart. The tripped state is stored and survives until an operator releases it.
- It does not treat a `release` that failed as a `trip`. A permission error leaves the switch exactly as it was.
- It does not count foreign positions toward this kernel's risk totals, so `mt5_positions` with `include_foreign` is how they are seen.
- It does not silently drop an unresolved intent; it keeps blocking new exposure until `mt5_reconcile` resolves it.
- It does not resume trading after a daily loss or drawdown trip. That needs an operator release.
