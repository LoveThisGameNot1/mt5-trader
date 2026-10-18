"""CFTC Commitments of Traders: what the large speculators actually hold.

This is the FX-adjacent answer to "what are the big players buying". The American
13F filings used for equities have no equivalent for a currency pair, and would be
useless anyway at forty-five days old. What exists instead is the CFTC's weekly
report of positions in the currency, metal, energy and index futures that sit
underneath the CFDs a MetaTrader terminal trades, published every Friday for the
Tuesday before it.

The numbers are behind the data, and that is not a footnote: a COT report is three
days old when it lands and describes Tuesday. It is a measure of how crowded a
positioning is, not a trigger.

The module reads a file prepared by ``tools/fetch_cot.py``. The server itself does
no network calls, which keeps a trade-path decision from depending on a website.

What each column means, since the report never says it:

``noncommercial``  large speculators: funds, CTAs. Trend followers, and the crowd
                   that gets crowded.
``commercial``     producers and hedgers. Their net position is usually the
                   opposite side, and is not a directional opinion.
``nonreportable``  small traders, below the reporting threshold.

Crowding is measured as a percentile of the last few years, not as a raw number,
because "net short 137,573 contracts" means nothing without the scale it normally
sits in.
"""

import json
import pathlib

from . import records

# Contract names exactly as the CFTC writes them, verified against the published
# annual file. Match on any alias; report honestly when none is present rather
# than guessing at a name.
CONTRACTS = {
    "EUR": ["EURO FX - CHICAGO MERCANTILE EXCHANGE"],
    "GBP": ["BRITISH POUND - CHICAGO MERCANTILE EXCHANGE"],
    "JPY": ["JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE"],
    "CHF": ["SWISS FRANC - CHICAGO MERCANTILE EXCHANGE"],
    "CAD": ["CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
    "AUD": ["AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
    "NZD": ["NEW ZEALAND DOLLAR - CHICAGO MERCANTILE EXCHANGE",
            "NZ DOLLAR - CHICAGO MERCANTILE EXCHANGE"],
    "MXN": ["MEXICAN PESO - CHICAGO MERCANTILE EXCHANGE"],
    "USD": ["USD INDEX - ICE FUTURES U.S."],
    "XAU": ["GOLD - COMMODITY EXCHANGE INC."],
    "XAG": ["SILVER - COMMODITY EXCHANGE INC."],
    "BTC": ["BITCOIN - CHICAGO MERCANTILE EXCHANGE"],
    "ETH": ["ETHER CASH SETTLED - CHICAGO MERCANTILE EXCHANGE"],
    "SPX": ["E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE"],
    "NDX": ["NASDAQ-100 Consolidated - CHICAGO MERCANTILE EXCHANGE"],
    "DJIA": ["DJIA Consolidated - CHICAGO BOARD OF TRADE"],
    "WTI": ["CRUDE OIL, LIGHT SWEET-WTI - ICE FUTURES EUROPE",
            "CRUDE OIL, LIGHT SWEET-WTI - NEW YORK MERCANTILE EXCHANGE"],
    "BRN": ["BRENT LAST DAY - NEW YORK MERCANTILE EXCHANGE"],
}

# A symbol's name, uppercased and stripped of punctuation, to a contract key. The
# base currency wins for a pair, because that is the contract with a directional
# opinion in it.
NAME_HINTS = (
    ("US30", "DJIA"), ("DOW", "DJIA"), ("US500", "SPX"), ("SPX", "SPX"),
    ("USTECH", "NDX"), ("NAS", "NDX"), ("GER40", "None"), ("DE40", "None"),
    ("BTC", "BTC"), ("BITCOIN", "BTC"), ("ETH", "ETH"), ("ETHER", "ETH"),
    ("XAU", "XAU"), ("GOLD", "XAU"), ("XAG", "XAG"), ("SILVER", "XAG"),
    ("WTI", "WTI"), ("BRENT", "BRN"),
)

CURRENCY_KEYS = {"EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "MXN", "USD"}

DEFAULT_FILENAME = "cot.json"


def contract_for(symbol, instrument=None):
    """(key, reason) for a symbol, or (None, why not)."""
    raw = str(symbol or "").upper().replace(".", "").replace("_", "").replace("-", "")
    for hint, key in NAME_HINTS:
        if hint in raw:
            if key == "None":
                return None, ("%s is a European index and this report covers American "
                              "futures only" % symbol)
            return key, "matched on %s in the symbol name" % hint
    base = str(records.get(instrument.info, "currency_base") or "").upper()
    if base in CONTRACTS:
        return base, "the base currency of the pair"
    profit = str(records.get(instrument.info, "currency_profit") or "").upper()
    if profit in CONTRACTS:
        return profit, "the profit currency of the pair"
    return None, ("no CFTC contract is mapped for %s. This report covers the currency, "
                  "metal, energy and American index futures; a cross like %s has no "
                  "contract of its own." % (symbol, symbol))


def path_for(settings):
    configured = settings.env.get("MT5_COT_FILE") or (settings.config.get("cot") or {}).get("file")
    if configured:
        return pathlib.Path(configured), pathlib.Path(configured).is_file()
    candidate = settings.state_dir / DEFAULT_FILENAME
    return candidate, candidate.is_file()


def load(settings):
    path, found = path_for(settings)
    if not found:
        return None, ("no COT data at %s. Run: python tools/fetch_cot.py (it is the "
                      "only part of this project that uses the network, and it is "
                      "deliberately not the server)." % path)
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (json.JSONDecodeError, OSError) as exc:
        return None, "%s could not be read: %s" % (path, exc)


def _percentile(series, value):
    clean = [v for v in series if v is not None]
    if len(clean) < 8 or value is None:
        return None
    below = sum(1 for v in clean if v <= value)
    return round(100.0 * below / len(clean), 1)


def _zscore(series, value):
    clean = [v for v in series if v is not None]
    if len(clean) < 8 or value is None:
        return None
    mean = sum(clean) / len(clean)
    variance = sum((v - mean) ** 2 for v in clean) / (len(clean) - 1)
    if variance <= 0:
        return None
    import math
    return round((value - mean) / math.sqrt(variance), 2)


def report(data, key, weeks=None):
    """One contract's latest positioning, its change, and how crowded it is."""
    markets = records.get(data, "markets", {}) or {}
    entry = markets.get(key)
    if not entry:
        available = ", ".join(sorted(markets)) or "none"
        return None, "no %s series in the COT file. Mapped contracts present: %s" % (
            key, available)
    series = list(entry.get("series") or [])
    if weeks:
        series = series[-int(weeks):]
    if not series:
        return None, "the %s series is empty" % key
    latest = series[-1]
    previous = series[-2] if len(series) > 1 else {}
    nets = [item.get("net") for item in series]
    long_change = ((latest.get("noncommercial_long") or 0)
                   - (previous.get("noncommercial_long") or 0)) if previous else None
    short_change = ((latest.get("noncommercial_short") or 0)
                    - (previous.get("noncommercial_short") or 0)) if previous else None
    net = latest.get("net")
    open_interest = latest.get("open_interest") or 0
    percentile = _percentile(nets, net)
    zscore = _zscore(nets, net)
    crowding = "unknown"
    if percentile is not None:
        if percentile >= 90:
            crowding = "crowded long: the largest net long in this window"
        elif percentile >= 75:
            crowding = "towards the long extreme of the window"
        elif percentile <= 10:
            crowding = "crowded short: the largest net short in this window"
        elif percentile <= 25:
            crowding = "towards the short extreme of the window"
        else:
            crowding = "middle of the window, no positioning extreme"
    return {
        "contract": key,
        "contract_name": entry.get("name"),
        "report_date": latest.get("date"),
        "weeks": len(series),
        "window_from": series[0].get("date"),
        "long_spec": latest.get("noncommercial_long"),
        "short_spec": latest.get("noncommercial_short"),
        "net_spec": net,
        "net_spec_percent_of_oi": (round(100.0 * net / open_interest, 2)
                                   if net is not None and open_interest else None),
        "open_interest": open_interest,
        "commercial_net": latest.get("commercial_net"),
        "long_change_week": long_change,
        "short_change_week": short_change,
        "net_change_week": (net - (previous.get("net") or 0)) if previous else None,
        "net_percentile": percentile,
        "net_zscore": zscore,
        "crowding": crowding,
        "source": records.get(data, "source"),
        "fetched": records.get(data, "fetched"),
        "caveat": ("published on a Friday for the Tuesday before it, so this describes "
                   "a position that is already three days old when it arrives. It "
                   "measures crowding, not a signal, and it is not a reason to trade "
                   "on its own."),
    }, None


def summary(settings, symbols, instruments=None):
    """Reports for a list of symbols, plus whatever could not be mapped."""
    data, error = load(settings)
    if error:
        return {"available": False, "reason": error}
    out = {"available": True, "reports": [], "unmapped": [],
           "source": records.get(data, "source"),
           "fetched": records.get(data, "fetched"),
           "report_date": records.get(data, "report_date")}
    for symbol in symbols:
        instrument = (instruments or {}).get(symbol)
        key, reason = contract_for(symbol, instrument)
        if not key:
            out["unmapped"].append({"symbol": symbol, "reason": reason})
            continue
        entry, problem = report(data, key)
        if not entry:
            out["unmapped"].append({"symbol": symbol, "reason": problem})
            continue
        out["reports"].append(dict(entry, symbol=symbol, mapped_because=reason))
    return out
