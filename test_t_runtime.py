"""Actual-fill journals must never infer fills, oversell or over-buyback."""
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from portfolio_test_fixture import account_fixture
import t_runtime as tr
from t_engine import _settings as signal_settings
import strategy_runtime as storage
from web_dashboard import create_app,CSRF,json_hash

class TRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.out=self.root/'output'
        self.cfg=account_fixture();self.cfg['positions']=[{'code':'600406','name':'测试底仓','type':'stock','quantity':300,'cost':20,'baseline_price':20}]
        self.cfg['t_settings']={'enabled':True};self.now=datetime.now(tr.CST)
    def avail(self,qty=300):return tr.confirm_available(self.out,self.cfg,{'code':'600406','available_quantity':qty},self.now)
    def sell(self,qty=100):return tr.record_sell(self.out,self.cfg,{'code':'600406','quantity':qty,'sell_price':21,'sell_filled':True},self.now)
    def test_legacy_without_t_section_remains_disabled_and_parameters_match_signals(self):
        self.assertFalse(tr.settings({})['enabled'])
        prefs = {'t_settings': {'enabled': True, 'fee_reserve': 5, 'max_fraction': 0,
                                'stock_deviation_pct': .1, 'etf_deviation_pct': .1}}
        observed = tr.settings(prefs)
        self.assertTrue(observed['enabled'])
        self.assertEqual(observed['fee_reserve'], 20)
        self.assertEqual(observed['max_fraction'], 0)
        self.assertEqual(observed['stock_deviation_pct'], .3)
        self.assertEqual(observed['etf_deviation_pct'], .15)
        self.assertEqual(observed['fee_model'], {})
        self.assertEqual({key: observed[key] for key in signal_settings(prefs)}, signal_settings(prefs))
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
    def test_known_leg_fees_settle_only_the_paired_quantity(self):
        self.avail()
        sell=tr.record_sell(self.out,self.cfg,{'code':'600406','quantity':100,'sell_price':21,
                                             'fee':7.31,'sell_filled':True},self.now)
        for quantity, price, fee in ((30,20,1.25),(20,20.5,0)):
            tr.record_buyback(self.out,self.cfg,sell['id'],{'quantity':quantity,'price':price,'fee':fee,
                                                           'buy_filled':True},self.now)
        row=tr.ledger(self.out,self.cfg,self.now)['cycles'][0]
        self.assertEqual((row['unbought_quantity'],row['paired_gross_estimate']), (50,40))
        self.assertEqual((row['paired_fee_actual'],row['paired_net_actual']), (4.91,35.09))
        self.assertEqual(row['unmatched_sell_fee_actual'],3.65)
        self.assertIsNone(row['paired_net_estimate'])
        self.assertEqual(row['fee_status'],'actual')
    def test_confirmed_fills_reuse_actual_quantity_price_fee_and_holdings(self):
        self.cfg['positions'][0]['quantity']=250
        def confirmed(fill_id,side,quantity,price,fee,before,after,offset):
            return {'id':'id-'+fill_id,'fill_id':fill_id,'date':str(self.now.date()),'asset_type':'stock',
                    'code':'600406','side':side,'quantity':quantity,'price':price,'fee':fee,
                    'quantity_before':before,'quantity_after':after,
                    'executed_at':(self.now+timedelta(minutes=offset)).isoformat()}
        self.cfg['reconciled_trades']=[
            confirmed('sell-17','sell',100,21,5.01,300,200,-10),
            confirmed('buy-17','buy',50,20,1.25,200,250,-5),
            confirmed('unrelated','buy',50,20,1.25,300,350,-4),
        ]
        sell=tr.record_sell(self.out,self.cfg,{'code':'600406','broker_fill_id':'sell-17','sell_filled':True},self.now)
        with self.assertRaisesRegex(ValueError,'前后持仓'):
            tr.record_buyback(self.out,self.cfg,sell['id'],{'broker_fill_id':'unrelated','buy_filled':True},self.now)
        tr.record_buyback(self.out,self.cfg,sell['id'],{'broker_fill_id':'buy-17','buy_filled':True},self.now)
        with self.assertRaisesRegex(ValueError,'不重复'):
            tr.record_buyback(self.out,self.cfg,sell['id'],{'broker_fill_id':'buy-17','buy_filled':True},self.now)
        book=tr.ledger(self.out,self.cfg,self.now)
        cycle=book['cycles'][0]
        self.assertEqual(cycle['sell_fill_id'],'id-sell-17')
        self.assertEqual(cycle['paired_net_actual'],46.24)
        self.assertFalse(cycle['ledger_sync_required'])
        self.assertEqual(self.cfg['positions'][0]['quantity'],250)
        self.assertEqual(len(tr.fee_samples(self.cfg,book)),3)
    def test_confirmed_fee_model_has_separate_stock_etf_assumptions(self):
        tr.save_fee_model(self.out,self.cfg,{'kind':'stock','commission_per_10k':5,'minimum_commission':None})
        terms=tr.save_fee_model(self.out,self.cfg,{'kind':'etf','commission_per_10k':1,'minimum_commission':0})
        self.assertIsNone(terms['stock']['minimum_commission'])
        self.assertEqual(terms['etf']['minimum_commission'],0)
        self.assertEqual(tr.settings(self.cfg,tr.ledger(self.out,self.cfg))['fee_model'],terms)
    def test_buyback_requires_fill_and_same_day(self):
        self.avail();c=self.sell()
        for body,when in [({'quantity':100,'price':20},self.now),({'quantity':100,'price':20,'buy_filled':True},self.now+timedelta(days=1))]:
            with self.assertRaises(ValueError):tr.record_buyback(self.out,self.cfg,c['id'],body,when)
    def test_revision_invalidates_pre_fill_signal(self):
        (self.root/'config.json').write_text(json.dumps(self.cfg));client=create_app(self.root).test_client();h={'Host':'localhost','X-CSRF-Token':CSRF}
        self.avail();book=tr.ledger(self.out,self.cfg)
        state={'config_hash':json_hash(self.cfg),'fetch_finished_at':self.now.isoformat(),'mode':'live','market_session':True,'calendar_ok':True,'risk':{'valid':True},'quote_health':{'block_trade_signals':False},
               't_observe':{'ledger_revision':book['revision'],'rows':[{'code':'600406','quantity':100,'cost_buyback_cap':20.60}],'signals':[{'category':'t'}]},'trade_signals':[{'category':'t'}],'new_trade_signals':[{'category':'t'}]}
        storage.write(self.out/'intraday_status.json',state)
        self.assertFalse(client.get('/api/t-observe',headers=h).json['stale'])
        self.sell()
        r=client.get('/api/t-observe',headers=h).json;self.assertTrue(r['stale']);self.assertEqual(r['signals'],[]);self.assertIsNone(r['rows'][0]['quantity'])
        self.assertNotIn('cost_buyback_cap',r['rows'][0])
        self.assertIn('做T台账已更新',r['status'])
        self.assertEqual(client.get('/api/intraday',headers=h).json['trade_signals'],[])
    def test_per_symbol_minute_expiry_removes_trade_hint_and_explains_block(self):
        (self.root/'config.json').write_text(json.dumps(self.cfg))
        book=tr.ledger(self.out,self.cfg)
        timestamp=self.now.isoformat()
        minute=(self.now-timedelta(minutes=1)).isoformat()
        stale=(self.now-timedelta(seconds=91)).isoformat()
        row={'code':'600406','quantity':100,'tradable':True,'quote_asof':timestamp,
             'minute_asof':minute,'minute_source_at':stale,'target_buy':20.60}
        state={'config_hash':json_hash(self.cfg),'fetch_finished_at':timestamp,'mode':'live','market_session':True,
               'calendar_ok':True,'risk':{'valid':True},'quote_health':{'block_trade_signals':False},
               't_observe':{'ledger_revision':book['revision'],'rows':[row],
                            'signals':[{'id':'t','code':'600406','trigger':'buyback_ready','category':'t'}]}}
        storage.write(self.out/'intraday_status.json',state)
        data=create_app(self.root).test_client().get('/api/t-observe').json
        self.assertFalse(data['stale'])
        self.assertFalse(data['signals'])
        self.assertNotIn('target_buy',data['rows'][0])
        self.assertIn('分钟数据已过期',data['rows'][0]['reason'])
    def test_api_csrf_and_no_automatic_portfolio_changes(self):
        (self.root/'config.json').write_text(json.dumps(self.cfg));client=create_app(self.root).test_client();h={'Host':'localhost','X-CSRF-Token':CSRF}
        terms={'kind':'stock','commission_per_10k':5,'minimum_commission':None}
        self.assertEqual(client.put('/api/t-fees',headers={'Host':'localhost'},json=terms).status_code,403)
        self.assertEqual(client.put('/api/t-fees',headers=h,json=terms).status_code,200)
        fee_model=client.get('/api/t-observe',headers=h).json['settings']['fee_model']
        self.assertIsNone(fee_model['stock']['minimum_commission'])
        self.assertEqual(fee_model['stock']['commission_per_10k'],5)
        r=client.put('/api/t-availability',headers={'Host':'localhost'},json={'code':'600406','available_quantity':300});self.assertEqual(r.status_code,403)
        self.assertEqual(client.put('/api/t-availability',headers=h,json={'code':'600406','available_quantity':300}).status_code,200)
        r=client.post('/api/t-cycles',headers=h,json={'code':'600406','quantity':100,'sell_price':21,'sell_filled':True});self.assertEqual(r.status_code,200)
        saved=json.loads((self.root/'config.json').read_text());self.assertEqual(saved,self.cfg)
        self.assertEqual(client.get('/api/t-observe',headers=h).json['cycles'][0]['unbought_quantity'],100)

    def test_cash_closeout_confirms_latest_remainder_without_inventing_buyback(self):
        self.avail();cycle=self.sell()
        tr.record_buyback(self.out,self.cfg,cycle['id'],{'quantity':40,'price':20,'fee':1,'buy_filled':True},self.now)
        before=json.dumps(self.cfg,sort_keys=True)
        with self.assertRaisesRegex(ValueError,'数量已变化'):
            tr.close_cycle(self.out,self.cfg,cycle['id'],{'leave_cash':True,'remaining_quantity':100},self.now)
        result=tr.close_cycle(self.out,self.cfg,cycle['id'],{'leave_cash':True,'remaining_quantity':60},self.now)
        self.assertEqual(result['status'],'cash')
        self.assertEqual(result['buyback_quantity'],40)
        self.assertEqual(result['buyback_cost'],800)
        self.assertEqual(len(result['fills']),1)
        self.assertEqual(result['closeout_confirmed_quantity'],60)
        self.assertEqual(result['closure_kind'],'retain_cash')
        self.assertEqual(result['closeout_snapshot']['remaining_quantity'],60)
        self.assertEqual(before,json.dumps(self.cfg,sort_keys=True))
        again=tr.close_cycle(self.out,self.cfg,cycle['id'],{'leave_cash':True,'remaining_quantity':60},self.now)
        self.assertEqual(again['closed_at'],result['closed_at'])

    def test_disabled_t_still_exposes_actual_endofday_remainder(self):
        from zoneinfo import ZoneInfo
        self.now=datetime(2026,11,11,14,50,tzinfo=ZoneInfo('Asia/Shanghai'))
        self.avail();self.sell();self.cfg['t_settings']['enabled']=False
        result=tr.observe(self.out,self.cfg,{}, {},now=self.now,market_session=True,calendar_ok=True)
        self.assertFalse(result['enabled'])
        self.assertEqual(result['signals'][0]['trigger'],'carry_cash')
        self.assertEqual(result['signals'][0]['closeout']['remaining_quantity'],100)

    def screenshot(self,quantity=200,asof=None):
        row={**self.cfg['positions'][0],'available_quantity':quantity,'screenshot_price':20,'market_value':6000}
        stamp=asof or self.now-timedelta(minutes=10)
        snapshot={'asof':stamp.isoformat(),'available_cash':self.cfg['cash'],
                  'total_market_value':6000,'total_assets':6000+self.cfg['cash'],'positions':[row]}
        folder=self.out/'broker_snapshots';folder.mkdir(parents=True,exist_ok=True)
        (folder/'broker_test.json').write_text(json.dumps(snapshot))

    def test_today_matching_screenshot_reused_without_mutating_ledger_or_account(self):
        self.screenshot();before=json.dumps(self.cfg)
        book=tr.ledger(self.out,self.cfg,self.now)
        item=book['availability']['600406']
        self.assertEqual((item['available_quantity'],item['base_quantity'],item['source']),(200,300,'broker_screenshot'))
        self.assertFalse((self.out/'t_ledger.json').exists())
        self.assertEqual(before,json.dumps(self.cfg))
        self.assertEqual(book['revision'],tr.ledger(self.out,self.cfg,self.now)['revision'])
        self.screenshot(quantity=0)
        self.assertEqual(tr.ledger(self.out,self.cfg,self.now)['availability']['600406']['available_quantity'],0)
        self.assertNotEqual(book['revision'],tr.ledger(self.out,self.cfg,self.now)['revision'])

    def test_old_future_mismatch_and_bad_sellable_screenshot_not_used(self):
        for quantity,when in [(301,self.now-timedelta(minutes=10)),(True,self.now-timedelta(minutes=10)),
                               (200,self.now-timedelta(days=1)),(200,self.now+timedelta(days=1))]:
            self.screenshot(quantity,when)
            self.assertEqual(tr.ledger(self.out,self.cfg,self.now)['availability'],{})
        self.screenshot();self.cfg['cash']+=1
        self.assertEqual(tr.ledger(self.out,self.cfg,self.now)['availability'],{})

    def test_newer_manual_and_actual_fill_override_screenshot(self):
        self.screenshot();self.avail(100)
        self.assertEqual(tr.ledger(self.out,self.cfg,self.now)['availability']['600406']['available_quantity'],100)
        self.screenshot(200,self.now-timedelta(seconds=1))
        self.assertEqual(tr.ledger(self.out,self.cfg,self.now)['availability']['600406']['available_quantity'],100)
        self.cfg['reconciled_trades']=[{'code':'600406','executed_at':self.now.isoformat()}]
        (self.out/'t_ledger.json').unlink()
        self.assertEqual(tr.ledger(self.out,self.cfg,self.now)['availability'],{})

    def test_saved_snapshot_evidence_revalidates_after_config_change(self):
        self.screenshot()
        tr.save_fee_model(self.out,self.cfg,{'kind':'etf','commission_per_10k':5,'minimum_commission':None})
        self.assertIn('600406',tr.ledger(self.out,self.cfg,self.now)['availability'])
        self.cfg['cash']+=1
        self.assertEqual(tr.ledger(self.out,self.cfg,self.now)['availability'],{})

    def test_api_renders_current_positions_when_worker_has_old_portfolio(self):
        (self.root/'config.json').write_text(json.dumps(self.cfg))
        storage.write(self.out/'intraday_status.json',{'t_observe':{'rows':[{'code':'000001','name':'已清仓','target_sell':20}]}})
        d=create_app(self.root).test_client().get('/api/t-observe').json
        self.assertTrue(d['stale']);self.assertEqual([r['code'] for r in d['rows']],['600406'])
        self.assertEqual(d['signals'],[])

if __name__=='__main__':unittest.main()
