---
name: mt5-market
description: Read the market and understand one instrument before planning a trade, using `mt5_symbol`, `mt5_quote`, `mt5_rates` and `mt5_symbols`. Use when a user asks "what is the spread on XAUUSD", "show me the last candles on EURUSD", "what are the stop levels for this symbol", "get me a quote", "which timeframe should I use", or before sizing any order.
argument-hint: "<symbol> [timeframe]"
---

Establish the instrument's real metadata and current price before any plan is made.

1. Call `mt5_symbols` with `search` to get the broker's exact name for the instrument. Never type a symbol from memory: the same code trades forex, gold, index CFDs and crypto, and the broker's decoration (`EURUSD.pro`, `XAUUSD.m`, `US30.cash`) is not guessable.

2. Call `mt5_symbol` with `symbol` set to that name. Everything the plan later says about the instrument must come from this reply, never from a pip assumption, because "1 lot is 100k and 10 dollars a pip" is true for only a handful of instrument types. Read these fields:
   - `digits`: price decimals. Prices are rounded half away from zero to this many digits.
   - `point`: the smallest price increment. This is the unit for `trade_stops_level_points`, `trade_freeze_level_points`, spread and deviation. It is not always equal to `tick_size`.
   - `tick_size`: the price step of one tick, the divisor in the money math.
   - `tick_value_loss`: account-currency loss per tick per lot. Risk uses the loss side because on a cross-currency symbol the loss and profit tick values differ, and the loss one is the one that bites.
   - `contract_size`: units per lot. Informational; sizing never assumes 100000.
   - `volume_min`, `volume_step`, `volume_max`: valid volumes are `volume_min + n * volume_step`, not multiples of the step, so 0.25 is a rejection when the step is 0.1.
   - `trade_stops_level_points`: the minimum distance between price and a stop, in points. A closer stop is rejected.
   - `trade_freeze_level_points`: a stop inside this distance cannot be modified (retcode 10029) until price moves away.
   - `trade_mode`: `full`, `long-only`, `short-only`, `close-only` or `disabled`. This decides `market_open` and which sides are permitted.
   - `filling_modes`: the fillings the symbol accepts; choosing a mode outside this list is retcode 10030.
   - `spread_points`: the current spread in points.
   - `bid` and `ask`: a symbol with no live quotes cannot be priced, so a market order on it cannot be built.

3. Read the rest of the `mt5_symbol` reply. `notes` carries anything the resolver changed, for example "resolved EURUSD to EURUSD.pro" or "has no live quotes; the market is probably closed". `path` is the symbol's group in the terminal, and `stop_distance_price` and `freeze_distance_price` are the two levels above converted from points into price terms.

4. Re-read `mt5_symbol` immediately before each order. Brokers widen `trade_stops_level_points` and spreads intraday, so a stop level read an hour ago may already be too tight.

5. Call `mt5_quote` with `symbols` to price several instruments at once. It is cheaper than `mt5_symbol` when only bid, ask, `spread_points`, `tick_time`, `has_quotes` and `market_open` are needed.

6. Call `mt5_rates` with `symbol`, and optionally `timeframe`, `count`, `from` and `to`. Accepted `timeframe` values are `M1`, `M2`, `M3`, `M4`, `M5`, `M6`, `M10`, `M12`, `M15`, `M20`, `M30`, `H1`, `H2`, `H3`, `H4`, `H6`, `H8`, `H12`, `D1`, `W1` and `MN1`. `timeframe` defaults to `H1`, `count` defaults to 100 and is capped at 5000, and `from`/`to` are ISO timestamps in server time that take precedence over `count`. Each bar carries `time`, `open`, `high`, `low`, `close`, `tick_volume`, `real_volume` and `spread`.

7. Judge whether the current spread is acceptable by comparing `mt5_symbol`'s `spread_points` with the recent average implied by the `spread` field on the bars from `mt5_rates`. A spread far above that recent average is a bad entry, and waiting is cheaper than crossing it.

8. Check `spread_float` on `mt5_symbol` to know whether the spread is variable. On a floating-spread symbol the current `spread_points` is a moment, not a promise, so the comparison in step 7 matters more than the single reading.

9. Check `swap_long` and `swap_short` on `mt5_symbol` when the trade may be held overnight. On a CFD position the overnight swap can exceed the profit on the trade.

Worked example:

```
mt5_symbols search=XAUUSD   -> ["XAUUSD.m"]
mt5_symbol  symbol=XAUUSD.m -> digits 2, point 0.01, trade_stops_level_points 50,
                               spread_points 30, market_open true, bid/ask present
mt5_rates   symbol=XAUUSD.m timeframe=M5 count=20
                            -> the spread column averages about 25 points
mt5_quote   symbols=["XAUUSD.m","EURUSD.pro"]
```

What it never does:

- It does not size, validate or send anything. These four tools are reads.
- It does not derive a stop or a size from pips. Every number comes from `mt5_symbol`.
- It does not price a market order on a symbol with no live quotes, because there is no price to use.
- It does not cache metadata across orders. `mt5_symbol` is re-read each time because the broker can change it intraday.
- It does not decide that a market is open from the clock alone. `market_open` comes from the symbol's own `trade_mode`.
- It does not pick a timeframe for the user. It accepts the one given and defaults to `H1` only when none is given.
- It does not convert points into pips for any trading decision. `pip_size_display_only` exists for display and nothing in the order path uses it.
- It does not treat `point` and `tick_size` as interchangeable, because on some symbols they differ.
- It does not assume a quote is fresh. `tick_time` on `mt5_quote` says when the quote was last updated.
