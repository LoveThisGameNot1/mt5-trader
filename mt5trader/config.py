"""Paths, configuration, and the hard ceilings.

Two layers, and the difference matters:

* ``HARD_CEILINGS`` are compiled into this file. A config file, a tool call, a
  prompt, or an edited file on disk can only ever make a limit *tighter*. Every
  value is clamped on load, and a clamp is reported, never silent.
* The config file supplies the working values. Absent a config, the defaults
  below are deliberately conservative.

Autonomy is not part of the config file on purpose. It comes from the process
environment, because the process environment is set by the human who launched
the server and cannot be changed by the model at runtime:

    MT5_AUTONOMY=propose   (default) read and plan only, no order tool at all
    MT5_AUTONOMY=auto      orders allowed, each one needs confirm=true and a
                           matching plan fingerprint
    MT5_AUTONOMY=full      orders allowed on a passing risk check alone

On top of that, any account whose trade mode is REAL is treated as read-only
unless the process was started with ``MT5_ALLOW_LIVE=1``.
"""

import copy
import json
import os
from pathlib import Path

# --------------------------------------------------------------------------
# The absolute maximums. Config may lower these. Nothing may raise them.
# --------------------------------------------------------------------------
HARD_CEILINGS = {
    "risk_per_trade_pct": 2.0,
    "risk_per_trade_usd": 2000.0,
    "total_risk_pct": 6.0,
    "daily_loss_pct": 5.0,
    "daily_loss_usd": 5000.0,
    "max_drawdown_pct": 20.0,
    "max_open_positions": 20,
    "orders_per_day": 50,
    "orders_per_minute": 6,
    "max_lots_per_order": 10.0,
    "max_spread_points": 200,
    "max_slippage_points": 50,
    "max_quote_age_seconds": 3600,
    "correlation_limit": 0.99,
    "calendar_max_age_seconds": 3600,
    "loss_cooldown_seconds": 900,
    "duplicate_window_seconds": 300,
}

DEFAULT_CONFIG = {
    "magic": 770001,
    "symbols": [],
    "symbol_suffix": "",
    "symbol_aliases": {},
    "risk": {
        "risk_per_trade_pct": 0.5,
        "risk_per_trade_usd": None,
        "total_risk_pct": 2.0,
        "daily_loss_pct": 2.0,
        "daily_loss_usd": None,
        "max_drawdown_pct": 10.0,
        "max_open_positions": 5,
        "orders_per_day": 10,
        "orders_per_minute": 3,
        "max_lots_per_order": 2.0,
        "loss_cooldown_seconds": 300,
        "duplicate_window_seconds": 120,
    },
    "execution": {
        "deviation_points": 15,
        "max_spread_points": 30,
        "max_slippage_points": 20,
        "filling": "auto",
        "retries": 2,
        "requote_pause_ms": 250,
        "pending_expiry_minutes": 1440,
        "close_retries": 3,
    },
    "guards": {
        "require_sl": True,
        "require_tp": False,
        "min_reward_risk": 1.0,
        "min_stop_distance_points": 0,
        "max_quote_age_seconds": 900,
        "correlation_limit": 0.8,
        "correlation_lookback_bars": 120,
        "correlation_min_points": 20,
        "correlation_action": "block",
        "calendar": "warn",
        "calendar_min_importance": "high",
        "calendar_before_minutes": 15,
        "calendar_after_minutes": 15,
        "calendar_max_age_seconds": 1800,
        "blocked_symbols": [],
        "allowed_sides": ["buy", "sell"],
        "blocked_server_hours": [],
        "news_blackout": [],
        "max_positions_per_symbol": 1,
    },
    "journal": {"dir": "journal"},
}

AUTONOMY_PROMPT = "propose"
AUTONOMY_ORDER = {"propose": 0, "auto": 1, "full": 2}


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _number(value, fallback=None):
    if value is None or value == "":
        return fallback
    try:
        f = float(value)
    except (TypeError, ValueError):
        return fallback
    return f


def _deep_merge(base, override):
    """Merge *override* into *base*, leaving *base* untouched.

    The copy has to be deep, and that is not a detail. ``dict(base)`` is shallow, so
    ``out["risk"]`` would still be the very dict inside ``DEFAULT_CONFIG``, and
    ``_clamp_all`` writes into it. One Settings object would then permanently change
    the defaults for every Settings created afterwards in the same process, and the
    visible symptom would be limits that are right the first time and wrong the
    second.
    """
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        elif value is not None:
            out[key] = copy.deepcopy(value)
    return out


def _clamp(value, ceiling, name, notes):
    """Never let a configured limit sit above its hard ceiling."""
    if value is None:
        return None
    if ceiling is None:
        return value
    if float(value) > float(ceiling):
        notes.append(
            "config %s=%s clamped to the hard ceiling %s" % (name, value, ceiling)
        )
        return ceiling
    return value


def find_root(start=None):
    """The project directory: first of MT5_ROOT, a config.json walking up from
    *start* (default cwd), or the cwd."""
    env = os.environ.get("MT5_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(start or os.getcwd()).resolve()
    for candidate in [here] + list(here.parents):
        if (candidate / "config.json").is_file():
            return candidate
    return here


class Settings:
    """Resolved configuration plus the environment switches that gate trading."""

    def __init__(self, root=None, config_path=None, environ=None):
        self.env = dict(os.environ if environ is None else environ)
        self.notes = []
        self.root = Path(root or find_root()).resolve()
        self.config_path = Path(config_path) if config_path else self.root / "config.json"
        raw = {}
        if self.config_path.is_file():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("%s is not valid JSON: %s" % (self.config_path, exc))
        elif config_path:
            raise FileNotFoundError("no config at %s" % self.config_path)
        self.raw = raw
        self.config = _deep_merge(DEFAULT_CONFIG, raw)

        state_env = self.env.get("MT5_STATE_DIR", "").strip()
        self.state_dir = Path(state_env).expanduser() if state_env else self.root / "state"
        journal = self.config.get("journal", {}).get("dir") or "journal"
        self.journal_dir = Path(journal)
        if not self.journal_dir.is_absolute():
            self.journal_dir = self.root / self.journal_dir

        self.magic = int(_number(self.env.get("MT5_MAGIC"), self.config.get("magic", 770001)))
        self.symbol_suffix = str(self.env.get("MT5_SYMBOL_SUFFIX", "") or
                                 self.config.get("symbol_suffix", "") or "")
        self.autonomy = self._autonomy()
        self.allow_live = _truthy(self.env.get("MT5_ALLOW_LIVE"))
        self.allow_flatten = _truthy(self.env.get("MT5_ALLOW_FLATTEN"))
        self.allow_risk_edit = _truthy(self.env.get("MT5_ALLOW_RISK_EDIT"))
        self.allow_kill_release = _truthy(self.env.get("MT5_ALLOW_KILL_RELEASE"))
        self.terminal_path = self.env.get("MT5_TERMINAL_PATH") or None
        self.login = _number(self.env.get("MT5_LOGIN"))
        self.password = self.env.get("MT5_PASSWORD") or None
        self.server = self.env.get("MT5_SERVER") or None
        self.portable = _truthy(self.env.get("MT5_PORTABLE"))
        self._clamp_all()

    # -- environment -------------------------------------------------------
    def _autonomy(self):
        value = str(self.env.get("MT5_AUTONOMY", AUTONOMY_PROMPT) or "").strip().lower()
        if value not in AUTONOMY_ORDER:
            self.notes.append(
                "MT5_AUTONOMY=%r is not one of propose/auto/full; using propose" % value
            )
            return AUTONOMY_PROMPT
        return value

    def at_least(self, level):
        return AUTONOMY_ORDER[self.autonomy] >= AUTONOMY_ORDER[level]

    # -- clamping ----------------------------------------------------------
    def _clamp_all(self):
        risk = self.config["risk"]
        for key, ceiling in HARD_CEILINGS.items():
            if key in risk:
                risk[key] = _clamp(risk[key], ceiling, "risk." + key, self.notes)
        guards = self.config["guards"]
        for key, ceiling in HARD_CEILINGS.items():
            if key in guards:
                guards[key] = _clamp(guards[key], ceiling, "guards." + key, self.notes)
        ex = self.config["execution"]
        for key in ("max_spread_points", "max_slippage_points"):
            ex[key] = _clamp(ex[key], HARD_CEILINGS[key], "execution." + key, self.notes)
        if int(_number(risk.get("orders_per_minute"), 3) or 3) < 1:
            risk["orders_per_minute"] = 1
            self.notes.append("risk.orders_per_minute raised to 1: a zero rate would "
                              "lock the kernel out of its own rate limiter")

    # -- convenience -------------------------------------------------------
    @property
    def risk(self):
        return self.config["risk"]

    @property
    def execution(self):
        return self.config["execution"]

    @property
    def guards(self):
        return self.config["guards"]

    def symbol(self, name):
        """Resolve a user-facing symbol to the broker's full name.

        Brokers append suffixes (EURUSD.pro, XAUUSDm, US30.cash). The kernel
        resolves through MT5 rather than guessing, but a config alias wins.
        """
        name = str(name or "").strip()
        alias = self.config.get("symbol_aliases", {}).get(name)
        if alias:
            return alias
        if self.symbol_suffix and not name.endswith(self.symbol_suffix):
            return name + self.symbol_suffix
        return name

    def ensure_dirs(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "intents").mkdir(exist_ok=True)
        (self.state_dir / "plans").mkdir(exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def summary(self):
        return {
            "root": str(self.root),
            "config": str(self.config_path),
            "state_dir": str(self.state_dir),
            "magic": self.magic,
            "autonomy": self.autonomy,
            "allow_live": self.allow_live,
            "allow_flatten": self.allow_flatten,
            "allow_risk_edit": self.allow_risk_edit,
            "terminal_path": self.terminal_path,
            "symbol_suffix": self.symbol_suffix,
            "notes": list(self.notes),
            "hard_ceilings": HARD_CEILINGS,
        }


def load_settings(root=None, config_path=None, environ=None):
    return Settings(root=root, config_path=config_path, environ=environ)
