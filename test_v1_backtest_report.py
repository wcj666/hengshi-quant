"""Check the report's financial arithmetic and incomplete-valuation behavior."""
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from v1_backtest_report import save_report, summarize


def day(date, equity, missing=None):
    return {"date": date, "equity": equity, "cash": 5000, "positions": 1, "missing_marks": missing or []}


class SummaryTests(unittest.TestCase):
    def test_mark_to_market_drawdown_includes_starting_cash(self):
        curve = [day("2026-01-05", 98000), day("2026-01-06", 103000), day("2026-01-07", 101000)]
        report = summarize(100000, curve, [], [], {}, [])
        self.assertTrue(report["valuations_complete"])
        self.assertEqual(report["net_pnl"], 1000)
        self.assertEqual(report["return_pct"], 1)
        self.assertAlmostEqual(report["annualized_return_pct"], ((1.01 ** (252 / 3)) - 1) * 100, places=3)
        self.assertEqual(report["max_drawdown"], 2000)
        self.assertEqual(report["max_drawdown_pct"], 2)

    def test_missing_mark_prevents_risk_metrics_without_filling_forward(self):
        curve = [day("2026-01-05", 98000, ["600406"]), day("2026-01-06", 101000)]
        report = summarize(100000, curve, [], [], None, [])
        self.assertFalse(report["valuations_complete"])
        self.assertEqual(report["missing_valuation_dates"], ["2026-01-05"])
        self.assertEqual(report["end_equity"], 101000)
        self.assertIsNone(report["max_drawdown"])
        self.assertIsNone(report["annualized_return_pct"])
        self.assertIsNone(report["turnover_pct"])
        report = summarize(100000, [day("2026-01-05", None)], [], [], None, [])
        self.assertIsNone(report["end_equity"])
        self.assertIsNone(report["net_pnl"])

    def test_costs_and_closed_trades_do_not_double_deduct_slippage(self):
        fills = [
            {"side": "buy", "type": "stock", "notional": 1000, "commission": 5, "stamp_tax": 0,
             "transfer": 0.01, "fees": 5.01, "slippage": 1.0},
            {"side": "sell", "type": "etf", "notional": 1100, "commission": 5, "stamp_tax": 0,
             "transfer": 0, "fees": 5, "slippage": 1.1},
        ]
        closed_trades = [
            {"type": "stock", "net_pnl": 100, "gross_pnl": 115, "net_r": 1},
            {"type": "stock", "net_pnl": -50, "gross_pnl": -43, "net_r": -.5},
            {"type": "etf", "net_pnl": -20, "gross_pnl": -15, "net_r": -.2},
            {"type": "stock", "net_pnl": 0, "gross_pnl": 5, "net_r": 0},
        ]
        report = summarize(100000, [day("2026-01-05", 101000)], fills, closed_trades, ["600406", "159971"],
                           [{"reason": "T+1"}, {"reason": "T+1"}])
        self.assertEqual(report["fee_total"], 10.01)
        self.assertEqual(report["slippage_total"], 2.1)
        self.assertEqual(report["estimated_execution_cost"], 12.11)
        self.assertEqual(report["return_pct"], 1)
        self.assertIsNone(report["annualized_return_pct"])
        self.assertEqual(report["realized_net_pnl"], 30)
        self.assertEqual(report["avg_net_r"], .075)
        self.assertEqual(report["win_rate_pct"], 25)
        self.assertEqual(report["win_loss_ratio"], 2.8571)
        self.assertEqual(report["max_consecutive_losses"], 2)
        self.assertEqual(report["by_asset_type"]["etf"]["closed_trade_count"], 1)
        self.assertEqual(report["rejection_counts"], {"T+1": 2})

    def test_export_avoids_spreadsheet_formulas_and_keeps_successful_prior_report(self):
        report = {"meta": {"source": "test fixture"}, "summary": {"end_equity": 10000}, "fills": [],
                  "closed_trades": [], "equity_curve": [day("2026-01-05", 10000)],
                  "decisions": [{"date": "2026-01-05", "code": "000001", "reason": "+test"}]}
        with TemporaryDirectory() as path:
            result = save_report(report, path)
            self.assertEqual(json.loads(result.read_text()), report)
            with (Path(path) / "decisions.csv").open(encoding="utf-8-sig", newline="") as file:
                [decision] = list(csv.DictReader(file))
                self.assertEqual(decision["reason"], "'+test")
                self.assertEqual(decision["code"], "000001")
            with self.assertRaises(ValueError):
                save_report({**report, "summary": {"end_equity": float("nan")}}, path)
            self.assertEqual(json.loads(result.read_text()), report)
            self.assertEqual(len(list(Path(path).glob("*.csv"))), 4)


if __name__ == "__main__":
    unittest.main()
