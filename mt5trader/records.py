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


def _field_names(record):
    """Field names of a structseq, a namedtuple, or a numpy structured scalar.

    The numpy case is the one that bites. When numpy is installed, the
    MetaTrader5 package returns ``copy_rates_*`` and ``copy_ticks_*`` as a numpy
    structured array, and one row of that is a ``numpy.void``: it keeps its field
    names in ``dtype.names``, is not a tuple, has no ``_fields``, does not
    advertise the fields in ``dir()``, and ``getattr(row, "close")`` returns
    nothing. Every route a naive implementation would take misses, and the bar
    silently arrives with no close price.
    """
    names = getattr(type(record), "_fields", None) or getattr(record, "_fields", None)
    if names:
        return tuple(names)
    names = getattr(getattr(record, "dtype", None), "names", None)
    if names and not _is_array(record):
        return tuple(names)
    return None


def _is_array(record):
    """True for a numpy array, false for a numpy structured scalar (shape ())."""
    shape = getattr(record, "shape", None)
    return bool(shape)


def _field(record, name, default=None):
    names = getattr(getattr(record, "dtype", None), "names", None)
    if names and name in names and not _is_array(record):
        return record[name]
    return getattr(record, name, default)


def get(record, name, default=None):
    """Read a field from a structseq, a dict or a numpy structured scalar, and
    never raise."""
    if record is None:
        return default
    if isinstance(record, dict):
        value = record.get(name, default)
    else:
        value = _field(record, name, default)
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
        fields = _field_names(record)
        if fields:
            source = {name: _field(record, name, None) for name in fields}
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
    # Records before anything sequence-like: an MT5 structseq, a namedtuple and a
    # numpy structured scalar are all sequence-like, so a sequence check ahead of
    # this one flattens a whole position, or a whole bar, into a bare list of
    # values with the meaning stripped off. A numpy structured *array* is not a
    # record and deliberately falls through to the sequence branch.
    if _field_names(value):
        return as_dict(value)
    if isinstance(value, datetime.datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, decimal.Decimal):
        return float(value)
    # A numpy integer is not a Python int, so without this it serialises as the
    # string "<numpy.int64 object at 0x...>" and every volume and timestamp
    # arrives at the model as text.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return to_jsonable(item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if hasattr(value, "__iter__"):
        # A numpy structured array is not a list, but it is a sequence of records,
        # and str() on it produces ASCII-art columns that parse back as nothing.
        return [to_jsonable(v) for v in value]
    return str(value)


def naive_utc_now():
    """The current UTC time as a naive datetime.

    ``datetime.utcnow()`` is deprecated and slated for removal. The functional
    reason to care is not tidiness: this server writes a JSON-RPC stream on stdout,
    and a DeprecationWarning printed there by the interpreter is a corrupted
    protocol frame as far as the client is concerned.
    """
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def naive_utc_from_epoch(epoch):
    """Epoch seconds as a naive UTC datetime.

    ``datetime.utcfromtimestamp`` is deprecated for the same reason. MT5 epochs
    already carry the server's wall clock, so this labels them UTC without shifting
    them, which is what the terminal's own windows show.
    """
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).replace(tzinfo=None)


def iso_time(epoch):
    """MT5 timestamps are epoch seconds that already carry the server's wall
    clock. Rendering them with UTC labelling reproduces exactly what the
    terminal shows in its own History and Trade tabs, which is what a human
    comparing the two will expect."""
    if epoch is None:
        return None
    try:
        return naive_utc_from_epoch(int(epoch)).isoformat() + "Z"
    except (OverflowError, OSError, ValueError):
        return None


def dumps(payload, indent=2):
    return json.dumps(to_jsonable(payload), indent=indent, sort_keys=False, ensure_ascii=False)
