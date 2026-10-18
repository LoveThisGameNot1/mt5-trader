import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import state


class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = state.Store(pathlib.Path(self.tmp.name) / "state")

    def test_atomic_write_leaves_no_temp_file(self):
        state.atomic_write_json(self.store.state_dir / "x.json", {"a": 1})
        leftovers = list(self.store.state_dir.glob("*.tmp*"))
        self.assertEqual(leftovers, [])
        self.assertEqual(json.loads((self.store.state_dir / "x.json").read_text()), {"a": 1})

    def test_missing_file_gives_the_default(self):
        self.assertEqual(state.read_json(self.store.state_dir / "nope.json", {"d": 1}),
                         {"d": 1})

    def test_corrupt_state_is_not_fatal(self):
        (self.store.state_dir / "state.json").write_text("{not json")
        self.assertEqual(self.store.load()["day"], None)

    def test_state_survives_a_restart(self):
        """The daily counters must not reset because the server was restarted."""
        self.store.mutate(lambda s: s.update({"day": "2026-10-18", "orders_today": 7}) or s)
        fresh = state.Store(self.store.state_dir)
        self.assertEqual(fresh.load()["orders_today"], 7)
        self.assertEqual(fresh.load()["day"], "2026-10-18")

    def test_unknown_keys_are_preserved_by_mutate(self):
        self.store.mutate(lambda s: s.update({"custom": "kept"}) or s)
        self.assertEqual(self.store.load()["custom"], "kept")

    def test_events_append_and_are_readable_in_order(self):
        self.store.event("a", {"n": 1})
        self.store.event("b", {"n": 2})
        kinds = [e["kind"] for e in self.store.events()]
        self.assertEqual(kinds, ["a", "b"])
        self.assertEqual([e["kind"] for e in self.store.events(kind="b")], ["b"])

    def test_event_survives_a_broken_line(self):
        self.store.event("a")
        with open(self.store.journal_dir / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write("this is not json\n")
        self.store.event("c")
        self.assertEqual([e["kind"] for e in self.store.events()], ["a", "c"])

    def test_intents_round_trip_and_unresolved_filter(self):
        self.store.save_intent({"intent_id": "i1", "status": "sent"})
        self.store.save_intent({"intent_id": "i2", "status": "executed"})
        self.store.save_intent({"intent_id": "i3", "status": "ambiguous"})
        unresolved = {i["intent_id"] for i in self.store.unresolved_intents()}
        self.assertEqual(unresolved, {"i1", "i3"})

    def test_intent_update_merges(self):
        self.store.save_intent({"intent_id": "i1", "status": "sent", "symbol": "EURUSD"})
        self.store.update_intent("i1", status="executed")
        record = self.store.load_intent("i1")
        self.assertEqual(record["status"], "executed")
        self.assertEqual(record["symbol"], "EURUSD")

    def test_plans_round_trip(self):
        self.store.save_plan({"plan_id": "p1", "fingerprint": "abc"})
        self.assertEqual(self.store.load_plan("p1")["fingerprint"], "abc")
        self.assertIsNone(self.store.load_plan("nope"))
        self.assertEqual(len(self.store.list_plans()), 1)

    def test_ids_are_unique(self):
        ids = {state.new_id("x") for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(i.startswith("x-") for i in ids))


if __name__ == "__main__":
    unittest.main()
