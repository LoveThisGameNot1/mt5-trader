import collections
import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import records

Rec = collections.namedtuple("Rec", ["a", "b", "c"])


class FieldAccess(unittest.TestCase):
    def test_reads_dicts_and_records_alike(self):
        record = Rec(a=1, b=None, c=3)
        self.assertEqual(records.get(record, "a"), 1)
        self.assertEqual(records.get({"a": 1}, "a"), 1)

    def test_missing_field_returns_the_default_not_an_attribute_error(self):
        """A terminal build without trade_tick_value_profit must not kill a loop."""
        self.assertIsNone(records.get(Rec(a=1, b=2, c=3), "nope"))
        self.assertEqual(records.get(Rec(a=1, b=2, c=3), "nope", 7), 7)

    def test_none_field_falls_back_to_the_default(self):
        self.assertEqual(records.get(Rec(a=None, b=1, c=2), "a", "fallback"), "fallback")

    def test_fnum_rejects_nan_and_infinity(self):
        """MT5 does return both, on a symbol that has never traded."""
        self.assertIsNone(records.fnum(float("nan")))
        self.assertIsNone(records.fnum(float("inf")))
        self.assertEqual(records.fnum("1.5"), 1.5)
        self.assertEqual(records.fnum("abc", 0.0), 0.0)

    def test_inum(self):
        self.assertEqual(records.inum("42"), 42)
        self.assertIsNone(records.inum(None))
        self.assertEqual(records.inum(None, 3), 3)

    def test_as_dict_keeps_only_known_fields(self):
        out = records.as_dict({"a": 1, "b": 2, "c": 3}, ["a", "c", "d"])
        self.assertEqual(out, {"a": 1, "c": 3})

    def test_as_dict_drops_fields_that_are_none(self):
        out = records.as_dict({"a": 1, "b": None}, ["a", "b"])
        self.assertEqual(out, {"a": 1})

    def test_to_jsonable_handles_datetimes_and_records(self):
        payload = {"t": datetime.datetime(2026, 1, 2, 3, 4, 5), "r": Rec(a=1, b=2, c=3),
                   "n": float("nan"), "l": (1, 2)}
        out = records.to_jsonable(payload)
        self.assertEqual(out["t"], "2026-01-02T03:04:05")
        self.assertEqual(out["r"], {"a": 1, "b": 2, "c": 3})
        self.assertIsNone(out["n"])
        self.assertEqual(out["l"], [1, 2])

    def test_iso_time_labels_server_clock_as_utc(self):
        """MT5 epochs already carry the server's wall clock, so labelling them Z
        reproduces what the terminal's own History tab shows."""
        self.assertEqual(records.iso_time(0), "1970-01-01T00:00:00Z")
        self.assertIsNone(records.iso_time(None))

    def test_dumps_is_plain_json(self):
        import json
        text = records.dumps({"a": float("nan"), "b": [1, 2]})
        self.assertEqual(json.loads(text), {"a": None, "b": [1, 2]})


if __name__ == "__main__":
    unittest.main()
