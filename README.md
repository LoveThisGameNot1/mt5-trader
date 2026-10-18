# mt5-trader

MetaTrader 5 trading as an MCP server, plus eight skills that drive it. Works
under Claude Code, the Codex CLI and opencode, from the same server and the same
tool names.

Sizing, stops, filling modes, retries and reconciliation are computed from the
terminal's own symbol metadata, so the same code trades EURUSD, gold, an index
CFD and a crypto contract without a pip assumption anywhere. Every order passes a
risk kernel that no tool can override, and autonomy is granted by the environment
the server was launched in rather than by anything the model can do at runtime.

On top of the execution core sit six read-side modules: structural levels, session
statistics, return correlation, the terminal's economic calendar, CFTC positioning
and the deal history. The calendar is a hard gate; the rest is context a human
reads before deciding.

```
you  ->  Claude Code / Codex / opencode  ->  MCP (stdio)  ->  mt5trader  ->  MetaTrader 5
                                                              |
                                                     risk kernel + intent log
```

No API keys, no third-party service, no network calls of its own. The MetaTrader5
python package and the terminal do all the talking.

## The parts that are not a wrapper

Most "LLM trades MT5" projects are a `order_send` behind a prompt. These are the
things that decide whether it survives a real account, and where each one lives:

* **One thread owns the module.** The MetaTrader5 package is not thread safe and
  must be called from the thread that ran `initialize()`. The whole module is
  confined to one worker thread behind a queue.
* **A timeout is never retried.** Retcode `10012` means *the order may be live*.
  Mt5-trader writes an intent before sending, reconciles ambiguous results against
  the deal history, and blocks new exposure until every intent is resolved.
* **Filling mode is negotiated from the symbol's bitmask.** Not sending the wrong
  one is the difference between a working market order and `10030`.
* **Volume is `min + n * step`, in Decimal.** Valid sizes are not "multiples of
  step", and `0.3 / 0.1` is `2.999...` in binary floating point.
* **Prices are decimal too.** `1.08512 - 1.08412` is `0.001000000000000112`, enough
  to floor a one-lot position to 0.99 for no reason.
* **Stops are validated against the price, not the entry.** Otherwise every
  trailing stop that ever worked gets refused.
* **Every mutation is read back.** A stop the broker accepted but did not apply is
  reported as *drifted*, never as done.
* **The broker's clock is measured.** MT5 has three clocks called "time" and no
  offset in the API. The offset is derived from the newest tick, and declared
  unmeasurable when the market is closed instead of invented.
* **The terminal's field names are asserted, not trusted.** They are
  `trade_tick_size` and `trade_tick_value_loss`. Getting that wrong yields a zero,
  and a zero tick value sizes every position at nothing without raising.
* **A quote that exists is not a quote that is current.** A Friday tick still has a
  bid, an ask and a one-point spread, so a closed market looks open until
  freshness is compared against the clock.
* **`copy_rates_*` returns numpy when numpy is installed**, as a structured array
  whose rows hide their fields from `dir()` and `getattr`. Handled, and tested
  against real numpy.

Long version, with the reproducing example for each: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Install

Requires Windows, a running MetaTrader 5 terminal and Python 3.8+.

```bash
pip install MetaTrader5        # the only dependency, and it is already in the terminal's own python
git clone https://github.com/LoveThisGameNot1/mt5-trader.git
cd mt5-trader
python -m mt5trader selftest   # offline checks, no terminal needed
python -m mt5trader doctor     # is the terminal reachable, is Algo Trading on
```

Two optional extras, both of which report what they found rather than assuming:

```bash
python tools/fetch_cot.py      # CFTC positioning, the only thing here that uses the network
python tools/build_sidecar.py  # install + compile the calendar service into the terminal
```

Then copy `config.example.json` to `config.json` and set your symbols and limits.
`python -m mt5trader config` prints what is actually in force and every clamp that
was applied.

## Wiring it into an agent

**Claude Code** — `.mcp.json` in the project, or `claude mcp add`:

```json
{
  "mcpServers": {
    "mt5": {
      "command": "python",
      "args": ["-m", "mt5trader", "serve"],
      "cwd": "C:/path/to/mt5-trader",
      "env": { "MT5_AUTONOMY": "propose" }
    }
  }
}
```

**Codex CLI** — append to `~/.codex/config.toml`:

```toml
[mcp_servers.mt5]
command = "python"
args = ["-m", "mt5trader", "serve"]
cwd = "C:/path/to/mt5-trader"
startup_timeout_sec = 60

[mcp_servers.mt5.env]
MT5_AUTONOMY = "propose"
```

**opencode** — same block in `opencode.json`.

A copy of each is in `hosts/`. GPT and Codex models get the tool schemas over MCP.
For models that do better with a written contract than with tool descriptions,
`hosts/AGENTS.md` carries the same workflow as prose, and the eight `SKILL.md`
files work as plain prompts when no MCP client is available.

## The environment switches

Autonomy and the money switches are deliberately **not** in the config file,
because a file the model can edit is not a boundary:

| variable | default | what it does |
| --- | --- | --- |
| `MT5_AUTONOMY` | `propose` | `propose` plans only, `auto` needs a per-order confirm, `full` sends on a passing risk check |
| `MT5_ALLOW_LIVE` | off | real accounts are read-only without it |
| `MT5_ALLOW_KILL_RELEASE` | off | clearing the kill switch is a human decision; tripping it never is |
| `MT5_ALLOW_FLATTEN` | off | closing positions this kernel does not own |
| `MT5_ALLOW_RISK_EDIT` | off | writing limits |
| `MT5_MAGIC` | `770001` | the magic number that marks this kernel's positions |
| `MT5_STATE_DIR` | `./state` | where state, plans, intents and the journal live |
| `MT5_TERMINAL_PATH`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` | – | for a terminal that needs launching or a specific account |

Start on a demo with `propose`. Read verdicts for a week. Then `auto`. `full` is a
decision about trusting your strategy, not about trusting the software.

## The eight skills

Each is a `SKILL.md` with the exact tool calls, the order to make them in, and
what the skill refuses to do.

| skill | job |
| --- | --- |
| `mt5-doctor` | Is the stack actually up? Module, terminal, connection, Algo Trading, account type, clock, symbols, switches. Answers with the terminal closed. |
| `mt5-market` | Read the market: symbol metadata, quotes, spread against its own recent average, bars, and what the numbers mean for this symbol. |
| `mt5-plan` | Turn an idea into an exact order: size from money at risk, stops inside the broker's limits, and a full kernel verdict with a fingerprint. |
| `mt5-execute` | Place it, once, with the kernel's authority, and reconcile instead of resending when the answer is unclear. |
| `mt5-manage` | Stops, partials, break-even, trailing, flatten. One trailing step per call, so the rate limits stay meaningful. |
| `mt5-guard` | The limits in force, the state of the day, the kill switch, unresolved intents, and how to go flat. |
| `mt5-review` | What actually happened: round trips, win rate, profit factor, expectancy, drawdown, loss streaks, close reasons, the realised curve. |
| `mt5-structure` | Where and when: period extremes, the weekly open, ATR bands and round numbers with their distance in ATR, plus the range the market actually delivered at each server hour. |
| `mt5-correlation` | Whether several positions are one position, and what the cluster threshold does to a new order. |
| `mt5-calendar` | Scheduled releases as blackout windows, the service that exports them, and what `enforce` does when the export stops. |
| `mt5-cot` | What the large speculators hold in the futures underneath the CFDs, and whether that positioning is crowded. |
| `mt5-autopilot` | The loop contract for unattended running: the pre-flight, the one-order-per-decision rule, when to trip the switch, when to stop entirely. |

## The tools

30 tools: 20 that only read, 10 that change something. `python -m mt5trader tools`
lists them. Full reference with every argument: [docs/TOOLS.md](docs/TOOLS.md).

## Where the read-side data comes from

| input | source | age |
| --- | --- | --- |
| prices, bars, symbol metadata | the terminal, live | now |
| structural levels, sessions, correlation | the terminal's own bar history | now |
| economic calendar | the terminal's calendar, exported by an MQL5 service | up to 5 minutes |
| CFTC positioning | `tools/fetch_cot.py` from cftc.gov | 3 days, by publication |
| what already happened | MT5 deal history | now |

Nothing is fetched while a trade decision is being made. The two file-based inputs
are written by separate small programs on their own schedule, because a trade-path
decision must not depend on a website being up.

## Safety

[docs/RISK.md](docs/RISK.md) is the whole model. The short version:

* `HARD_CEILINGS` are compiled into the source. Config can tighten them, never
  raise them, and a clamp is reported rather than silent.
* Autonomy is an environment variable read at startup, not a config file.
* Real accounts are read-only unless the process was launched with
  `MT5_ALLOW_LIVE=1`.
* Unresolved intents block new exposure. Closing is always allowed.
* A stop-less position is treated as eating the whole remaining allowance.
* The day is measured in equity against server-midnight equity, so floating losses
  count, and hitting the daily loss limit trips the kill switch.
* Missing data fails closed. "I could not verify this" and "this is safe" never
  produce the same verdict.

## Tests

```bash
python -m unittest discover -s tests -t .   # 408 tests, offline, ~3s
```

The suite needs no terminal, no Windows and no MetaTrader5 wheel. `fixtures.py`
carries a stand-in terminal with an in-memory book and a scripted failure queue,
so retries, filling-mode rotation, reconciliation, freeze levels and read-back
drift are all covered. The tested instrument set is the one that breaks naive
code: a 5-digit forex pair, gold, a one-digit index CFD, a crypto contract, and a
symbol whose partial close strands a remainder under the minimum.

## Honest limits

* **Not financial advice.** Every limit here bounds how much you can lose, not
  whether the edge exists. A negative-expectancy strategy loses on schedule.
* **Correlation is not counted as a model, only as a tripwire.** Five positions
  across correlated pairs are one trade, and `guards.correlation_limit` catches the
  obvious case. It is measured over the recent window and it is not stable.
* **COT data is three days old when it arrives.** It is published on a Friday for
  the Tuesday before it. It measures crowding, not direction and not timing.
* **Gap risk lands outside every limit.** A Sunday open or a news spike fills where
  it fills. A stop is a request.
* **`FIFO`, close-by and exchange-execution accounts** are detected and reported
  but never used: closes go out as an opposite market order on the same position,
  which every account type accepts. On a FIFO account the broker decides which
  position closes first, not you.
* **A wrong clock reading is possible when the market is closed.** It is declared,
  not guessed, and it self-corrects when anything trades.
* **The tested-at date is in the commit log, not in this file.**

## Layout

```
mt5trader/      the kernel, the market readers and the MCP server
mql5/           CalendarExport.mq5, the service the terminal runs
tools/          fetch_cot.py, build_sidecar.py, render_docs.py
skills/         twelve SKILL.md files
hosts/          Claude, Codex and opencode wiring, plus AGENTS.md
docs/           ARCHITECTURE.md, RISK.md, TOOLS.md
tests/          the offline suite
config.example.json
```

MIT. Built by [LoveThisGameNot1](https://github.com/LoveThisGameNot1).



