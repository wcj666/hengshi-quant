"""Calendar, freshness, failure recovery and real-account isolation checks."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from paper_contest import CN, execute, continue_account
from paper_runtime import refresh_store, read_status
from paper_view import load_snapshot
from test_paper_contest import account, quote
from web_dashboard import create_app


class PaperRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=self.root/'output/paper_contest_20261113_20261119'
        self.store.mkdir(parents=True);self.path=self.store/'state.json'
        self.state=account([{'code':'600406','name':'测试证券','type':'stock',
            'quantity':200,'available_quantity':100,'cost':15.,
            'screenshot_price':10.,'market_value':2000.}])
        self.path.write_text(json.dumps(self.state))
        self.cfg=self.root/'config.json';self.cfg.write_text('private real config: never touch')
        self.now=datetime(2026,11,13,10,tzinfo=CN)

    def fetcher(self, now=None):
        return Mock(return_value=({'600406':quote(now or self.now,9.8,9.79,9.8)},[]))

    def test_continued_account_refreshes_and_accepts_new_verified_calendar(self):
        now=self.now.replace(day=20)
        continue_account(self.state,{'verified':True,'dates':self.state['calendar']},'用户确认',now)
        self.path.write_text(json.dumps(self.state))
        before=self.cfg.read_bytes()
        result=refresh_store(self.store,now=now,fetcher=self.fetcher(now))
        self.assertEqual(result['status'],'ok')
        later=now.replace(day=23)
        (self.root/'output'/'strategy_calendar.json').write_text(json.dumps({
            'verified':True,'dates':self.state['calendar']+['2026-11-23']}))
        result=refresh_store(self.store,now=later,fetcher=self.fetcher(later))
        self.assertEqual(result['status'],'ok')
        state=json.loads(self.path.read_text())
        self.assertEqual(state['fixed_rules_hash'],self.state['fixed_rules_hash'])
        self.assertIn('2026-11-23',state['continuation']['calendar'])
        self.assertEqual(state['trades'],[])
        self.assertEqual(self.cfg.read_bytes(),before)
        weekend=now.replace(day=21)
        feed=self.fetcher(weekend)
        self.assertEqual(refresh_store(self.store,now=weekend,fetcher=feed)['status'],'idle')
        feed.assert_not_called()

    def test_poll_updates_prices_and_nav_without_orders_or_real_config(self):
        before=self.cfg.read_bytes()
        result=refresh_store(self.store,now=self.now,fetcher=self.fetcher())
        state=json.loads(self.path.read_text());view=load_snapshot(self.store,self.now)
        self.assertEqual(result['status'],'ok');self.assertEqual(state['last_equity'],99960.)
        self.assertEqual(view['positions'][0]['price'],9.8);self.assertFalse(view['stale'])
        self.assertEqual(state['trades'],[]);self.assertEqual(self.cfg.read_bytes(),before)

    def test_missing_or_failed_quotes_preserve_last_good_nav_and_recover(self):
        refresh_store(self.store,now=self.now,fetcher=self.fetcher())
        before=self.path.read_bytes()
        for fetcher in [Mock(return_value=({},['provider timed out'])),Mock(side_effect=OSError('offline'))]:
            result=refresh_store(self.store,now=self.now+timedelta(minutes=1),force=True,fetcher=fetcher)
            self.assertEqual(result['status'],'error');self.assertEqual(self.path.read_bytes(),before)
            self.assertEqual(result['last_success_at'],self.now.isoformat())
        result=refresh_store(self.store,now=self.now+timedelta(minutes=2),fetcher=self.fetcher(self.now+timedelta(minutes=2)))
        self.assertEqual(result['status'],'ok')

    def test_weekend_holiday_lunch_and_outside_contest_never_fetch_even_force(self):
        for now in [self.now.replace(day=14),self.now.replace(hour=12),
                    self.now.replace(day=20),self.now.replace(hour=9,minute=0)]:
            fetcher=self.fetcher(now);before=self.path.read_bytes()
            self.assertEqual(refresh_store(self.store,now=now,force=True,fetcher=fetcher)['status'],'idle')
            fetcher.assert_not_called();self.assertEqual(self.path.read_bytes(),before)
        state=deepcopy(self.state);state['calendar'].remove('2026-11-13')
        from paper_contest import fixed_rules_hash
        state['fixed_rules_hash']=fixed_rules_hash(state);self.path.write_text(json.dumps(state))
        fetcher=self.fetcher();refresh_store(self.store,now=self.now,fetcher=fetcher)
        fetcher.assert_not_called()

    def test_weekend_fill_rejected_even_if_calendar_contains_it(self):
        state=deepcopy(self.state);state['calendar'].append('2026-11-14')
        now=self.now.replace(day=14)
        with self.assertRaises(ValueError):
            execute(state,'600406','sell',100,{'600406':quote(now)},now,'weekend')
        self.assertFalse(state['trades'])

    def test_poll_throttled_and_closing_is_only_accepted_after_provider_close(self):
        refresh_store(self.store,now=self.now,fetcher=self.fetcher())
        fetcher=self.fetcher();refresh_store(self.store,now=self.now+timedelta(seconds=10),fetcher=fetcher)
        fetcher.assert_not_called()
        later=self.now.replace(hour=15,minute=10)
        self.assertEqual(refresh_store(self.store,now=later,fetcher=self.fetcher())['status'],'error')
        view=load_snapshot(self.store,later)
        self.assertTrue(view['stale']);self.assertIn('等待当日',view['label'])
        self.assertEqual(refresh_store(self.store,now=later+timedelta(minutes=1),
            fetcher=self.fetcher(later.replace(minute=0)))['status'],'ok')
        view=load_snapshot(self.store,later+timedelta(hours=2))
        self.assertFalse(view['stale']);self.assertEqual(view['label'],'收盘估值')

    def test_manual_refresh_is_csrf_protected_and_cannot_place_orders(self):
        client=create_app(self.root).test_client();headers={'Host':'localhost'}
        self.assertEqual(client.post('/api/paper-contest/refresh',headers=headers).status_code,403)
        token=client.get('/api/session',headers=headers).json['csrf_token']
        headers['X-CSRF-Token']=token
        with patch('paper_runtime.clock_now',return_value=self.now),patch('paper_runtime.fetch_quotes',self.fetcher()):
            r=client.post('/api/paper-contest/refresh',headers=headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.json['snapshot']['equity'],99960.)
        self.assertEqual(r.json['snapshot']['trades'],[])


if __name__=='__main__':unittest.main()
