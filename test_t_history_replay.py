import unittest
from copy import deepcopy

from t_history_replay import evaluate_t_window
from v1_backtest_data import MINUTE_ENDS, demo_bundle, validate_bundle


def example():
    bundle = demo_bundle()
    code = '600000'
    security = bundle['securities'][code]
    start, following = bundle['calendar'][-3:-1]
    prior = bundle['calendar'][-4]
    bar = next(row for row in security['daily'] if row['date'] == prior)
    bar.update(open=20, close=20, high=20, low=20)
    bundle['t_replay'] = {
        'source': '合成形态单元测试',
        'recorded_at': prior + 'T15:01:00+08:00',
        'positions': [{'code': code, 'quantity': 300, 'stop_loss': 19.40}],
    }
    prices = [20] * 48 + [20.8, 21, 21.1, 21.2, 21.3, 21.4, 21.5, 21.4, 21.3, 21.2, 21.1, 21,
                         20.5, 20, 19.8, 19.7, 19.65, 19.7, 19.75, 19.80]
    prices.extend([19.8] * (len(MINUTE_ENDS) - len(prices)))
    bars = [dict(time=label, open=price, close=price, high=price, low=price, volume=10000, amount=price * 10000)
            for label, price in zip(MINUTE_ENDS, prices)]
    session = {'status': 'open', 'corporate_action_clear': True, 'open': 20, 'lower_limit': 18, 'upper_limit': 22,
               'minutes': bars}
    security['sessions'].update({start: session, following: deepcopy(session)})
    validate_bundle(bundle)
    return bundle, code, start, following


class THistoryReplayTests(unittest.TestCase):
    def test_paired_result_uses_next_open_and_actual_costs(self):
        bundle, code, start, _ = example()
        result = evaluate_t_window(bundle, start, start, 100000)
        self.assertTrue(result['covered'])
        self.assertEqual(result['sample_count'], 1)
        sold, bought = result['fills']
        self.assertEqual((sold['side'], bought['side']), ('sell', 'buy'))
        self.assertEqual(sold['quantity'], 100)
        self.assertEqual(bought['quantity'], 100)
        self.assertGreater(sold['time'], sold['signal_at'][11:16])
        self.assertLess(sold['price'], sold['reference_price'])
        self.assertGreater(bought['price'], bought['reference_price'])
        self.assertEqual(result['strategy']['fees'], round(sold['fee'] + bought['fee'], 2))
        self.assertEqual(result['baseline']['net_pnl'], -60)
        net_effect = 100 * (sold['price'] - bought['price']) - result['strategy']['fees']
        self.assertAlmostEqual(result['relative_net_pnl'], net_effect, places=2)
        self.assertEqual(result['reminders']['sell_ready']['fill_rate_pct'], 100)
        self.assertEqual(result['reminders']['buyback_ready']['fill_rate_pct'], 100)

    def test_configured_t_fraction_changes_t_replay_signals(self):
        bundle, _, start, _ = example()
        configured = {'enabled': True, 'max_fraction': .2}
        result = evaluate_t_window(bundle, start, start, 100000, t_settings=configured)
        self.assertTrue(result['covered'])
        self.assertEqual(result['t_settings']['max_fraction'], .2)
        self.assertEqual(result['reminders']['sell_ready']['triggers'], 0)
        self.assertEqual(result['fills'], [])

    def test_missing_amount_blocks_coverage_instead_of_finding_zero_triggers(self):
        bundle, code, start, _ = example()
        bundle['securities'][code]['sessions'][start]['minutes'][45].pop('amount')
        result = evaluate_t_window(bundle, start, start, 100000)
        self.assertFalse(result['covered'])
        self.assertIsNone(result['strategy']['net_return_pct'])
        self.assertIsNone(result['reminders']['sell_ready']['triggers'])
        self.assertIn('成交额', result['coverage_examples'][0]['reason'])

    def test_cannot_recapture_signal_in_same_bar_or_without_depth(self):
        bundle, _, start, _ = example()
        result = evaluate_t_window(bundle, start, start, 100000, {'prior_minute_participation': .001})
        self.assertTrue(result['covered'])
        self.assertEqual(result['sample_count'], 0)
        self.assertEqual(result['reminders']['sell_ready']['triggers'], 1)
        self.assertEqual(result['reminders']['sell_ready']['fill_rate_pct'], 0)
        self.assertEqual(result['fills'], [])

    def test_unpaired_position_retains_cash_and_is_not_automatically_bought_next_day(self):
        bundle, code, start, following = example()
        bars = bundle['securities'][code]['sessions'][start]['minutes']
        for row in bars[60:]:
            row.update(open=20.95, close=20.95, high=20.95, low=20.95, amount=209500)
        result = evaluate_t_window(bundle, start, following, 100000)
        self.assertTrue(result['covered'])
        self.assertEqual(result['sample_count'], 0)
        self.assertEqual(result['unmatched_cycles'], 1)
        self.assertEqual(result['unmatched_by_code'], {code: 100})
        self.assertEqual([fill['side'] for fill in result['fills']], ['sell'])
        self.assertEqual(result['equity_curve'][-1]['date'], following)

    def test_missing_structure_history_cannot_establish_effect(self):
        bundle, code, start, _ = example()
        bundle['securities'][code]['daily'] = bundle['securities'][code]['daily'][-4:]
        result = evaluate_t_window(bundle, start, start, 100000)
        self.assertFalse(result['covered'])
        self.assertIn('结构风险', result['coverage_examples'][0]['reason'])

    def test_seed_requires_known_old_positions_and_pre_window_timestamp(self):
        bundle, _, start, _ = example()
        bundle['t_replay']['recorded_at'] = start + 'T09:35:00+08:00'
        result = evaluate_t_window(bundle, start, start, 100000)
        self.assertFalse(result['covered'])
        self.assertIn('事前时间', result['warnings'][0])
        bundle.pop('t_replay')
        self.assertIsNone(evaluate_t_window(bundle, start, start, 100000)['baseline']['net_pnl'])


if __name__ == '__main__':
    unittest.main()
