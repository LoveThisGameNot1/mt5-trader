"""Broker time, realised profit and loss, and the statistics built on them.

**The time zone problem first, because everything else depends on it.** MT5 has
three different clocks and they are all called "time":

* the terminal's ``datetime`` values carry *server* time, presented as if it were
  UTC. A tick at 10:00 broker time has an epoch that decodes to 10:00 UTC, even
  though the broker's server sits at UTC+2 or UTC+3;
* the python module's ``last_error``, your file system and the model all live in
  local or UTC time;
* the broker's *trading day* is what a daily loss limit has to be measured
  against, and it starts at server midnight, not yours.

So the offset between server time and real UTC is measured, not assumed: the
millisecond timestamp on the newest tick is compared with the machine clock.
That is accurate to network latency and needs no broker-specific table. Every
day boundary in the kernel comes from it, and the source of the offset is
reported so a wrong reading is visible rather than silent.

Realised PnL is the sum of four fields, not one. ``profit`` alone misses swap,
commission and fee, which on a hold-overnight CFD position can exceed the
profit. A daily loss limit that watches only ``profit`` is a daily loss limit
that does not fire.
"""

import datetime
import time

from . import records

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1
DEAL_ENTRY_INOUT = 2
DEAL_ENTRY_OUT_BY = 3

DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
NON_TRADE_DEAL_TYPES = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17)

CLOSING_ENTRIES = (DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY)


class TimeUnavailable(RuntimeError):
    pass


def server_offset(bridge, symbol_hint=None):
    """(offset_seconds, source, note).

    offset = server clock reading minus real UTC.

    MT5 exposes the server clock only through tick timestamps and bar opens, so the
    offset has to be measured rather than looked up in a broker table. That works
    while the market is open and breaks on a weekend: every tick is then hours old,
    and the age of the newest tick is not a time zone. Reporting "-36 hours" as an
    offset would shift every daily boundary by a day and a half, so the offset is
    declared unmeasurable instead and server time is treated as local until a
    symbol trades again.

    A tick ahead of the local clock is normal and is accepted: a tick cannot come
    from the future, so a positive difference is the broker sitting at a positive
    offset. Only a tick more than fifteen minutes *behind* local time is
    ambiguous, and that is the case this refuses to guess at.

    Candidates are ordered Market Watch first and then the rest of the terminal.
    That order is not cosmetic: on a weekend every FX pair is stale while a crypto
    CFD three hundred symbols down the alphabetical list is trading, and sampling
    only the first handful of symbols reported the offset as unmeasurable when it
    was sitting right there.
    """
    candidates = []
    if symbol_hint:
        candidates.append(symbol_hint)
    visible, rest = [], []
    for info in (bridge.call("symbols_get") or ()):
        name = records.get(info, "name")
        if not name or name in candidates:
            continue
        if records.get(info, "visible", False):
            visible.append(name)
        else:
            rest.append(name)
    candidates.extend(visible)
    candidates.extend(rest)
    best = None
    for name in candidates[:60]:
        tick = bridge.call("symbol_info_tick", name)
        if tick is None:
            continue
        ms = records.get(tick, "time_msc")
        epoch = records.fnum(ms, None)
        if not epoch:
            seconds = records.fnum(records.get(tick, "time"), None)
            epoch = seconds * 1000.0 if seconds else None
        if not epoch:
            continue
        if best is None or epoch > best[0]:
            best = (epoch, name)
    if best is None:
        return 0, "fallback_local", (
            "no tick with a timestamp was available, so server time is being "
            "treated as local time. Daily boundaries may be wrong by the "
            "broker's offset until a quote arrives."
        )
    epoch_seconds = best[0] / 1000.0
    age = time.time() - epoch_seconds
    if age > 900:
        return 0, "stale_tick", (
            "the newest tick anywhere on this terminal is %.1f hours old, so the "
            "market is closed and the server offset cannot be measured. Server time "
            "is being treated as local time, which puts a daily boundary off by the "
            "broker's offset until a symbol trades. Everything else on this account "
            "is unaffected." % (age / 3600.0)
        )
    offset = epoch_seconds - time.time()
    rounded = int(round(offset / 900.0) * 900)  # snap to a quarter hour
    return rounded, "tick_msc", (
        "measured from the newest tick on %s, %.0fs ago; raw offset %.0fs, snapped "
        "to %ds" % (best[1], -age, offset, rounded))


class Clock:
    """Server-clock arithmetic for one server session."""

    def __init__(self, offset_seconds=0, source="fallback_local", note=""):
        self.offset = int(offset_seconds)
        self.source = source
        self.note = note

    @classmethod
    def measure(cls, bridge, symbol_hint=None):
        offset, source, note = server_offset(bridge, symbol_hint)
        return cls(offset, source, note)

    @property
    def now(self):
        """Server wall clock as a naive datetime."""
        return datetime.datetime.utcfromtimestamp(time.time() + self.offset)

    @property
    def utc_now(self):
        return datetime.datetime.utcnow()

    def to_server(self, moment):
        return moment + datetime.timedelta(seconds=self.offset)

    def server_epoch(self, moment=None):
        moment = moment or datetime.datetime.utcnow()
        return time.mktime(moment.timetuple()) + self.offset

    def day_start(self, moment=None):
        """Server midnight of the broker's current trading day.

        Returned as a naive datetime whose value equals the server clock, which
        is what the history functions compare against.
        """
        now = moment or self.now
        return datetime.datetime(now.year, now.month, now.day)

    def day_key(self, moment=None):
        return (moment or self.now).strftime("%Y-%m-%d")

    def describe(self):
        return {
            "server_offset_seconds": self.offset,
            "server_offset_hours": round(self.offset / 3600.0, 2),
            "server_now": self.now.replace(microsecond=0).isoformat(),
            "utc_now": self.utc_now.replace(microsecond=0).isoformat(),
            "local_now": datetime.datetime.now().replace(microsecond=0).isoformat(),
            "source": self.source,
            "note": self.note,
            "caveat": ("the broker's trading day starts at server midnight; some "
                       "brokers roll the day at a different wall-clock time, which "
                       "shifts a daily loss limit by that much"),
        }


# --------------------------------------------------------------------------
# deals
# --------------------------------------------------------------------------
def deals(bridge, date_from, date_to=None):
    """Deals in a window. Both bounds are given, because a one-sided query makes
    the terminal scan far more history than it needs to."""
    return bridge.call("history_deals_get", date_from, date_to or
                       datetime.datetime.utcfromtimestamp(time.time() + 86400)) or ()


def deals_for_magic(bridge, magic, date_from, date_to=None):
    return [d for d in deals(bridge, date_from, date_to)
            if records.inum(records.get(d, "magic"), 0) == int(magic)]


def deals_for_position(bridge, ticket):
    return bridge.call("history_deals_get", position=int(ticket)) or ()


def deal_money(deal):
    """The full realised amount of one deal.

    profit, swap, commission and fee are four separate columns. Summing only
    profit is the classic way a loss limit silently under-counts."""
    total = 0.0
    for field in ("profit", "swap", "commission", "fee"):
        total += records.fnum(records.get(deal, field), 0.0) or 0.0
    return total


def is_trade(deal):
    return records.inum(records.get(deal, "type"), -1) not in NON_TRADE_DEAL_TYPES


def realized_total(deal_list):
    return sum(deal_money(d) for d in deal_list if is_trade(d))


def realized_summary(deal_list):
    """Closed-deal totals plus the running balance moves that are not trading."""
    trading = [d for d in deal_list if is_trade(d)]
    closing = [d for d in trading if records.inum(records.get(d, "entry"), -1)
               in CLOSING_ENTRIES]
    other = [d for d in deal_list if not is_trade(d)]
    return {
        "deals": len(deal_list),
        "trading_deals": len(trading),
        "closing_deals": len(closing),
        "realized_net": round(realized_total(trading), 2),
        "profit_gross": round(sum(records.fnum(records.get(d, "profit"), 0.0) or 0.0
                                 for d in trading), 2),
        "swap": round(sum(records.fnum(records.get(d, "swap"), 0.0) or 0.0
                          for d in trading), 2),
        "commission": round(sum(records.fnum(records.get(d, "commission"), 0.0) or 0.0
                                for d in trading), 2),
        "fee": round(sum(records.fnum(records.get(d, "fee"), 0.0) or 0.0
                         for d in trading), 2),
        "non_trading_net": round(realized_total(other), 2),
        "first_deal": records.iso_time(min((records.inum(records.get(d, "time"), 0)
                                            for d in deal_list), default=None)),
        "last_deal": records.iso_time(max((records.inum(records.get(d, "time"), 0)
                                           for d in deal_list), default=None)),
    }


def round_trips(deal_list):
    """Group deals into closed positions, one entry per position id.

    A position can be filled in pieces and closed in pieces, so the unit of
    accounting is the position, not the deal.
    """
    by_position = {}
    for deal in sorted(deal_list, key=lambda d: records.inum(records.get(d, "time"), 0)):
        if not is_trade(deal):
            continue
        pid = records.get(deal, "position_id")
        if pid is None:
            continue
        by_position.setdefault(pid, []).append(deal)
    out = []
    for pid, group in by_position.items():
        closes = [d for d in group
                  if records.inum(records.get(d, "entry"), -1) in CLOSING_ENTRIES]
        opens = [d for d in group if records.inum(records.get(d, "entry"), -1) == DEAL_ENTRY_IN]
        if not closes:
            continue
        volume = sum(records.fnum(records.get(d, "volume"), 0.0) or 0.0 for d in closes)
        opened_volume = sum(records.fnum(records.get(d, "volume"), 0.0) or 0.0 for d in opens)
        if opened_volume and volume < opened_volume * 0.999:
            continue  # still partly open, it belongs to the open side
        net = realized_total(group)
        entry_price = None
        if opens:
            weight = sum(records.fnum(records.get(d, "volume"), 0.0) or 0.0 for d in opens)
            if weight:
                entry_price = sum((records.fnum(records.get(d, "price"), 0.0) or 0.0)
                                  * (records.fnum(records.get(d, "volume"), 0.0) or 0.0)
                                  for d in opens) / weight
        out.append({
            "position_id": pid,
            "symbol": records.get(opens[0] if opens else closes[0], "symbol"),
            "magic": records.inum(records.get(group[0], "magic"), 0),
            "volume": round(volume, 4),
            "entry_price": round(entry_price, 8) if entry_price else None,
            "entry_time": records.iso_time(min(records.inum(records.get(d, "time"), 0)
                                               for d in opens) if opens else None),
            "close_time": records.iso_time(max(records.inum(records.get(d, "time"), 0)
                                               for d in closes)),
            "net": round(net, 2),
            "swap": round(sum(records.fnum(records.get(d, "swap"), 0.0) or 0.0
                              for d in group), 2),
            "commission": round(sum(records.fnum(records.get(d, "commission"), 0.0) or 0.0
                                    for d in group), 2),
            "close_reason": _close_reason(group),
        })
    return out


def _close_reason(group):
    reasons = {records.inum(records.get(d, "reason"), -1) for d in group}
    for code, name in ((5, "take-profit"), (4, "stop-loss"), (6, "stop-out"),
                       (0, "manual"), (3, "expert"), (1, "mobile"), (2, "web")):
        if code in reasons:
            return name
    return "unknown"


def stats(deal_list):
    """Everything a report needs, from the closed round trips."""
    trips = round_trips(deal_list)
    if not trips:
        return {"closed_positions": 0, "note": "no closed positions in this window"}
    wins = [t for t in trips if t["net"] > 0]
    losses = [t for t in trips if t["net"] < 0]
    flat = [t for t in trips if t["net"] == 0]
    gross_profit = sum(t["net"] for t in wins)
    gross_loss = -sum(t["net"] for t in losses)
    ordered = sorted(trips, key=lambda t: t["close_time"] or "")
    curve, running, peak, max_dd = [], 0.0, 0.0, 0.0
    for trip in ordered:
        running += trip["net"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
        curve.append({"time": trip["close_time"], "cumulative": round(running, 2)})
    per_symbol = {}
    for trip in trips:
        bucket = per_symbol.setdefault(trip["symbol"], {"trades": 0, "net": 0.0})
        bucket["trades"] += 1
        bucket["net"] = round(bucket["net"] + trip["net"], 2)
    longest_loss = 0
    streak = 0
    for trip in ordered:
        streak = streak + 1 if trip["net"] < 0 else 0
        longest_loss = max(longest_loss, streak)
    return {
        "closed_positions": len(trips),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flat),
        "win_rate": round(100.0 * len(wins) / len(trips), 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net": round(gross_profit - gross_loss, 2),
        "profit_factor": (round(gross_profit / gross_loss, 3) if gross_loss else None),
        "average_win": round(gross_profit / len(wins), 2) if wins else None,
        "average_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "expectancy": round((gross_profit - gross_loss) / len(trips), 2),
        "best": round(max(t["net"] for t in trips), 2),
        "worst": round(min(t["net"] for t in trips), 2),
        "max_drawdown_realized": round(max_dd, 2),
        "longest_loss_streak": longest_loss,
        "avg_hold_minutes": _avg_hold(ordered),
        "close_reasons": _tally(t["close_reason"] for t in trips),
        "per_symbol": per_symbol,
        "equity_curve_realized": curve[-400:],
        "first_close": ordered[0]["close_time"],
        "last_close": ordered[-1]["close_time"],
    }


def _avg_hold(trips):
    spans = []
    for trip in trips:
        if not trip["entry_time"] or not trip["close_time"]:
            continue
        try:
            start = datetime.datetime.fromisoformat(trip["entry_time"].rstrip("Z"))
            end = datetime.datetime.fromisoformat(trip["close_time"].rstrip("Z"))
        except ValueError:
            continue
        spans.append((end - start).total_seconds() / 60.0)
    return round(sum(spans) / len(spans), 1) if spans else None


def _tally(values):
    out = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


def equity_series(bridge, date_from, magic=None, step_minutes=60):
    """Realised-equity walk, sampled. Not a tick-level equity curve: MT5 keeps
    no equity history, only balance moves and deals, so this is the honest
    version of that number."""
    deal_list = deals(bridge, date_from)
    if magic is not None:
        deal_list = [d for d in deal_list
                     if records.inum(records.get(d, "magic"), 0) == int(magic)]
    running = 0.0
    points = []
    for deal in sorted(deal_list, key=lambda d: records.inum(records.get(d, "time"), 0)):
        running += deal_money(deal)
        points.append({"time": records.iso_time(records.inum(records.get(deal, "time"), 0)),
                       "cumulative": round(running, 2)})
    return points
