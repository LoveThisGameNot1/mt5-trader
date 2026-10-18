"""Correlation between symbols, and the cluster risk it exposes.

The kernel counts money at risk per position. It does not, by itself, notice that
being long EURUSD and long GBPUSD is one trade with a doubled position size, and
that is the gap this closes.

Returns are correlated, not price levels: two series that both trend upward have a
price-level correlation near 1 while having nothing to do with each other, which is
the oldest way to produce a meaningless number. Returns are also aligned on the
intersection of bar timestamps, so an index that closed for a day is not compared
bar-for-bar across the hole.

Correlation is reported with the number of points it was computed from, and it is
not computed at all below a floor. With twenty observations a coefficient of 0.8
means almost nothing, and a risk gate that fires on noise is worse than none.
"""

from . import market

DEFAULT_TIMEFRAME = "D1"
DEFAULT_BARS = 120
MIN_POINTS = 20


def matrix(bridge, symbols, timeframe=DEFAULT_TIMEFRAME, count=DEFAULT_BARS,
           threshold=0.8, min_points=MIN_POINTS):
    """Pairwise return correlation for a set of symbols."""
    symbols = [str(s) for s in symbols]
    series, missing = {}, []
    for symbol in symbols:
        rows = market.bars(bridge, symbol, timeframe, count, use_cache=True)
        if len(rows) < min_points + 1:
            missing.append({"symbol": symbol, "bars": len(rows)})
            continue
        series[symbol] = rows
    aligned, stamps = market.align_returns(series)
    points = max(0, len(stamps) - 1)

    correlations = {}
    pairs = []
    usable = sorted(series)
    for i, left in enumerate(usable):
        correlations.setdefault(left, {left: 1.0})
        for right in usable[i + 1:]:
            value = market.correlation(aligned[left], aligned[right])
            correlations.setdefault(right, {})[left] = (
                round(value, 4) if value is not None else None)
            correlations[left][right] = round(value, 4) if value is not None else None
            if value is not None:
                pairs.append({"left": left, "right": right, "r": round(value, 4),
                              "points": min(len(aligned[left]), len(aligned[right]))})
    pairs.sort(key=lambda p: abs(p["r"]), reverse=True)

    return {
        "symbols": usable,
        "timeframe": timeframe,
        "bars_requested": count,
        "aligned_points": points,
        "min_points": min_points,
        "correlations": correlations,
        "pairs": pairs,
        "clusters": clusters(pairs, threshold=threshold),
        "threshold": threshold,
        "missing": missing,
        "caveat": ("correlation is not causation and it is not stable: it is measured "
                   "on returns over the window above, and it changes with the regime. "
                   "Reproduce it before acting on it."),
    }


def clusters(pairs, threshold=0.8):
    """Groups of symbols whose absolute correlation reaches the threshold."""
    parent = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    links = []
    for pair in pairs:
        if abs(pair["r"]) >= threshold:
            union(pair["left"], pair["right"])
            links.append(pair)
    groups = {}
    for node in list(parent):
        groups.setdefault(find(node), set()).add(node)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        inside = [p for p in links if p["left"] in members and p["right"] in members]
        out.append({
            "symbols": sorted(members),
            "correlation": (round(sum(abs(p["r"]) for p in inside) / len(inside), 4)
                            if inside else None),
            "note": ("these move together: a position on each is one position with a "
                     "bigger size"),
        })
    out.sort(key=lambda c: len(c["symbols"]), reverse=True)
    return out


def exposure(bridge, new_symbol, others, timeframe=DEFAULT_TIMEFRAME, count=DEFAULT_BARS,
             min_points=MIN_POINTS):
    """Correlation of a proposed symbol against the symbols already open.

    Returns the coefficient per open symbol plus the ones that could not be
    measured, because "I could not check this" and "this is fine" must not look
    the same to the kernel.
    """
    others = [str(s) for s in others if str(s) != str(new_symbol)]
    out = {"new_symbol": new_symbol, "compared": [], "unverified": [],
           "timeframe": timeframe, "points": 0}
    if not others:
        return out
    series = {}
    for symbol in [new_symbol] + others:
        rows = market.bars(bridge, symbol, timeframe, count, use_cache=True)
        if len(rows) < min_points + 1:
            out["unverified"].append({"symbol": symbol, "bars": len(rows),
                                      "reason": "not enough history to correlate"})
            continue
        series[symbol] = rows
    if new_symbol not in series:
        return out
    aligned, stamps = market.align_returns(series)
    out["points"] = max(0, len(stamps) - 1)
    for symbol in others:
        if symbol not in series:
            continue
        value = market.correlation(aligned[new_symbol], aligned[symbol])
        if value is None:
            out["unverified"].append({"symbol": symbol, "reason": "no variance in the window"})
            continue
        out["compared"].append({"symbol": symbol, "r": round(value, 4),
                                "points": out["points"]})
    out["compared"].sort(key=lambda item: abs(item["r"]), reverse=True)
    return out
