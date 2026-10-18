import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import cot, instruments
from tests import harness


def week(date, long_spec, short_spec, oi=100000, long_comm=40000, short_comm=60000):
    return {"date": date, "open_interest": oi, "noncommercial_long": long_spec,
            "noncommercial_short": short_spec, "noncommercial_spreading": 0,
            "commercial_long": long_comm, "commercial_short": short_comm,
            "net": long_spec - short_spec, "commercial_net": long_comm - short_comm}


def write_cot(folder, series, key="EUR", name="EURO FX - CHICAGO MERCANTILE EXCHANGE"):
    """Write where cot.path_for looks: <root>/state/cot.json."""
    folder = pathlib.Path(folder) / "state"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "cot.json"
    payload = {
        "source": ["https://www.cftc.gov/files/dea/history/deacot2026.zip"],
        "fetched": "2026-10-18T00:00:00Z",
        "report_date": series[-1]["date"],
        "markets": {key: {"name": name, "series": series,
                          "first_week": series[0]["date"],
                          "last_week": series[-1]["date"], "weeks": len(series)}},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class Mapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)

    def load(self, name):
        return instruments.load(self.h.bridge, self.h.settings, name)[0]

    def test_a_currency_pair_maps_to_its_base_contract(self):
        key, why = cot.contract_for("EURUSD", self.load("EURUSD"))
        self.assertEqual(key, "EUR")
        self.assertIn("base currency", why)

    def test_gold_and_silver_map_to_their_own_contracts(self):
        self.assertEqual(cot.contract_for("XAUUSD", self.load("XAUUSD"))[0], "XAU")

    def test_an_index_maps_by_its_symbol_name(self):
        key, why = cot.contract_for("US30", self.load("US30"))
        self.assertEqual(key, "DJIA")
        self.assertIn("US30", why)

    def test_a_european_index_is_refused_with_the_reason(self):
        key, why = cot.contract_for("DE40Cash")
        self.assertIsNone(key)
        self.assertIn("American futures only", why)

    def test_a_cross_has_no_contract_of_its_own(self):
        instrument = instruments.build({"name": "EURGBP", "currency_base": "EUR",
                                        "currency_profit": "GBP"})
        key, why = cot.contract_for("EURGBP", instrument)
        self.assertEqual(key, "EUR")

    def test_an_unmapped_instrument_says_so(self):
        instrument = instruments.build({"name": "WOBBLE", "currency_base": "XXX",
                                        "currency_profit": "YYY"})
        key, why = cot.contract_for("WOBBLE", instrument)
        self.assertIsNone(key)
        self.assertIn("no CFTC contract", why)

    def test_every_mapped_alias_looks_like_a_cftc_name(self):
        for key, aliases in cot.CONTRACTS.items():
            for alias in aliases:
                self.assertIn(" - ", alias, key)


class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.series = [week("2026-%02d-01" % (index + 1), 100 + index * 5,
                            200 - index * 2, oi=1000)
                       for index in range(12)]

    def data(self):
        write_cot(self.tmp.name, self.series)
        settings = harness.build(self.tmp.name, env=harness.AUTONOMOUS).settings
        return cot.load(settings)[0]

    def test_the_latest_net_and_the_weekly_change(self):
        out, problem = cot.report(self.data(), "EUR")
        self.assertIsNone(problem)
        self.assertEqual(out["net_spec"], 100 + 11 * 5 - (200 - 11 * 2))
        self.assertEqual(out["report_date"], self.series[-1]["date"])
        self.assertEqual(out["long_change_week"], 5)
        self.assertEqual(out["short_change_week"], -2)

    def test_the_crowding_percentile_ranks_the_latest_against_the_window(self):
        out, _ = cot.report(self.data(), "EUR")
        self.assertEqual(out["net_percentile"], 100.0)
        self.assertIn("crowded long", out["crowding"])

    def test_a_middle_of_the_range_reading_is_not_called_an_extreme(self):
        # the series runs from a net of -100 to -23; -60 sits in the middle of it
        mixed = self.series + [week("2027-01-01", 140, 200, oi=1000)]
        write_cot(self.tmp.name, mixed)
        settings = harness.build(self.tmp.name, env=harness.AUTONOMOUS).settings
        out, _ = cot.report(cot.load(settings)[0], "EUR")
        self.assertIn("middle of the window", out["crowding"])
        self.assertGreater(out["net_percentile"], 25)
        self.assertLess(out["net_percentile"], 75)

    def test_the_zscore_is_reported_alongside(self):
        out, _ = cot.report(self.data(), "EUR")
        self.assertIsNotNone(out["net_zscore"])
        self.assertGreater(out["net_zscore"], 1)

    def test_the_window_can_be_shortened(self):
        short, _ = cot.report(self.data(), "EUR", weeks=4)
        self.assertEqual(short["weeks"], 4)
        self.assertEqual(short["window_from"], self.series[-4]["date"])

    def test_the_caveat_says_the_report_is_already_three_days_old(self):
        out, _ = cot.report(self.data(), "EUR")
        self.assertIn("three days old", out["caveat"])

    def test_a_contract_that_is_not_in_the_file_lists_what_is(self):
        out, problem = cot.report(self.data(), "JPY")
        self.assertIsNone(out)
        self.assertIn("EUR", problem)

    def test_a_short_series_gets_no_percentile_rather_than_a_bad_one(self):
        write_cot(self.tmp.name, self.series[:3])
        settings = harness.build(self.tmp.name, env=harness.AUTONOMOUS).settings
        out, _ = cot.report(cot.load(settings)[0], "EUR")
        self.assertIsNone(out["net_percentile"])
        self.assertEqual(out["crowding"], "unknown")


class Summary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = harness.build(self.tmp.name, env=harness.AUTONOMOUS)
        self.series = [week("2026-%02d-01" % (index + 1), 100 + index, 90 + index)
                       for index in range(12)]

    def test_a_missing_file_names_the_fetch_tool(self):
        out = cot.summary(self.h.settings, ["EURUSD"])
        self.assertFalse(out["available"])
        self.assertIn("fetch_cot.py", out["reason"])

    def test_mapped_and_unmapped_symbols_are_separated(self):
        write_cot(self.tmp.name, self.series)
        by_symbol = {}
        for name in ("EURUSD", "US30", "DE40Cash"):
            try:
                by_symbol[name] = instruments.load(self.h.bridge, self.h.settings, name)[0]
            except Exception:
                by_symbol[name] = instruments.build({"name": name,
                                                     "currency_base": "DE",
                                                     "currency_profit": "EUR"})
        out = cot.summary(self.h.settings, ["EURUSD", "US30", "DE40Cash"],
                          by_symbol)
        self.assertTrue(out["available"])
        self.assertEqual([r["symbol"] for r in out["reports"]], ["EURUSD"])
        unmapped = {u["symbol"] for u in out["unmapped"]}
        self.assertIn("US30", unmapped)      # DJIA is not in this fixture file
        self.assertIn("DE40Cash", unmapped)
        self.assertEqual(out["report_date"], self.series[-1]["date"])


if __name__ == "__main__":
    unittest.main()
