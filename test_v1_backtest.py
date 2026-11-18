"""Deterministic V1 replay and dashboard entry points."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from v1_backtest import run_backtest
from v1_backtest_data import demo_bundle
from v1_backtest_report import save_report

INDEX = '600000'
START, SECOND, END = '2024-06-17', '2024-06-18', '2024-06-19'


def session(bundle, day):
    return bundle['securities'][INDEX]['sessions'][day]


class ReplayTests(TestCase):
    def test_exit_fills_and_costs_reconcile(self):
        result = run_backtest(demo_bundle(), START, END)
        fills, summary = result['fills'], result['summary']
        self.assertEqual([f['reason'] for f in fills], ['breakout', 'take_profit_1', 'take_profit_2'])
        self.assertEqual([f['date'] for f in fills], [START, SECOND, END])
        self.assertEqual(fills[0]['price_bar_end'], '09:46')
        self.assertLessEqual(fills[0]['price'], fills[0]['entry_cap'])
        self.assertEqual(sum(f['quantity'] for f in fills[1:]), fills[0]['quantity'])
        self.assertEqual(round(sum(f['fees'] for f in fills), 2), summary['fee_total'])
        net = round(sum(f['notional'] if f['side'] == 'sell' else -f['notional'] for f in fills)
                    - summary['fee_total'], 2)
        self.assertEqual(net, summary['net_pnl'])
        self.assertEqual(net, result['closed_trades'][0]['net_pnl'])
        self.assertLessEqual(result['closed_trades'][0]['initial_risk'], 150)
        self.assertTrue(summary['execution_coverage_complete'])

    def test_future_daily_close_cannot_change_the_previous_fill(self):
        bundle = demo_bundle()
        base = run_backtest(bundle, START, START)
        altered = deepcopy(bundle)
        for series in (altered['benchmark'], altered['securities'][INDEX]['daily']):
            for bar in series:
                if bar['date'] > '2024-06-14':
                    for field in ('open', 'high', 'low', 'close'):
                        bar[field] *= 2
        trial = run_backtest(altered, START, START)
        self.assertEqual(base['fills'], trial['fills'])

    def test_t_plus_one_and_limit_down_keep_triggered_stop_pending(self):
        bundle = demo_bundle()
        buy_day = session(bundle, START)
        row = next(bar for bar in buy_day['minutes'] if bar['time'] == '10:00')
        row['low'] = 11.10
        sell_day = session(bundle, SECOND)
        lower = sell_day['lower_limit']
        sell_day['open'] = lower
        for bar in sell_day['minutes']:
            bar.update(open=lower, high=lower, low=lower, close=lower)
        result = run_backtest(bundle, START, SECOND)
        self.assertEqual([f['side'] for f in result['fills']], ['buy'])
        self.assertTrue(result['open_positions'][0]['stop_pending'])
        self.assertTrue({'t_plus_one', 'exit_price_limit'}.issubset(d['reason'] for d in result['decisions']))
        stops = [item for item in result['reminders'] if item['kind'] == 'stop_loss']
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0]['date'], START)
        self.assertEqual(stops[0]['at'], START + 'T10:00:00+08:00')
        self.assertFalse(stops[0]['filled'])
        self.assertEqual(set(stops[0]['blocked_reasons']), {'t_plus_one', 'exit_price_limit'})

    def test_known_trigger_before_missing_execution_bar_counts_without_fill(self):
        bundle = demo_bundle()
        bars = session(bundle, START)['minutes']
        bars.pop(next(index for index, bar in enumerate(bars) if bar['time'] == '09:46'))
        result = run_backtest(bundle, START, START)
        self.assertFalse(result['summary']['execution_coverage_complete'])
        self.assertEqual(result['fills'], [])
        entry, = result['reminders']
        self.assertEqual(entry['kind'], 'breakout')
        self.assertEqual(entry['at'], START + 'T09:45:00+08:00')
        self.assertFalse(entry['filled'])
        self.assertIn('minute_gap', entry['blocked_reasons'])

    def test_split_execution_does_not_inflate_the_stop_or_target_count(self):
        bundle = demo_bundle()
        for bar in session(bundle, END)['minutes']:
            bar['volume'] = 1000
        result = run_backtest(bundle, START, END)
        target, = [item for item in result['reminders'] if item['kind'] == 'take_profit_2']
        self.assertEqual(target['target_quantity'], 200)
        self.assertTrue(target['filled'])
        self.assertEqual(target['filled_quantity'], 200)
        self.assertEqual(target['fill_count'], 2)

    def test_profit_ablations_leave_entry_sizing_and_mandatory_stop_intact(self):
        bundle = demo_bundle()
        full = run_backtest(bundle, START, END)
        first_off = run_backtest(bundle, START, END, disabled_profit_targets=('take_profit_1',))
        second_off = run_backtest(bundle, START, END, disabled_profit_targets=('take_profit_2',))
        self.assertEqual(first_off['meta']['disabled_profit_targets'], ['take_profit_1'])
        self.assertEqual(first_off['fills'][0], full['fills'][0])
        self.assertEqual(second_off['fills'][0], full['fills'][0])
        self.assertEqual([f['reason'] for f in first_off['fills']], ['breakout', 'take_profit_2'])
        self.assertEqual([f['reason'] for f in second_off['fills']], ['breakout', 'take_profit_1'])
        self.assertEqual(first_off['fills'][-1]['quantity'], full['fills'][0]['quantity'])
        self.assertEqual(second_off['open_positions'][0]['stop_loss'], full['fills'][0]['price'])
        both_off = ('take_profit_1', 'take_profit_2')
        no_exits = run_backtest(bundle, START, SECOND, disabled_profit_targets=both_off)
        original_stop = full['fills'][0]['stop_loss']
        self.assertEqual(no_exits['open_positions'][0]['stop_loss'], original_stop)
        row = next(bar for bar in session(bundle, SECOND)['minutes'] if bar['time'] == '09:45')
        row['low'] = original_stop
        stopped = run_backtest(bundle, START, SECOND, disabled_profit_targets=both_off)
        self.assertEqual([f['reason'] for f in stopped['fills']], ['breakout', 'stop_loss'])

    def test_future_review_cannot_authorize_earlier_entry(self):
        bundle = demo_bundle()
        review = bundle['securities'][INDEX]['reviews']['2024-06-14']
        review['recorded_at'] = START + 'T15:05:00+08:00'
        result = run_backtest(bundle, START, START)
        self.assertEqual(result['fills'], [])
        self.assertFalse(result['summary']['execution_coverage_complete'])
        self.assertIn('point_in_time_review_missing', {d['reason'] for d in result['decisions']})

    def test_negative_archived_review_is_a_rejection_not_a_coverage_gap(self):
        bundle = demo_bundle()
        bundle['securities'][INDEX]['reviews']['2024-06-14']['fundamentals_ok'] = False
        result = run_backtest(bundle, START, START)
        self.assertEqual(result['fills'], [])
        self.assertTrue(result['summary']['execution_coverage_complete'])
        self.assertIn('point_in_time_review_rejected', {d['reason'] for d in result['decisions']})

    def test_dated_universe_uses_signal_day_and_exits_after_removal(self):
        bundle = demo_bundle()
        bundle.update(
            universe_source='合成名单',
            universe={'2024-06-14': [INDEX], START: [], SECOND: []},
        )
        result = run_backtest(bundle, START, END)
        self.assertEqual([f['reason'] for f in result['fills']],
                         ['breakout', 'take_profit_1', 'take_profit_2'])
        self.assertTrue(result['summary']['execution_coverage_complete'])
        self.assertEqual(result['meta']['input_coverage']['symbol_sessions'], 1)

        del bundle['universe']['2024-06-14']
        result = run_backtest(bundle, START, START)
        self.assertEqual(result['fills'], [])
        self.assertFalse(result['summary']['execution_coverage_complete'])
        self.assertEqual(result['decisions'][0]['reason'], 'historical_universe_missing')

        bundle['universe']['2024-06-14'] = []
        result = run_backtest(bundle, START, START)
        self.assertEqual(result['fills'], [])
        self.assertTrue(result['summary']['execution_coverage_complete'])

    def test_gap_above_cap_cancels_the_session_even_after_retrace(self):
        bundle = demo_bundle()
        session(bundle, START)['open'] = 11.70
        result = run_backtest(bundle, START, START)
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['decisions'][0]['reason'], 'open_above_cap')

    def test_missing_minute_and_missing_held_session_are_explicit(self):
        bundle = demo_bundle()
        session(bundle, START)['minutes'].pop(0)
        result = run_backtest(bundle, START, START)
        self.assertEqual(result['fills'], [])
        self.assertFalse(result['summary']['execution_coverage_complete'])
        bundle = demo_bundle()
        del bundle['securities'][INDEX]['sessions'][SECOND]
        result = run_backtest(bundle, START, END)
        self.assertEqual(result['meta']['halted']['date'], SECOND)
        self.assertIsNone(result['summary']['end_equity'])
        self.assertIsNone(result['summary']['max_drawdown'])

    def test_standalone_backtest_has_no_web_entry_or_download(self):
        from web_dashboard import create_app
        with TemporaryDirectory() as folder:
            root = Path(folder)
            client = create_app(root).test_client()
            save_report(run_backtest(demo_bundle(), START, END), root / 'output' / 'v1_backtest')
            self.assertEqual(client.get('/backtest').status_code, 404)
            self.assertEqual(client.get('/api/backtest').status_code, 404)
            self.assertEqual(client.get('/backtest/download/equity_curve.csv').status_code, 404)
            with client.get('/') as dashboard:
                self.assertNotIn(b'href="/backtest"', dashboard.data)
            self.assertFalse((root / 'config.json').exists())

    def test_validation_dashboard_reads_only_comparison(self):
        from web_dashboard import create_app
        from strategy_runtime import write
        with TemporaryDirectory() as folder:
            root = Path(folder)
            client = create_app(root).test_client()
            with client.get('/backtest/validation') as dashboard:
                self.assertEqual(dashboard.status_code, 200)
                self.assertNotIn(b'href="/backtest"', dashboard.data)
            self.assertEqual(client.get('/api/backtest/validation').status_code, 404)
            path = root / 'output' / 'v1_validation' / 'comparison.json'
            write(path, {'meta': {'demo': True}, 'periods': []})
            self.assertTrue(client.get('/api/backtest/validation').json['meta']['demo'])
            with client.get('/backtest/validation/download') as comparison:
                self.assertIn('comparison.json', comparison.headers['Content-Disposition'])
            run_id = 'a' * 32
            write(path.parent / 'runs' / run_id / 'holdout' / 'result.json', {'fills': []})
            route = f'/backtest/validation/download/{run_id}/holdout'
            with client.get(f'{route}/result.json') as download:
                self.assertEqual(download.status_code, 200)
            self.assertEqual(client.get(f'{route}/config.json').status_code, 404)
            path.write_text('{')
            self.assertEqual(client.get('/api/backtest/validation').status_code, 409)


if __name__ == '__main__':
    main()
