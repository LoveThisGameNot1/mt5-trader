"""Managing what is already open: stops, partial closes, trailing, flattening.

Two rules run through the whole module.

**Only our own positions are touched.** Every function that changes something
refuses a ticket whose ``magic`` is not the kernel's, unless it is explicitly
asked to manage foreign positions. A second EA, a manual trade or a hedge left
over from last week is not something an autonomous loop may close by accident.

**Every change is verified by re-reading the position.** MT5 returns
TRADE_RETCODE_DONE for a request it accepted; that is not the same as the
position now looking the way it was asked to. A stop that silently failed to move
is the most dangerous state in trading, so the answer of every mutation is the
state afterwards, not the return code.

And the detail that catches people on the modify path: MT5's
``TRADE_ACTION_SLTP`` is absolute, not a patch. Omitting the take profit does not
leave it alone, it clears it. So a modify reads the position first and fills in
whatever the caller did not specify.
"""

import time

from . import errors, records
from .instruments import InstrumentError

ACTION_DEAL = 1
ACTION_SLTP = 6
ACTION_REMOVE = 8
ACTION_CLOSE_BY = 10

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

MARGIN_MODE_NETTING = 0
MARGIN_MODE_HEDGING = 2


class PositionError(RuntimeError):
    pass


def positions(bridge, settings=None, symbol=None, magic_only=True):
    raw = bridge.call("positions_get")
    out = []
    for position in (raw or ()):
        if magic_only and settings is not None and \
                records.inum(records.get(position, "magic"), 0) != settings.magic:
            continue
        if symbol and str(records.get(position, "symbol")) != str(symbol):
            continue
        out.append(position)
    return out


def find(bridge, ticket):
    found = bridge.call("positions_get", ticket=int(ticket))
    if not found:
        return None
    return found[0]


def require(bridge, settings, ticket, allow_foreign=False):
    position = find(bridge, ticket)
    if position is None:
        raise PositionError("no open position with ticket %s. It may already be closed."
                            % ticket)
    if not allow_foreign and records.inum(records.get(position, "magic"), 0) != settings.magic:
        raise PositionError(
            "ticket %s carries magic %s, not this kernel's %s. It belongs to another "
            "program or to a manual trade, and this kernel does not touch it."
            % (ticket, records.get(position, "magic"), settings.magic))
    return position


def side_of(position):
    return "buy" if records.inum(records.get(position, "type"), -1) == POSITION_TYPE_BUY else "sell"


def describe(position, instrument=None):
    volume = records.fnum(records.get(position, "volume"), 0.0)
    entry = records.fnum(records.get(position, "price_open"), None)
    stop = records.fnum(records.get(position, "sl"), 0.0)
    out = {
        "ticket": records.inum(records.get(position, "ticket"), None),
        "symbol": records.get(position, "symbol"),
        "side": side_of(position),
        "volume": volume,
        "price_open": entry,
        "price_current": records.fnum(records.get(position, "price_current"), None),
        "sl": stop or None,
        "tp": records.fnum(records.get(position, "tp"), 0.0) or None,
        "profit": round(records.fnum(records.get(position, "profit"), 0.0) or 0.0, 2),
        "swap": round(records.fnum(records.get(position, "swap"), 0.0) or 0.0, 2),
        "magic": records.inum(records.get(position, "magic"), None),
        "comment": records.get(position, "comment"),
        "opened": records.iso_time(records.inum(records.get(position, "time"), None)),
        "reason": records.inum(records.get(position, "reason"), None),
    }
    if instrument is not None and entry is not None:
        current = out["price_current"]
        if stop:
            out["risk_money"] = round(instrument.risk_money(volume, entry, stop) or 0.0, 2)
            out["risk_now"] = round(instrument.risk_money(volume, current, stop) or 0.0, 2)
        if stop and not out["sl"]:
            out["stop_missing"] = True
        if stop and current:
            risk = abs(entry - stop)
            if risk > 0:
                move = (current - entry) if out["side"] == "buy" else (entry - current)
                out["r_now"] = round(move / risk, 3)
    return out


# --------------------------------------------------------------------------
# mutate
# --------------------------------------------------------------------------
def _send_and_verify(bridge, request, verify, attempts=3, pause=0.25):
    """Send, then prove the change landed by reading the state back."""
    tries = []
    for attempt in range(1, attempts + 1):
        result = bridge.call("order_send", request)
        summary = {
            "attempt": attempt,
            "retcode": records.inum(records.get(result, "retcode"), None),
            "comment": records.get(result, "comment"),
            "request": dict(request),
        }
        summary.update(errors.explain(summary["retcode"]))
        tries.append(summary)
        if summary["kind"] == errors.AMBIGUOUS:
            state = verify()
            return {"status": "ambiguous", "attempts": tries, "state": state,
                    "message": "the terminal never confirmed the change; the state "
                               "shown may or may not include it"}
        if summary["kind"] == errors.RETRY and attempt < attempts:
            if summary["retcode"] == 10020 or summary["retcode"] == 10004:
                pass  # price moved; the request is rebuilt by the caller's verify path
            time.sleep(pause)
            continue
        if summary["kind"] != errors.OK:
            return {"status": "rejected", "attempts": tries, "state": verify(),
                    "message": "rejected: %s, %s" % (summary["name"], summary["comment"])}
        state = verify()
        return {"status": "done", "attempts": tries, "state": state,
                "message": "done: %s" % summary["name"]}
    return {"status": "failed", "attempts": tries, "state": verify(),
            "message": "gave up after %d attempts" % attempts}


def modify(bridge, settings, ticket, sl=None, tp=None, instrument=None,
           allow_foreign=False, clear_sl=False, clear_tp=False):
    """Move a stop and/or a target.

    The request is absolute, so anything not specified is carried over from the
    position as it is now, and clearing has to be asked for explicitly with
    clear_sl / clear_tp. Sending a bare modify with one field omitted is how a
    take profit disappears without anyone asking for it.
    """
    position = require(bridge, settings, ticket, allow_foreign=allow_foreign)
    symbol = str(records.get(position, "symbol"))
    if instrument is None:
        from . import instruments
        instrument, _ = instruments.load(bridge, settings, symbol)
    side = side_of(position)
    current = instrument.exit_price(side)

    existing_sl = records.fnum(records.get(position, "sl"), 0.0) or None
    existing_tp = records.fnum(records.get(position, "tp"), 0.0) or None
    want_sl = sl is not None or clear_sl
    want_tp = tp is not None or clear_tp
    new_sl = None if clear_sl else existing_sl
    new_tp = None if clear_tp else existing_tp

    notes = []
    if want_sl or want_tp:
        entry_reference = records.fnum(records.get(position, "price_open"), None)
        try:
            # Only what was asked for is validated. An existing take profit that the
            # market has already run past is perfectly legal to keep, and refusing to
            # move a stop because of it would block every trailing stop that worked.
            valid_sl, valid_tp, stop_notes = instrument.normalize_stops(
                side, entry_reference if entry_reference else current,
                None if clear_sl else sl, None if clear_tp else tp, current=current)
        except InstrumentError as exc:
            return {"status": "refused", "message": str(exc),
                    "position": describe(position, instrument)}
        notes.extend(stop_notes)
        if want_sl:
            new_sl = None if clear_sl else valid_sl
        if want_tp:
            new_tp = None if clear_tp else valid_tp
    freeze = instrument.check_freeze(new_sl if want_sl else None,
                                     new_tp if want_tp else None, current)
    if freeze:
        return {"status": "refused", "message": freeze, "position": describe(position, instrument)}

    request = {
        "action": ACTION_SLTP,
        "symbol": symbol,
        "position": int(ticket),
        "sl": instrument.normalize_price(new_sl) if new_sl else 0.0,
        "tp": instrument.normalize_price(new_tp) if new_tp else 0.0,
    }

    def verify():
        after = find(bridge, ticket)
        return describe(after, instrument) if after else None

    out = _send_and_verify(bridge, request, verify)
    out["position"] = out.pop("state", None)
    out["requested"] = {"sl": new_sl, "tp": new_tp}
    out["notes"] = notes
    if out["status"] == "done" and out["position"]:
        landed_sl = out["position"].get("sl")
        landed_tp = out["position"].get("tp")
        expected_sl = instrument.normalize_price(new_sl) if new_sl else None
        expected_tp = instrument.normalize_price(new_tp) if new_tp else None
        if (landed_sl or None) != expected_sl or (landed_tp or None) != expected_tp:
            out["status"] = "drifted"
            out["message"] = ("the broker accepted the change but the position reads "
                              "back differently (sl %s vs %s, tp %s vs %s). Check the "
                              "broker's own stop level before trusting this trade."
                              % (landed_sl, expected_sl, landed_tp, expected_tp))
    return out


def break_even(bridge, settings, ticket, offset_points=0, instrument=None,
               allow_foreign=False, require_profit=True):
    """Move the stop to the entry price, plus an offset in points.

    ``require_profit`` refuses to do it while the trade is underwater, because a
    break-even stop on a losing position is just a closer stop loss wearing a
    nicer name.
    """
    position = require(bridge, settings, ticket, allow_foreign=allow_foreign)
    if instrument is None:
        from . import instruments
        instrument, _ = instruments.load(bridge, settings, str(records.get(position, "symbol")))
    side = side_of(position)
    entry = records.fnum(records.get(position, "price_open"), None)
    current = instrument.exit_price(side)
    if entry is None or current is None:
        return {"status": "refused", "message": "no entry price or no quote to work from"}
    if require_profit:
        in_profit = (current > entry) if side == "buy" else (current < entry)
        if not in_profit:
            return {"status": "refused",
                    "message": "the position is not in profit; break-even would only "
                               "move the stop loss closer, not protect anything",
                    "position": describe(position, instrument)}
    offset = instrument.points_to_price(offset_points)
    target = entry + offset if side == "buy" else entry - offset
    target = instrument.normalize_price(target)
    floor = instrument.min_stop_distance
    if abs(current - target) < floor:
        return {"status": "refused",
                "message": "the entry is %.5f from the current price and the broker needs "
                           "%.5f before a stop may sit there" % (abs(current - target), floor),
                "position": describe(position, instrument)}
    return modify(bridge, settings, ticket, sl=target, instrument=instrument,
                  allow_foreign=allow_foreign)


def trail(bridge, settings, ticket, distance_points, step_points=0, instrument=None,
          allow_foreign=False):
    """One trailing step: move the stop to ``distance_points`` behind the price,
    never backwards, and only if it improves by at least ``step_points``.

    Deliberately one step, not a loop. The caller decides how often to call it,
    which keeps the rate limits in :mod:`mt5trader.risk` meaningful. A trailing
    loop that runs inside a single tool call is a loop the kernel cannot see.
    """
    position = require(bridge, settings, ticket, allow_foreign=allow_foreign)
    if instrument is None:
        from . import instruments
        instrument, _ = instruments.load(bridge, settings, str(records.get(position, "symbol")))
    side = side_of(position)
    current = instrument.exit_price(side)
    if current is None:
        return {"status": "refused", "message": "no live quote for %s" % instrument.symbol}
    distance = instrument.points_to_price(distance_points)
    if distance <= 0:
        return {"status": "refused", "message": "distance_points must be positive"}
    # Round to the broker's precision before comparing, so a candidate that is a
    # floating-point hair better than the current stop is recognised as no change
    # instead of burning a modify request on nothing.
    candidate = instrument.normalize_price(
        current - distance if side == "buy" else current + distance)
    existing = records.fnum(records.get(position, "sl"), 0.0) or None
    if existing is not None:
        improves = (candidate > existing) if side == "buy" else (candidate < existing)
        if not improves:
            return {"status": "no-change",
                    "message": "the trailing stop at %s is not better than the current %s"
                               % (instrument.normalize_price(candidate), existing),
                    "position": describe(position, instrument)}
        if step_points and abs(candidate - existing) < instrument.points_to_price(step_points):
            return {"status": "no-change",
                    "message": "the improvement is under the %s point step" % step_points,
                    "position": describe(position, instrument)}
    return modify(bridge, settings, ticket, sl=candidate, instrument=instrument,
                  allow_foreign=allow_foreign)


def close(bridge, settings, ticket, volume=None, comment="mt5trader close",
          instrument=None, allow_foreign=False, deviation=None):
    """Close a position, in full or in part, with an opposite market order.

    The opposite deal carries ``position=ticket``, which is what makes it a close
    rather than a hedge: on a netting account it reduces the single position, on
    a hedging account it targets exactly this ticket.

    A partial close that would leave a remainder below the symbol's minimum
    volume is refused by the broker, so the remainder is closed in full instead
    and the caller is told that is what happened.
    """
    position = require(bridge, settings, ticket, allow_foreign=allow_foreign)
    symbol = str(records.get(position, "symbol"))
    if instrument is None:
        from . import instruments
        instrument, _ = instruments.load(bridge, settings, symbol)
    side = side_of(position)
    held = records.fnum(records.get(position, "volume"), 0.0) or 0.0
    if held <= 0:
        raise PositionError("position %s reports no volume" % ticket)

    requested = records.fnum(volume, None)
    notes = []
    if requested is None or requested >= held:
        amount = held
    else:
        amount = instrument.normalize_volume(requested)
        if amount >= held:
            amount = held
        remainder = held - amount
        if remainder < instrument.volume_min:
            notes.append(
                "a partial close of %.4f would leave %.4f, under the minimum %.4f, so "
                "the whole %.4f was closed instead"
                % (amount, remainder, instrument.volume_min, held))
            amount = held

    freeze = None
    if instrument.trade_freeze_level:
        current = instrument.exit_price(side)
        stop = records.fnum(records.get(position, "tp"), 0.0) or None
        loss = records.fnum(records.get(position, "sl"), 0.0) or None
        freeze = instrument.check_freeze(loss, stop, current)

    opposite = "sell" if side == "buy" else "buy"
    request = {
        "action": ACTION_DEAL,
        "symbol": symbol,
        "volume": float(amount),
        "type": POSITION_TYPE_SELL if side == "buy" else POSITION_TYPE_BUY,
        "position": int(ticket),
        "price": instrument.normalize_price(instrument.exit_price(side)),
        "deviation": int(deviation or settings.execution.get("deviation_points") or 15),
        "magic": settings.magic,
        "comment": str(comment)[:31],
        "type_filling": instrument.filling_candidates("auto")[0],
    }

    def verify():
        after = find(bridge, ticket)
        return describe(after, instrument) if after else {"closed": True}

    out = _send_and_verify(bridge, request, verify,
                           attempts=max(1, int(settings.execution.get("close_retries", 3))))
    out["closed_volume"] = amount
    out["remaining_volume"] = round(held - amount, 4)
    out["position"] = out.pop("state", None)
    out["notes"] = notes
    if freeze:
        out["freeze_note"] = freeze
    if out["status"] == "done" and out["position"] and not out["position"].get("closed"):
        left = records.fnum(out["position"].get("volume"), None)
        if left is not None and left >= held - 1e-8:
            out["status"] = "not_closed"
            out["message"] = ("the broker accepted the close but the position still shows "
                              "%.4f lots. Send the close again." % left)
        else:
            out["message"] = "closed %.4f of %.4f lots" % (amount, held)
    return out


def close_all(bridge, settings, symbol=None, side=None, scope="ours", comment="mt5trader flatten"):
    """Flatten. ``scope`` is ``ours`` (this magic only) or ``all`` (everything on
    the symbol, which needs the operator's MT5_ALLOW_FLATTEN switch)."""
    if scope not in ("ours", "all"):
        raise PositionError("scope must be 'ours' or 'all'")
    if scope == "all" and not settings.allow_flatten:
        raise PermissionError(
            "closing positions that this kernel does not own needs MT5_ALLOW_FLATTEN=1 "
            "in the environment of the server process.")
    targets = positions(bridge, settings, symbol=symbol, magic_only=(scope == "ours"))
    if side:
        targets = [p for p in targets if side_of(p) == side]
    results = []
    for position in targets:
        ticket = records.inum(records.get(position, "ticket"), None)
        try:
            results.append({"ticket": ticket,
                            "result": close(bridge, settings, ticket, comment=comment,
                                            allow_foreign=(scope == "all"))})
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            results.append({"ticket": ticket, "error": str(exc)})
    remaining = positions(bridge, settings, symbol=symbol, magic_only=(scope == "ours"))
    return {
        "requested": len(targets),
        "closed": sum(1 for r in results if r.get("result", {}).get("status") == "done"),
        "remaining": len(remaining),
        "details": results,
        "note": ("still %d position(s) open; run it again" % len(remaining)
                 if remaining else "flat"),
    }


def cancel_pending(bridge, settings, ticket):
    order = bridge.call("orders_get", ticket=int(ticket))
    if not order:
        raise PositionError("no pending order with ticket %s" % ticket)
    order = order[0]
    request = {"action": ACTION_REMOVE, "order": int(ticket)}
    result = bridge.call("order_send", request)
    retcode = records.inum(records.get(result, "retcode"), None)
    return {
        "ticket": ticket,
        "request": request,
        "result": errors.explain(retcode),
        "comment": records.get(result, "comment"),
        "still_there": bool(bridge.call("orders_get", ticket=int(ticket))),
    }


def account_margin_mode(bridge):
    account = bridge.call("account_info")
    mode = records.inum(records.get(account, "margin_mode"), None)
    return {
        "mode": mode,
        "name": {MARGIN_MODE_NETTING: "netting", MARGIN_MODE_HEDGING: "hedging"}.get(
            mode, "exchange or unknown"),
        "hedging": mode == MARGIN_MODE_HEDGING,
        "fifo_close": bool(records.get(account, "fifo_close", False)),
    }
