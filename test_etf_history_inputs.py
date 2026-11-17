"""Verify real-data preparation boundaries without network or account access."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from etf_history_inputs import load_daily, parse_daily, refresh_daily


CST = ZoneInfo('Asia/Shanghai')
CALENDAR = ['2026-11-13', '2026-11-16', '2026-11-17', '2026-11-18']
PRIOR, TODAY = CALENDAR[1:3]
CLOCK = datetime(2026, 11, 17, 15, 6, tzinfo=CST)
ROWS = [
    [PRIOR, '0.839', '0.813', '0.842', '0.808', '3691555', {}, '0', '30384.43'],
    [TODAY, '0.802', '0.799', '0.822', '0.797', '2666055', {}, '0', '21525'],
]


def payload(code, rows=None, quote_stamp='20261117150001', matching=True):
    bars = deepcopy(ROWS if rows is None else rows)
    if code == '000300' and rows is None:
        for bar in bars:
            bar[1:6] = ['3965', '3963', '3985', '3942', '184670000']
    symbol = ('sz' if code.startswith('15') else 'sh') + code
    quote = [''] * 37
    quote[2], quote[30] = code, quote_stamp
    quote[5], quote[3], quote[33], quote[34], quote[36] = bars[-1][1:6]
    if not matching:
        quote[36] = '1'
    return {'code': 0, 'data': {symbol: {'day': bars, 'qt': {symbol: quote}}}}


class DailyInputsTests(unittest.TestCase):
    def test_raw_true_etf_amount_and_index_price_only(self):
        bars, skipped, close_at = parse_daily(payload('159971'), '159971', CALENDAR, CLOCK)
        self.assertFalse(skipped)
        self.assertEqual(close_at, '2026-11-17T15:00:01+08:00')
        self.assertEqual([row['date'] for row in bars], [PRIOR, TODAY])
        self.assertEqual(bars[0]['volume'], 369155500)
        self.assertEqual(bars[0]['amount'], 303844300)
        self.assertEqual(bars[0]['close'], .813)
        benchmark, _, _ = parse_daily(payload('000300'), '000300', CALENDAR, CLOCK)
        self.assertEqual(benchmark[0]['close'], 3963)
        self.assertNotIn('amount', benchmark[0])

    def test_intraday_stale_or_disagreeing_same_day_quote_not_admitted(self):
        samples = [
            (payload('159971', quote_stamp='20261117145959'), CLOCK),
            (payload('159971', quote_stamp='20261116153000'), CLOCK),
            (payload('159971', quote_stamp='20261117150630'), CLOCK),
            (payload('159971', matching=False), CLOCK),
            (payload('159971'), CLOCK.replace(minute=4)),
        ]
        for response, now in samples:
            bars, skipped, close_at = parse_daily(response, '159971', CALENDAR, now)
            self.assertEqual([row['date'] for row in bars], [PRIOR])
            self.assertIn(TODAY, skipped)
            self.assertIsNone(close_at)

    def test_missing_amount_and_mismatched_units_do_not_generate_daily_bars(self):
        for amount in (None, '', '303844300', False, 'NaN'):
            response = payload('159971')
            response['data']['sz159971']['day'][0][8] = amount
            bars, skipped, _ = parse_daily(response, '159971', CALENDAR, CLOCK)
            self.assertIn(PRIOR, skipped)
            self.assertEqual([row['date'] for row in bars], [TODAY])
        response = payload('159971')
        response['data']['sz159971']['qfqday'] = response['data']['sz159971'].pop('day')
        with self.assertRaisesRegex(ValueError, '原始 day'):
            parse_daily(response, '159971', CALENDAR, CLOCK)

    def test_duplicates_and_future_rows_are_not_cached(self):
        for rows in (ROWS + [ROWS[-1]], ROWS + [[CALENDAR[-1], *ROWS[-1][1:]]]):
            with self.assertRaisesRegex(ValueError, '重复、乱序或来自未来'):
                parse_daily(payload('159971', rows), '159971', CALENDAR, CLOCK)

    def test_refresh_keeps_per_symbol_cache_during_failure_and_skips_current_day(self):
        with TemporaryDirectory() as tmp:
            early = CLOCK.replace(hour=14, minute=0)
            with patch('etf_history_inputs._fetch', side_effect=lambda code, _: (payload(code), 'a'*64)):
                got = refresh_daily(tmp, ('159971', '588170'), CALENDAR, now=early)
            self.assertEqual([bars[-1]['date'] for bars in got.values()], [PRIOR]*3)
            with patch('etf_history_inputs._fetch', side_effect=OSError('网络中断')):
                failed = refresh_daily(tmp, ('159971', '588170'), CALENDAR, now=CLOCK)
            self.assertEqual(failed, got)
            from json import loads
            state = loads((Path(tmp) / 'tencent_history_daily' / 'status.json').read_text())
            self.assertEqual(set(state['errors']), {'159971', '588170', '000300'})
            with patch('etf_history_inputs._fetch', side_effect=lambda code, _: (payload(code), 'a'*64)):
                closed = refresh_daily(tmp, ('159971', '588170'), CALENDAR, now=CLOCK)
            self.assertEqual([bars[-1]['date'] for bars in closed.values()], [TODAY]*3)
            self.assertEqual(load_daily(tmp, ('159971', '588170'), CALENDAR, now=early), got)
            with patch('etf_history_inputs._fetch', side_effect=AssertionError('已完成当天，不重复拉取')):
                self.assertEqual(refresh_daily(tmp, ('159971', '588170'), CALENDAR, now=CLOCK), closed)


if __name__ == '__main__':
    unittest.main()
