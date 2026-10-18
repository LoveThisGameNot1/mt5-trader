---
name: mt5-correlation
description: Show how much several positions are really one position, using `mt5_correlation` and the kernel's `portfolio_correlation` check. Use when a user asks "are these trades the same trade", "is my portfolio one bet", "correlation between EURUSD and GBPUSD", "why is my order blocked by correlation", "what are my clusters", or before adding a position while others are open.
argument-hint: "[symbols...] [timeframe] [threshold]"
---

See that several positions are one position, before the money at risk is counted as if it were several.

1. Call `mt5_correlation` with optional `symbols`, `timeframe`, `count` and `threshold`. `symbols` defaults to `config.symbols`, `timeframe` defaults to `D1`, `count` defaults to 120 bars, and `threshold` defaults to 0.8. `timeframe` accepts the same codes as `mt5_rates` (`M1` through `MN1`). Whatever is already open is added to the list whether or not it is on the watchlist, because that is the case the kernel cares about. Only closed bars are read: the still-forming bar is dropped before any return is computed, so a reading does not change under you between two calls.

2. Understand the gap this closes. The kernel counts money at risk per position (`open_risk.total`). It cannot, by itself, see that being long EURUSD and long GBPUSD is one trade with twice the size. Correlation is the number that exposes it.

3. Know that the measure is on *returns*, never on price levels. Two series that both trend upward have a price-level correlation near 1 while having nothing to do with each other, which is the oldest way to produce a meaningless number. `mt5_correlation` correlates period-over-period returns, so a shared trend does not masquerade as a shared driver.

4. Read `aligned_points`. The two series are aligned on the *intersection* of bar timestamps first, so an index that closed for a day is not compared bar-for-bar across the hole. `aligned_points` is how many observations survived that intersection; `bars_requested` is not.

5. Trust the floor. Below `min_points` observations the coefficient is refused rather than reported, and `correlation_min_points` cannot be lowered past 20 by config: the kernel takes `max(20, configured)`. A coefficient from a dozen bars is noise, and a gate that fires on noise is worse than no gate. The reply carries `min_points` so the floor in force is visible next to `aligned_points`.

6. Separate the two kinds of missing data. A symbol that did not return enough bars to reach the floor is listed in `missing`, with its `bars` count, and is dropped from the matrix entirely. A symbol that was named but could not be resolved is listed in `failed`, with a `reason`.

7. Read `pairs` and `correlations`. `pairs` is sorted by absolute `r` descending, each entry carrying `left`, `right`, `r` and `points` (the observations behind that specific pair). `correlations` is the full matrix, diagonal set to 1.0. A `null` value means the pair had no variance in the window, which is a state, not a zero.

8. Read `clusters`. A cluster is a group whose absolute correlation reaches `threshold`, including negative correlation: an inverse pair is just as much one bet as a same-direction pair, so a long and a short on correlated symbols are grouped together. Each cluster carries its `symbols` and the mean absolute `correlation` inside it, and is sorted by size.

9. Read the `caveat` and act on it. Correlation is not stable: it is measured on returns over the window above and changes with the regime. Reproduce it before acting on it rather than treating one reading as a property of the pair.

10. Expect the kernel to run the same measurement inside `portfolio_correlation` on every plan. It compares the proposed `symbol` against every open position using `correlation.exposure`: the highest absolute `r` against any open position, over `guards.correlation_lookback_bars` D1 bars (default 120), at the same `correlation_min_points` floor.

11. Read the direction handling. Same direction at or above `guards.correlation_limit` blocks, or warns when `guards.correlation_action` is `warn`, because two aligned longs are one bet at double size. The opposite direction only warns, because two opposed correlated positions partly offset each other: the finding is reported under `offsets`, never as a block.

12. Read the fail-closed rule. If a symbol's history cannot be read, or has no variance, the pair lands in `unverified` and the check fails closed, because "I could not check" and "this is fine" must not look the same. Under `correlation_action=block` an unverified pair blocks; the check detail says the cluster risk is unverified.

13. Read `failed_checks` on the verdict. The finding is recorded there even in `warn` mode, because it is a finding; what `warn` changes is whether it blocks, and the check detail says so. `correlation_limit` is clamped to the hard ceiling of 0.99 in `config.HARD_CEILINGS`, so the config can tighten the gate toward certainty but never disable it from a file the model could edit.

14. Read the two list fields the tool adds on top of the matrix. `open_positions` is every symbol this kernel currently holds, whether or not it was on the watchlist, so you can see the set the kernel would correlate against. `failed` names any symbol that was given but could not be resolved, with its reason.

15. Use it as the portfolio view before sizing, not after. `mt5_validate` runs `portfolio_correlation` itself, so a plan on a symbol that is one bet with an open position is caught there; `mt5_correlation` is how you see the clustering in advance and choose a genuinely separate idea instead.

Worked example:

```
mt5_correlation  symbols=["EURUSD.pro","GBPUSD.pro","XAUUSD.m"] threshold=0.8
                 -> aligned_points 118, pairs sorted by |r|,
                    clusters [["EURUSD.pro","GBPUSD.pro"]],
                    caveat "correlation ... changes with the regime"

# XAUUSD.m open, a new long EURUSD.pro plan
mt5_validate     symbol=EURUSD.pro side=buy stop=... volume=0.10
                 -> portfolio_correlation ok true,
                    detail "highest |r| against an open position: 0.5, over 118 points",
                    passes because 0.5 is under the 0.8 limit, and says what it measured
```

What it never does:

- It does not correlate price levels. Only returns, so a shared trend is not a shared cause.
- It does not compare bars across a timestamp gap. It aligns on the intersection first and reports the surviving count as `aligned_points`.
- It does not report a coefficient below 20 observations. The floor cannot be lowered by config.
- It does not treat a negative correlation as safe. `clusters` uses absolute `r`, because an inverse pair is one bet.
- It does not block an opposing position. Only same-direction correlation at or above the limit blocks; the opposed case warns.
- It does not pass silently when a history is unreadable. The pair is `unverified`, and that fails closed.
- It does not hide a finding in `warn` mode. It records the check in `failed_checks` and states that it did not block.
- It does not claim the number is stable. It says the opposite, and asks you to reproduce it.
- It does not add a held symbol from memory. Each symbol is resolved to the broker's own name before its bars are read.
- It does not present `bars_requested` as the sample size. The number behind a coefficient is `points`, after the timestamp intersection.
- It does not decide direction on its own. The matrix reports `r` and `clusters`; the kernel, which knows each position's side, decides whether that is an addition or an offset.
- It does not read a symbol's correlation twice under two names. Whatever is open is merged into the single list, not appended alongside the watchlist.
- It does not use the still-forming bar. Every series ends at the last closed bar, so two calls a minute apart do not disagree because the current candle changed.
- It does not re-ask the terminal for a series it already has. Within a short in-process cache the same bars come back, which is what keeps one verdict from hammering the terminal per open position.
- It does not call a flat-looking account diversified. It reports the clustering, and a cluster of size two or more is one bet regardless of how the account is split into tickets.
