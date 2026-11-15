import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from v1_backtest import run_backtest
from v1_backtest_data import demo_bundle
from v1_validation import PERIOD_KEYS, check_windows, evaluate, save_comparison


class ChronologicalEvaluationTests(TestCase):
    def setUp(self):
        self.bundle = demo_bundle()
        self.calendar = self.bundle["calendar"]
        self.windows = {key: (day, day) for key, day in zip(PERIOD_KEYS, self.calendar[-3:])}

    def test_windows_reject_overlap_reverse_or_dates_outside_calendar(self):
        development_day = self.windows["development"][0]
        for replacement in (
            {"validation": (development_day, development_day)},
            {"holdout": (self.calendar[-1], development_day)},
            {"holdout": ("1900-01-01", "1900-01-02")},
        ):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                evaluate(self.bundle, {**self.windows, **replacement})

    def test_intervening_trading_days_are_counted(self):
        windows = {**self.windows, "development": (self.calendar[-5], self.calendar[-5])}
        _, gaps = check_windows(windows, self.calendar)
        self.assertEqual(gaps, [{
            "after": self.calendar[-5], "before": self.calendar[-2],
            "start": self.calendar[-4], "end": self.calendar[-3], "session_count": 2,
        }])

    def test_identical_provenance_and_cash_without_carrying_positions(self):
        result = evaluate(self.bundle, self.windows, initial_cash=200000, bundle_sha256="a" * 64)
        meta = result["meta"]
        self.assertTrue(meta["demo"])
        self.assertEqual(meta["bundle_sha256"], "a" * 64)
        self.assertEqual(meta["initial_cash"], 200000)
        for key, report in result["reports"].items():
            self.assertEqual(report["meta"]["evaluation_period"], key)
            for field in ("initial_cash", "engine_sha256", "bundle_sha256", "rules", "costs"):
                self.assertEqual(report["meta"][field], meta[field])
        first, second, third = result["periods"]
        self.assertEqual(first["open_position_count"], 1)
        for period in (second, third):
            self.assertEqual(period["summary"]["fill_count"], 0)
            self.assertEqual(period["open_position_count"], 0)
            self.assertEqual(period["summary"]["end_equity"], 200000)
        self.assertTrue(all(p["input_coverage"] for p in result["periods"]))
        self.assertTrue(all(any("样本偏少" in warning for warning in p["warnings"]) for p in result["periods"]))
        self.assertTrue(any("幸存者偏差" in warning for warning in meta["warnings"]))

    def test_coverage_gaps_are_visible_even_without_fills(self):
        code = next(iter(self.bundle["securities"]))
        self.bundle["securities"][code]["sessions"].pop(self.windows["development"][0])
        result = evaluate(self.bundle, self.windows)
        first = result["periods"][0]
        self.assertFalse(first["summary"]["execution_coverage_complete"])
        self.assertEqual(first["input_coverage"]["missing_sessions"], 1)
        self.assertTrue(any("执行证据不完整" in warning for warning in first["warnings"]))

    def test_cross_period_rule_change_is_rejected(self):
        def altered(bundle, start, end, initial_cash, *, bundle_sha256):
            report = run_backtest(bundle, start, end, initial_cash, bundle_sha256=bundle_sha256)
            if start == self.windows["validation"][0]:
                report["meta"]["costs"]["minimum_commission"] += 1
            return report

        with patch("v1_validation.run_backtest", side_effect=altered), self.assertRaisesRegex(ValueError, "不一致"):
            evaluate(self.bundle, self.windows)

    def test_new_publication_keeps_previous_run_reports(self):
        result = evaluate(self.bundle, self.windows)
        next_result = evaluate(self.bundle, self.windows, initial_cash=200000)
        with TemporaryDirectory() as folder:
            out = Path(folder)
            comparison = save_comparison(result, out)
            first = json.loads(comparison.read_bytes())
            self.assertEqual(set(first), {"meta", "periods"})
            first_run = out / "runs" / first["meta"]["run_id"]
            self.assertRegex(first_run.name, r"^[0-9a-f]{32}$")
            files = {"result.json", "fills.csv", "closed_trades.csv", "equity_curve.csv", "decisions.csv"}
            for key in PERIOD_KEYS:
                self.assertEqual({file.name for file in (first_run / key).iterdir()}, files)
            original = {file: file.read_bytes() for file in first_run.glob("*/*")}
            self.assertEqual(save_comparison(next_result, out), comparison)
            second = json.loads(comparison.read_bytes())
            second_run = out / "runs" / second["meta"]["run_id"]
            self.assertNotEqual(first_run, second_run)
            self.assertEqual({file: file.read_bytes() for file in first_run.glob("*/*")}, original)
            self.assertEqual({file.name for file in (second_run / "development").iterdir()}, files)
            next_development = json.loads((second_run / "development" / "result.json").read_bytes())
            self.assertEqual(next_development["meta"]["initial_cash"], 200000)

    def test_failed_staging_keeps_previous_comparison_and_run(self):
        result = evaluate(self.bundle, self.windows)
        with TemporaryDirectory() as folder:
            out = Path(folder)
            comparison = save_comparison(result, out)
            original = comparison.read_bytes()
            first_run = out / "runs" / json.loads(original)["meta"]["run_id"]
            development = (first_run / "development" / "result.json").read_bytes()

            def fail_after_first(report, path):
                if path.name == "validation":
                    raise OSError("cannot write report")
                path.mkdir()
                (path / "result.json").write_text("{}")

            with patch("v1_validation.save_report", side_effect=fail_after_first), self.assertRaises(OSError):
                save_comparison(result, out)
            self.assertEqual(comparison.read_bytes(), original)
            self.assertEqual((first_run / "development" / "result.json").read_bytes(), development)
            self.assertEqual(list((out / "runs").iterdir()), [first_run])


if __name__ == "__main__":
    main()
