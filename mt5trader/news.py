"""The economic calendar, read from a file the terminal writes.

```MetaTrader 5`` has a built-in economic calendar, and the Python module has no
access to it: there is no ``calendar_*`` function in the API at all. The only way
to reach it is from MQL5, so a small service in ``mql5/CalendarExport.mq5`` runs
inside the terminal and exports the events to JSON on a timer. This module reads
that file.

Three decisions worth stating:

**The event becomes a window, not an article.** The kernel does not get a headline
to read and interpret; it gets a blackout window and a reason. Free text from the
outside landing in the same context that holds the order tools is a prompt
injection surface, and a monitor that produces sentences is a monitor a model can
talk itself out of.

**A stale file fails closed when enforcement is on.** If the export stops, the
kernel cannot know whether a release is imminent. Under
``guards.calendar = enforce`` a stale or missing file blocks market orders with an
instruction to start the service, rather than silently deciding there is no news.

**Currency matching, not symbol matching.** An event is relevant to EURUSD if its
currency is EUR or USD, and to XAUUSD if it is XAU or USD, which is derived from
the instrument's own ``currency_base`` and ``currency_profit``.
"""

import json
import pathlib

from . import records

FILENAME = "mt5trader_calendar.json"

# The MQL5 service exports should be a plain read of these.
IMPORTANCE_NAMES = {0: "low", 1: "moderate", 2: "high"}

DEFAULT_MIN_IMPORTANCE = "high"
DEFAULT_BEFORE_MINUTES = 15
DEFAULT_AFTER_MINUTES = 15
DEFAULT_MAX_AGE_SECONDS = 1800


def importance_level(value):
    """high / moderate / low, or a digit, to the 2 / 1 / 0 the export uses.

    MetaTrader's own enum has four levels (none, low, moderate, high) and the JSON
    therefore carries both the normalised value and the raw one, so a mismatch
    between the two is visible instead of silent.
    """
    text = str(value if value is not None else DEFAULT_MIN_IMPORTANCE).strip().lower()
    if text.isdigit():
        return max(0, min(2, int(text)))
    return {"high": 2, "moderate": 1, "medium": 1, "low": 0, "none": 0}.get(text, 2)


def importance_name(level):
    try:
        return IMPORTANCE_NAMES.get(int(level), str(level))
    except (TypeError, ValueError):
        return str(level)


def locate(settings, terminal=None):
    """Where the calendar file is, and whether it was found.

    Checked in order: an explicit path in the environment or config, the terminal's
    common data folder (shared between terminals), the terminal's own data folder.
    """
    configured = (settings.env.get("MT5_CALENDAR_FILE")
                  or (settings.config.get("calendar") or {}).get("file"))
    if configured:
        path = pathlib.Path(configured)
        return path, path.is_file(), "configured"
    common = records.get(terminal, "commondata_path")
    if common:
        path = pathlib.Path(common) / "Files" / FILENAME
        if path.is_file():
            return path, True, "terminal common data folder"
    own = records.get(terminal, "data_path")
    if own:
        path = pathlib.Path(own) / "MQL5" / "Files" / FILENAME
        if path.is_file():
            return path, True, "terminal data folder"
    guesses = []
    if own:
        guesses.append(pathlib.Path(own) / "MQL5" / "Files" / FILENAME)
    if common:
        guesses.append(pathlib.Path(common) / "Files" / FILENAME)
    return (guesses[0] if guesses else pathlib.Path(FILENAME)), False, "not found"


def load(path):
    """(data, error). Never raises: a missing or broken file is a state, not a crash."""
    try:
        path = pathlib.Path(path)
    except TypeError:
        return None, "no calendar path"
    if not path.is_file():
        return None, "no calendar file at %s" % path
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, "%s could not be read: %s" % (path, exc)
    if not isinstance(raw, dict) or "events" not in raw:
        return None, "%s does not look like a calendar export" % path
    return raw, None


def freshness(data, now_epoch=None):
    """How old the export is. The heartbeat is the export time, not the last
    event in it, because a quiet calendar still has to prove it is being read."""
    import time
    now_epoch = now_epoch or time.time()
    generated = records.inum(records.get(data, "generated_server"), None)
    if generated is None:
        return {"age_seconds": None, "stale": True,
                "reason": "the export carries no generation timestamp"}
    age = int(now_epoch - generated)
    return {"age_seconds": age, "generated_server": generated, "stale": False,
            "note": "age relative to the server clock"}


def normalise(data):
    """Events as a clean list, high importance first, soonest first."""
    out = []
    for event in records.get(data, "events", []) or []:
        if not isinstance(event, dict):
            continue
        when = records.inum(event.get("time"), None)
        if when is None:
            continue
        importance = records.inum(event.get("importance"), 0)
        out.append({
            "time": when,
            "time_iso": records.iso_time(when),
            "name": str(event.get("name") or event.get("code") or "unnamed event"),
            "currency": str(event.get("currency") or "").upper(),
            "country": str(event.get("country") or "").upper(),
            "importance": importance,
            "importance_name": IMPORTANCE_NAMES.get(importance, str(importance)),
            "actual": records.fnum(event.get("actual"), None),
            "forecast": records.fnum(event.get("forecast"), None),
            "previous": records.fnum(event.get("previous"), None),
            "unit": event.get("unit"),
            "period": event.get("period"),
        })
    out.sort(key=lambda e: (e["time"], -e["importance"]))
    return out


def within(events, now_epoch, hours_ahead=48, min_importance=2, currencies=None):
    horizon = now_epoch + int(hours_ahead) * 3600
    wanted = {c.upper() for c in (currencies or [])}
    out = []
    for event in events:
        if event["time"] < now_epoch - 3600 or event["time"] > horizon:
            continue
        if event["importance"] < int(min_importance):
            continue
        if wanted and event["currency"] not in wanted:
            continue
        out.append(dict(event, in_hours=round((event["time"] - now_epoch) / 3600.0, 2)))
    return out


def active_blackout(events, now_epoch, before_minutes=DEFAULT_BEFORE_MINUTES,
                    after_minutes=DEFAULT_AFTER_MINUTES, min_importance=2,
                    currencies=None):
    """The window we are inside right now, or None."""
    wanted = {c.upper() for c in (currencies or [])}
    before, after = int(before_minutes) * 60, int(after_minutes) * 60
    hits = []
    for event in events:
        if event["importance"] < int(min_importance):
            continue
        if wanted and event["currency"] not in wanted:
            continue
        if event["time"] - before <= now_epoch <= event["time"] + after:
            hits.append({
                "name": event["name"],
                "currency": event["currency"],
                "importance": event["importance_name"],
                "time": event["time_iso"],
                "starts_in_seconds": int(event["time"] - now_epoch),
                "window": "%d min before to %d min after" % (before_minutes, after_minutes),
                "actual": event["actual"],
                "forecast": event["forecast"],
                "previous": event["previous"],
            })
    return hits or None


def currencies_for(instrument):
    """The currencies an instrument's price depends on.

    The base and the profit currency, not the margin currency: margin currency is
    about how the position is collateralised, and a news release moves the price,
    not the collateral.
    """
    if instrument is None:
        return set()
    out = set()
    for field in ("currency_base", "currency_profit"):
        value = str(records.get(instrument.info, field) or "").upper().strip()
        if value:
            out.add(value)
    return out


def status(settings, terminal, clock, now_epoch=None):
    """Everything the guard check and the mt5_calendar tool need, in one call."""
    import time
    now_epoch = now_epoch or time.time()
    path, found, source = locate(settings, terminal)
    if not found:
        return {"available": False, "path": str(path), "source": source,
                "reason": ("no calendar export found. The MQL5 service has to be "
                           "running in the terminal: see mql5/CalendarExport.mq5 and "
                           "the mt5-calendar skill.")}
    data, error = load(path)
    if error:
        return {"available": False, "path": str(path), "source": source, "reason": error}
    age = records.inum(records.get(data, "generated_server"), None)
    now_server = (now_epoch + (clock.offset if clock else 0))
    report = {
        "available": True,
        "path": str(path),
        "source": source,
        "terminal_build": records.inum(records.get(data, "terminal_build"), None),
        "export_interval_minutes": records.inum(records.get(data, "interval_minutes"), None),
        "server_now": records.iso_time(int(now_server)),
        "generated_server": records.iso_time(age),
    }
    if age:
        report["age_seconds"] = int(now_server - age)
        report["stale"] = report["age_seconds"] > records.inum(
            settings.guards.get("calendar_max_age_seconds"), DEFAULT_MAX_AGE_SECONDS)
    else:
        report["age_seconds"] = None
        report["stale"] = True
    report["events"] = normalise(data)
    return report
