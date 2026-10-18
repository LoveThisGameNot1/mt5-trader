---
name: mt5-structure
description: Read where the market's structure is and when the symbol actually moves, using `mt5_levels` and `mt5_session`. Use when a user asks "where are the key levels on EURUSD", "what is support and resistance here", "how far is the next round number", "when should I trade this", "what is the best hour for gold", "is London still open", or "is it quiet right now", before a stop is chosen or an entry is timed.
argument-hint: "<symbol> [days]"
---

Find the levels a chart already shows and the hours the symbol actually moves, both from the terminal's own bar history.

1. Call `mt5_levels` with `symbol`; `reach_atr` (default `2.0`) and `atr_period` (default `14`) are optional. The reply's `levels` holds the last completed daily high, low and close, the previous daily high and low, the 5-day high and low, the last completed weekly high, low and open, and the last completed monthly high and low.

2. Read the top-level context before the levels: `price` (the mid, or the last close when there is no quote), `digits` and `point`, and `atr` with its `D1`, `H4` and `H1` values. Every distance is measured against that `price`, so a level is only meaningful next to it.

3. Read how far each level is from the current price rather than reading an opinion: `distance` in price, `distance_points` in points and `distance_atr` in D1 ATRs. A level that comes from a bar carries all three. That is what turns "support below" into "the prior day low is 0.3 ATR away".

4. Trust the `date` on each level, never the words "today" or "yesterday". On a Monday the newest completed daily bar belongs to Friday, so a label of "today's open" would be a lie that reads as a fact. The forming bar is dropped by `market.bars` before any level is computed, which is why the newest bar in the series is always closed.

5. Read `atr_source`: the ATR is the plain mean of the true range, not Wilder's smoothing. Wilder's is what most charts draw, but the mean is exactly reproducible from a stated window with no seed state, and a number that sets a stop should be reproducible.

6. Expect `date: null` on the arithmetic levels. The ATR bands and the round numbers are not bars, so they have no bar date; only the period extremes carry one first.

7. Read `round_numbers`. They are always reported even when none is near, because "the nearest round number is 0.4 ATR away" is a useful answer even when nothing is close. Each entry carries `price`, `step`, `distance_points`, `distance_atr` and `within_reach`; `within_reach` is true when that number sits inside the ATR reach.

8. Read `round_number_rule` to know the steps: two orders of magnitude below the price, and five times that. For a price near 1.12 the steps are 0.01, 0.05 and 0.1; for 39450 they are 100, 500 and 1000. These are the steps a human reads off a chart.

9. Read `nearest` for the six closest levels to the price. The ATR reach filter applies here only: `nearest` keeps levels within `reach_atr` ATRs, while the top-level `round_numbers` list is never filtered by distance.

10. Call `mt5_session` with `symbol`; `days` (default `20`) and `timeframe` (default `H1`) are optional. Read `by_server_hour`, one entry per server hour, each with `bars`, `avg_range`, `avg_body` and `range_share_pct`. An hour with no bars reports `avg_range` as `null`, never `0`, because shut is not the same as quiet.

11. Read `current` for the live hour: `hour_avg_range`, `hour_share_pct`, `sessions_open`, and `vs_fair_share`, where `1.0` is exactly an average hour and `2.0` is twice the usual range.

12. Read `sessions` for the named sessions mapped onto the broker's clock with `server_offset_hours`. A broker at UTC+3 shows London as 10:00-19:00 server, and the numbers follow the charts' clock, not UTC. Each entry gives `server_hours`, `hours`, `bars`, `avg_range_per_hour` and `avg_range_over_session`. There is deliberately no "share of the day": the windows overlap, so shares would double count and add up past one hundred percent.

13. Read `busiest_server_hours`, `quietest_server_hours` and `overlap`, the last being the London/New York window in server hours.

14. Know that `recent_bars` counts the series separately: weekly and monthly levels come from the terminal's own `W1` and `MN1` bars, never from aggregating daily bars.

15. Read `notes` for anything the resolver changed, for example "resolved EURUSD to EURUSD.pro", and `caveat`, which restates the bar-date rule.

Worked example:

```
mt5_levels symbol=XAUUSD.m reach_atr=1.5
    -> atr D1 24.5, nearest: round number 4000 at 0.2 ATR,
       last completed daily low 3971.4 date 2026-10-16
mt5_session symbol=XAUUSD.m days=20
    -> server_offset_hours 3.0, London 10:00-19:00 server,
       current server_hour 14 vs_fair_share 1.8, overlap 15:00-19:00,
       session "New York" avg_range_per_hour 19.2
```

What it never does:

- It does not place, size, validate or send anything. These two tools are reads.
- It does not use the forming bar; levels, ATR and session statistics are all closed-bar values.
- It does not label a level "today" or "yesterday". The `date` field is the bar's own timestamp.
- It does not use Wilder's smoothing for the ATR.
- It does not give an arithmetic level a bar date.
- It does not report a closed hour as zero.
- It does not report a session's share of the day, because the windows overlap.
- It does not pin sessions to fixed UTC hours. It uses the measured server offset, so the numbers match the charts.
- It does not build a weekly or monthly level by aggregating daily bars.
- It does not hide a round number because it is far away.
- It does not apply the ATR reach filter to the `round_numbers` list, only to `nearest`.
- It does not convert points into pips for any decision.
- It does not decide that now is a good time to trade. It reports the numbers for the plan and the human to weigh.
