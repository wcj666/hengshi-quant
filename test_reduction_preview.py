from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from action_center import CN
from portfolio_test_fixture import account_fixture
from reduction_preview import build_reduction_preview
from t_costs import cash_impact
from trade_reconciliation import position_signature
from web_dashboard import CSRF, create_app


class ReductionPreviewTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 17, 10, 30, tzinfo=CN)
        self.cfg = account_fixture()
        self.cfg['cash'] = 500
        self.cfg['positions'] = [
            dict(code='159971', name='ETF A', quantity=2000, type='etf', cost=2., baseline_price=2.),
            dict(code='588170', name='ETF B', quantity=2000, type='etf', cost=3., baseline_price=3.),
        ]
        self.state = {
            'mode': 'live', 'market_session': True, 'calendar_ok': True,
            'quote_health': {'all_held_usable': True}, 'risk': {'valid': True},
            'fetch_finished_at': self.now.isoformat(),
            'quotes': [
                dict(code='159971', held=True, price=2., source_timestamp=self.now.isoformat(), limit_status_verified=True),
                dict(code='588170', held=True, price=3., source_timestamp=self.now.isoformat(), limit_status_verified=True),
            ],
        }
        self.book = {'availability': {
            p['code']: dict(date=str(self.now.date()), base_quantity=p['quantity'], available_quantity=600,
                           confirmed_at=self.now.isoformat()) for p in self.cfg['positions']
        }}

    def build(self, quantities=None, now=None, completed_day=None):
        return build_reduction_preview(self.cfg, self.state, self.book, quantities or {},
                                       now=now or self.now, completed_day=completed_day)

    def test_two_sales_deduct_each_fee_once_and_recompute_post_fee_weights(self):
        before = deepcopy(self.cfg)
        result = self.build({'159971': 300, '588170': 600})
        actual = result['preview']
        self.assertTrue(actual['valid'])
        self.assertIsNone(actual['fees'])
        self.assertEqual(actual['reserved_cost'], 40)
        self.assertEqual(actual['net_proceeds'], 2360)
        self.assertEqual(actual['cash_after'], 2860)
        self.assertEqual(actual['market_value_after'], 7600)
        self.assertIsNone(actual['equity_after'])
        self.assertEqual(actual['conservative_equity_after'], 10460)
        self.assertEqual(actual['exposure_after_pct'], 72.66)
        self.assertEqual(actual['cash_shortfall'], 1324)
        self.assertEqual(actual['rows'][1]['remaining_quantity'], 1400)
        self.assertEqual(actual['rows'][1]['remaining_weight_pct'], 40.15)
        self.assertEqual(actual['unverified'], [])
        self.assertEqual(self.cfg, before)

    def test_known_fee_and_extra_reserve_are_separate_from_estimated_equity(self):
        self.book['fee_model'] = {'etf': {'commission_per_10k': 5, 'minimum_commission': 5}}
        result = self.build({'159971': 300, '588170': 600})['preview']
        self.assertEqual(result['estimated_fees'], 10)
        self.assertEqual(result['safety_reserve'], 30)
        self.assertEqual(result['reserved_cost'], 40)
        self.assertEqual(result['estimated_net_proceeds'], 2390)
        self.assertEqual(result['estimated_cash_after'], 2890)
        self.assertEqual(result['estimated_equity_after'], 10490)
        self.assertEqual(result['equity_after'], 10490)
        self.assertEqual(result['conservative_equity_after'], 10460)
        self.assertEqual(result['exposure_after_pct'], 72.45)
        self.assertEqual(result['cash_after'], 2860)
        self.assertEqual(result['rows'][0]['estimated_fee'], 5)
        self.assertEqual(result['rows'][0]['safety_reserve'], 15)

    def test_unknown_terms_do_not_turn_reserve_into_known_charges(self):
        result = self.build({'159971': 300})['preview']
        self.assertIsNone(result['estimated_fees'])
        self.assertIsNone(result['estimated_cash_after'])
        self.assertIsNone(result['estimated_equity_after'])
        self.assertEqual(result['safety_reserve'], 20)
        self.assertEqual(result['cash_after'], 1080)
        self.assertEqual(result['rows'][0]['fee_basis'], 'reserve')

    def test_shared_cash_helper_handles_terms_samples_and_zero_trade(self):
        model = {'etf': {'commission_per_10k': 5, 'minimum_commission': 5}}
        sell = cash_impact('etf', 'sell', 1000, 20, model)
        self.assertEqual(sell['estimated_fee'], 5)
        self.assertEqual(sell['safety_reserve'], 15)
        self.assertEqual(sell['estimated_cash_delta'], 995)
        self.assertEqual(sell['conservative_cash_delta'], 980)
        actual = [{'kind': 'etf', 'side': 'sell', 'notional': 1000, 'fee': 30}]
        cost = cash_impact('etf', 'sell', 1000, 20, model, actual)
        self.assertEqual(cost['estimated_fee'], 30)
        self.assertEqual(cost['fee_basis'], 'comparable_fills')
        self.assertEqual(cost['safety_reserve'], 0)
        self.assertEqual(cost['conservative_cash_delta'], 970)
        self.assertEqual(cash_impact('etf', 'buy', 1000, 20, model)['conservative_cash_delta'], -1020)
        self.assertEqual(cash_impact('stock', 'sell', 1000, 20,
                         {'stock': {'commission_per_10k': 5, 'minimum_commission': 5}})['estimated_fee'], 5.51)
        self.assertEqual(cash_impact('etf', 'sell', 0, 20)['reserved_cost'], 0)
        for amount in (-1, float('nan'), True):
            with self.assertRaises(ValueError):
                cash_impact('etf', 'sell', amount, 20)

    def test_yesterday_and_insufficient_inventory_are_hypothetical(self):
        self.book['availability']['159971'].update(date='2026-11-16')
        result = self.build({'159971': 100, '588170': 700})
        self.assertIsNone(result['positions'][0]['available_quantity'])
        self.assertEqual(result['positions'][1]['available_quantity'], 600)
        self.assertEqual(result['preview']['execution_status'], '按假设全部成交测算')
        self.assertEqual(len(result['preview']['unverified']), 2)
        self.assertEqual(set(result['suggested']['quantities']), {'159971', '588170'})

    def test_only_matching_same_day_receipt_can_prove_available(self):
        self.book['availability'].clear()
        self.cfg['trade_reconciliation'] = {
            'broker_asof': self.now.isoformat(), 'available_cash': self.cfg['cash'],
            'position_signature': position_signature(self.cfg['positions']),
            'positions': {'159971': {'quantity': 2000, 'available_quantity': 0}},
        }
        result = self.build()
        self.assertEqual(result['positions'][0]['available_quantity'], 0)
        self.assertIsNone(result['positions'][1]['available_quantity'])
        self.cfg['cash'] += 1
        self.assertIsNone(self.build()['positions'][0]['available_quantity'])

    def test_stale_held_quote_blocks_whole_combination_and_suggestion(self):
        self.state['quotes'][1]['source_timestamp'] = (self.now - timedelta(seconds=90)).isoformat()
        result = self.build({'159971': 100})
        self.assertEqual(result['mode'], 'unavailable')
        self.assertFalse(result['preview']['valid'])
        self.assertIsNone(result['preview']['cash_after'])
        self.assertFalse(result['suggested']['quantities'])

    def test_recent_close_is_hypothetical_and_not_a_live_fallback(self):
        close = self.now.replace(hour=15, minute=1)
        self.state['display_snapshot'] = {
            'historical': True,
            'quotes': [dict(q, source_timestamp=close.isoformat()) for q in self.state['quotes']],
        }
        result = self.build({'159971': 100}, now=close + timedelta(minutes=10), completed_day='2026-11-17')
        self.assertEqual(result['mode'], 'close')
        self.assertTrue(result['preview']['valid'])
        self.assertIsNone(result['positions'][0]['available_quantity'])
        self.assertEqual(self.build(now=self.now + timedelta(minutes=1), completed_day='2026-11-17')['mode'], 'unavailable')
        self.assertEqual(self.build(now=close + timedelta(minutes=10), completed_day='2026-11-18')['mode'], 'unavailable')

    def test_invalid_quantities_rejected_and_full_odd_lot_allowed(self):
        for draft in ({'159971': 1}, {'159971': 2100}, {'159971': True}, {'600000': 100}):
            self.assertFalse(self.build(draft)['preview']['valid'])
        self.cfg['positions'][0]['quantity'] = 2001
        self.assertTrue(self.build({'159971': 2001})['preview']['valid'])

    def test_endpoint_only_reads_account_and_requires_csrf_for_post(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_file = root / 'config.json'
            cfg_file.write_text(json.dumps(self.cfg))
            original = cfg_file.read_bytes()
            client = create_app(root).test_client()
            url = '/api/reduction-preview'
            baseline = client.get(url)
            self.assertEqual(baseline.status_code, 200)
            self.assertFalse(baseline.json['preview']['valid'])
            self.assertEqual(client.post(url, json={'quantities': {}}).status_code, 403)
            headers = {'X-CSRF-Token': CSRF}
            self.assertEqual(client.post(url, headers=headers, json={'quantities': []}).status_code, 400)
            self.assertEqual(client.post(url, headers=headers, json={'quantities': {}}).status_code, 200)
            self.assertEqual(cfg_file.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
