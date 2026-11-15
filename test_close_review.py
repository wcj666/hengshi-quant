import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from close_review import build_close_review
from web_dashboard import create_app


class CloseReviewTests(unittest.TestCase):
    def setUp(self):
        self.config = {"strategy_v1": {"max_position_pct": 15, "max_exposure_pct": 60,
                                       "max_positions": 4, "per_trade_risk": 150,
                                       "fee_reserve": 20}}
        self.snapshot = {"asof": "2026-11-13T15:07:00+08:00", "available_cash": 10000,
                         "withdrawable_cash": 3000,
                         "positions": [
                             {"code": "600001", "name": "弱势股", "type": "stock", "quantity": 500,
                              "available_quantity": 500, "cost": 20},
                             {"code": "600002", "name": "弱势ETF", "type": "etf", "quantity": 1000,
                              "available_quantity": 1000, "cost": 10},
                             {"code": "600003", "name": "中性股", "type": "stock", "quantity": 100,
                              "available_quantity": 100, "cost": 30},
                             {"code": "600004", "name": "强势股", "type": "stock", "quantity": 100,
                              "available_quantity": 100, "cost": 30},
                             {"code": "600005", "name": "第五只", "type": "stock", "quantity": 100,
                              "available_quantity": 100, "cost": 30},
                         ]}
        self.quotes = {
            "600001": {"price": 18, "previous_close": 20, "source_timestamp": "2026-11-13T07:00:00+00:00"},
            "600002": {"price": 9.5, "previous_close": 10, "source_timestamp": "2026-11-13T07:00:00+00:00"},
            "600003": {"price": 30, "previous_close": 30, "source_timestamp": "2026-11-13T07:00:00+00:00"},
            "600004": {"price": 31, "previous_close": 30, "source_timestamp": "2026-11-13T07:00:00+00:00"},
            "600005": {"price": 30, "previous_close": 30, "source_timestamp": "2026-11-13T07:00:00+00:00"},
        }

    def set_stop(self, code, stop):
        self.config.setdefault("trade_plans", {})[code] = {
            "stop_loss": stop, "entry_price": stop + 1, "take_profit_1": stop + 3,
            "take_profit_2": stop + 4,
        }

    def test_count_cap_prioritizes_concentration_and_cash_reserve_is_explicit(self):
        result = build_close_review(self.config, self.snapshot, self.quotes,
                                    data_verified=True)
        self.assertEqual(result["position_count"], 5)
        self.assertEqual(result["sell_plan"][0]["code"], "600002")
        self.assertGreaterEqual(result["cash_policy"]["minimum_reserve"], 15000)
        self.assertEqual(result["execution_status"], "可生成次日计划")

    def test_position_cap_is_lot_rounded(self):
        self.set_stop("600001", 19)
        result = build_close_review(self.config, self.snapshot, self.quotes,
                                    data_verified=True)
        row = next(x for x in result["sell_plan"] if x["code"] == "600002")
        self.assertEqual(row["quantity"] % 100, 0)
        self.assertIn("单只市值", row["reason"])

    def test_incomplete_data_blocks_execution(self):
        result = build_close_review(self.config, self.snapshot, {"600001": self.quotes["600001"]},
                                    data_verified=False)
        self.assertEqual(result["execution_status"], "仅供复盘，暂不可执行")
        self.assertTrue(result["errors"])

    def test_buy_requires_explicit_review_evidence(self):
        candidate = {"code": "600006", "name": "候选", "entry_price": 10,
                     "stop_loss": 9, "take_profit_1": 12, "take_profit_2": 13,
                     "setup_verified": False, "review_complete": False}
        result = build_close_review(self.config, self.snapshot, self.quotes, [candidate],
                                    data_verified=True)
        self.assertFalse(result["buy_plan"][0]["approved"])
        self.assertEqual(result["buy_plan"][0]["quantity"], 0)

    def test_unavailable_t1_quantity_is_reported_separately(self):
        self.set_stop("600001", 19)
        snapshot = {**self.snapshot, "positions": [
            {**row, "available_quantity": 0 if row["code"] == "600001" else row["available_quantity"]}
            for row in self.snapshot["positions"]
        ]}
        result = build_close_review(self.config, snapshot, self.quotes, data_verified=True)
        pending = next(item for item in result["pending_sell_plan"] if item["code"] == "600001")
        self.assertEqual(pending["quantity"], 500)
        self.assertEqual(pending["available_now"], 0)
        self.assertEqual(pending["trigger"], "stop")

    def test_cash_policy_exposes_immediate_and_full_plan_values(self):
        self.set_stop("600001", 19)
        snapshot = {**self.snapshot, "positions": [
            {**row, "available_quantity": 0 if row["code"] == "600001" else row["available_quantity"]}
            for row in self.snapshot["positions"]
        ]}
        result = build_close_review(self.config, snapshot, self.quotes, data_verified=True)
        policy = result["cash_policy"]
        self.assertGreaterEqual(policy["cash_after_all_planned_sales"], policy["cash_after_planned_sales"])
        self.assertIn("available_after_all_planned_sales", policy)
        self.assertEqual(policy["broker_available_cash"], 10000)
        self.assertEqual(policy["broker_withdrawable_cash"], 3000)
        self.assertEqual(policy["cash_after_planned_sales"], 10000 + policy["estimated_proceeds_sellable"])

    def test_fixed_stop_overrides_relative_price_strength_and_stale_data_blocks_it(self):
        self.set_stop("600004", 31)
        result = build_close_review(self.config, self.snapshot, self.quotes, data_verified=True)
        sale = result["sell_plan"][0]
        self.assertEqual((sale["code"], sale["trigger"], sale["quantity"]), ("600004", "stop", 100))
        self.assertGreater(self.quotes["600004"]["price"], self.quotes["600004"]["previous_close"])
        stale = build_close_review(self.config, self.snapshot, self.quotes, data_verified=False)
        holding = next(row for row in stale["positions"] if row["code"] == "600004")
        self.assertEqual(holding["fixed_exit"]["status"], "收盘行情待核验")
        self.assertEqual(stale["sell_plan"], [])
        self.assertIsNone(stale["cash_policy"]["estimated_proceeds_sellable"])

    def test_first_profit_uses_unsold_target_quantity_and_distinguishes_unknown_sellable(self):
        self.config["trade_plans"] = {"600001": {
            "entry_price": 16, "stop_loss": 15, "take_profit_1": 17, "take_profit_2": 20,
            "actual_entry": 16, "initial_quantity": 600, "tp1_sold_quantity": 150,
        }}
        row = self.snapshot["positions"][0]
        row.update(quantity=450, available_quantity=150)
        self.snapshot.update(positions=[row], available_cash=100000)
        result = build_close_review(self.config, self.snapshot, self.quotes, data_verified=True)
        self.assertEqual(result["sell_plan"][0]["quantity"], 100)
        self.assertEqual(result["pending_sell_plan"][0]["quantity"], 50)
        self.assertEqual(result["pending_sell_plan"][0]["trigger"], "take_profit_1")
        row.pop("available_quantity")
        unknown = build_close_review(self.config, self.snapshot, self.quotes, data_verified=True)
        self.assertEqual(unknown["sell_plan"], [])
        self.assertIsNone(unknown["pending_sell_plan"][0]["available_now"])

    def test_industry_cap_combines_manually_classified_etf_and_stock(self):
        self.snapshot["positions"] = self.snapshot["positions"][:4]
        self.snapshot["positions"][1].update(quantity=800, available_quantity=0)
        self.snapshot["positions"][2].update(quantity=200, available_quantity=200)
        self.snapshot["available_cash"] = 45000
        self.config["positions"] = [
            {"code": code, "sector": "半导体", "theme": "芯片"}
            for code in ("600001", "600002", "600003")
        ]
        self.config["trade_plans"] = {"600002": {"sector": "半导体"}}
        result = build_close_review(self.config, self.snapshot, self.quotes, data_verified=True)
        sector = result["sector_exposure"]["半导体"]
        self.assertIn("600002", sector["codes"])
        self.assertEqual(sector["value"], 22600)
        self.assertGreater(sector["pct"], 30)
        self.assertLessEqual(sector["pct_after_available"], 30)
        self.assertEqual(result["theme_exposure"]["芯片"], sector)
        self.assertEqual(result["unclassified_codes"], ["600004"])
        self.assertEqual([row["code"] for row in result["sell_plan"]], ["600001"])
        self.assertIn("行业敞口", result["sell_plan"][0]["reason"])

    def test_previous_day_quote_blocks_close_review_quantities_and_budget(self):
        feed = {}
        for position in self.snapshot["positions"]:
            code = position["code"]
            quote = self.quotes[code]
            stamp = "2026-11-12T07:00:00+00:00" if code == "600001" else quote["source_timestamp"]
            feed[(position["type"], code)] = (quote["price"], position["name"], quote["previous_close"], stamp)
        with TemporaryDirectory() as folder, patch("web_dashboard.load_config", return_value=self.config), \
                patch("web_dashboard.latest_broker_snapshot", return_value=self.snapshot), \
                patch("web_dashboard.strategy.latest_completed_day", return_value="2026-11-13"), \
                patch("web_dashboard.snapshot_selected_tencent", return_value=(feed, [])):
            client = create_app(Path(folder)).test_client()
            result = client.get("/api/close-review").get_json()
        self.assertFalse(result["data_verified"])
        self.assertEqual(result["sell_plan"], [])
        self.assertIsNone(result["cash_policy"]["minimum_reserve"])

    def test_verified_route_uses_fixed_exits_and_broker_cash_separately(self):
        self.set_stop("600004", 31)
        feed = {
            (row["type"], row["code"]): (
                self.quotes[row["code"]]["price"], row["name"], self.quotes[row["code"]]["previous_close"],
                self.quotes[row["code"]]["source_timestamp"],
            )
            for row in self.snapshot["positions"]
        }
        with TemporaryDirectory() as folder, patch("web_dashboard.load_config", return_value=self.config), \
                patch("web_dashboard.latest_broker_snapshot", return_value=self.snapshot), \
                patch("web_dashboard.strategy.latest_completed_day", return_value="2026-11-13"), \
                patch("web_dashboard.snapshot_selected_tencent", return_value=(feed, [])):
            client = create_app(Path(folder)).test_client()
            response = client.get("/api/close-review")
            self.assertEqual(response.status_code, 200)
            page = client.get("/close-review")
            self.assertEqual(page.status_code, 200)
            self.assertIn('id="withdrawable"', page.get_data(as_text=True))
        result = response.get_json()
        self.assertTrue(result["data_verified"])
        self.assertEqual(result["sell_plan"][0]["trigger"], "stop")
        self.assertEqual(result["cash_policy"]["broker_withdrawable_cash"], 3000)


if __name__ == "__main__":
    unittest.main()
