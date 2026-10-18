---
name: mt5-cot
description: Read what large speculators actually hold in the futures underneath a CFD, using `mt5_cot` and the shell command `python tools/fetch_cot.py`. Use when a user asks "what are the large speculators doing", "is EURUSD long crowded", "what do the big players hold in gold", "show me the COT report", "is positioning at an extreme", or "are the funds still building", before treating a crowded market as a fresh one.
argument-hint: "[symbol ...] [--years N]"
---

Read the CFTC Commitments of Traders positioning for the instruments beneath the CFDs, and refresh the file it comes from.

1. Refresh the data first, by hand or on a schedule: `python tools/fetch_cot.py`. This is the only part of the project that uses the network, and it is deliberately not the server, because a trade-path decision must not depend on a website being up. It downloads the CFTC annual files, writes `state/cot.json`, and the server reads that file.

2. Use `--years N` (default `3`) for more history, `--out` to write elsewhere, and `--file annual.txt` to parse a file you already have. The download is the CFTC legacy futures-only annual file, which is where the currency futures live.

3. Call `mt5_cot` with optional `symbols` (defaults to `config.symbols`) and optional `weeks`. If the reply is `available: false`, its `how_to_fix` field names the command above. The server itself makes no network call.

4. Understand why this stands in for equity research. A currency has no 13F equivalent, and a 13F would be forty-five days stale anyway. What exists is the CFTC report of positions in the currency, metal, energy and American index futures underneath the CFDs, published weekly.

5. Read the three report categories. Non-commercial is the large speculators, funds and CTAs, the crowd that gets crowded; `long_spec`, `short_spec` and `net_spec` are their latest position. Commercial is producers and hedgers, usually the opposite side, and `commercial_net` is not a directional opinion. Non-reportable is small traders, below the reporting threshold.

6. Know the age. The report is published on a Friday for the Tuesday before it, so it is three days old when it arrives and describes a position that already happened. It measures crowding, not a signal.

7. Read `net_percentile` and `net_zscore`: the current net position ranked against the last few years. The `crowding` sentence says in words whether it is at an extreme, such as "crowded long: the largest net long in this window", or "middle of the window, no positioning extreme".

8. Read `long_change_week`, `short_change_week` and `net_change_week`. These week-over-week changes separate a crowded position that is being unwound from one still being built, which are different situations behind the same net.

9. Read `net_spec_percent_of_oi` against `open_interest` to scale the net to the market's size, and `report_date` and `window_from` with `weeks` to see exactly which window the ranking used.

10. Expect `mapped_because` on each report and read `unmapped` for the rest. The contract is chosen by a verified exact CFTC contract name, never by a guess. A European index such as DE40 or GER40 has no contract in this report, which covers American futures only, and is reported as unmapped with that reason. A cross such as EURGBP maps to its base currency's contract, EUR, because that is the leg with a directional opinion in it.

11. Pass `weeks` to re-rank against a shorter window than the file holds; the reply then adds `ranked_over_weeks`. The percentile and z-score need at least eight weeks of history and are reported as `null` below that.

12. Read `source` and `fetched` on the reply, and `caveat`, which restates the three-day lag, to know how old the file is.

Worked example:

```
python tools/fetch_cot.py --years 5
mt5_cot symbols=["EURUSD.pro","XAUUSD.m","DE40.cash"] weeks=104
    -> EURUSD.pro report net_spec -137573, net_percentile 4.2,
       crowding "crowded short: the largest net short in this window"
       XAUUSD.m   report net_percentile 91.0, long_change_week +8120
       unmapped: DE40.cash "a European index and this report covers
                 American futures only"
       ranked_over_weeks 104
```

What it never does:

- It does not make a network call from the server. `tools/fetch_cot.py` is the only network code.
- It does not guess a contract for an unmapped symbol. It says so and gives the reason.
- It does not treat the commercial net as a directional opinion.
- It does not present the report as current. It is three days old on arrival.
- It does not compute a percentile or z-score under eight weeks of history.
- It does not cover a European index or a cross with a contract of its own.
- It does not place, size, validate or send anything. It is a read.
- It does not turn crowding into an entry or exit trigger on its own.
- It does not interpolate a missing week or a missing contract.
- It does not refresh the file on its own. `mt5_cot` reads what `tools/fetch_cot.py` last wrote.
- It does not match a contract by fuzzy name. Only the documented exact CFTC names are used.
- It does not treat `weeks` as a fetch parameter. It only shortens the window the ranking is computed over.
- It does not report a position without the window it was ranked against.
