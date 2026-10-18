"""Building and sending orders, and surviving an unclear answer.

Three things in here are the difference between an executor that works and one
that quietly doubles positions:

**An intent is written before the request goes out.** MT5 has no client order
id, so there is no server-side key to look the order up by later. A local record
with the symbol, volume, magic and send time is the only thing that lets a
timeout be resolved afterwards.

**A timeout is not a failure.** Retcode 10012 and an empty result both mean "the
terminal did not confirm". The order may be live. :func:`reconcile` looks for a
matching deal before anything else is allowed to happen, and
:mod:`mt5trader.risk` blocks new exposure while an intent is unresolved.

**Filling mode is negotiated, not assumed.** Symbols advertise FOK and IOC in a
bitmask, and market-execution symbols accept RETURN. Retcode 10030 means the
mode was wrong, which is a retry with the next candidate, not an error.

Retry policy, by classification from :mod:`mt5trader.errors`:

    retry      fresh price (or next filling mode), short pause, send again
    ambiguous  stop, mark the intent, reconcile
    fatal      stop, report the broker's reason, do not resend
"""

import datetime
import time

from . import errors, history, records, state

ACTION_DEAL = 1
ACTION_PENDING = 5
ACTION_SLTP = 6
ACTION_MODIFY = 7
ACTION_REMOVE = 8
ACTION_CLOSE_BY = 10

ORDER_TYPES = {
    ("buy", "market"): 0,
    ("sell", "market"): 1,
    ("buy", "limit"): 2,
    ("sell", "limit"): 3,
    ("buy", "stop"): 4,
    ("sell", "stop"): 5,
    ("buy", "stop_limit"): 6,
    ("sell", "stop_limit"): 7,
}

TIME_IN_FORCE = {
    "gtc": 0,
    "day": 1,
    "specified": 2,
    "specified_day": 3,
    "specified-day": 3,
}

SUCCESS_STATES = ("filled", "placed", "partial")


class OrderError(RuntimeError):
    """The request could not be built, or the broker refused it."""


def order_type_value(side, kind):
    key = (side, "market" if kind == "market" else kind)
    if key not in ORDER_TYPES:
        raise OrderError("no MetaTrader order type for %s %s" % (side, kind))
    return ORDER_TYPES[key]


def order_type_name(value):
    for (side, kind), code in ORDER_TYPES.items():
        if code == value:
            return "%s %s" % (side, kind)
    return "type %s" % value


def build_request(bridge, settings, spec, instrument=None, position=None):
    """Turn a normalised order spec into an MT5 request dict.

    A market order in MT5 still carries a price: the ask for a buy, the bid for
    a sell. Leaving it out, or sending the mid, is the most common cause of a
    "market closed" or "invalid price" rejection in an otherwise correct bot.
    """
    instrument = instrument or spec.get("instrument")
    if instrument is None:
        raise OrderError("the spec carries no instrument metadata")

    kind = spec.get("kind", "market")
    side = spec["side"]
    action = ACTION_DEAL if kind == "market" else ACTION_PENDING
    otype = order_type_value(side, spec.get("type", "market") if kind == "pending" else "market")

    price = spec.get("price")
    if kind == "market":
        if not instrument.has_quotes:
            raise OrderError("%s has no live quotes; a market order cannot be priced"
                             % instrument.symbol)
        price = instrument.entry_price(side)
    elif price is None:
        raise OrderError("a pending order needs a price")

    request = {
        "action": action,
        "symbol": spec["symbol"],
        "volume": float(spec["volume"]),
        "type": otype,
        "price": instrument.normalize_price(price),
        "magic": int(spec.get("magic", settings.magic)),
        "comment": str(spec.get("comment") or "mt5trader")[:31],
        "deviation": int(spec.get("deviation") or settings.execution.get("deviation_points") or 15),
    }
    if spec.get("stop"):
        request["sl"] = instrument.normalize_price(spec["stop"])
    if spec.get("target"):
        request["tp"] = instrument.normalize_price(spec["target"])
    if kind == "pending":
        candidates = instrument.filling_candidates(spec.get("filling") or "auto")
        request["type_filling"] = candidates[0]
        tif = TIME_IN_FORCE.get(str(spec.get("time_in_force") or "gtc").lower())
        if tif is None:
            raise OrderError("unknown time_in_force %r" % spec.get("time_in_force"))
        request["type_time"] = tif
        expiration = spec.get("expiration")
        if tif in (2, 3):
            if not expiration:
                minutes = int(settings.execution.get("pending_expiry_minutes") or 1440)
                base = spec.get("_clock")
                moment = base.now if base is not None else records.naive_utc_now()
                request["expiration"] = moment + datetime.timedelta(minutes=minutes)
            elif isinstance(expiration, str):
                request["expiration"] = datetime.datetime.fromisoformat(expiration)
            else:
                request["expiration"] = expiration
        if spec.get("type") == "stop_limit":
            stop_limit = spec.get("stop_limit_price")
            if not stop_limit:
                raise OrderError("a stop_limit order needs a stop_limit_price")
            request["stoplimit"] = instrument.normalize_price(stop_limit)
    else:
        candidates = instrument.filling_candidates(spec.get("filling") or "auto")
        request["type_filling"] = candidates[0]
    if position is not None:
        request["position"] = int(position)
    return request


def preflight(bridge, request):
    """``order_check``: the broker-side validation, without sending anything.

    Costs one round trip and catches margin, volume and stop problems that the
    local kernel cannot see -- for example a symbol whose margin rate changed
    minutes ago.
    """
    result = bridge.call("order_check", request)
    if result is None:
        return {"available": False, "note": "order_check returned nothing"}
    return {
        "available": True,
        "retcode": records.inum(records.get(result, "retcode"), None),
        "explanation": errors.explain(records.inum(records.get(result, "retcode"), None)),
        "comment": records.get(result, "comment"),
        "margin": records.fnum(records.get(result, "margin"), None),
        "margin_free": records.fnum(records.get(result, "margin_free"), None),
        "balance": records.fnum(records.get(result, "balance"), None),
        "equity": records.fnum(records.get(result, "equity"), None),
        "profit": records.fnum(records.get(result, "profit"), None),
        "ok": errors.is_ok(records.inum(records.get(result, "retcode"), None)),
    }


def result_summary(result):
    if result is None:
        return {
            "retcode": None,
            "name": "NO_RESULT",
            "kind": errors.AMBIGUOUS,
            "ok": False,
            "ambiguous": True,
            "comment": "order_send returned nothing",
            "order": None,
            "deal": None,
            "volume": None,
            "price": None,
        }
    retcode = records.inum(records.get(result, "retcode"), None)
    info = errors.explain(retcode)
    return {
        "retcode": retcode,
        "name": info["name"],
        "kind": info["kind"],
        "text": info["text"],
        "advice": info["advice"],
        "ok": info["kind"] == errors.OK,
        "ambiguous": info["kind"] == errors.AMBIGUOUS,
        "comment": records.get(result, "comment"),
        "order": records.inum(records.get(result, "order"), None),
        "deal": records.inum(records.get(result, "deal"), None),
        "volume": records.fnum(records.get(result, "volume"), None),
        "price": records.fnum(records.get(result, "price"), None),
        "bid": records.fnum(records.get(result, "bid"), None),
        "ask": records.fnum(records.get(result, "ask"), None),
        "request_id": records.inum(records.get(result, "request_id"), None),
        "retcode_external": records.inum(records.get(result, "retcode_external"), None),
    }


def send(bridge, store, spec, instrument, request, settings, clock=None,
         max_attempts=None):
    """Send one order, with the retry and reconciliation policy above.

    Returns a dict with the final result, every attempt, and a ``status`` of
    ``sent``, ``ambiguous``, ``rejected`` or ``blocked``.
    """
    max_attempts = max_attempts or max(1, int(settings.execution.get("retries", 2)) + 1)
    intent_id = state.new_id("int")
    plan_id = spec.get("plan_id")
    intent = {
        "intent_id": intent_id,
        "plan_id": plan_id,
        "fingerprint": spec.get("fingerprint"),
        "symbol": spec["symbol"],
        "side": spec["side"],
        "kind": spec.get("kind"),
        "volume": spec["volume"],
        "stop": spec.get("stop"),
        "target": spec.get("target"),
        "magic": spec.get("magic"),
        "deviation": spec.get("deviation"),
        "status": "prepared",
        "attempts": [],
        "created_at": records.naive_utc_now().replace(microsecond=0).isoformat() + "Z",
        "server_time": (clock.now.replace(microsecond=0).isoformat() if clock else None),
        "sent_epoch": int(time.time()),
    }
    store.save_intent(intent)
    store.event("order_intent", {"intent_id": intent_id, "symbol": spec["symbol"],
                                 "side": spec["side"], "volume": spec["volume"],
                                 "plan_id": plan_id})

    candidates = instrument.filling_candidates(spec.get("filling") or "auto")
    attempts = []
    final = None
    status = "rejected"
    pause = max(0.0, float(settings.execution.get("requote_pause_ms", 250)) / 1000.0)

    for attempt in range(1, max_attempts + 1):
        candidate = candidates[min(attempt - 1, len(candidates) - 1)]
        try:
            request["type_filling"] = candidate
            if spec.get("kind", "market") == "market":
                live = instrument.entry_price(spec["side"])
                if live is None:
                    raise OrderError("no quotes to price the market order")
                request["price"] = instrument.normalize_price(live)
            elif spec.get("kind") == "pending" and attempt > 1:
                if not request.get("type_time") and settings.execution.get("pending_expiry_minutes"):
                    request["type_time"] = TIME_IN_FORCE["specified"]
                    base = clock.now if clock else records.naive_utc_now()
                    request["expiration"] = base + datetime.timedelta(
                        minutes=int(settings.execution.get("pending_expiry_minutes") or 1440))
            result = bridge.call("order_send", request)
            summary = result_summary(result)
        except Exception as exc:  # noqa: BLE001 - a bridge failure is ambiguous by nature
            summary = {
                "retcode": None, "name": "BRIDGE_ERROR", "kind": errors.AMBIGUOUS,
                "ok": False, "ambiguous": True, "comment": str(exc),
            }
        summary["attempt"] = attempt
        summary["filling"] = candidate
        summary["request"] = dict(request)
        attempts.append(summary)
        final = summary
        store.update_intent(intent_id, status="sending", attempts=attempts,
                            last_retcode=summary.get("retcode"))

        if summary["ok"]:
            status = "sent"
            break
        if summary.get("ambiguous"):
            status = "ambiguous"
            break
        if summary["kind"] == errors.RETRY:
            retcode = summary.get("retcode")
            if retcode == 10030:
                continue  # next loop iteration rotates the filling mode
            time.sleep(pause)
            continue
        status = "rejected"
        break

    store.update_intent(intent_id, status=status, attempts=attempts,
                        result=attempts[-1] if attempts else None,
                        finished_at=records.naive_utc_now().replace(microsecond=0).isoformat() + "Z")
    if status == "sent":
        store.event("order_sent", {"intent_id": intent_id, "retcode": final.get("retcode"),
                                   "order": final.get("order"), "deal": final.get("deal"),
                                   "symbol": spec["symbol"], "volume": final.get("volume")})
    elif status == "ambiguous":
        store.event("order_ambiguous", {"intent_id": intent_id,
                                        "retcode": final.get("retcode"),
                                        "comment": final.get("comment")})
    else:
        store.event("order_rejected", {"intent_id": intent_id,
                                       "retcode": final.get("retcode"),
                                       "comment": final.get("comment")})

    return {
        "intent_id": intent_id,
        "status": status,
        "attempts": attempts,
        "result": final,
        "order_type_checked": order_type_name(request["type"]),
        "message": _message(status, final),
    }


def _message(status, final):
    if status == "sent":
        return "accepted by the broker: %s, %s lot(s) at %s" % (
            final.get("name"), final.get("volume"), final.get("price"))
    if status == "ambiguous":
        return ("the terminal never confirmed the outcome (%s: %s). The order may be "
                "live. Reconcile with mt5_reconcile before doing anything else."
                % (final.get("name"), final.get("comment")))
    return "rejected: %s, %s" % (final.get("name"), final.get("comment"))


def reconcile(bridge, settings, intent):
    """Decide whether an unconfirmed intent actually produced a deal.

    Looks for a deal with the same symbol, magic and volume in the window from
    the send attempt onwards, then checks open positions and pending orders as a
    fallback for a fill whose deal has not been reported yet.
    """
    sent_epoch = records.inum(intent.get("sent_epoch"), None)
    if not sent_epoch:
        return {"intent_id": intent.get("intent_id"), "status": "unknown",
                "reason": "the intent has no send timestamp to search from"}
    magic = records.inum(intent.get("magic"), settings.magic)
    symbol = intent.get("symbol")
    volume = records.fnum(intent.get("volume"), None)
    from_dt = records.naive_utc_from_epoch(sent_epoch - 120)

    deal_list = history.deals_for_magic(bridge, magic, from_dt)
    matches = []
    for deal in deal_list:
        stamp = records.inum(records.get(deal, "time"), 0) or 0
        if stamp < sent_epoch - 120:
            continue
        if str(records.get(deal, "symbol")) != str(symbol):
            continue
        if records.inum(records.get(deal, "entry"), -1) != history.DEAL_ENTRY_IN:
            continue
        deal_volume = records.fnum(records.get(deal, "volume"), None)
        if volume and deal_volume and abs(deal_volume - volume) > max(1e-8, volume * 0.001):
            continue
        matches.append({
            "deal": records.inum(records.get(deal, "ticket"), None),
            "order": records.inum(records.get(deal, "order"), None),
            "position_id": records.inum(records.get(deal, "position_id"), None),
            "volume": deal_volume,
            "price": records.fnum(records.get(deal, "price"), None),
            "time": records.iso_time(stamp),
            "comment": records.get(deal, "comment"),
        })

    positions = [records.as_dict(p, ["ticket", "symbol", "volume", "price_open", "time",
                                     "magic", "comment"])
                 for p in (bridge.call("positions_get") or ())
                 if records.inum(records.get(p, "magic"), 0) == int(magic)
                 and str(records.get(p, "symbol")) == str(symbol)]
    pending = [records.as_dict(o, ["ticket", "symbol", "volume_current", "price_open", "time_setup",
                                   "magic", "comment"])
               for o in (bridge.call("orders_get") or ())
               if records.inum(records.get(o, "magic"), 0) == int(magic)
               and str(records.get(o, "symbol")) == str(symbol)]

    if matches:
        status, reason = "executed", "%d matching entry deal(s) found" % len(matches)
    elif positions:
        status, reason = "maybe_executed", (
            "no matching deal yet, but %d position(s) exist on %s with this magic. A "
            "deal report can lag a fill; treat this as executed until the next check."
            % (len(positions), symbol))
    elif pending:
        status, reason = "placed", "%d pending order(s) exist on %s with this magic" % (
            len(pending), symbol)
    else:
        status, reason = "not_executed", (
            "no deal, position or pending order matched within the window; the order "
            "did not reach the market")
    return {
        "intent_id": intent.get("intent_id"),
        "status": status,
        "reason": reason,
        "matched_deals": matches,
        "positions": positions,
        "pending": pending,
    }


def reconcile_all(bridge, store, settings):
    """Reconcile every unresolved intent. Run this before opening anything new."""
    out = []
    for intent in store.unresolved_intents():
        verdict = reconcile(bridge, settings, intent)
        if verdict["status"] in ("executed", "maybe_executed", "placed", "not_executed"):
            store.update_intent(intent["intent_id"], status=verdict["status"],
                                reconciled_at=records.naive_utc_now().replace(microsecond=0).isoformat() + "Z",
                                reconcile_reason=verdict["reason"])
            store.event("intent_reconciled", {"intent_id": intent["intent_id"],
                                              "status": verdict["status"]})
        out.append(verdict)
    return out

