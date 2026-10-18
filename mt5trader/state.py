"""Atomic state, the intent log, and the append-only event journal.

Three kinds of thing are persisted, and they have different requirements:

``state.json``      counters that must survive a restart: the broker day
                    anchor, orders sent today, the per-minute rate window, the
                    loss cooldown, and the kill switch. Losing this file is the
                    difference between a daily loss limit that holds and one
                    that resets itself every time the server is restarted.
``plans/<id>.json`` a validated trade plan, with a fingerprint over the exact
                    normalised order. The executor accepts only a plan whose
                    fingerprint still matches, so an edit between plan and
                    execution invalidates the plan instead of sneaking through.
``intents/<id>.json`` one record per order attempt. Written *before* the
                    request goes out, so an ambiguous result (a timeout) can
                    always be reconciled afterwards against the deal history.

Writes are atomic: a temporary file in the same directory, then os.replace.
On Windows os.replace over an existing file is atomic, so a crash mid-write
leaves either the old file or the new one, never a truncated one.
"""

import json
import os
import threading
import uuid
from pathlib import Path

from . import records

EVENTS = "events.jsonl"
STATE = "state.json"


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(records.to_jsonable(payload), handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def new_id(prefix):
    return "%s-%s-%s" % (
        prefix,
        records.naive_utc_now().strftime("%Y%m%d-%H%M%S"),
        uuid.uuid4().hex[:6],
    )


BLANK_STATE = {
    "day": None,
    "day_start_equity": None,
    "day_start_balance": None,
    "peak_equity": None,
    "orders_today": 0,
    "order_times": [],
    "last_order_ts": None,
    "last_loss_ts": None,
    "loss_streak": 0,
    "consecutive_blocks": 0,
    "kill_switch": False,
    "kill_reason": None,
    "kill_since": None,
    "tripped_days": [],
}


class Store:
    """All persistence for one project directory. One instance per process."""

    def __init__(self, state_dir, journal_dir=None):
        self.state_dir = Path(state_dir)
        self.journal_dir = Path(journal_dir) if journal_dir else self.state_dir
        self._lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "intents").mkdir(exist_ok=True)
        (self.state_dir / "plans").mkdir(exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    # -- state -------------------------------------------------------------
    def load(self):
        data = read_json(self.state_dir / STATE, None)
        if not isinstance(data, dict):
            return dict(BLANK_STATE)
        merged = dict(BLANK_STATE)
        merged.update(data)
        return merged

    def save(self, state):
        with self._lock:
            return atomic_write_json(self.state_dir / STATE, state)

    def mutate(self, fn):
        """Read, hand to *fn*, write back, under one lock."""
        with self._lock:
            state = self.load()
            result = fn(state)
            self.save(state)
            return result

    # -- events ------------------------------------------------------------
    def event(self, kind, payload=None, **extra):
        """Append one line to the journal.

        Timestamps are UTC and carry a Z, like every other timestamp this project
        writes. A bare local time in an audit trail is ambiguous, and the one thing
        an audit trail may not be is ambiguous.
        """
        record = {
            "ts": records.naive_utc_now().replace(microsecond=0).isoformat() + "Z",
            "kind": kind,
        }
        if payload:
            record.update(records.to_jsonable(payload))
        if extra:
            record.update(records.to_jsonable(extra))
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self.journal_dir.mkdir(parents=True, exist_ok=True)
            with open(self.journal_dir / EVENTS, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record

    def events(self, limit=200, kind=None):
        path = self.journal_dir / EVENTS
        if not path.is_file():
            return []
        out = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if kind and record.get("kind") != kind:
                    continue
                out.append(record)
        return out[-limit:]

    # -- plans -------------------------------------------------------------
    def plan_path(self, plan_id):
        return self.state_dir / "plans" / ("%s.json" % plan_id)

    def save_plan(self, plan):
        return atomic_write_json(self.plan_path(plan["plan_id"]), plan)

    def load_plan(self, plan_id):
        return read_json(self.plan_path(plan_id), None)

    def list_plans(self, limit=50):
        folder = self.state_dir / "plans"
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for path in files[:limit]:
            record = read_json(path, None)
            if record:
                out.append(record)
        return out

    # -- intents -----------------------------------------------------------
    def intent_path(self, intent_id):
        return self.state_dir / "intents" / ("%s.json" % intent_id)

    def save_intent(self, intent):
        return atomic_write_json(self.intent_path(intent["intent_id"]), intent)

    def load_intent(self, intent_id):
        return read_json(self.intent_path(intent_id), None)

    def update_intent(self, intent_id, **fields):
        with self._lock:
            intent = self.load_intent(intent_id) or {"intent_id": intent_id}
            intent.update(records.to_jsonable(fields))
            self.save_intent(intent)
            return intent

    def unresolved_intents(self, limit=100):
        """Intents whose outcome MT5 never confirmed. These block new orders
        until they are reconciled, because the position may already exist."""
        folder = self.state_dir / "intents"
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for path in files[:limit]:
            record = read_json(path, None)
            if record and record.get("status") in ("sent", "ambiguous", "unknown"):
                out.append(record)
        return out
