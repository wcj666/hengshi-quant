"""Tencent quote regressions using synthetic rows shaped like a verified response.

No network calls. The provider's field 30 supplies Beijing date/time; local fetch
time must never stand in for a missing provider clock.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
import unittest
from unittest.mock import patch

import requests

import portfolio_quotes as quotes
from quote_health import assess, parse_provider_time


TARGETS = {
    ('etf', '159562'), ('etf', '159929'), ('stock', '600641'),
    ('stock', '002463'), ('stock', '300274'), ('stock', '002080'),
}
CFG = {
    'positions': [dict(type=kind, code=code, quantity=100) for kind, code in sorted(TARGETS)],
    'watchlist': [{'code': '000682'}, {'code': '600641'}],
}


def row(symbol, code, name='测试证券', current='33.21', previous='32.72', stamp='20261110145145'):
    fields = [''] * 88
    fields[0], fields[1], fields[2] = '51', name, code
    fields[3], fields[4], fields[30] = current, previous, stamp
    return f'v_{symbol}="' + '~'.join(fields) + '";'


SAMPLE = '\n'.join([
    row('sz159562', '159562', '黄金股ETF华夏', '1.928', '1.912', '20261110145142'),
    row('sz159929', '159929', '医药ETF', '1.334', '1.338'),
    row('sh600641', '600641', '先导基电'),
    row('sz002463', '002463', '沪电股份', '81.82', '77.10'),
    row('sz300274', '300274', '阳光电源', '78.44', '77.85'),
    row('sz002080', '002080', '中材科技', '33.78', '33.95'),
])


def response(text=SAMPLE):
    result = requests.Response()
    result.status_code = 200
    result.url = 'https://qt.gtimg.cn/'
    result._content = text.encode('gb18030')
    return result


class TencentQuotesTest(unittest.TestCase):
    def test_market_symbols(self):
        expected = {'600641': 'sh600641', '002463': 'sz002463', '300274': 'sz300274',
                    '159562': 'sz159562', '159929': 'sz159929', '510300': 'sh510300',
                    '113000': 'sh113000', '123290': 'sz123290'}
        for code, symbol in expected.items():
            self.assertEqual(quotes._tencent_symbol(code), symbol)

    def test_open_and_limit_metadata_not_invented(self):
        sample=row('sh600641','600641')
        feed,_=quotes._parse_tencent_quotes(sample,{('stock','600641')})
        self.assertIsNone(feed[('stock','600641')][4]['open'])
        self.assertFalse(feed[('stock','600641')][4]['limit_status_verified'])
        fields=sample.split('"')[1].split('~');fields[5]='32.9';fields[47]='35.99';fields[3]='35.99'
        feed,_=quotes._parse_tencent_quotes('v_sh600641="'+'~'.join(fields)+'";',{('stock','600641')})
        self.assertEqual(feed[('stock','600641')][4]['open'],32.9)
        self.assertTrue(feed[('stock','600641')][4]['at_limit_up'])

    def test_all_six_holdings_keep_requested_asset_type(self):
        feed, errors = quotes._parse_tencent_quotes(SAMPLE, TARGETS)
        self.assertEqual(set(feed), TARGETS)
        self.assertFalse(errors)
        self.assertEqual(feed[('etf', '159562')][:3], (1.928, '黄金股ETF华夏', 1.912))
        self.assertEqual(feed[('stock', '600641')][:3], (33.21, '先导基电', 32.72))
        self.assertEqual(parse_provider_time(feed[('stock', '600641')][3]),
                         datetime(2026, 11, 10, 6, 51, 45, tzinfo=timezone.utc))

    def test_batched_request_decodes_gb18030_and_excludes_watches(self):
        with patch.object(quotes.requests, 'get', return_value=response()) as get:
            feed, errors = quotes.snapshot_selected_tencent(CFG, include_watch=False)
        self.assertEqual(set(feed), TARGETS)
        self.assertFalse(errors)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], 'https://qt.gtimg.cn/')
        sent = set(get.call_args.kwargs['params']['q'].split(','))
        self.assertEqual(sent, {quotes._tencent_symbol(code) for _, code in TARGETS})
        self.assertEqual(feed[('stock', '600641')][1], '先导基电')

    def test_live_compatibility_entry_uses_tencent(self):
        with patch.object(quotes, 'snapshot_selected_tencent', return_value=({'sentinel': 1}, [])) as new:
            self.assertEqual(quotes.snapshot_selected_akshare(CFG, include_watch=False, deadline_seconds=2),
                             ({'sentinel': 1}, []))
        new.assert_called_once_with(CFG, include_watch=False, deadline_seconds=2)

    def test_unrequested_or_mismatched_security_is_not_used(self):
        target = {('etf', '159562')}
        mismatched = row('sz159562', '159929', current='1.928', previous='1.912')
        feed, errors = quotes._parse_tencent_quotes(mismatched, target)
        self.assertFalse(feed)
        self.assertTrue(errors)
        extra = row('sh600000', '600000')
        feed, errors = quotes._parse_tencent_quotes(SAMPLE + '\n' + extra, TARGETS)
        self.assertEqual(set(feed), TARGETS)

    def test_partial_batch_preserves_valid_quotes_and_reports_missing(self):
        text = row('sh600641', '600641', '先导基电')
        feed, errors = quotes._parse_tencent_quotes(text, TARGETS)
        self.assertEqual(set(feed), {('stock', '600641')})
        message = '\n'.join(errors)
        for kind, code in TARGETS - {('stock', '600641')}:
            self.assertIn(code, message)
        cfg = {'positions': [dict(type=kind, code=code, quantity=100) for kind, code in TARGETS]}
        health = assess(cfg, feed, now=datetime(2026, 11, 10, 6, 51, 50, tzinfo=timezone.utc))
        self.assertFalse(health['all_held_usable'])
        self.assertTrue(health['block_trade_signals'])

    def test_duplicate_rows_are_not_arbitrarily_selected(self):
        target = {('stock', '600641')}
        text = row('sh600641', '600641', current='33.21') + '\n' + row('sh600641', '600641', current='99.99')
        feed, errors = quotes._parse_tencent_quotes(text, target)
        self.assertFalse(feed)
        self.assertTrue(errors)

    def test_held_etf_in_watchlist_keeps_one_etf_quote(self):
        cfg = {'positions': [{'type': 'etf', 'code': '159562', 'quantity': 100}],
               'watchlist': [{'code': '159562'}]}
        with patch.object(quotes.requests, 'get', return_value=response()) as get:
            feed, errors = quotes.snapshot_selected_tencent(cfg)
        self.assertEqual(set(feed), {('etf', '159562')})
        self.assertEqual(get.call_args.kwargs['params']['q'], 'sz159562')

    def test_empty_invalid_and_nonfinite_current_prices_are_missing(self):
        target = {('stock', '600641')}
        for text in ('', 'v_pv_none_match="1";', 'v_sh600641="51~短数据";'):
            with self.subTest(text=text):
                feed, errors = quotes._parse_tencent_quotes(text, target)
                self.assertFalse(feed)
                self.assertTrue(errors)
        for bad in ('', '-', '0', '-1', 'NaN', 'inf'):
            with self.subTest(price=bad):
                feed, errors = quotes._parse_tencent_quotes(row('sh600641', '600641', current=bad), target)
                self.assertFalse(feed)
                self.assertTrue(errors)

    def test_missing_or_invalid_provider_clock_is_not_fabricated(self):
        target = {('stock', '600641')}
        for stamp in ('', '145145', '20261310145145', '20261110149945'):
            with self.subTest(stamp=stamp):
                feed, errors = quotes._parse_tencent_quotes(row('sh600641', '600641', stamp=stamp), target)
                self.assertEqual(feed[('stock', '600641')][0], 33.21)
                self.assertIsNone(parse_provider_time(feed[('stock', '600641')][3]))
                health = assess({'positions': [{'type': 'stock', 'code': '600641', 'quantity': 100}]},
                                feed, now=datetime(2026, 11, 10, 6, 51, 50, tzinfo=timezone.utc))
                self.assertFalse(health['provider_timestamps_verified'])
                self.assertTrue(health['block_trade_signals'])

    def test_bad_previous_close_does_not_invent_daily_pnl_input(self):
        feed, _ = quotes._parse_tencent_quotes(row('sh600641', '600641', previous='-'), {('stock', '600641')})
        self.assertEqual(feed[('stock', '600641')][0], 33.21)
        self.assertIsNone(feed[('stock', '600641')][2])

    def test_stale_or_future_provider_time_still_blocks_signals(self):
        target = {('stock', '600641')}
        cfg = {'positions': [{'type': 'stock', 'code': '600641', 'quantity': 100}]}
        now = datetime(2026, 11, 10, 6, 51, 50, tzinfo=timezone.utc)
        for stamp in ('20261110144900', '20261110145200'):
            with self.subTest(stamp=stamp):
                feed, _ = quotes._parse_tencent_quotes(row('sh600641', '600641', stamp=stamp), target)
                health = assess(cfg, feed, now=now)
                self.assertFalse(health['all_held_usable'])
                self.assertFalse(health['provider_timestamps_verified'])
                self.assertTrue(health['block_trade_signals'])

    def test_http_failure_and_timeout_do_not_reuse_last_success(self):
        cfg = {'positions': [{'type': 'stock', 'code': '600641', 'quantity': 100}]}
        with patch.object(quotes.requests, 'get', return_value=response(row('sh600641', '600641'))):
            good, _ = quotes.snapshot_selected_tencent(cfg)
        self.assertIn(('stock', '600641'), good)
        for exception in (requests.Timeout('simulated timeout'), requests.HTTPError('simulated HTTP error')):
            with self.subTest(exception=type(exception).__name__):
                with patch.object(quotes.requests, 'get', side_effect=exception):
                    feed, errors = quotes.snapshot_selected_tencent(cfg)
                self.assertFalse(feed)
                self.assertTrue(errors)

    def test_transient_transport_failure_retries_once_with_new_response(self):
        cfg = {'positions': [{'type': 'stock', 'code': '600641', 'quantity': 100}]}
        with patch.object(quotes.requests, 'get', side_effect=[
            requests.Timeout('first attempt'), response(row('sh600641', '600641')),
        ]) as get:
            feed, errors = quotes.snapshot_selected_tencent(cfg)
        self.assertFalse(errors)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(feed[('stock', '600641')][0], 33.21)

    def test_deadline_single_flight_discards_late_result(self):
        cfg = {'positions': [{'type': 'stock', 'code': '600000', 'quantity': 100}]}
        started, release, returned = threading.Event(), threading.Event(), threading.Event()
        calls = []

        def get(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                release.wait(3)
                returned.set()
                return response(row('sh600000', '600000', current='10.00'))
            return response(row('sh600000', '600000', current='11.00'))

        with patch.object(quotes.requests, 'get', side_effect=get):
            try:
                feed, errors = quotes.snapshot_selected_tencent(cfg, deadline_seconds=.1)
                self.assertTrue(started.is_set())
                self.assertFalse(feed)
                self.assertTrue(errors)
                feed, errors = quotes.snapshot_selected_tencent(cfg, deadline_seconds=.1)
                self.assertFalse(feed)
                self.assertTrue(errors)
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
            self.assertTrue(returned.wait(1))
            # Give the worker time to parse/release its future; its price is never
            # accepted by a subsequent polling cycle as a newly fetched result.
            time.sleep(.03)
            fresh, errors = quotes.snapshot_selected_tencent(cfg, deadline_seconds=1)
            self.assertEqual(fresh[('stock', '600000')][0], 11.0)
            self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
