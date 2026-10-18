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
  260-test suite possible;
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

## Structs are read defensively

Every MT5 result is a CPython structseq, and the field sets are not frozen:
MetaQuotes adds fields between builds and some exist only on certain account and
instrument types. `records.get` never raises — a missing field degrades to a known
value. `records.to_jsonable` checks `_fields` *before* the tuple branch, because a
structseq is a tuple subclass and the other order silently flattens a whole
position into a list of bare values. (A test caught exactly that.)

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
  server.py       the MCP stdio server and the tool registry
  cli.py          doctor / config / tools / selftest / serve
  fixtures.py     four instrument types and a stand-in terminal, for tests
tests/            260 tests, offline, no terminal
```

## Testing

```
python -m unittest discover -s tests -t .      # 260 tests, about 2 seconds
python -m mt5trader selftest                   # the same guarantees, for a shell
```

`fixtures.py` carries a fake `MetaTrader5` with an in-memory book of positions,
orders and deals, and a scripted failure queue, so the retry, rotate-filling,
reconciliation, freeze and drift paths are all exercised without a broker. Four
instrument types are in there specifically because they break naive code: a
5-digit forex pair, gold, an index CFD with one digit, a crypto contract, and a
symbol whose partial closes strand a remainder below the minimum.

