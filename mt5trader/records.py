"""Defensive field access for MetaTrader 5 records.

Every MT5 result is a CPython structseq built by the extension module:
``symbol_info``, ``account_info``, ``positions_get`` members, ``TradeDeal`` and
so on. The field sets are not frozen -- MetaQuotes adds fields between builds,
some fields only exist for certain account and instrument types, and a couple
(``trade_tick_value_profit``, ``external_id``, ``type_filling`` on an order)
are simply absent on older terminals.

Touching ``pos.foo`` and hoping is how an otherwise correct trading loop dies
halfway through an order with an AttributeError it cannot roll back. Every read
here goes through :func:`get` with an explicit default, so a missing field
degrades to a known value and the kernel can decide what that means.

These helpers also accept plain dicts, which is what lets the whole package be
tested against fixture metadata with no terminal attached.
"""

import datetime
import decimal
import json
import math


def get(record, name, default=None):
    """Read a field from a structseq or a dict without ever raising."""
    if record is None:
        return default
    if isinstance(record, dict):
        value = record.get(name, default)
    else:
        value = getattr(record, name, default)
    return default if value is None else value


def has(record, name):
    return get(record, name, None) is not None


def fnum(value, default=None):
    """Float or the default. Rejects NaN and infinity, which MT5 does emit."""
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def inum(value, default=None):
    out = fnum(value, None)
    if out is None:
        return default
    try:
        return int(out)
    except (TypeError, ValueError):
        return default


def as_dict(record, names=None):
    """Plain dict of a record. With *names*, only those keys, missing ones
    dropped rather than defaulted to None, so the output schema is stable."""
    if record is None:
        return None
    if isinstance(record, dict):
        source = record
    else:
        fields = getattr(type(record), "_fields", None) or getattr(record, "_fields", None)
        if fields:
            source = {name: getattr(record, name, None) for name in fields}
        else:
            source = {k: getattr(record, k) for k in dir(record) if not k.startswith("_")}
    if names is None:
        return {k: to_jsonable(v) for k, v in source.items()}
    out = {}
    for name in names:
        if name in source and source[name] is not None:
            out[name] = to_jsonable(source[name])
    return out


def to_jsonable(value):
    """Recursively make a value JSON-safe without losing information."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.datetime):
        return value.replace(microsecond=0).isoformat()
    # Records first: an MT5 structseq and a namedtuple are both tuple subclasses,
    # so a tuple check ahead of this one would flatten a whole position into a
    # bare list of values.
    if hasattr(value, "_fields"):
        return as_dict(value)
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return str(value)


def iso_time(epoch):
    """MT5 timestamps are epoch seconds that already carry the server's wall
    clock. Rendering them with UTC labelling reproduces exactly what the
    terminal shows in its own History and Trade tabs, which is what a human
    comparing the two will expect."""
    if epoch is None:
        return None
    try:
        return datetime.datetime.utcfromtimestamp(int(epoch)).isoformat() + "Z"
    except (OverflowError, OSError, ValueError):
        return None


def dumps(payload, indent=2):
    return json.dumps(to_jsonable(payload), indent=indent, sort_keys=False, ensure_ascii=False)
