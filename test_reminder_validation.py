from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reminder_validation import baseline_window, decision, reminder_counts
from v1_backtest import DEFAULT_COSTS
from v1_backtest_data import demo_bundle
from v1_validation import PERIOD_KEYS, evaluate, save_comparison
from web_dashboard import create_app


class ReminderValidationTests(unittest.TestCase):
    def test_baseline_uses_prior_minute_depth_and_includes_costs(self):
        def bar(time, price, volume):
            return {'time': time, 'open': price, 'close': price, 'volume': volume}

        first, second = '2026-11-16', '2026-11-17'
        session = dict(status='open', corporate_action_clear=True, upper_limit=11, lower_limit=9,
                       minutes=[bar('09:31', 10, 1000), bar('09:32', 10, 90000), bar('15:00', 9.5, 1)])
        bundle = {'calendar': [first, second], 'securities': {'600000': {'type': 'stock', 'sessions': {
            first: session, second: dict(status='suspended', corporate_action_clear=True),
        }}}}
        metric, covered, _ = baseline_window(bundle, first, second, 100_000, DEFAULT_COSTS, '600000')
        self.assertTrue(covered)
        self.assertEqual(metric['net_pnl'], -56.01)
        self.assertEqual(metric['fees'], 5.01)
        self.assertAlmostEqual(metric['max_drawdown_pct'], .056)
        bundle['securities']['600000']['sessions'][second]['corporate_action_clear'] = False
        self.assertIsNone(baseline_window(bundle, first, second, 100_000, DEFAULT_COSTS, '600000')[0]['net_pnl'])

    def test_counts_zero_denominator_and_partial_fills(self):
        result = reminder_counts([
            {'kind': 'stop_loss', 'filled': False, 'blocked_reasons': ['t_plus_one']},
            {'kind': 'stop_loss', 'filled': True, 'status': 'partial', 'fill_count': 2},
        ], ('stop_loss', 'breakout'))
        self.assertEqual(result['stop_loss']['triggers'], 2)
        self.assertEqual(result['stop_loss']['filled'], 1)
        self.assertEqual(result['stop_loss']['fill_rate_pct'], 50)
        self.assertEqual(result['stop_loss']['partial'], 1)
        self.assertIsNone(result['breakout']['fill_rate_pct'])

    def test_filter_requires_both_out_of_sample_windows_and_full_evidence(self):
        periods = []
        for key in PERIOD_KEYS:
            periods.append({'key': key, 'trade': dict(
                covered=True, sample_count=40, session_count=25,
                reminders={'take_profit_1': {'triggers': 30}},
                strategy=dict(net_return_pct=0, max_drawdown_pct=1),
                ablation={'take_profit_1': dict(net_return_pct=1, max_drawdown_pct=1, covered=True)},
            )})
        def check(demo=False):
            return decision(periods, 'take_profit_1', 'trade', demo, '600000', True)[0]
        self.assertEqual(check(), '移出强提醒候选')
        self.assertEqual(check(True), '待验证')
        periods[-1]['trade']['strategy']['net_return_pct'] = 2
        self.assertEqual(check(), '待验证')
        periods[-2]['trade']['strategy']['net_return_pct'] = 2
        self.assertEqual(check(), '验证通过')
        periods[-1]['trade']['reminders']['take_profit_1']['triggers'] = 2
        self.assertEqual(check(), '待验证')
        self.assertEqual(decision(periods, 'stop_loss', 'trade', False, None, False)[0], '保留风控')

    def test_saved_demo_api_isolated_and_never_shortlists(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = create_app(root).test_client()
            route = '/api/backtest/reminder-validation'
            self.assertEqual(client.get(route).json['periods'], [])
            bundle = demo_bundle()
            windows = {key: (day, day) for key, day in zip(PERIOD_KEYS, bundle['calendar'][-3:])}
            result = evaluate(bundle, windows, baseline_code=next(iter(bundle['securities'])))
            save_comparison(result, root/'output'/'v1_validation')
            report = client.get(route).json
            self.assertTrue(report['meta']['demo'])
            self.assertEqual(len(report['periods']), 3)
            self.assertEqual(report['strong_candidates'], [])
            self.assertEqual(report['excluded_candidates'], [])
            self.assertTrue(all(p['t']['covered'] is False for p in report['periods']))


if __name__ == '__main__':
    unittest.main()
