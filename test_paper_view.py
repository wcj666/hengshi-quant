"""Paper API, freshness, and separation from real account data."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
import time
from paper_contest import initialize,observe,execute,money,CN,continue_account
from paper_view import load_snapshot
from web_dashboard import CSRF, create_app


class PaperViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=self.root/'output/paper_contest_20261113_20261119'
        self.store.mkdir(parents=True);self.file=self.store/'state.json'
        opening={'asof':'2026-11-12T15:07:00+08:00','available_cash':98000.,'total_assets':100000.,
                 'total_market_value':2000.,'broker_total_pnl':-1234.56,
                 'account_suffix':'SECRET','source_images':['/private/screenshot.jpg'],
                 'positions':[{'code':'600406','name':'测试证券','type':'stock','quantity':200,
                    'available_quantity':100,'cost':15.,'screenshot_price':10.,'market_value':2000.}]}
        self.state=initialize(opening,['2026-11-12','2026-11-13','2026-11-19','2026-11-20'],
                              '2026-11-13','2026-11-19',now=datetime(2026,11,12,18,tzinfo=CN))
        self.file.write_text(json.dumps(self.state))
        (self.root/'config.json').write_text('invalid-real-config-that-must-not-be-read')
        self.client=create_app(self.root).test_client()

    def test_get_is_read_only_and_keeps_paper_data_separate(self):
        before=self.file.read_bytes();cfg=(self.root/'config.json').read_bytes()
        r=self.client.get('/api/paper-contest',headers={'Host':'localhost'})
        self.assertEqual(r.status_code,200);self.assertEqual(r.json['equity'],100000.)
        self.assertEqual(r.json['net_pnl'],0);self.assertEqual(r.json['trade_count'],0)
        self.assertEqual(r.json['positions'][0]['sellable'],100)
        self.assertNotIn('SECRET',r.text);self.assertNotIn('/private/',r.text)
        self.assertEqual(self.file.read_bytes(),before);self.assertEqual((self.root/'config.json').read_bytes(),cfg)

    def test_page_uses_local_assets_and_no_broker_order_controls(self):
        r=self.client.get('/paper-contest',headers={'Host':'localhost'})
        self.assertEqual(r.status_code,200)
        self.assertIn('/static/paper_contest.js',r.text);self.assertIn('独立虚拟账户',r.text)
        self.assertIn("script-src 'self'",r.headers['Content-Security-Policy'])

    def test_missing_or_corrupt_ledger_has_no_real_account_fallback(self):
        s=dict(self.state);s['cash']+=1;self.file.write_text(json.dumps(s))
        self.assertEqual(self.client.get('/api/paper-contest',headers={'Host':'localhost'}).status_code,409)
        self.file.unlink()
        self.assertEqual(self.client.get('/api/paper-contest',headers={'Host':'localhost'}).status_code,404)

    def test_fresh_per_position_marks_come_from_paper_observation(self):
        now=datetime(2026,11,13,10,tzinfo=CN)
        observe(self.state,{'600406':{'price':9.8,'previous_close':10.0,'source_timestamp':now.isoformat()}},now)
        self.file.write_text(json.dumps(self.state))
        d=load_snapshot(self.store,now)
        self.assertEqual(d['equity'],99960.);self.assertEqual(d['positions'][0]['price'],9.8)
        self.assertEqual(d['positions'][0]['contest_pnl'],-40.);self.assertFalse(d['stale'])
        self.assertEqual(d['positions'][0]['cost'],15.)
        self.assertEqual(d['daily']['pnl'],-40.)
        self.assertEqual(d['daily']['basis_label'],'开赛快照')
        self.assertEqual(d['positions'][0]['daily_pnl'],-40.)

    def test_daily_pnl_uses_prior_complete_close_and_actual_sale_cost(self):
        close=datetime(2026,11,13,15,5,tzinfo=CN)
        observe(self.state,{'600406':{'price':9.9,'source_timestamp':close.isoformat()}},close,closing=True)
        now=datetime(2026,11,19,10,tzinfo=CN)
        quotes={'600406':{'price':9.8,'previous_close':9.9,'source_timestamp':now.isoformat(),
                            'upper_limit':10.89,'lower_limit':8.91,'limit_status_verified':True,
                            'bid1':9.79,'bid1_shares':1100,'ask1':9.8}}
        trade=execute(self.state,'600406','sell',100,quotes,now,'合成减仓')
        observe(self.state,quotes,now)
        self.file.write_text(json.dumps(self.state))
        d=load_snapshot(self.store,now)
        expected=money(100*9.8-200*9.9+100*trade['price']-trade['fee']['total'])
        self.assertEqual(d['daily']['date'],'2026-11-19')
        self.assertEqual(d['daily']['basis_asof'],close.isoformat())
        self.assertEqual(d['daily']['pnl'],expected)
        self.assertEqual(d['positions'][0]['daily_pnl'],expected)

    def test_stale_intraday_or_missing_previous_close_does_not_show_zero(self):
        now=datetime(2026,11,19,10,tzinfo=CN)
        observe(self.state,{'600406':{'price':9.8,'source_timestamp':now.isoformat()}},now)
        self.file.write_text(json.dumps(self.state))
        d=load_snapshot(self.store,now)
        self.assertIsNone(d['daily']['pnl'])
        self.assertIn('缺少',d['daily']['message'])
        self.assertIsNone(d['positions'][0]['daily_pnl'])
        stale=load_snapshot(self.store,now+timedelta(minutes=3))
        self.assertTrue(stale['stale'])
        self.assertEqual(stale['daily']['message'],'等待有效行情')
        self.assertIsNone(stale['positions'][0]['daily_pnl'])

    def test_daily_pnl_waits_for_a_mark_after_latest_trade(self):
        now=datetime(2026,11,13,10,tzinfo=CN)
        quotes={'600406':{'price':9.8,'previous_close':10.0,'source_timestamp':now.isoformat(),
                          'upper_limit':11,'lower_limit':9,'limit_status_verified':True,
                          'bid1':9.79,'bid1_shares':1100,'ask1':9.8}}
        observe(self.state,quotes,now)
        execute(self.state,'600406','sell',100,quotes,now+timedelta(seconds=1),'合成减仓')
        self.file.write_text(json.dumps(self.state))
        d=load_snapshot(self.store,now+timedelta(seconds=2))
        self.assertTrue(d['stale'])
        self.assertIsNone(d['daily']['pnl'])

    def test_continuous_account_uses_extended_calendar_and_preserves_opening_results(self):
        prior=datetime(2026,11,19,15,5,tzinfo=CN)
        observe(self.state,{'600406':{'price':9.9,'source_timestamp':prior.isoformat()}},prior,closing=True)
        now=datetime(2026,11,20,10,tzinfo=CN)
        continue_account(self.state,{'verified':True,'dates':['2026-11-19','2026-11-20','2026-11-23']},
                         '用户授权持续模拟',now)
        observe(self.state,{'600406':{'price':10.0,'previous_close':9.9,'source_timestamp':now.isoformat()}},now)
        self.file.write_text(json.dumps(self.state))
        view=load_snapshot(self.store,now)
        self.assertTrue(view['ongoing'])
        self.assertFalse(view['ended'])
        self.assertIsNone(view['end'])
        self.assertTrue(view['trading']['session_open'])
        self.assertEqual(view['daily']['pnl'],20.)
        self.assertEqual(view['daily']['basis_asof'],prior.isoformat())
        self.assertEqual(view['initial_equity'],100000.)
        self.assertFalse(load_snapshot(self.store,now.replace(day=21))['trading']['session_open'])

    def test_foreign_host_is_rejected(self):
        self.assertEqual(self.client.get('/api/paper-contest',headers={'Host':'foreign.example'}).status_code,403)

    def sell_request(self):
        return {'code':'600406','side':'sell','quantity':100,'reason':'合成测试减仓'}

    def live_book(self, now, bid=9.79):
        return {'600406':{'price':9.8,'previous_close':10.,'source_timestamp':now.isoformat(),
                         'upper_limit':11.,'lower_limit':9.,'limit_status_verified':True,
                         'bid1':bid,'bid1_shares':1100,'ask1':9.8}}, []

    def operation(self, step, data, *, secure=True):
        headers = {'X-CSRF-Token': CSRF} if secure else {}
        return self.client.post('/api/paper-contest/trade/'+step, json=data, headers=headers)

    def test_web_preview_and_confirmation_are_separate_and_repeat_safe(self):
        now = datetime(2026,11,13,10,tzinfo=CN)
        body = self.sell_request()
        config = (self.root/'config.json').read_bytes()
        before = self.file.read_bytes()
        with patch('paper_contest.clock_now', return_value=now), patch('paper_contest.fetch_quotes', return_value=self.live_book(now)) as fetch:
            self.assertEqual(self.operation('preview', body, secure=False).status_code,403)
            response = self.operation('preview', body)
            self.assertEqual(response.status_code,200,response.get_json())
            preview = response.json['preview']
            self.assertEqual(preview['position_after'],100)
            self.assertGreater(preview['cash_after'],self.state['cash'])
            self.assertEqual(preview['fee_total'],preview['fee']['total'])
            self.assertEqual(self.file.read_bytes(),before)
            confirm = {'token':response.json['token'],'trade':body}
            self.assertEqual(self.operation('confirm', confirm, secure=False).status_code,403)
            # Periodic valuation between preview and confirmation is compatible.
            observe(self.state,self.live_book(now)[0],now)
            self.file.write_text(json.dumps(self.state))
            saved = self.operation('confirm', confirm)
            self.assertEqual(saved.status_code,200,saved.get_json())
            self.assertEqual(saved.json['snapshot']['positions'][0]['sellable'],0)
            self.assertEqual(saved.json['snapshot']['trade_count'],1)
            duplicate = self.operation('confirm',confirm)
            self.assertTrue(duplicate.json['repeated'])
            self.assertEqual(duplicate.json['trade']['sequence'],1)
            self.assertEqual(fetch.call_count,2)
        self.assertEqual(len(json.loads(self.file.read_text())['trades']),1)
        self.assertEqual((self.root/'config.json').read_bytes(),config)

    def test_worse_quote_changed_inventory_and_expired_preview_never_save_a_fill(self):
        now = datetime(2026,11,13,10,tzinfo=CN)
        body = self.sell_request()
        before = self.file.read_bytes()
        with patch('paper_contest.clock_now', return_value=now), patch('paper_contest.fetch_quotes', side_effect=[self.live_book(now),self.live_book(now,bid=9.70)]) as fetch:
            preview = self.operation('preview',body).json
            request = {'token':preview['token'],'trade':body}
            changed = self.operation('confirm',{**request,'trade':{**body,'reason':'另一项操作'}})
            self.assertEqual(changed.status_code,409)
            with patch('itsdangerous.timed.time.time',return_value=time.time()+40):
                expired = self.operation('confirm',request)
            self.assertEqual(expired.status_code,409)
            self.assertEqual(fetch.call_count,1)
            worse = self.operation('confirm',request)
            self.assertEqual(worse.status_code,409)
            self.assertIn('劣于预览',worse.json['error'])
            self.assertEqual(self.file.read_bytes(),before)
            self.state['positions']['600406']['opening_available']=0
            self.file.write_text(json.dumps(self.state))
            changed = self.operation('confirm',request)
            self.assertEqual(changed.status_code,409)
            self.assertIn('账本已变化',changed.json['error'])
            self.assertEqual(fetch.call_count,2)
            self.assertEqual(json.loads(self.file.read_text())['trades'],[])

    def test_session_gate_rejects_before_network_and_shows_tail_cutoffs(self):
        body = self.sell_request()
        now = datetime(2026,11,13,9,40,tzinfo=CN)
        with patch('paper_contest.clock_now',return_value=now), patch('paper_contest.fetch_quotes') as fetch:
            denied = self.operation('preview',body)
            self.assertEqual(denied.status_code,400)
            fetch.assert_not_called()
        self.assertFalse(load_snapshot(self.store,now)['trading']['session_open'])
        late = load_snapshot(self.store,now.replace(hour=14,minute=45))['trading']
        self.assertTrue(late['sell_allowed'])
        self.assertFalse(late['buy_allowed'])
        self.assertFalse(load_snapshot(self.store,now.replace(hour=14,minute=57))['trading']['session_open'])


if __name__=='__main__':unittest.main()
