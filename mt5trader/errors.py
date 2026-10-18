"""MetaTrader 5 return codes, translated.

Every ``order_send`` result and every ``last_error`` value goes through here so
the kernel and the model never have to reason about bare integers. The
classification that matters:

``RETRY``      the request was cleanly rejected and can be sent again, usually
               after re-reading the price. A requote is not a failure, it is
               information that the price moved.
``AMBIGUOUS``  the terminal did not confirm the outcome. The order may exist.
               Never resend one of these; reconcile against the deal history
               first. This is the single most expensive mistake in MT5
               automation, because it silently doubles a position.
``FATAL``      the request is wrong or the market will not take it. Do not
               resend; change the plan.
``OK``         accepted. ``PLACED`` for a pending order, ``DONE`` for a deal,
               ``DONE_PARTIAL`` when only part of the volume filled.

Codes come from the installed MetaTrader5 module's own constants, which is why
this table is written out rather than imported: the kernel must stay importable
and testable on a machine with no terminal.
"""

OK = "ok"
RETRY = "retry"
AMBIGUOUS = "ambiguous"
FATAL = "fatal"

# retcode -> (name, kind, english text, what to do about it)
RETCODES = {
    10004: ("REQUOTE", RETRY,
            "The price changed between the request and the server.",
            "Re-read the tick and send again."),
    10006: ("REJECT", FATAL,
            "The request was rejected by the trade server or the dealer.",
            "Read the comment on the result, fix the request, do not resend it unchanged."),
    10007: ("CANCEL", FATAL,
            "The request was cancelled by the trader.",
            "Nothing was executed."),
    10008: ("PLACED", OK,
            "A pending order was placed.",
            "Track it with orders_get; it is not a position yet."),
    10009: ("DONE", OK,
            "The request was completed.",
            "Confirm the position with positions_get before reporting success."),
    10010: ("DONE_PARTIAL", OK,
            "Only part of the requested volume was filled.",
            "The rest is gone, not pending. Report the actual volume."),
    10011: ("ERROR", FATAL,
            "The request could not be processed.",
            "Check the request fields, then check the terminal journal."),
    10012: ("TIMEOUT", AMBIGUOUS,
            "The request was sent but no answer arrived in time.",
            "Reconcile against the deal history. Do not resend blindly."),
    10013: ("INVALID", FATAL,
            "The request contains invalid parameters.",
            "Compare against the instrument metadata."),
    10014: ("INVALID_VOLUME", FATAL,
            "The volume is not a valid size for this symbol.",
            "Normalise to volume_min/step/max and try a different size."),
    10015: ("INVALID_PRICE", FATAL,
            "The price is not valid for this symbol.",
            "Round to the symbol's digits and re-read the tick."),
    10016: ("INVALID_STOPS", FATAL,
            "The stops are too close, on the wrong side, or not normalised.",
            "Respect trade_stops_level and trade_freeze_level, then round to digits."),
    10017: ("TRADE_DISABLED", FATAL,
            "Trading is disabled for this account.",
            "Nothing can be sent until the account is enabled."),
    10018: ("MARKET_CLOSED", FATAL,
            "The market is closed for this symbol.",
            "Wait for the session, or use a pending order."),
    10019: ("NO_MONEY", FATAL,
            "There is not enough free margin for this request.",
            "Reduce the volume or free up margin. Do not retry the same size."),
    10020: ("PRICE_CHANGED", RETRY,
            "The price changed.",
            "Re-read the tick and send again."),
    10021: ("PRICE_OFF", RETRY,
            "There are no quotes to process the request.",
            "The feed is momentarily empty; wait and re-read the tick."),
    10022: ("INVALID_EXPIRATION", FATAL,
            "The expiration time is invalid for this order type.",
            "Use ORDER_TIME_GTC or a future server time."),
    10023: ("ORDER_CHANGED", FATAL,
            "The order or position was modified by someone else.",
            "Re-read the current state before doing anything else."),
    10024: ("TOO_MANY_REQUESTS", RETRY,
            "Too many requests are being sent too quickly.",
            "Back off. Repeated hits mean the loop rate limit is broken."),
    10025: ("NO_CHANGES", FATAL,
            "The request makes no change to the order or position.",
            "Read the current values first; this is usually a duplicate call."),
    10026: ("SERVER_DISABLES_AT", FATAL,
            "Automated trading is disabled on the server.",
            "Nothing local can fix this; it is a broker setting."),
    10027: ("CLIENT_DISABLES_AT", FATAL,
            "Automated trading is disabled in the terminal.",
            "Turn on the Algo Trading button in the terminal. This is local."),
    10028: ("LOCKED", FATAL,
            "The request is locked and cannot be processed.",
            "Something else is operating this position; wait and re-read."),
    10029: ("FROZEN", FATAL,
            "The order or position is frozen and cannot be changed.",
            "The price is inside the freeze level; wait for it to move away."),
    10030: ("INVALID_FILL", RETRY,
            "The requested filling mode is not supported for this symbol.",
            "Re-send with another filling mode from the symbol's allowed set."),
    10031: ("CONNECTION", RETRY,
            "There is no connection to the trade server.",
            "Wait for the terminal to reconnect. Consider the request unsent."),
    10032: ("ONLY_REAL", FATAL,
            "The operation is allowed only on a real account.",
            "The broker refuses this on demo."),
    10033: ("LIMIT_ORDERS", FATAL,
            "The limit on the number of pending orders has been reached.",
            "Cancel something first, or raise the limit at the broker."),
    10034: ("LIMIT_VOLUME", FATAL,
            "The limit on the total volume of orders has been reached.",
            "Reduce the size or close something."),
    10035: ("INVALID_ORDER", FATAL,
            "The order type is wrong for this request.",
            "Check action/type pairing."),
    10036: ("POSITION_CLOSED", FATAL,
            "The position is already closed.",
            "Re-read positions_get; the ticket is gone."),
    10038: ("INVALID_CLOSE_VOLUME", FATAL,
            "The volume being closed is not valid.",
            "Close in steps of volume_step and never more than the position holds."),
    10039: ("CLOSE_ORDER_EXIST", FATAL,
            "A close request for this position is already in flight.",
            "Wait for it to land; do not send a second one."),
    10040: ("LIMIT_POSITIONS", FATAL,
            "The limit on the number of open positions has been reached.",
            "Close something, or raise the limit at the broker."),
    10041: ("REJECT_CANCEL", FATAL,
            "The request to cancel was rejected.",
            "The order is no longer in a state that can be cancelled."),
    10042: ("LONG_ONLY", FATAL,
            "Only long positions are allowed for this symbol.",
            "Sell requests will never work here."),
    10043: ("SHORT_ONLY", FATAL,
            "Only short positions are allowed for this symbol.",
            "Buy requests will never work here."),
    10044: ("CLOSE_ONLY", FATAL,
            "Only closing is allowed for this symbol.",
            "No new exposure can be opened."),
    10045: ("FIFO_CLOSE", FATAL,
            "The position must be closed by an opposite position, FIFO rule.",
            "Use an opposing position on the same symbol instead of a market close."),
}

# last_error() values from the module itself, not the trade server.
CLIENT_ERRORS = {
    1: ("OK", "No error."),
    -1: ("FAIL", "Generic failure."),
    -2: ("INVALID_PARAMS", "The arguments passed to the MT5 function were wrong."),
    -3: ("NO_MEMORY", "Out of memory."),
    -4: ("NOT_FOUND", "No such history or no such object."),
    -5: ("INVALID_VERSION", "The terminal build and the python module do not match."),
    -6: ("AUTH_FAILED", "Login or password rejected."),
    -7: ("UNSUPPORTED", "This terminal does not support the called method."),
    -8: ("AUTO_TRADING_DISABLED",
         "The Algo Trading button in the terminal is off. Local setting, one click to fix."),
    -10000: ("INTERNAL_FAIL", "Internal IPC error between the module and the terminal."),
    -10001: ("INTERNAL_SEND", "IPC send failed."),
    -10002: ("INTERNAL_RECV", "IPC receive failed."),
    -10003: ("INTERNAL_INIT", "IPC initialisation failed; the terminal is probably not running."),
    -10004: ("INTERNAL_CONNECT", "No IPC connection; the terminal is closed or not logged in."),
    -10005: ("INTERNAL_TIMEOUT", "The terminal did not answer in time."),
}


def classify(retcode):
    """(name, kind, text, advice) for a trade server return code."""
    if retcode is None:
        return ("NO_RESULT", AMBIGUOUS,
                "order_send returned nothing at all.",
                "The terminal bridge dropped the request. Reconcile before anything else.")
    known = RETCODES.get(int(retcode))
    if known:
        return known
    return ("UNKNOWN_%s" % retcode, FATAL,
            "Unrecognised return code.",
            "Read the terminal's Experts journal for this timestamp.")


def is_ok(retcode):
    return classify(retcode)[1] == OK


def is_retry(retcode):
    return classify(retcode)[1] == RETRY


def is_ambiguous(retcode):
    return classify(retcode)[1] == AMBIGUOUS


def is_fatal(retcode):
    return classify(retcode)[1] == FATAL


def explain(retcode):
    name, kind, text, advice = classify(retcode)
    return {
        "retcode": retcode,
        "name": name,
        "kind": kind,
        "text": text,
        "advice": advice,
    }


def explain_client(code, text=None):
    name, hint = CLIENT_ERRORS.get(int(code) if code is not None else 0, ("UNKNOWN", ""))
    return {
        "code": code,
        "name": name,
        "text": text or hint,
        "advice": hint,
    }


def render_retcode(retcode):
    """One line a human reads."""
    info = explain(retcode)
    return "retcode %s %s [%s]: %s %s" % (
        info["retcode"], info["name"], info["kind"].upper(), info["text"], info["advice"],
    )
