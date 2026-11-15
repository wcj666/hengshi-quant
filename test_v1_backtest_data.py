"""Backtest input regressions. All fixtures are synthetic."""
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from strategy_setup import build_setup
from v1_backtest_data import MINUTE_ENDS, demo_bundle, load_bundle, validate_bundle


class BacktestInputTests(unittest.TestCase):
    def test_demo_contains_a_v1_signal_and_complete_execution_minutes(self):
        bundle = validate_bundle(demo_bundle())
        security = bundle["securities"]["600000"]
        review = security["reviews"]["2024-06-14"]
        setup = build_setup(
            security["daily"], bundle["benchmark"], name=review["name"],
            now=datetime.fromisoformat("2024-06-14T15:05:00+08:00"),
        )
        self.assertTrue(bundle["demo"])
        self.assertTrue(setup["eligible"], setup["blockers"])
        self.assertEqual(setup["signal_date"], "2024-06-14")
        self.assertEqual(
            tuple(bar["time"] for bar in security["sessions"]["2024-06-17"]["minutes"]), MINUTE_ENDS,
        )
        self.assertEqual(len(MINUTE_ENDS), 240)

    def test_missing_history_remains_visible_to_replay(self):
        bundle = demo_bundle()
        sessions = bundle["securities"]["600000"]["sessions"]
        del sessions["2024-06-18"]
        sessions["2024-06-17"]["minutes"].pop()
        self.assertIs(validate_bundle(bundle), bundle)
        self.assertNotIn("2024-06-18", sessions)
        self.assertEqual(len(sessions["2024-06-17"]["minutes"]), 239)

    def test_rejects_malformed_candles_reviews_and_execution_limits(self):
        changes = (
            ("raw", lambda data, stock: data.update(price_basis="qfq")),
            ("calendar", lambda data, stock: data["calendar"].insert(1, data["calendar"][0])),
            ("最高", lambda data, stock: stock["daily"][0].update(high=0.01)),
            ("终", lambda data, stock: stock["sessions"]["2024-06-17"]["minutes"][0].update(time="12:01")),
            ("涨跌停", lambda data, stock: stock["sessions"]["2024-06-17"].update(upper_limit=1)),
            ("volume", lambda data, stock: stock["sessions"]["2024-06-17"]["minutes"][0].update(volume=True)),
            ("recorded_at", lambda data, stock: stock["reviews"]["2024-06-14"].update(recorded_at="2024-06-14T15:05:00")),
            ("sources", lambda data, stock: stock["reviews"]["2024-06-14"].update(sources=[])),
        )
        for label, change in changes:
            with self.subTest(label=label):
                bundle = demo_bundle()
                change(bundle, bundle["securities"]["600000"])
                with self.assertRaises(ValueError):
                    validate_bundle(bundle)

    def test_suspended_session_does_not_masquerade_as_open(self):
        bundle = demo_bundle()
        sessions = bundle["securities"]["600000"]["sessions"]
        sessions["2024-06-18"] = {"status": "suspended", "corporate_action_clear": True}
        validate_bundle(bundle)
        sessions["2024-06-18"]["open"] = 12.2
        with self.assertRaisesRegex(ValueError, "停牌"):
            validate_bundle(bundle)

    def test_load_preserves_byte_digest_and_rejects_duplicate_fields(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "input.json"
            bundle = demo_bundle()
            payload = json.dumps(bundle, ensure_ascii=False, indent=2).encode()
            path.write_bytes(payload)
            loaded, digest = load_bundle(path)
            self.assertEqual(loaded, bundle)
            self.assertEqual(digest, sha256(payload).hexdigest())
            path.write_text('{"version": 1, "version": 2}')
            with self.assertRaisesRegex(ValueError, "重复"):
                load_bundle(path)


if __name__ == "__main__":
    unittest.main()
