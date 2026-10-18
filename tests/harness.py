"""Test harness: a kernel wired to the fake terminal, in a temp directory.

Every test gets its own project root, so state, plans, intents and the journal
cannot leak between them, and the environment is passed explicitly rather than
inherited, so a developer's own MT5_* variables cannot make a test pass.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader.bridge import Mt5Bridge          # noqa: E402
from mt5trader.config import Settings           # noqa: E402
from mt5trader.fixtures import FakeMt5          # noqa: E402
from mt5trader.risk import RiskKernel           # noqa: E402
from mt5trader.state import Store               # noqa: E402

AUTONOMOUS = {"MT5_AUTONOMY": "full"}
CONFIRM = {"MT5_AUTONOMY": "auto"}
PROPOSE = {}


class Harness:
    def __init__(self, root, settings, fake, bridge, store, kernel):
        self.root = root
        self.settings = settings
        self.fake = fake
        self.bridge = bridge
        self.store = store
        self.kernel = kernel

    def reconnect(self):
        """Drop the clock cache so a changed tick time is picked up."""
        self.kernel._clock = None


def build(root, env=None, symbols=None, account=None, terminal=None, positions=None,
          orders=None, deals=None, config=None):
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if config is not None:
        import json
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    from mt5trader import market
    market.clear_cache()   # bar series are cached per process; tests must not share
    settings = Settings(root=str(root), config_path=None, environ=dict(env or {}))
    fake = FakeMt5(symbols=symbols, account=account, terminal=terminal,
                   positions=positions, orders=orders, deals=deals)
    if symbols is None:
        fake.symbols = {s["name"]: s for s in _default_symbols()}
    bridge = Mt5Bridge(settings, module=fake)
    store = Store(settings.state_dir, settings.journal_dir)
    kernel = RiskKernel(settings, store, bridge)
    return Harness(root, settings, fake, bridge, store, kernel)


def _default_symbols():
    from mt5trader.fixtures import (btcusd, eurusd, eurusd_wide_stops, fractional_step,
                                    freeze_only, one_lot_minimum, testm, us30, xauusd)
    return [eurusd(), xauusd(), us30(), btcusd(), testm(), one_lot_minimum(),
            eurusd_wide_stops(), fractional_step(), freeze_only()]


def clean_plan(**overrides):
    plan = {
        "symbol": "EURUSD",
        "side": "buy",
        "risk_percent": 0.5,
        "stop": 1.08412,
    }
    plan.update(overrides)
    return plan
