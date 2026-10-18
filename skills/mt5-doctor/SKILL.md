---
name: mt5-doctor
description: Diagnose whether the whole MetaTrader 5 stack is up before any trading is attempted, using `mt5_status`, `mt5_clock`, `mt5_account`, `mt5_symbols` and `mt5_symbol`. Use when a user asks "is MT5 connected", "is Algo Trading on", "why is the bot not trading", "check the terminal", "what account am I on", "why did that order fail", or when any order tool returns an error whose cause is not obvious.
argument-hint: "[no arguments: run this first]"
---

Decide whether the stack can trade at all, and name the one thing to fix when it cannot.

1. Call `mt5_status` with no arguments. It is the only tool registered with `needs_terminal=False`, so it answers even when MetaTrader 5 is closed. The bridge connects lazily, on the first tool call that actually needs the terminal, so a closed terminal does not stop this diagnostic from running. Read `verdict`, `connected`, `problems` and `next`. `verdict` is `ready` or `not connected`, and `problems` lists every gate that is currently closed.

2. Read `server` and `module` in the same reply. `server` carries the server name, version and protocol; `module` carries the MetaTrader5 python module's version and author. When `module.version` is empty the python module is not importable, which is a different fault from the terminal being closed: it is an installation problem, not a running-MetaTrader problem.

3. Read the `gates` block before anything else in the payload.
   - `autonomy` is the level in force, taken from `MT5_AUTONOMY` in the server's environment.
   - `can_place_orders` is true only at `auto` or `full`; in `propose` no order can be sent.
   - `live_account` is true when the account's `trade_mode` is 2 (real).
   - `live_orders_allowed` mirrors `MT5_ALLOW_LIVE`. When `live_account` is true and `live_orders_allowed` is false, the account is read-only: nothing here can place an order on it.

4. When `connected` is false, read `last_error`. retcode -8 means the terminal's Algo Trading button is off, which is the `terminal_autotrading` gate, and it is a local one-click fix in the terminal toolbar. retcode -10004 or -10003 means the terminal is not running or not logged in. retcode -6 means the login was rejected. Fix the connection, then call `mt5_status` again.

5. Call `mt5_clock` with no arguments. It reports `clock.server_now`, `clock.utc_now` and `clock.local_now` side by side, plus the measured offset. Read `clock.source`.
   - `tick_msc`: the offset was measured from the newest tick. Trust it.
   - `stale_tick`: the newest tick is over fifteen minutes old, so the market is closed and the server offset is unmeasurable. Server time is treated as local time, so a daily boundary can be off by the broker's offset until a symbol trades. This is a declared limitation, not a fault.
   - `fallback_local`: no tick with a timestamp was available at all, so the offset is unknown.

6. Call `mt5_account` with no arguments. `live_account` repeats the real/demo verdict, and `today` gives day start equity and day PnL, which is what the daily loss limit is measured against. `mode` reports whether the account is netting or hedging.

7. Call `mt5_symbols` with `search` set to the instrument you intend to trade. Brokers decorate symbol names (`EURUSD.pro`, `XAUUSD.m`, `US30.cash`) and the decoration cannot be guessed, so the name must be resolved here rather than typed from memory. The reply gives `total_on_terminal` and `returned`; widen `limit` when the search is too narrow.

8. Call `mt5_symbol` with `symbol` set to one name from step 7. Confirm `market_open` is true and that the metadata resolves. This proves the instrument is reachable and tradeable before any planning starts.

9. Read `settings` and `bridge_stats` for the wiring. `settings` gives `root`, `config`, `state_dir`, `magic` and `notes`; `notes` lists any value that was clamped against a hard ceiling. `bridge_stats` gives `calls`, `errors` and `reconnects`: a rising `errors` count while `connected` is true points at a symbol-specific fault rather than a global one.

10. The same diagnosis is available from a shell, without the MCP host: `python -m mt5trader doctor` prints the same payload as `mt5_status` (add `--json` for machine output), and `python -m mt5trader config` prints the effective limits, every clamp that was applied, and the hard ceilings.

Worked example:

```
mt5_status                    -> verdict "not connected", problems: terminal not answering,
                                 last_error retcode -8: Algo Trading is off
                              -> enable the Algo Trading button in the terminal toolbar
mt5_status                    -> verdict "ready", connected true, module version present
mt5_clock                     -> clock.source "stale_tick" (weekend), offset unmeasurable
mt5_account                   -> live_account false, today day_pnl 0.0
mt5_symbols search=EURUSD     -> ["EURUSD.pro", "EURUSD.raw"], total_on_terminal 1204
mt5_symbol  symbol=EURUSD.pro -> market_open true, digits 5, volume_min 0.01
```

What it never does:

- It does not send, modify or close anything. Every tool named here is a read.
- It does not guess a symbol name; the name comes from `mt5_symbols`.
- It does not assume the server offset is known when the market is closed. It reports `stale_tick` and continues.
- It does not treat an empty `module.version` as a terminal fault; that is an installation fault.
- It does not raise the autonomy level or enable live trading. Both are set in the server's environment before it starts.
- It does not retry a symbol that failed once. It reports the error and stops, so the next call is a deliberate decision.
- It does not write to the journal, the state directory or the plans directory.
- It does not treat a `not connected` verdict as a reason to attempt an order anyway. The connection is fixed first.
- It does not report an instrument as tradeable while `market_open` is false, whatever the clock says.
- It does not change any environment switch, so a second run always reports the same gates.
