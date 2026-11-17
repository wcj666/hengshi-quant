"""T-review accounting: actual pairs, unknown costs and unpaired exposure differ."""
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest

from t_performance import build_close_review, build_review


class TPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, 14, 30, tzinfo=ZoneInfo('Asia/Shanghai'))

    def cycle(self, ident='one', paired=100, status='closed', buy_price=9, sell_fee=2, buy_fee=3, day=None):
        day = day or self.now.date()
        return {
            'id': ident, 'code': '159971', 'name': '测试ETF', 'date': day.isoformat(),
            'status': status, 'quantity': 100, 'sell_price': 10, 'sell_fee': sell_fee,
            'buyback_quantity': paired, 'buyback_cost': paired * buy_price, 'fee_reserve': 20,
            'recorded_at': datetime.combine(day, self.now.time(), self.now.tzinfo).isoformat(),
            'fills': [{'quantity': paired, 'price': buy_price, 'fee': buy_fee}] if paired else [],
        }

    def report(self, *cycles, quotes=None):
        return build_review({'cycles': list(cycles)}, quotes, self.now)

    def quote(self, price=8, age=0):
        return {'159971': {'price': price, 'source_timestamp': (self.now - timedelta(seconds=age)).isoformat()}}

    def test_no_sample_does_not_claim_zero_profit_or_win_rate(self):
        summary = self.report()['summary']
        self.assertIsNone(summary['actual_paired_net'])
        self.assertIsNone(summary['win_rate'])
        self.assertIsNone(summary['cost_ratio_pct'])
        self.assertEqual(summary['closed_actual_samples'], 0)
        self.assertEqual(summary['sample_status'], 'unvalidated')

    def test_real_pair_is_incremental_cash_vs_untouched_holdings_after_actual_fees(self):
        summary = self.report(self.cycle())['summary']
        self.assertEqual(summary['actual_paired_net'], 95)
        self.assertEqual(summary['actual_paired_gross'], 100)
        self.assertEqual(summary['actual_paired_fees'], 5)
        self.assertEqual(summary['cost_ratio_pct'], 5)
        self.assertEqual(summary['win_rate'], 100)
        self.assertIsNone(summary['estimated_paired_net'])

    def test_estimates_never_enter_actual_profit_or_win_rate(self):
        summary = self.report(self.cycle(sell_fee=None))['summary']
        self.assertIsNone(summary['actual_paired_net'])
        self.assertIsNone(summary['win_rate'])
        self.assertEqual(summary['estimated_paired_net'], 80)
        self.assertEqual(summary['estimated_paired_count'], 1)
        self.assertEqual(summary['closed_actual_samples'], 0)

    def test_cash_reduction_remainder_never_counts_as_completed_t_profit(self):
        report = self.report(self.cycle(paired=50, status='cash'), quotes=self.quote())
        row, summary = report['rows'][0], report['summary']
        self.assertEqual(row['paired_net_actual'], 46)
        self.assertEqual(row['unmatched_sell_fee_actual'], 1)
        self.assertEqual(row['unmatched_comparison'], 99)
        self.assertEqual(summary['actual_paired_net'], 46)
        self.assertEqual(summary['unmatched_mark_comparison'], 99)
        self.assertEqual(summary['closed_actual_samples'], 0)
        self.assertIsNone(summary['win_rate'])
        self.assertEqual(summary['cash_cycles'], 1)

    def test_unpaired_sale_has_no_realized_t_gain(self):
        report = self.report(self.cycle(paired=0, status='open'), quotes=self.quote())
        self.assertIsNone(report['summary']['actual_paired_net'])
        self.assertIsNone(report['summary']['estimated_paired_net'])
        self.assertEqual(report['rows'][0]['unmatched_comparison'], 198)
        self.assertEqual(report['summary']['open_cycles'], 1)

    def test_mark_unknown_fees_remain_unknown_and_not_zero(self):
        report = self.report(self.cycle(paired=0, status='open', sell_fee=None), quotes=self.quote())
        row = report['rows'][0]
        self.assertEqual(row['unmatched_gross_comparison'], 200)
        self.assertIsNone(row['unmatched_comparison'])
        self.assertEqual(row['comparison_status'], 'sell_fee_unknown')
        self.assertEqual(report['summary']['unmatched_unpriced_count'], 1)

    def test_future_stale_and_previous_day_marks_cannot_value_remainder(self):
        for age in (-1, 91, 86400):
            report = self.report(self.cycle(paired=0, status='open'), quotes=self.quote(age=age))
            self.assertIsNone(report['rows'][0]['unmatched_comparison'], age)
            self.assertIsNone(report['summary']['unmatched_mark_comparison'])
        report = self.report(self.cycle(paired=0, status='open'), quotes=self.quote(age=90))
        self.assertEqual(report['summary']['unmatched_mark_comparison'], 198)

    def test_mixed_actual_wins_losses_flat_only_closed_complete_pairs(self):
        cycles = [self.cycle(), self.cycle('loss', buy_price=11), self.cycle('flat', buy_price=10, sell_fee=0, buy_fee=0),
                  self.cycle('unknown', sell_fee=None), self.cycle('cash', paired=50, status='cash')]
        summary = self.report(*cycles)['summary']
        self.assertEqual((summary['wins'], summary['losses'], summary['flat']), (1, 1, 1))
        self.assertEqual(summary['closed_actual_samples'], 3)
        self.assertEqual(summary['win_rate'], 33.33)
        self.assertEqual(summary['average_closed_net'], -3.33)
        self.assertEqual(summary['actual_paired_net'], 36)
        self.assertEqual(summary['estimated_paired_net'], 80)

    def test_seven_calendar_day_window_includes_boundary_excludes_future(self):
        report = self.report(self.cycle(), self.cycle('boundary', day=self.now.date()-timedelta(days=6)),
                             self.cycle('old', day=self.now.date()-timedelta(days=7)),
                             self.cycle('future', day=self.now.date()+timedelta(days=1)))
        self.assertEqual(report['window_start'], '2026-11-10')
        self.assertEqual(report['window_end'], '2026-11-16')
        self.assertEqual(report['summary']['cycle_count'], 2)
        self.assertEqual(report['summary']['outside_window_cycles'], 1)
        self.assertEqual(report['summary']['excluded_invalid_cycles'], 1)

    def test_bad_quantities_aggregate_cost_or_future_fill_excluded(self):
        for key, value in [('quantity', True), ('buyback_quantity', 101), ('buyback_cost', 0),
                           ('sell_fee', -1), ('recorded_at', (self.now+timedelta(seconds=1)).isoformat())]:
            cycle = self.cycle()
            cycle[key] = value
            report = self.report(cycle)
            self.assertEqual(report['summary']['excluded_invalid_cycles'], 1, key)
            self.assertIsNone(report['summary']['actual_paired_net'], key)
        cycle = self.cycle()
        cycle['fills'][0]['filled_at'] = (self.now+timedelta(seconds=1)).isoformat()
        self.assertEqual(self.report(cycle)['summary']['excluded_invalid_cycles'], 1)

    def test_duplicate_cycles_are_not_double_counted(self):
        report = self.report(self.cycle(), self.cycle())
        self.assertEqual(report['summary']['excluded_invalid_cycles'], 2)
        self.assertEqual(report['summary']['cycle_count'], 0)

    def test_pure_report_preserves_input_records(self):
        book = {'cycles': [self.cycle(paired=50, status='cash')]}
        quotes = self.quote()
        before = deepcopy((book, quotes))
        build_review(book, quotes, self.now)
        self.assertEqual((book, quotes), before)

    def close_review(self, *cycles, when=None, mark=None, day=None):
        clock = when or self.now.replace(hour=16)
        return build_close_review({'cycles': list(cycles)}, (day or self.now.date()).isoformat(), mark, clock)

    def close_quote(self, price=8, when=None):
        stamp = when or self.now.replace(hour=15, minute=0)
        return {'159971': {'price': price, 'source_timestamp': stamp.isoformat()}}

    def test_close_combines_actual_paired_profit_and_unmatched_valuation_separately(self):
        report = self.close_review(self.cycle(paired=50, status='open'), mark=self.close_quote())
        row = report['rows'][0]
        self.assertEqual(row['paired_buy_price'], 9)
        self.assertEqual(row['paired_fee_actual'], 4)
        self.assertEqual(row['paired_net_actual'], 46)
        self.assertEqual(row['unmatched_quantity'], 50)
        self.assertEqual(row['unmatched_sell_fee_actual'], 1)
        self.assertEqual(row['unmatched_comparison'], 99)
        self.assertEqual(row['net_vs_hold'], 145)
        self.assertEqual(report['summary']['net_vs_hold'], 145)
        self.assertEqual(report['summary']['net_status'], 'complete')

    def test_full_pair_at_a_loss_with_zero_fees_needs_no_mark(self):
        row = self.close_review(self.cycle(buy_price=11, sell_fee=0, buy_fee=0))['rows'][0]
        self.assertEqual(row['paired_fee_actual'], 0)
        self.assertEqual(row['unmatched_quantity'], 0)
        self.assertIsNone(row['unmatched_comparison'])
        self.assertEqual(row['net_vs_hold'], -100)

    def test_missing_fee_and_missing_mark_block_total_even_when_other_parts_are_known(self):
        missing_fee = self.close_review(self.cycle(paired=50, status='cash', buy_fee=None), mark=self.close_quote())
        self.assertIsNone(missing_fee['rows'][0]['paired_net_actual'])
        self.assertEqual(missing_fee['rows'][0]['paired_net_estimate'], 30)
        self.assertEqual(missing_fee['rows'][0]['unmatched_comparison'], 99)
        self.assertIsNone(missing_fee['summary']['net_vs_hold'])

        missing_mark = self.close_review(self.cycle(paired=50, status='open'))
        self.assertEqual(missing_mark['summary']['paired_net_actual'], 46)
        self.assertIsNone(missing_mark['summary']['unmatched_comparison'])
        self.assertEqual(missing_mark['summary']['net_status'], 'incomplete')

    def test_does_not_mark_from_intraday_other_date_or_future_provider_stamp(self):
        sale = self.cycle(paired=0, status='cash')
        for stamp in (self.now, self.now - timedelta(days=1), self.now + timedelta(days=1),
                      self.now.replace(hour=16, minute=59)):
            with self.subTest(stamp=stamp):
                row = self.close_review(sale, mark=self.close_quote(when=stamp))['rows'][0]
                self.assertIsNone(row['unmatched_price'])
                self.assertIsNone(row['net_vs_hold'])
        historical = self.close_review(sale, when=self.now.replace(hour=16) + timedelta(days=1),
                                       mark=self.close_quote())
        self.assertEqual(historical['summary']['net_vs_hold'], 198)
        self.assertIsNone(historical['summary']['paired_net_actual'])

    def test_report_for_no_samples_or_before_close_has_no_estimated_total(self):
        empty = self.close_review(self.cycle(day=self.now.date()-timedelta(days=1)))
        self.assertEqual(empty['summary']['net_status'], 'no_samples')
        self.assertIsNone(empty['summary']['net_vs_hold'])
        intraday = self.close_review(self.cycle(), when=self.now)
        self.assertEqual(intraday['summary']['net_status'], 'waiting_close')
        self.assertIsNone(intraday['summary']['net_vs_hold'])

    def test_duplicate_linked_broker_fills_or_invalid_cycles_prevent_day_total(self):
        first, conflicting, valid = (self.cycle(ident) for ident in ('first', 'conflicting', 'valid'))
        first['sell_fill_id'] = conflicting['sell_fill_id'] = 'broker-sell-001'
        result = self.close_review(first, conflicting, valid)
        self.assertEqual([row['id'] for row in result['rows']], ['valid'])
        self.assertEqual(result['summary']['excluded_invalid_cycles'], 2)
        self.assertEqual(result['summary']['paired_net_actual'], 95)
        self.assertIsNone(result['summary']['net_vs_hold'])

        broken = self.cycle('broken')
        broken['buyback_cost'] = 1
        result = self.close_review(broken, valid)
        self.assertEqual(result['summary']['excluded_invalid_cycles'], 1)
        self.assertEqual(result['summary']['net_status'], 'incomplete')
        self.assertIsNone(result['summary']['net_vs_hold'])


if __name__ == '__main__':
    unittest.main()
