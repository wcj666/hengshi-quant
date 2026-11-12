"""Actual-fill journals must never infer fills, oversell or over-buyback."""
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from portfolio_test_fixture import account_fixture
import t_runtime as tr
import strategy_runtime as storage
from web_dashboard import create_app,CSRF,json_hash

class TRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.out=self.root/'output'
        self.cfg=account_fixture();self.cfg['positions']=[{'code':'600406','name':'测试底仓','type':'stock','quantity':300,'cost':20,'baseline_price':20}]
        self.cfg['t_settings']={'enabled':True};self.now=datetime.now(tr.CST)
    def avail(self,qty=300):return tr.confirm_available(self.out,self.cfg,{'code':'600406','available_quantity':qty},self.now)
    def sell(self,qty=100):return tr.record_sell(self.out,self.cfg,{'code':'600406','quantity':qty,'sell_price':21,'sell_filled':True},self.now)
    def test_no_sellable_confirmation_no_cycle(self):
        with self.assertRaises(ValueError):self.sell()
        self.avail()
        with self.assertRaises(ValueError):tr.record_sell(self.out,self.cfg,{'code':'600406','quantity':100,'sell_price':21},self.now)
        self.assertEqual(tr.ledger(self.out,self.cfg)['cycles'],[])
    def test_old_sellable_quantity_not_reused(self):
        self.avail()
        with self.assertRaises(ValueError):tr.record_sell(self.out,self.cfg,{'code':'600406','quantity':100,'sell_price':21,'sell_filled':True},self.now+timedelta(days=1))
    def test_core_fraction_and_no_repeat_round(self):
        self.avail()
        with self.assertRaises(ValueError):self.sell(200)
        c=self.sell()
        tr.close_cycle(self.out,self.cfg,c['id'],{'leave_cash':True},self.now)
        with self.assertRaises(ValueError):self.sell()
    def test_one_lot_position_not_t_inventory(self):
        self.cfg['positions'][0]['quantity']=100;self.avail(100)
        with self.assertRaises(ValueError):self.sell()
    def test_sync_and_partial_actual_fill_remaining(self):
        self.avail();c=self.sell()
        row=tr.ledger(self.out,self.cfg)['cycles'][0]
        self.assertTrue(row['ledger_sync_required']);self.assertEqual(row['expected_quantity'],200)
        self.cfg['positions'][0]['quantity']=200
        self.assertFalse(tr.ledger(self.out,self.cfg)['cycles'][0]['ledger_sync_required'])
        tr.record_buyback(self.out,self.cfg,c['id'],{'quantity':50,'price':20,'buy_filled':True},self.now)
        row=tr.ledger(self.out,self.cfg)['cycles'][0]
        self.assertEqual(row['unbought_quantity'],50);self.assertEqual(row['expected_quantity'],250)
        self.assertEqual(row['paired_gross_estimate'],50);self.assertEqual(row['paired_net_estimate'],30)
        with self.assertRaises(ValueError):tr.record_buyback(self.out,self.cfg,c['id'],{'quantity':100,'price':20,'buy_filled':True},self.now)
        self.assertEqual(tr.ledger(self.out,self.cfg)['cycles'][0]['buyback_quantity'],50)
    def test_partial_sell_log_allowed_but_no_fabricated_t_profit(self):
        self.avail();self.sell(50)
        row=tr.ledger(self.out,self.cfg)['cycles'][0]
        self.assertEqual(row['unbought_quantity'],50);self.assertIsNone(row['paired_net_estimate'])
    def test_buyback_requires_fill_and_same_day(self):
        self.avail();c=self.sell()
        for body,when in [({'quantity':100,'price':20},self.now),({'quantity':100,'price':20,'buy_filled':True},self.now+timedelta(days=1))]:
            with self.assertRaises(ValueError):tr.record_buyback(self.out,self.cfg,c['id'],body,when)
    def test_revision_invalidates_pre_fill_signal(self):
        (self.root/'config.json').write_text(json.dumps(self.cfg));client=create_app(self.root).test_client();h={'Host':'localhost','X-CSRF-Token':CSRF}
        self.avail();book=tr.ledger(self.out,self.cfg)
        state={'config_hash':json_hash(self.cfg),'fetch_finished_at':self.now.isoformat(),'mode':'live','market_session':True,'calendar_ok':True,'risk':{'valid':True},'quote_health':{'block_trade_signals':False},
               't_observe':{'ledger_revision':book['revision'],'rows':[{'code':'600406','quantity':100}],'signals':[{'category':'t'}]},'trade_signals':[{'category':'t'}],'new_trade_signals':[{'category':'t'}]}
        storage.write(self.out/'intraday_status.json',state)
        self.assertFalse(client.get('/api/t-observe',headers=h).json['stale'])
        self.sell()
        r=client.get('/api/t-observe',headers=h).json;self.assertTrue(r['stale']);self.assertEqual(r['signals'],[]);self.assertIsNone(r['rows'][0]['quantity'])
        self.assertEqual(client.get('/api/intraday',headers=h).json['trade_signals'],[])
    def test_api_csrf_and_no_automatic_portfolio_changes(self):
        (self.root/'config.json').write_text(json.dumps(self.cfg));client=create_app(self.root).test_client();h={'Host':'localhost','X-CSRF-Token':CSRF}
        r=client.put('/api/t-availability',headers={'Host':'localhost'},json={'code':'600406','available_quantity':300});self.assertEqual(r.status_code,403)
        self.assertEqual(client.put('/api/t-availability',headers=h,json={'code':'600406','available_quantity':300}).status_code,200)
        r=client.post('/api/t-cycles',headers=h,json={'code':'600406','quantity':100,'sell_price':21,'sell_filled':True});self.assertEqual(r.status_code,200)
        saved=json.loads((self.root/'config.json').read_text());self.assertEqual(saved,self.cfg)
        self.assertEqual(client.get('/api/t-observe',headers=h).json['cycles'][0]['unbought_quantity'],100)

if __name__=='__main__':unittest.main()
