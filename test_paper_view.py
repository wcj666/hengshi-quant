"""Read-only paper page/API must not mutate ledgers or expose real config."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import datetime, timedelta
from paper_contest import initialize,observe,execute,money,CN
from paper_view import load_snapshot
from web_dashboard import create_app


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

    def test_foreign_host_is_rejected(self):
        self.assertEqual(self.client.get('/api/paper-contest',headers={'Host':'foreign.example'}).status_code,403)


if __name__=='__main__':unittest.main()
