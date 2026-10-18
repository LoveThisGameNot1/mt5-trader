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


class NumpyStructuredRecords(unittest.TestCase):
    """When numpy is installed the MetaTrader5 package returns copy_rates_* and
    copy_ticks_* as a numpy structured array, and one row of that is a numpy.void.
    Its fields are in the dtype, invisible to ``dir()``, and ``getattr(row,
    "close")`` returns nothing -- so a bar arrives with no close price and the
    only symptom is a KeyError several layers away.
    """

    @classmethod
    def setUpClass(cls):
        import importlib
        cls.np = importlib.import_module("numpy")

    def setUp(self):
        self.dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"),
                      ("close", "<f8"), ("tick_volume", "<u8"), ("spread", "<i4"),
                      ("real_volume", "<u8")]
        self.rows = self.np.array(
            [(1792324800, 81731.78, 82295.46, 81731.78, 81931.32, 2601, 1, 0),
             (1792339200, 81931.32, 82400.00, 81800.00, 82100.55, 3100, 1, 0)],
            dtype=self.dtype)
        self.row = self.rows[0]

    def test_dir_does_not_advertise_the_fields_which_is_the_whole_problem(self):
        self.assertNotIn("close", dir(self.row))

    def test_getattr_on_a_void_returns_nothing(self):
        self.assertIsNone(getattr(self.row, "close", None))

    def test_get_reads_through_the_dtype(self):
        self.assertAlmostEqual(records.get(self.row, "close"), 81931.32)

    def test_as_dict_keeps_the_named_fields(self):
        out = records.as_dict(self.row, ["time", "open", "high", "low", "close", "spread"])
        self.assertEqual(set(out), {"time", "open", "high", "low", "close", "spread"})
        self.assertAlmostEqual(out["close"], 81931.32)
        self.assertAlmostEqual(out["high"], 82295.46)

    def test_numpy_integers_become_real_integers_not_strings(self):
        out = records.as_dict(self.row, ["time", "tick_volume"])
        self.assertIsInstance(out["time"], int)
        self.assertIsInstance(out["tick_volume"], int)
        self.assertEqual(out["time"], 1792324800)

    def test_a_record_is_json_serialisable(self):
        import json
        text = json.dumps(records.as_dict(self.row))
        self.assertIn("81931.32", text)

    def test_a_structured_array_is_iterated_not_treated_as_one_record(self):
        """Only the scalar is a record. The array is a sequence of records, and
        reading it as one record returns a dict of whole columns."""
        out = records.to_jsonable(self.rows)
        self.assertIsInstance(out, list)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0]["close"], 81931.32)
        self.assertAlmostEqual(out[1]["close"], 82100.55)

    def test_the_whole_array_survives_a_round_trip(self):
        import json
        payload = json.loads(records.dumps(self.rows))
        self.assertEqual(len(payload), 2)
        self.assertEqual(set(payload[0]), {name for name, _ in self.dtype})


if __name__ == "__main__":
    unittest.main()
