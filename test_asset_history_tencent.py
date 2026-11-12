"""Bounded real-data provider routing, adjustment, dates and units (offline)."""
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import pandas as pd

import asset_history as history


TODAY = date(2026,11,12)
ROWS = [
    ['2026-11-11','78.57','80.09','80.50','76.81','659752',{},'4.16','521184.47',''],
    ['2026-11-12','78.49','78.26','80.70','78.16','522597',{},'3.29','414058',''],
]


def payload(rows=None, key='qfqday', symbol='sz300274'):
    return {'code':0,'data':{symbol:{key:deepcopy(ROWS if rows is None else rows)}}}


def parse(data, symbol='sz300274', adjust='qfq'):
    return history._tx_frame(data,symbol,adjust=adjust,begin=date(2026,11,1),end=TODAY,today=TODAY)


class TencentDailyTests(unittest.TestCase):
    def test_adjusted_prices_amount_and_volume_are_provider_fields(self):
        df=parse(payload())
        self.assertEqual(list(df.date.dt.strftime('%Y-%m-%d')),['2026-11-11','2026-11-12'])
        self.assertEqual(df.iloc[-1].close,78.26)
        self.assertEqual(df.iloc[-1].volume,52_259_700)
        self.assertEqual(df.iloc[-1].amount,4_140_580_000)
        self.assertEqual(df.attrs['adjust'],'qfq')

    def test_raw_candles_cannot_masquerade_as_qfq(self):
        with self.assertRaisesRegex(ValueError,'复权'):parse(payload(key='day'))
        self.assertEqual(len(parse(payload(key='day'),adjust='')),2)

    def test_missing_amount_stays_missing_and_does_not_use_close_times_volume(self):
        df=parse(payload(rows=[row[:6] for row in ROWS]))
        self.assertNotIn('amount',df)
        self.assertEqual(df.iloc[-1].close,78.26)

    def test_bad_identity_status_empty_and_oversized_rejected(self):
        for data in [payload(symbol='sh300274'),{'code':1,'data':{}},
                     {'code':False,'data':payload()['data']},payload(rows=[]),payload(rows=ROWS*400)]:
            with self.subTest(data=str(data)[:50]),self.assertRaises(ValueError):parse(data)

    def test_duplicate_future_nonfinite_and_inconsistent_prices_rejected(self):
        cases=[]
        cases.append(ROWS+[ROWS[-1]])
        for index,value in [(0,'2026-11-13'),(2,'nan'),(3,'76'),(4,'81'),(5,'-1'),(8,'inf')]:
            rows=deepcopy(ROWS);rows[-1][index]=value;cases.append(rows)
        for rows in cases:
            with self.subTest(rows=rows[-1]),self.assertRaises(ValueError):parse(payload(rows=rows))

    def test_query_current_end_is_open_but_results_obey_requested_window(self):
        fixed=SimpleNamespace(now=lambda zone:datetime(2026,11,12,15,8,tzinfo=zone))
        response=SimpleNamespace(content=b'{}',json=lambda:payload(),raise_for_status=lambda:None)
        with patch.object(history,'datetime',fixed),patch('requests.get',return_value=response) as get:
            df=history.tencent_history('300274','stock','20261112','20261112')
        self.assertEqual(len(df),1)
        self.assertEqual(get.call_args.kwargs['params'],{'param':'sz300274,day,,,640,qfq'})
        self.assertEqual(get.call_args.kwargs['timeout'],(3,5))

    def test_past_end_uses_exclusive_next_day_boundary(self):
        fixed=SimpleNamespace(now=lambda zone:datetime(2026,11,12,15,8,tzinfo=zone))
        response=SimpleNamespace(content=b'{}',json=lambda:payload(),raise_for_status=lambda:None)
        with patch.object(history,'datetime',fixed),patch('requests.get',return_value=response) as get:
            df=history.tencent_history('300274','stock','20261101','20261111')
        self.assertEqual(len(df),1)
        self.assertEqual(get.call_args.kwargs['params']['param'],'sz300274,day,,2026-11-12,640,qfq')

    def test_etf_and_csi300_have_explicit_unadjusted_market_identity(self):
        self.assertEqual(history._tx_symbol('159929','etf'),'sz159929')
        self.assertEqual(history._tx_symbol('000300','index'),'sh000300')
        self.assertEqual(history._tx_symbol('688001','stock'),'sh688001')
        with self.assertRaises(ValueError):history._tx_symbol('000001','index')
        with self.assertRaises(ValueError):history._tx_symbol('abc','stock')

    def test_star_volume_is_not_multiplied_again(self):
        df=parse(payload(symbol='sh688001'),'sh688001')
        self.assertEqual(df.iloc[-1].volume,522597)

    def test_tencent_primary_avoids_rate_limited_eastmoney(self):
        wanted=parse(payload())
        with patch.object(history,'tencent_history',return_value=wanted),patch('app.stock_history',side_effect=AssertionError('unneeded Eastmoney')):
            self.assertIs(history.history_by_asset('300274','stock','20260101'),wanted)

    def test_stock_and_benchmark_fallback_remain_real_provider_data(self):
        wanted=parse(payload())
        with (patch.object(history,'tencent_history',side_effect=ValueError('no verified qfq')),
              patch('app.stock_history',return_value=wanted)):
            self.assertEqual(history.history_by_asset('300274','stock','20260101').iloc[-1].close,78.26)
        with (patch.object(history,'tencent_history',side_effect=ValueError('failure')),
              patch('akshare.index_zh_a_hist',return_value=wanted) as fallback):
            self.assertEqual(history.benchmark_history('20260101','20261112').iloc[-1].close,78.26)
            self.assertEqual(fallback.call_args.kwargs['symbol'],'000300')


if __name__=='__main__':unittest.main()
