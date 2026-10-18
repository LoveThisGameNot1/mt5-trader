# Architecture, and the MetaTrader 5 traps it is shaped around

Most MT5 automation fails for reasons that have nothing to do with strategy.
This document is the list of those reasons and where each one is handled.

## The module owns one thread

The `MetaTrader5` python package is an IPC client for a desktop terminal, and it
is not thread safe: every call must come from the thread that ran `initialize()`.
Get it wrong and `order_send` works nine times and returns nonsense the tenth,
which reads like a broker problem and is not one.

`mt5trader/bridge.py` confines the whole module to one private worker thread.
The server's stdio loop hands jobs to that thread over a queue and blocks for the
answer. Consequences, all deliberate:

* calls are serialised — MT5 is one desktop terminal, parallelising wins nothing;
* `initialize`/`shutdown` run on the same thread, so the terminal handle never
  crosses a boundary;
* the module is imported *inside* the worker, so the kernel stays importable and
  testable on a machine with no Windows and no terminal, which is what makes the
  281-test suite possible;
* a terminal that was restarted between two calls is detected and re-initialised
  instead of poisoning every call after it.

## A timeout is not a failure

MT5 has no client order id, so there is no server-side key to look an order up
by. Retcode `10012` (timeout) and an empty `order_send` result both mean "the
terminal did not confirm", which means **the order may be live**.

`orders.send` therefore writes an intent record to disk *before* the request goes
out, and an ambiguous answer stops the attempt loop rather than retrying.
`orders.reconcile` then searches the deal history, open positions and pending
orders for a match, and `RiskKernel` blocks all new exposure while any intent is
unresolved. The failure this prevents is the expensive one: silently doubling a
position on a flaky connection.

Retcode classification lives in `errors.py`, in four buckets:

| bucket | codes | what happens |
| --- | --- | --- |
| ok | 10008 placed, 10009 done, 10010 partial | record it |
| retry | 10004 requote, 10020, 10021, 10024, 10030, 10031 | re-price or rotate filling mode, send again |
| ambiguous | 10012, or no result at all | stop, mark the intent, reconcile |
| fatal | everything else | stop, report the broker's reason |

## Filling mode is negotiated

Symbols advertise FOK and IOC in a bitmask; market-execution symbols also accept
RETURN, which is what makes a market order succeed when only part of the volume
can be filled. Sending the wrong one is retcode `10030`, the most common
rejection in MT5 automation after sizing, and it looks like a broker problem.

`Instrument.filling_candidates` builds the list from the symbol's own bitmask
(there is no `SYMBOL_FILLING_*` constant in the python module, so bits 1 and 2 are
named locally), and `orders.send` rotates to the next candidate on `10030`.

## Volume is `min + n * step`

Valid sizes are `volume_min + n * volume_step`, not "any multiple of step". Sending
0.25 when the step is 0.1 and the minimum is 0.01 is a rejection, not a rounding.
And `0.3 / 0.1` is `2.9999999999999996` in binary floating point, which floors to
0.2 without Decimal. `Instrument.normalize_volume` runs through `Decimal` and
floors, never rounds up: rounding up would put on more risk than the plan sized
for.

## Prices and risk are decimal arithmetic

`1.08512 - 1.08412` is `0.001000000000000112` in floating point. That is enough to
make a hundred-point stop look like a hundred and one, and to floor a one-lot
position down to 0.99 for a reason nobody can explain to a user. Every price
difference that decides a position size runs through `Decimal` after rounding to
the symbol's digits.

`normalize_price` rounds half away from zero, which is what MQL's
`NormalizeDouble` does. Python's `round()` is banker's rounding and disagrees with
the terminal.

## Stops come from tick metadata, not pips

"1 lot = 100k = $10 a pip" is true for a handful of instruments. The same code
has to size EURUSD, XAUUSD, US30 and BTCUSD, so everything is derived from
`tick_size` and `tick_value_loss`:

| symbol | tick size | tick value (loss) | result |
| --- | --- | --- | --- |
| EURUSD | 0.00001 | $1.00 | $10 per pip per lot |
| XAUUSD | 0.01 | $1.00 | $100 per $1 move per lot |
| US30 | 0.1 | $0.10 | $1 per index point per lot |
| BTCUSD | 0.01 | $0.01 | $1 per $1 move per lot |

Risk uses `tick_value_loss`, not `tick_value`: on a cross-currency symbol the two
differ, and the loss side is the one that bites.

## `trade_stops_level` is in points, and points are not pips

`trade_stops_level` and `trade_freeze_level` are in points, `point` is not always
`tick_size`, and a pip is ten points on a five-digit forex symbol and irrelevant
everywhere else. A stop closer than the stop level is rejected (`10016`); a stop
inside the freeze level cannot be modified at all (`10029`). Both are checked
before a request is built, and the freeze check uses the *current* price, because
that is what the terminal compares against.

## A stop is validated against the price, not the entry

Validating a stop against the entry price refuses every trailing stop that worked:
a trailing stop on a winner sits *above* the entry, which is entirely legal.
`normalize_stops` measures against the price the broker will compare with — the
order price for a new order, the current price when moving an existing stop.

Related: MT5's `TRADE_ACTION_SLTP` is absolute, not a patch. Omitting the take
profit does not leave it alone, it clears it. So `positions.modify` reads the
position first, validates only what the caller asked to change, and carries the
rest through untouched.

## Every mutation is verified by reading it back

`order_send` returning `10009` means the request was accepted, not that the
position now looks the way it was asked to. A stop that silently failed to move is
the most dangerous state in trading. `positions._send_and_verify` re-reads the
position and reports `drifted` when the broker accepted a change it did not
apply.

## The broker's clock is measured, not assumed

MT5 has three clocks called "time": the terminal's `datetime` values carry server
time presented as if it were UTC, your machine has its own, and the broker's
trading day starts at server midnight. There is no offset in the API to look up.

`history.server_offset` measures it from the newest tick's millisecond timestamp,
which is the only sub-second server clock MT5 exposes. On a weekend every tick is
hours old, and the age of the newest tick is not a time zone — reporting "-36
hours" would shift every daily boundary by a day and a half — so in that case the
offset is declared unmeasurable, server time is treated as local, and the
condition is re-checked on every call so it self-corrects as soon as anything
trades.

## Netting and hedging

Netting accounts hold one position per symbol; hedging accounts hold many. Closes
go out as an opposite market order carrying `position=ticket`, which reduces the
single position on a netting account and targets exactly one ticket on a hedging
account. A partial close that would leave a remainder under the symbol's minimum
volume is refused by the broker, so the remainder is closed in full instead and
the caller is told.

## Structs are read defensively, and the names are verified

Every MT5 result is a CPython structseq, and the field sets are not frozen:
MetaQuotes adds fields between builds and some exist only on certain account and
instrument types. `records.get` never raises — a missing field degrades to a known
value. `records.to_jsonable` checks the record case *before* the sequence case,
because a structseq is a tuple subclass and the other order silently flattens a
whole position into a list of bare values. (A test caught exactly that.)

Two harder versions of the same problem, both found by pointing the thing at a
real terminal rather than at a fixture:

**The tick fields have a `trade_` prefix.** They are `trade_tick_size` and
`trade_tick_value_loss`, not `tick_size` and `tick_value`. Reading a name the
terminal does not have returns None, a None falls back to a default, and the
default for a tick value is zero. A zero tick value does not raise: it makes every
position risk nothing and every symbol look quiet. So `instruments.check_schema`
asserts the names against the live terminal, `mt5_status` reports a
`symbol_schema` block, and a missing required field is printed as a problem
rather than discovered later as a wrong position size.

**`copy_rates_*` returns numpy when numpy is installed.** One row of that array is
a `numpy.void`: its fields live in `dtype.names`, `dir()` does not list them,
`getattr(row, "close")` returns nothing, and it is not a tuple. Every obvious way
to read it misses, so the bar arrives with no close price. `records._field_names`
handles all three shapes (structseq, namedtuple, numpy structured scalar), and a
numpy structured *array* is treated as a sequence of records rather than as one
record whose fields are whole columns. `to_jsonable` converts numpy integers with
`.item()`, because otherwise a volume serialises as `"<numpy.int64 object at
0x...>"` and reaches the model as text.

## A quote can exist and still be stale

A Friday tick still carries a bid, an ask and a one-point spread. On a Sunday the
kernel will happily report `market_open: true` (that flag is about broker
permission, not sessions) with a spread of one point, and the order comes back
`MARKET_CLOSED` after a pointless round trip. `quote_freshness` compares the
tick's server timestamp against the measured clock, so a closed market is caught
locally. Pending orders skip the check: resting until the price arrives is
exactly what a breakout order is for, and the closed session is when it belongs in
the market.

## Reading the market beyond the tick

Six modules sit on top of the bar data, and they all go through
`market.bars`, which drops the forming bar and returns plain dicts whatever shape
the terminal handed back:

| module | what it answers |
| --- | --- |
| `levels.py` | where the structure is: period extremes, the weekly open, ATR bands, round numbers, each with a distance in price, points and ATR |
| `sessions.py` | when it moves: the range delivered per server hour, the named sessions mapped onto the broker's clock, the current hour's share of a typical day |
| `correlation.py` | whether two positions are one position: return correlation over the intersection of timestamps, and the clusters it exposes |
| `news.py` | what is scheduled: the terminal's economic calendar, read from JSON a service writes, turned into blackout windows |
| `cot.py` | what the large speculators hold, and how crowded that is against the last few years |
| `history.py` | what already happened, from the deal history |

Two of them read files rather than the terminal, and that is deliberate: the
calendar export and the COT report are written by separate small programs, so a
trade-path decision never depends on a website being up. `tools/fetch_cot.py` is
the only thing in the project that touches the network.

## The calendar, and why there is an MQL5 file

The `MetaTrader5` python package exposes no `calendar_*` function. Not a limited
one: none. The terminal's economic calendar is reachable only from MQL5, so
`mql5/CalendarExport.mq5` is a service that runs inside the terminal, reads
`CalendarValueHistory`, and writes one JSON file on a timer. `tools/build_sidecar.py`
finds the terminal's data folder (by asking the running terminal, not by guessing),
copies the source there, drives `MetaEditor64.exe` headless, and reads the
`Result: N errors` line out of the compile log rather than trusting the exit code,
which is 1 even on a clean compile.

The Python side turns an event into a *window*, never into text for the model to
interpret. Free content from outside the process, landing in the same context that
holds the order tools, is a prompt injection surface; a monitor that emits
sentences is a monitor a model can argue with. A window cannot be argued with.

The export carries both a normalised importance (0/1/2) and the terminal's raw
enum value, because MQL5's `ENUM_CALENDAR_EVENT_IMPORTANCE` has four levels and a
wrong assumption about that would otherwise be invisible.

## Two bugs the fixtures had, and why they mattered

Worth recording, because both were the *test* lying rather than the code failing:

**An H1 bar was sixty hours long.** The fake decoded an MT5 timeframe code as
minutes and then multiplied by sixty, so `H1` became `16385 minutes`. Every
per-hour statistic was computed across a grid of sixty-hour buckets. The real
encoding is: minutes as-is, hours as `n | 0x4000`, weeks as `1 | 0x8000`, months
as `1 | 0xC000`.

**Every symbol had the same straight line.** The fake generated one linear series
for every symbol, so the correlation between any two of them was 1.0 and every
correlation test would have been meaningless in the direction of passing. Each
symbol now gets its own seeded walk, and `set_rates` overrides one when a test
needs two series that really do move together.

## Layout

```
mt5trader/
  config.py       paths, config, HARD_CEILINGS, the environment switches
  errors.py       retcodes as text and as "may I retry this"
  records.py      defensive field access and JSON-safe conversion
  state.py        atomic state, the intent log, the append-only journal
  bridge.py       the one thread allowed to touch the module
  instruments.py  symbol metadata, volume/price/stop normalisation
  sizing.py       size from money at risk, R multiples, margin
  risk.py         the kernel: floors, kill switch, exposure, rate limits
  orders.py       request building, retries, reconciliation
  positions.py    SL/TP, partial close, break-even, trailing, flatten
  history.py      broker clock, realised PnL, statistics
  market.py       bars, EMA/ATR/returns, correlation, timestamp alignment
  levels.py       period extremes, weekly open, ATR bands, round numbers
  sessions.py     per-server-hour range statistics and the named sessions
  correlation.py  return correlation and clusters
  news.py         the economic calendar as blackout windows
  cot.py          CFTC positioning, crowding percentiles
  server.py       the MCP stdio server and the tool registry
  cli.py          doctor / config / tools / selftest / serve
  fixtures.py     instrument types and a stand-in terminal, for tests
mql5/             CalendarExport.mq5, the calendar service the terminal runs
tools/            fetch_cot.py, build_sidecar.py, render_docs.py
tests/            386 tests, offline, no terminal
```

## Testing

```
python -m unittest discover -s tests -t .      # 281 tests, about 2 seconds
python -m mt5trader selftest                   # the same guarantees, for a shell
```

`fixtures.py` carries a fake `MetaTrader5` with an in-memory book of positions,
orders and deals, and a scripted failure queue, so the retry, rotate-filling,
reconciliation, freeze and drift paths are all exercised without a broker. Four
instrument types are in there specifically because they break naive code: a
5-digit forex pair, gold, an index CFD with one digit, a crypto contract, and a
symbol whose partial closes strand a remainder below the minimum.


