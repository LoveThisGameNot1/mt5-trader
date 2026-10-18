---
name: mt5-calendar
description: Run scheduled economic releases as a blackout window rather than a headline, using `mt5_calendar`, the MQL5 service in `mql5/CalendarExport.mq5`, `python tools/build_sidecar.py` and the kernel's `event_blackout` check. Use when a user asks "is there news coming", "what is the economic calendar", "why is my order blocked by the calendar", "is the calendar service running", "install the calendar export", or before sending anything around a release.
argument-hint: "[hours] [min_importance] [currencies...]"
---

Turn scheduled releases into a window that blocks orders, not into a paragraph to read.

1. Know why the service exists. The Python `MetaTrader5` module has no calendar functions at all: there is no `calendar_*` call in the API. The only route is MQL5, so a small service runs inside the terminal and writes `mt5trader_calendar.json` into the terminal's data folder on a timer. `mt5_calendar` reads that file.

2. Install it with `python tools/build_sidecar.py`. It copies `mql5/CalendarExport.mq5` to `<data folder>\MQL5\Services\mt5trader\`, compiles it with `MetaEditor64.exe` headless (`/compile` plus `/log`), and reports the data folder it used in `data_path` and `data_path_from`. Use `--data-path` when the terminal is closed, `--copy-only` to install without compiling, and `--json` for the report. The build result is read from MetaEditor's own `Result: N errors, N warnings` line, not from the exit code, which is 1 on a clean compile.

3. Start it as a human step. In the terminal: View -> Navigator -> Services -> CalendarExport -> Start. A service needs no chart and no Algo Trading switch. The file lands in the terminal's *common* data folder as `Files\mt5trader_calendar.json`, so several terminals can share one export. Its own inputs set the export window and cadence: `HoursAhead` (336 forward), `HoursBack` (48 back) and `MinutesBetween` (5), which the reply echoes back as `export_interval_minutes`.

4. Call `mt5_calendar` with optional `hours` (default 72), `min_importance` (`high` default, `moderate` or `low`), and `currencies`. With no `currencies`, it uses the currencies of the config symbols.

5. Read the not-running case first. If `available` is false, the reply gives `reason`, `path_checked`, the `how_to_fix` (`python tools/build_sidecar.py`, then start the service in the Navigator), and `what_enforce_does`. Read the path before guessing, since the file may sit in either the common or the terminal data folder. The reader checks in order: `MT5_CALENDAR_FILE`, `config.calendar.file`, the terminal's common data folder, then its own data folder.

6. Read the running case. The reply carries `path`, `source`, `age_seconds`, `stale`, `export_interval_minutes`, `terminal_build`, `server_now`, `window_hours`, `min_importance`, `currencies`, `events_total_in_file`, `upcoming`, `inside_window_now`, and `note`. `upcoming` lists events within `hours` whose currency is relevant and whose normalised `importance` is at least `min_importance`, soonest first, each with `name`, `currency`, `time`, `time_iso`, `importance`, `importance_name`, `actual`, `forecast`, `previous` and `in_hours`.

7. Trust currency relevance, not symbol matching. An event hits EURUSD if its currency is EUR or USD, and XAUUSD if it is XAU or USD. The set comes from the instrument's own `currency_base` and `currency_profit`; the margin currency is not used, because news moves the price, not the collateral. The reply lists the set it used in `currencies`.

8. Do not treat the tool as the guard. The kernel does not hand the model a headline to interpret; it turns the event into a window and a reason. Free text from outside, landing in the same context that holds the order tools, is a prompt injection surface, and a monitor that produces sentences is one a model can talk itself out of.

9. Know the three modes of `guards.calendar`: `off`, `warn` (the default) and `enforce`. Under `enforce`, a missing or stale export *blocks* market orders instead of silently deciding there is no news. That is deliberate fail-closed behaviour: if the export stopped, the kernel cannot know whether a release is imminent, and "cannot verify" must not look like "no news".

10. Know the window shape. `guards.calendar_min_importance` (default `high`), `guards.calendar_before_minutes` and `guards.calendar_after_minutes` (15 each) set what counts and how wide the window is. `inside_window_now` shows the events whose window contains the current server time, each with `starts_in_seconds` and its `window`.

11. Expect pending orders to be blocked too. A buy stop placed before a release triggers during it, so the window covers pending orders, not just market orders. That is the case the window exists for.

12. Read the heartbeat. The service writes the file even when the calendar is quiet, and the freshness comes from the file's `generated_server`, not from the last event in it. The kernel treats a stale heartbeat as "cannot verify", not as "no news", and `stale` plus `age_seconds` is how it says so.

13. Read the two importance columns. The export carries a normalised `importance` (0 low, 1 moderate, 2 high) and the terminal's raw enum as `importance_raw`, so a mismatch between the two is visible instead of silent. `importance_name` is the normalised value in words. The same double-export applies to event values: `actual`, `forecast` and `previous` are the raw integers divided by `10^digits`, and the `*_raw` columns are the terminal's own numbers.

14. Know the disconnected case. The calendar needs the terminal connected; with no connection the service still writes a heartbeat with an empty event list, and the Python side treats a stale heartbeat as "cannot verify", not as "no news". The `how_to_fix` from `build_sidecar.py` is what to follow when the source is missing.

15. Note what happens when the build fails. `build_sidecar.py` reports the `errors` and the MetaEditor log tail, and leaves the source in `mql5/` unchanged, so a broken build never edits the file it was built from.

16. Read `events_total_in_file` before trusting a short `upcoming` list. It is how many events the export holds in total, while `upcoming` is only those inside `hours`, above `min_importance` and in a relevant currency. A small `upcoming` next to a large total means the filters did the work, not that the calendar is empty.

17. Read `note`. It states that event times are server time, the same clock the terminal's own calendar tab shows, which is why `server_now` and `time` are comparable without a conversion.

Worked example:

```
mt5_calendar                 -> available false, reason "no calendar export found",
                                path_checked "...\MQL5\Files\mt5trader_calendar.json",
                                how_to_fix "python tools/build_sidecar.py, then ..."
python tools/build_sidecar.py -> data_path "<hash>", compiled true, ex5 bytes
# start CalendarExport in Navigator -> Services
mt5_calendar hours=24        -> available true, stale false, age_seconds 42,
                                currencies ["EUR","USD"],
                                upcoming high-impact events for EUR and USD
```

What it never does:

- It does not read the terminal's calendar from Python. That is impossible; the MQL5 service is the only route.
- It does not start the service. Starting it is a human step in the terminal's Navigator.
- It does not hand the model a headline to interpret. The event becomes a window and a reason.
- It does not match events by symbol. Relevance is by currency, from `currency_base` and `currency_profit`.
- It does not use the margin currency for relevance.
- It does not pass a stale or missing export when `guards.calendar=enforce`; it blocks, because an unseen release cannot be ruled out.
- It does not treat a quiet calendar as an unread one. A heartbeat is written and its ageing is what matters.
- It does not apply only to market orders. Pending orders rest until the release and are covered by the same window.
- It does not hide an importance mismatch. The normalised value and the raw enum are both exported.
- It does not decide there is no news from a stale heartbeat. It reports that it cannot verify.
- It does not rely on the compiler's exit code. MetaEditor returns 1 on a clean compile, so `build_sidecar.py` reads the `Result:` line instead.
- It does not modify the MQL5 source. A failed build leaves `mql5/CalendarExport.mq5` as it was.
- It does not treat event values as plain numbers. They are raw integers scaled by the event's `digits`, and both forms are exported.
- It does not read a headline the user can argue with. It returns a window, a reason and timestamps, all in server time.
- It does not run its own scheduler. The polling loop lives in the MQL5 service, and the Python side only reads what that loop wrote.
