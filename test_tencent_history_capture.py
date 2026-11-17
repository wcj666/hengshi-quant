from datetime import datetime
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path

from tencent_history_capture import capture_day, normalize_market_day, REGULAR, CN


DAY = '2026-11-16'
SYMBOL = 'sh588170'
CODE = '588170'
AFTER_CLOSE = datetime(2026, 11, 16, 15, 14, tzinfo=CN)


def responses():
    low = high = close = Decimal('.800')
    labels = ['0930', *REGULAR]
    klines, turnover, cumulative = [], [], 0
    for index, stamp in enumerate(labels):
        shares = 1000 + (1 if 20 <= index <= 29 else 0)
        cumulative += shares
        klines.append([DAY.replace('-', '') + stamp, str(close), str(close), str(high),
                       str(low), str(Decimal(shares)/100)])
        turnover.append(f'{stamp} {close} {cumulative // 100} {Decimal(cumulative)*close}')
    quote = [''] * 49
    for index, value in {2: CODE, 3: '0.800', 4: '0.790', 5: '0.800',
                         30: '20261116150105', 47: '0.948', 48: '0.632'}.items():
        quote[index] = value
    payloads = {'code': 0, 'data': {SYMBOL: {'data': [{'date': '20261116', 'data': turnover}],
                                              'qt': {SYMBOL: quote}}}}
    kline = {'code': 0, 'data': {SYMBOL: {'m1': klines, 'qt': {SYMBOL: quote.copy()}}}}
    return kline, payloads


def entry(kline, turnover):
    return normalize_market_day(CODE, DAY, AFTER_CLOSE, kline=kline, turnover=turnover)


class TencentHistoryCaptureTests(unittest.TestCase):
    def test_precise_share_volume_and_actual_amount_are_joined(self):
        kline, turnover = responses()
        report = entry(kline, turnover)
        self.assertEqual((report['minutes_count'], report['turnover_count'], report['amount_count']), (240, 240, 240))
        self.assertEqual(report['session']['minutes'][19]['volume'], 1001)
        self.assertEqual(report['session']['minutes'][19]['amount'], 800.8)
        self.assertEqual(report['session']['open'], .8)
        self.assertEqual(report['session']['upper_limit'], .948)
        self.assertFalse(report['session']['corporate_action_clear'])
        self.assertEqual(report['raw_responses']['day'], turnover)

    def test_outside_hilo_is_not_amount_data_and_does_not_discard_ohlcv(self):
        kline, turnover = responses()
        rows = turnover['data'][SYMBOL]['data'][0]['data']
        stamp, last_px, qty, cumulative = rows[30].split()
        rows[30] = f'{stamp} {last_px} {qty} {Decimal(cumulative)+100}'
        report = entry(kline, turnover)
        self.assertEqual(report['amount_count'], 238)
        self.assertEqual(report['discrepancy_count'], 2)
        self.assertNotIn('amount', report['session']['minutes'][29])
        self.assertNotIn('amount', report['session']['minutes'][30])
        self.assertEqual(report['session']['minutes'][29]['volume'], 1000)
        self.assertEqual(report['raw_responses']['day']['data'][SYMBOL]['data'][0]['data'][30], rows[30])

    def test_missing_predecessor_never_assigns_multiminute_difference(self):
        kline, turnover = responses()
        rows = turnover['data'][SYMBOL]['data'][0]['data']
        del rows[40]
        report = entry(kline, turnover)
        self.assertEqual(report['turnover_count'], 239)
        self.assertEqual(report['amount_count'], 238)
        self.assertNotIn('amount', report['session']['minutes'][40])
        self.assertIn('跨越缺口', report['discrepancies'][0]['reason'])

    def test_wrong_day_or_future_quote_keeps_raw_data_without_session(self):
        for wrong_date in ('20261117160000', '20261116160000'):
            kline, turnover = responses()
            kline['data'][SYMBOL]['qt'][SYMBOL][30] = wrong_date
            turnover['data'][SYMBOL]['qt'][SYMBOL][30] = wrong_date
            report = entry(kline, turnover)
            self.assertIsNone(report['session'])
            self.assertEqual(report['amount_count'], 240)
            self.assertEqual(len(report['raw_sha256']['m1']), 64)

    def test_capture_has_exact_path_reuses_complete_archive_and_gates_close(self):
        with TemporaryDirectory() as tmp:
            kline, turnover = responses()
            queries = []
            def fetch_k(symbol, day):
                queries.append((symbol, day))
                return kline
            def fetch_t(symbol):
                queries.append(symbol)
                return turnover

            kwargs = dict(output=tmp, kline_fetch=fetch_k, turnover_fetch=fetch_t)
            with self.assertRaisesRegex(ValueError, '15:05'):
                capture_day(DAY, [CODE], now=AFTER_CLOSE.replace(hour=14), **kwargs)
            self.assertEqual(queries, [])
            res = capture_day(DAY, [CODE], now=AFTER_CLOSE, **kwargs)
            self.assertTrue(res['results'][CODE]['has_session'])
            self.assertEqual(len(queries), 2)
            stored = json.loads((Path(tmp) / DAY / f'{CODE}.json').read_text())
            self.assertEqual(stored['session']['minutes'][0]['time'], '09:31')
            repeat = capture_day(DAY, [CODE], now=AFTER_CLOSE, **kwargs)
            self.assertTrue(repeat['results'][CODE]['archived'])
            self.assertEqual(len(queries), 2)


if __name__ == '__main__':
    unittest.main()
