"""Validate Tencent archives without making live requests."""
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from v1_tencent_archive import REGULAR_LABELS, capture_session, write_archive


SYMBOL = "sh600000"
DAY = "2026-11-13"
AFTER_CLOSE = datetime.fromisoformat("2026-11-13T07:10:00+00:00")


def minute_payload(symbol=SYMBOL, *, auction=True):
    def candle(stamp):
        return [stamp, "10.19", "10.17", "10.19", "10.17", "3087.00", {}, "0.0927"]

    labels = ("0930", *REGULAR_LABELS) if auction else REGULAR_LABELS
    rows = [candle("202611121500"), *(candle("20261113" + label) for label in labels)]
    return {"code": 0, "data": {symbol: {"m1": rows, "prec": 2}}}


class TencentArchiveTests(unittest.TestCase):
    def test_saves_complete_raw_response_and_shanghai_timestamp(self):
        payload = minute_payload()
        with TemporaryDirectory() as folder:
            target = Path(folder) / "archive" / "session.json"
            archive = capture_session(DAY, [SYMBOL], target, fetch=lambda symbol: payload, now=AFTER_CLOSE)
            self.assertEqual(json.loads(target.read_text()), archive)
            self.assertEqual(target.stat().st_mode & 0o077, 0)
        self.assertEqual(archive["raw_responses"][SYMBOL], payload)
        self.assertEqual(archive["captured_at"], "2026-11-13T15:10:00+08:00")
        self.assertEqual(archive["volume_unit"], "provider_unverified")
        self.assertEqual(archive["session_summary"][SYMBOL], {"regular_rows": 240, "auction_0930": True})

    def test_accepts_complete_requested_day_when_newer_partial_day_is_present(self):
        payload = minute_payload(auction=False)
        rows = payload["data"][SYMBOL]["m1"]
        rows.append(["202611160931", *rows[-1][1:]])
        with TemporaryDirectory() as folder:
            target = Path(folder) / "session.json"
            now = datetime.fromisoformat("2026-11-16T09:32:00+08:00")
            archive = capture_session(DAY, [SYMBOL], target, fetch=lambda symbol: payload, now=now)
        self.assertEqual(archive["session_date"], DAY)
        self.assertEqual(archive["raw_responses"][SYMBOL], payload)
        self.assertEqual(archive["session_summary"][SYMBOL], {"regular_rows": 240, "auction_0930": False})

    def test_missing_duplicate_wrong_date_and_invalid_prices_never_publish(self):
        def missing(rows):
            rows.pop(16)

        def duplicate(rows):
            rows.insert(16, rows[16])

        def wrong_date(rows):
            rows[:] = [["20261116" + row[0][8:], *row[1:]] for row in rows[1:]]

        def invalid_price(rows):
            rows[-1][3] = "0.12"

        for change in (missing, duplicate, wrong_date, invalid_price):
            with self.subTest(change=change.__name__), TemporaryDirectory() as folder:
                bad = minute_payload("sz159919", auction=False)
                change(bad["data"]["sz159919"]["m1"])
                fetch = Mock(side_effect=[minute_payload(), bad])
                target = Path(folder) / "session.json"
                with self.assertRaisesRegex(ValueError, "sz159919"):
                    capture_session(DAY, [SYMBOL, "sz159919"], target, fetch=fetch, now=AFTER_CLOSE)
                self.assertFalse(target.exists())

    def test_invalid_arguments_and_early_capture_skip_requests(self):
        early = datetime.fromisoformat("2026-11-13T14:59:59+08:00")
        with TemporaryDirectory() as folder:
            target = Path(folder) / "session.json"
            for day, symbols, now in ((DAY, [SYMBOL], early), ("2026-11-14", [SYMBOL], AFTER_CLOSE),
                                      (DAY, ["600000"], AFTER_CLOSE), (DAY, [SYMBOL, SYMBOL], AFTER_CLOSE)):
                with self.subTest(day=day, symbols=symbols, now=now):
                    fetch = Mock()
                    with self.assertRaises(ValueError):
                        capture_session(day, symbols, target, fetch=fetch, now=now)
                    fetch.assert_not_called()

    def test_existing_archive_is_preserved_even_during_publication(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / "session.json"
            write_archive(target, {"first": True})
            fetch = Mock()
            with self.assertRaises(FileExistsError):
                capture_session(DAY, [SYMBOL], target, fetch=fetch, now=AFTER_CLOSE)
            fetch.assert_not_called()
            with self.assertRaises(FileExistsError):
                write_archive(target, {"second": True})
            self.assertEqual(json.loads(target.read_text()), {"first": True})
            self.assertEqual(list(target.parent.iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
