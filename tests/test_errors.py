import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from mt5trader import errors


class RetcodeClassification(unittest.TestCase):
    def test_requote_is_retryable_not_fatal(self):
        self.assertEqual(errors.classify(10004)[1], errors.RETRY)

    def test_price_changed_and_off_are_retryable(self):
        for code in (10020, 10021):
            self.assertEqual(errors.classify(code)[1], errors.RETRY, code)

    def test_unsupported_filling_mode_is_retryable(self):
        """10030 is a retry with another filling mode, not an error. Treating it
        as fatal is why so many MT5 bots fail on their first market order."""
        self.assertEqual(errors.classify(10030)[1], errors.RETRY)

    def test_timeout_is_ambiguous_never_retryable(self):
        self.assertEqual(errors.classify(10012)[1], errors.AMBIGUOUS)

    def test_no_result_is_ambiguous(self):
        self.assertEqual(errors.classify(None)[1], errors.AMBIGUOUS)

    def test_invalid_stops_is_fatal(self):
        for code in (10016, 10014, 10015, 10018, 10019, 10027, 10040):
            self.assertEqual(errors.classify(code)[1], errors.FATAL, code)

    def test_success_codes(self):
        for code in (10008, 10009, 10010):
            self.assertEqual(errors.classify(code)[1], errors.OK, code)

    def test_unknown_code_is_fatal_not_silently_ok(self):
        name, kind, _, advice = errors.classify(10999)
        self.assertEqual(kind, errors.FATAL)
        self.assertIn("UNKNOWN", name)
        self.assertIn("journal", advice)

    def test_every_documented_code_has_text_and_advice(self):
        for code, entry in errors.RETCODES.items():
            self.assertEqual(len(entry), 4, code)
            self.assertTrue(entry[2] and entry[3], code)

    def test_client_error_auto_trading_disabled_is_explained(self):
        info = errors.explain_client(-8)
        self.assertEqual(info["name"], "AUTO_TRADING_DISABLED")
        self.assertIn("Algo Trading", info["advice"])

    def test_render_is_one_line(self):
        text = errors.render_retcode(10016)
        self.assertIn("INVALID_STOPS", text)
        self.assertIn("FATAL", text)
        self.assertNotIn("\n", text)


if __name__ == "__main__":
    unittest.main()
