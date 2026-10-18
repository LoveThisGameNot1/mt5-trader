"""Download and normalise the CFTC Commitments of Traders report.

This is the only part of the project that touches the network, and it is
deliberately not the server. A trade-path decision must not depend on a website
being up, so this script is run by a person or a scheduled task, writes one JSON
file, and the server reads that file.

    python tools/fetch_cot.py                     # current year and two before it
    python tools/fetch_cot.py --years 5
    python tools/fetch_cot.py --file annual.txt   # parse a file you already have
    python tools/fetch_cot.py --out state/cot.json

Source: https://www.cftc.gov/files/dea/history/deacot{year}.zip  (legacy
futures-only report, which is where the currency futures live).

Column positions are read from the file's own header rather than hard-coded. The
header has changed before and will change again, and a silent column shift is
exactly the kind of bug that produces confident wrong numbers.
"""

import argparse
import csv
import datetime
import io
import json
import pathlib
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mt5trader.cot import CONTRACTS          # noqa: E402

URL = "https://www.cftc.gov/files/dea/history/deacot{year}.zip"

# Header text as the CFTC writes it, mapped to the field we want.
WANTED = {
    "name": "Market and Exchange Names",
    "date": "As of Date in Form YYYY-MM-DD",
    "open_interest": "Open Interest (All)",
    "noncommercial_long": "Noncommercial Positions-Long (All)",
    "noncommercial_short": "Noncommercial Positions-Short (All)",
    "noncommercial_spreading": "Noncommercial Positions-Spreading (All)",
    "commercial_long": "Commercial Positions-Long (All)",
    "commercial_short": "Commercial Positions-Short (All)",
    "change_open_interest": "Change in Open Interest (All)",
    "change_noncommercial_long": "Change in Noncommercial-Long (All)",
    "change_noncommercial_short": "Change in Noncommercial-Short (All)",
}

ALIASES = {}
for key, names in CONTRACTS.items():
    for name in names:
        ALIASES[name] = key


def index_map(header):
    positions = {}
    for field, label in WANTED.items():
        try:
            positions[field] = header.index(label)
        except ValueError:
            raise SystemExit(
                "the CFTC header no longer has a column called %r. The file format "
                "changed; fix WANTED in %s before trusting any number from it."
                % (label, __file__))
    return positions


def number(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse(text, markets):
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    positions = index_map(header)
    rows = 0
    for row in reader:
        if len(row) < len(header):
            continue
        name = row[positions["name"]].strip()
        key = ALIASES.get(name)
        if key is None:
            continue
        date = row[positions["date"]].strip()
        if not date:
            continue
        long_spec = number(row[positions["noncommercial_long"]])
        short_spec = number(row[positions["noncommercial_short"]])
        long_comm = number(row[positions["commercial_long"]])
        short_comm = number(row[positions["commercial_short"]])
        entry = {
            "date": date,
            "open_interest": number(row[positions["open_interest"]]),
            "noncommercial_long": long_spec,
            "noncommercial_short": short_spec,
            "noncommercial_spreading": number(row[positions["noncommercial_spreading"]]),
            "commercial_long": long_comm,
            "commercial_short": short_comm,
            "net": (long_spec - short_spec) if long_spec is not None
                   and short_spec is not None else None,
            "commercial_net": (long_comm - short_comm) if long_comm is not None
                              and short_comm is not None else None,
        }
        market = markets.setdefault(key, {"name": name, "series": {}})
        market["name"] = name
        market["series"][date] = entry
        rows += 1
    return rows


def download(year, timeout=60):
    url = URL.format(year=year)
    request = urllib.request.Request(url, headers={"User-Agent": "mt5-trader/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        blob = response.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        name = next((n for n in archive.namelist() if n.lower().endswith(".txt")), None)
        if name is None:
            raise SystemExit("%s has no .txt inside it" % url)
        return archive.read(name).decode("utf-8", "replace"), url


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--years", type=int, default=3,
                        help="how many annual files to fetch, ending with this year")
    parser.add_argument("--out", help="where to write (default: <root>/state/cot.json)")
    parser.add_argument("--file", help="parse a local annual.txt or .zip instead of "
                                       "downloading")
    parser.add_argument("--state-dir", help="where state lives, for the default --out")
    args = parser.parse_args(argv)

    markets = {}
    sources = []
    this_year = datetime.date.today().year

    if args.file:
        path = pathlib.Path(args.file)
        if not path.is_file():
            raise SystemExit("no such file: %s" % path)
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                inner = next((n for n in archive.namelist()
                              if n.lower().endswith(".txt")), None)
                text = archive.read(inner).decode("utf-8", "replace")
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        rows = parse(text, markets)
        sources.append(str(path))
        print("parsed %d rows from %s" % (rows, path))
    else:
        for year in range(this_year - max(0, args.years - 1), this_year + 1):
            try:
                text, url = download(year)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                print("  %d: could not fetch (%s)" % (year, exc))
                continue
            rows = parse(text, markets)
            sources.append(url)
            print("  %d: %d rows" % (year, rows))

    if not markets:
        raise SystemExit("nothing was parsed. Check the year range and the network.")

    out = []
    for key, market in markets.items():
        series = [market["series"][date] for date in sorted(market["series"])]
        entry = {"name": market["name"], "series": series,
                 "first_week": series[0]["date"], "last_week": series[-1]["date"],
                 "weeks": len(series)}
        out.append((key, entry))

    payload = {
        "source": sources,
        "source_note": ("CFTC legacy futures-only report, the annual file per year. "
                        "Non-commercial is large speculators, commercial is hedgers."),
        "fetched": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "report_date": max(entry["last_week"] for _, entry in out),
        "markets": {key: entry for key, entry in sorted(out)},
    }

    if args.out:
        target = pathlib.Path(args.out)
    else:
        state = pathlib.Path(args.state_dir) if args.state_dir else ROOT / "state"
        target = state / "cot.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\nwrote %s" % target)
    print("%d contracts, report date %s" % (len(payload["markets"]), payload["report_date"]))
    for key, entry in sorted(out):
        latest = entry["series"][-1]
        print("  %-5s %-6s net %+9s  (%s weeks)"
              % (key, latest["date"], latest["net"], entry["weeks"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
