"""Offline Tencent minute schema, clocks, cumulative fields and bounded I/O."""
from copy import deepcopy
from datetime import datetime,timedelta
import threading
import time
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import requests
import t_quotes as quotes

CN=ZoneInfo('Asia/Shanghai')
NOW=datetime(2026,11,11,10,12,20,tzinfo=CN)


def payload(code='600641',symbol='sh600641',now=NOW,count=43):
    qt=['']*88;qt[1]='测试证券';qt[2]=code;qt[3]='20';qt[30]=now.strftime('%Y%m%d%H%M%S')
    start=now.replace(hour=9,minute=30,second=0,microsecond=0)
    rows=[]
    for i in range(count):
        moment=start+timedelta(minutes=i);volume=(i+1)*100
        rows.append(f'{moment:%H%M} 20 {volume} {volume*100*20:.2f}')
    return {'code':0,'msg':'','data':{symbol:{'data':{'date':now.strftime('%Y%m%d'),'data':rows},'qt':{symbol:qt}}}}


class ParseMinutesTests(unittest.TestCase):
    def test_current_minute_excluded_and_cumulative_values_preserved(self):
        got=quotes.parse_t_minutes(payload(),'600641',NOW)
        self.assertTrue(got['valid'],got);self.assertEqual(len(got['rows']),42)
        self.assertEqual(got['rows'][-1]['timestamp'],'2026-11-11T10:11:00+08:00')
        self.assertEqual(got['rows'][-1]['cum_volume'],4200)
        self.assertEqual(got['rows'][-1]['cum_amount'],8_400_000)
        self.assertEqual(got['source_timestamp'],NOW.isoformat())
    def test_missing_full_source_clock_never_uses_fetch_time(self):
        p=payload();p['data']['sh600641']['qt']['sh600641'][30]='10:12:20'
        got=quotes.parse_t_minutes(p,'600641',NOW)
        self.assertFalse(got['valid']);self.assertIsNone(got['source_timestamp']);self.assertEqual(got['rows'],[])
    def test_source_clock_stale_or_future(self):
        for delta in (-91,1):
            with self.subTest(delta=delta):
                p=payload();p['data']['sh600641']['qt']['sh600641'][30]=(NOW+timedelta(seconds=delta)).strftime('%Y%m%d%H%M%S')
                self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW)['valid'])
    def test_other_day_or_mismatched_quote_date_rejected(self):
        p=payload();p['data']['sh600641']['data']['date']='20261110'
        self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW)['valid'])
        p=payload();p['data']['sh600641']['qt']['sh600641'][30]='20261110101220'
        self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW)['valid'])
    def test_wrong_security_and_failure_status(self):
        for mutate in ('code','section','status'):
            p=payload()
            if mutate=='code':p['data']['sh600641']['qt']['sh600641'][2]='600000'
            if mutate=='section':p['data']['sz600641']=p['data'].pop('sh600641')
            if mutate=='status':p['code']=1
            self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW)['valid'])
    def test_cumulative_amount_or_volume_never_goes_backwards(self):
        for field in (2,3):
            p=payload();rows=p['data']['sh600641']['data']['data'];fields=rows[10].split();fields[field]='1';rows[10]=' '.join(fields)
            got=quotes.parse_t_minutes(p,'600641',NOW);self.assertFalse(got['valid']);self.assertIn('回退',got['error'])
    def test_invalid_numbers_or_missing_turnover_rejected(self):
        for bad in ('1000 NaN','-1 100','1000 -1','0 100','1000','1000.5 1000'):
            p=payload();p['data']['sh600641']['data']['data'][9]='0939 20 '+bad
            self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW)['valid'],bad)
        p=payload();p['data']['sh600641']['data']['data'][9]='0939 0 1000 2000000'
        self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW)['valid'])
    def test_future_minutes_duplicates_and_non_session_times_rejected(self):
        for bad in ('1014 20 5000 10000000','0940 20 5000 10000000','1200 20 5000 10000000'):
            p=payload();p['data']['sh600641']['data']['data'].append(bad)
            self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW)['valid'])
    def test_next_end_label_is_excluded_not_used_as_completed_turn(self):
        p=payload();p['data']['sh600641']['data']['data'].append('1013 20 5000 10000000')
        got=quotes.parse_t_minutes(p,'600641',NOW)
        self.assertTrue(got['valid']);self.assertEqual(len(got['rows']),42)
        self.assertEqual(got['rows'][-1]['timestamp'],'2026-11-11T10:11:00+08:00')
        p['data']['sh600641']['data']['data'][-1]='1013 20 0 0'
        self.assertTrue(quotes.parse_t_minutes(p,'600641',NOW)['valid'])
    def test_minimum_bars_and_last_bar_freshness(self):
        clock=NOW.replace(hour=10,minute=0,second=10)
        self.assertTrue(quotes.parse_t_minutes(payload(now=clock,count=31),'600641',clock)['valid'])
        clock=NOW.replace(hour=9,minute=59,second=10)
        self.assertFalse(quotes.parse_t_minutes(payload(now=clock,count=30),'600641',clock)['valid'])
        p=payload(count=39);self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW)['valid'])
    def test_lunch_does_not_fake_quote_freshness(self):
        p=payload();p['data']['sh600641']['qt']['sh600641'][30]='20261111113000'
        self.assertFalse(quotes.parse_t_minutes(p,'600641',NOW.replace(hour=12,minute=0))['valid'])
    def test_real_asset_prefix_for_etf(self):
        got=quotes.parse_t_minutes(payload('159562','sz159562'),'159562',NOW)
        self.assertTrue(got['valid'],got)


class FetchMinutesTests(unittest.TestCase):
    cfg={'positions':[{'code':'600641','type':'stock','quantity':300},
                      {'code':'159562','type':'etf','quantity':5700},
                      {'code':'123290','type':'convertible','quantity':100},
                      {'code':'002463','type':'stock','quantity':0}],
         'watchlist':[{'code':'600406'}]}
    def test_only_positive_stock_and_etf_positions_requested(self):
        def provider(code):return payload(code,'sz'+code if code.startswith('1') else 'sh'+code)
        with patch.object(quotes,'_fetch_payload',side_effect=provider) as get:
            got=quotes.fetch_t_minutes(self.cfg,NOW)
        self.assertEqual(set(got),{'600641','159562'});self.assertTrue(all(v['valid'] for v in got.values()))
        self.assertEqual({x.args[0] for x in get.call_args_list},{'600641','159562'})
    def test_request_uses_provider_endpoint_and_bounded_socket_timeout(self):
        response=unittest.mock.Mock();response.json.return_value=payload();response.raise_for_status.return_value=None
        with patch.object(quotes.requests,'get',return_value=response) as get:
            quotes._fetch_payload('600641')
        self.assertEqual(get.call_args.args[0],'https://web.ifzq.gtimg.cn/appstock/app/minute/query')
        self.assertEqual(get.call_args.kwargs['params'],{'code':'sh600641'})
        self.assertEqual(get.call_args.kwargs['timeout'],(2,3))
    def test_transport_failure_yields_no_rows_or_stale_fallback(self):
        with patch.object(quotes,'_fetch_payload',side_effect=requests.Timeout('late')):
            got=quotes.fetch_t_minutes({'positions':[self.cfg['positions'][0]]},NOW)
        self.assertFalse(got['600641']['valid']);self.assertEqual(got['600641']['rows'],[])
        self.assertIn('Timeout',got['600641']['error'])
    def test_timeout_single_flight_and_late_result_discarded(self):
        entered=threading.Event();release=threading.Event();cfg={'positions':[self.cfg['positions'][0]]};calls=[]
        def slow(code):
            calls.append(code);entered.set();release.wait(2);return payload()
        try:
            with patch.object(quotes,'_fetch_payload',side_effect=slow):
                start=time.monotonic();first=quotes.fetch_t_minutes(cfg,NOW,deadline_seconds=.02)
                self.assertLess(time.monotonic()-start,.4);self.assertTrue(entered.is_set())
                self.assertFalse(first['600641']['valid']);self.assertEqual(first['600641']['rows'],[])
                second=quotes.fetch_t_minutes(cfg,NOW,deadline_seconds=.02)
                self.assertFalse(second['600641']['valid']);self.assertIn('上一轮',second['600641']['error']);self.assertEqual(len(calls),1)
                release.set()
                with quotes._FLIGHT_LOCK:prior=quotes._FLIGHTS.get('600641')
                if prior is not None:prior.result(timeout=2)
                third=quotes.fetch_t_minutes(cfg,NOW,deadline_seconds=.2)
                self.assertEqual(len(calls),2);self.assertTrue(third['600641']['valid'])
        finally:release.set()
    def test_deadline_cannot_exceed_eight_seconds(self):
        for value in (0,9):
            with self.assertRaises(ValueError):quotes.fetch_t_minutes(self.cfg,NOW,deadline_seconds=value)

if __name__=='__main__':unittest.main()
