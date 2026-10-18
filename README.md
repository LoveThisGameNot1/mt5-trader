# mt5-trader

MetaTrader 5 trading as an MCP server, plus eight skills that drive it. Works
under Claude Code, the Codex CLI and opencode, from the same server and the same
tool names.

Sizing, stops, filling modes, retries and reconciliation are computed from the
terminal's own symbol metadata, so the same code trades EURUSD, gold, an index
CFD and a crypto contract without a pip assumption anywhere. Every order passes a
risk kernel that no tool can override, and autonomy is granted by the environment
the server was launched in rather than by anything the model can do at runtime.

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
| `mt5-autopilot` | The loop contract for unattended running: the pre-flight, the one-order-per-decision rule, when to trip the switch, when to stop entirely. |

## The tools

25 tools: 15 that only read, 10 that change something. `python -m mt5trader tools`
lists them. Full reference with every argument: [docs/TOOLS.md](docs/TOOLS.md).

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
python -m unittest discover -s tests -t .   # 260 tests, offline, ~2s
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
* **Correlation is not counted.** Five positions across correlated pairs are one
  trade; the kernel counts risk, not correlation.
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
mt5trader/      the kernel and the MCP server   (see docs/ARCHITECTURE.md)
skills/         eight SKILL.md files
hosts/          Claude, Codex and opencode wiring, plus AGENTS.md
docs/           ARCHITECTURE.md, RISK.md, TOOLS.md
tests/          the offline suite
config.example.json
```

MIT. Built by [LoveThisGameNot1](https://github.com/LoveThisGameNot1).

