"""Cash-flow accounting, evidence authority and local API regressions, no network."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
import strategy_runtime as runtime
from portfolio_test_fixture import account_fixture
from web_dashboard import create_app,CSRF,json_hash

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.out=Path(self.tmp.name)/'output'
        self.cfg=account_fixture();self.cfg['strategy_v1']={'enabled':True,**runtime.DEFAULTS}
        self.cfg['trade_plans']={};self.now=datetime.now(runtime.CST)
    def test_unverified_never_initializes(self):
        a=runtime.observe(self.out,self.cfg,60000,verified=False)
        self.assertFalse(a['initialized']);self.assertFalse(a['verified'])
    def test_example_needs_explicit_account_reconciliation(self):
        cfg = {**self.cfg, 'account_profile': 'example', 'positions': []}
        account = runtime.observe(self.out, cfg, cfg['cash'], verified=True)
        self.assertTrue(account['example_unconfirmed'])
        self.assertFalse((self.out/'strategy_account.json').exists())
        account = runtime.observe(self.out, cfg, 59_000, verified=True, reconcile=True)
        self.assertEqual(account['high_water'], 59_000)
        self.assertFalse(account['example_unconfirmed'])
    def test_unreadable_initialized_account_never_creates_new_start(self):
        self.out.mkdir()
        path = self.out/'strategy_account.json'
        path.write_text('{invalid')
        with self.assertRaisesRegex(ValueError, '禁止重置高水位'):
            runtime.observe(self.out, self.cfg, 60_000, verified=True)
        self.assertEqual(path.read_text(), '{invalid')
    def test_initialization_and_effective_risk_tiers_share_settings(self):
        from risk_extensions import tier_alerts, risk_level
        from strategy_v1 import strategy_prefs
        cfg = {'strategy_v1': {'enabled':True, 'loss_budget':900, 'account_risk_budget':950,
                               'min_fees':25, 'fee_reserve':30, 'pause_drawdown':300, 'reduce_drawdown':600}}
        limits = runtime.settings(cfg)
        self.assertEqual(limits, strategy_prefs(cfg))
        self.assertEqual((limits['loss_budget'],limits['min_fees']), (900,30))
        risk = {'valid':True,'budget':900,'used':610,'settings':limits,'strategy':{'enabled':True},
                'existing_planned_stress':0,'remaining':290}
        self.assertEqual(tier_alerts(risk)[0]['level'], 600)
        self.assertEqual(risk_level(risk)['drawdown_level'], 'high')
    def test_cash_flow_preserves_high_and_loss(self):
        a=runtime.observe(self.out,self.cfg,60000,verified=True)
        self.assertEqual(a['high_water'],60000)
        runtime.observe(self.out,self.cfg,59600,verified=True)
        runtime.add_flow(self.out,self.cfg,{'date':str(self.now.date()),'amount':8000})
        self.cfg['cash']+=8000
        a=runtime.observe(self.out,self.cfg,67600,verified=True)
        self.assertTrue(a['needs_reconcile']);self.assertFalse(a['verified'])
        a=runtime.observe(self.out,self.cfg,67600,verified=True,reconcile=True)
        self.assertEqual(a['high_water'],60000);self.assertEqual(a['drawdown'],400)
        self.assertEqual(a['adjusted_equity'],59600);self.assertEqual(a['net_external_flow'],8000)
        a=runtime.observe(self.out,self.cfg,68200,verified=True)
        self.assertEqual(a['high_water'],60200);self.assertEqual(a['drawdown'],0)
    def test_ledger_change_cannot_auto_reset(self):
        runtime.observe(self.out,self.cfg,60000,verified=True)
        self.cfg['positions'][0]['quantity']+=100
        a=runtime.observe(self.out,self.cfg,60500,verified=True)
        self.assertTrue(a['needs_reconcile']);self.assertEqual(a['high_water'],60000)
    def test_three_losses_calendar_pause_and_duplicate(self):
        runtime.observe(self.out,self.cfg,60000,verified=True)
        ds=[str((self.now+timedelta(days=i)).date()) for i in (0,1,2,3)]
        runtime.write(self.out/'strategy_calendar.json',{'verified':True,'dates':ds})
        for i in range(3):
            row=runtime.add_review(self.out,self.cfg,{'code':f'60000{i}','opened_on':ds[0],'closed_on':ds[0],'net_pnl':-100,'planned_risk':150})
        self.assertTrue(row['pause_trigger']);self.assertAlmostEqual(row['net_r'],-2/3,places=3)
        a=runtime.get_account(self.out,self.cfg);self.assertTrue(a['paused']);self.assertEqual(a['pause_until'],ds[2])
        with self.assertRaises(ValueError):runtime.add_review(self.out,self.cfg,{'code':'600002','opened_on':ds[0],'closed_on':ds[0],'net_pnl':1,'planned_risk':150})
        self.assertFalse(runtime.get_account(self.out,self.cfg,self.now+timedelta(days=3))['paused'])
    def test_budget_latch_survives_rebound(self):
        runtime.observe(self.out,self.cfg,60000,verified=True)
        a=runtime.observe(self.out,self.cfg,59000,verified=True);self.assertTrue(a['paused'])
        self.assertTrue(runtime.observe(self.out,self.cfg,61000,verified=True)['paused'])
    def test_forged_setup_flag_rejected(self):
        fixed={'entry_price':10,'stop_loss':9.5,'take_profit_1':11,'take_profit_2':11.5}
        with self.assertRaises(ValueError):runtime.validate_metadata(self.out,self.cfg,'600000',{'sector':'银行','setup_verified':True},fixed)
    def test_legacy_does_not_fabricate_dates_or_setup(self):
        p=self.cfg['positions'][0];code=str(p['code'])
        fixed={'entry_price':10,'stop_loss':9.5,'take_profit_1':11,'take_profit_2':11.5}
        plan=runtime.validate_metadata(self.out,self.cfg,code,{'sector':'测试','legacy_exit_only':True},fixed)
        self.assertTrue(plan['legacy_exit_only']);self.assertFalse(plan['setup_verified'])
        self.assertNotIn('buy_date',plan);self.assertEqual(plan['initial_stop_loss'],9.5)
    def test_settings_cannot_increase_design_caps(self):
        with self.assertRaises(ValueError):runtime.validate_settings({'per_trade_risk':300})
        self.assertEqual(runtime.validate_settings({})['min_fees'],20)
    def test_complete_daily_cache_does_not_refetch_slow_upstream(self):
        today=str(self.now.date());yesterday=str((self.now-timedelta(days=1)).date())
        future=str((self.now+timedelta(days=7)).date())
        runtime.write(self.out/'strategy_calendar.json',{'verified':True,'dates':[yesterday,today,future],'fetched_at':self.now.isoformat()})
        expected=today if self.now.hour*60+self.now.minute>=905 else yesterday
        cache={'history':[{'date':expected}]*120,'fetched_at':self.now.isoformat()}
        runtime.write(self.out/'strategy_evidence'/'benchmark.json',cache)
        runtime.write(self.out/'strategy_evidence'/'002463.json',cache)
        with patch('akshare.tool_trade_date_hist_sina',side_effect=AssertionError('refetched calendar')),patch('akshare.index_zh_a_hist',side_effect=AssertionError('refetched index')),patch('asset_history.history_by_asset',side_effect=AssertionError('refetched stock')):
            self.assertEqual(runtime.refresh_evidence(self.out,self.cfg,'002463'),[])
    def test_filled_plan_preserves_s0_and_execution_fields(self):
        p=self.cfg['positions'][0];code=str(p['code'])
        prior={'entry_price':10,'stop_loss':9.5,'take_profit_1':11,'take_profit_2':11.5,
               'sector':'旧仓','legacy_exit_only':True,'buy_date':str(self.now.date()),'actual_entry':9.9,
               'initial_quantity':p['quantity'],'initial_stop_loss':9.5}
        self.cfg['trade_plans'][code]=prior
        raised={k:prior[k] for k in ('entry_price','stop_loss','take_profit_1','take_profit_2')};raised['stop_loss']=9.8
        plan=runtime.validate_metadata(self.out,self.cfg,code,{'sector':'旧仓'},raised)
        self.assertEqual(plan['initial_stop_loss'],9.5);self.assertEqual(plan['actual_entry'],9.9)
        self.assertEqual(plan['buy_date'],prior['buy_date']);self.assertEqual(plan['initial_quantity'],p['quantity'])
        with self.assertRaises(ValueError):runtime.validate_metadata(self.out,self.cfg,code,{'actual_entry':10.1},raised)
    def test_expired_unfilled_plan_can_be_reprepared(self):
        code='600000';self.cfg['trade_plans'][code]={'entry_price':8,'stop_loss':7.5,'take_profit_1':9,'take_profit_2':9.5,'valid_on':'2026-01-01'}
        levels={'entry_price':10,'stop_loss':9.5,'take_profit_1':11,'take_profit_2':11.5,'trigger_price':9.99}
        runtime.write(self.out/'strategy_prepared.json',{'proof':{'code':code,'setup':{'levels':levels,'valid_on':str(self.now.date()),'signal_date':str((self.now-timedelta(days=1)).date())}}})
        body={'sector':'银行','prepared_id':'proof','review':{k:True for k in ['fundamentals_ok','event_clear','corporate_action_clear','resistance_clear']}}
        plan=runtime.validate_metadata(self.out,self.cfg,code,body,{k:v for k,v in levels.items() if k!='trigger_price'})
        self.assertEqual(plan['entry_price'],10);self.assertEqual(plan['initial_stop_loss'],9.5)
    def test_api_and_unknown_risk_keeps_equity(self):
        (Path(self.tmp.name)/'config.json').write_text(json.dumps(self.cfg))
        app=create_app(Path(self.tmp.name));client=app.test_client();headers={'Host':'localhost','X-CSRF-Token':CSRF}
        self.assertEqual(client.get('/api/strategy',headers=headers).status_code,200)
        r=client.put('/api/strategy/settings',headers=headers,json={'fee_reserve':25,'account_risk_budget':900})
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(client.delete('/api/trade-plan/'+self.cfg['positions'][0]['code'],headers=headers).status_code,400)
        cfg=json.loads((Path(self.tmp.name)/'config.json').read_text());self.assertEqual(cfg['strategy_v1']['min_fees'],25)
        self.assertEqual(cfg['strategy_v1']['loss_budget'],900);self.assertEqual(cfg['baseline_equity'],self.cfg['baseline_equity'])
        r=client.put('/api/trade-plan',headers=headers,json={'code':self.cfg['positions'][0]['code'],'sector':'旧仓','legacy_exit_only':True,'entry_price':10,'stop_loss':9.5,'take_profit_1':11,'take_profit_2':11.5})
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(client.post('/api/strategy/reconcile',headers=headers,json={}).status_code,400)
        self.assertEqual(client.put('/api/strategy/settings',headers={'Host':'localhost'},json={}).status_code,403)
        from risk_engine import compute
        prices={str(p['code']):float(p['baseline_price']) for p in cfg['positions']}
        risk=compute(cfg,prices,fresh=True,strategy_account=runtime.get_account(self.out,cfg))
        self.assertTrue(risk['valid']);self.assertIsNotNone(risk['equity']);self.assertIsNone(risk['existing_planned_stress'])
        self.assertEqual(risk['available_new_risk'],0)

if __name__=='__main__':unittest.main()
