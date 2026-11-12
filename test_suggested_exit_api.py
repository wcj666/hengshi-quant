"""Signed exit draft adoption, unknown legacy metadata, and chart window checks."""
from copy import deepcopy
from datetime import datetime, timedelta, time
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch
import pandas as pd
import strategy_runtime as runtime
from portfolio_test_fixture import account_fixture
from web_dashboard import create_app, CSRF


class SuggestedExitApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.out=self.root/'output'
        self.cfg=account_fixture();self.cfg['strategy_v1']={'enabled':True,**runtime.DEFAULTS}
        self.code=self.cfg['positions'][0]['code'];self.cfg['trade_plans']={}
        self.save_cfg()
        self.headers={'Host':'localhost','X-CSRF-Token':CSRF}
        self.client=create_app(self.root).test_client()
        self.clock=datetime.now(runtime.CST)
        dates=pd.bdate_range(start=self.clock.date()-timedelta(days=700),end=self.clock.date()+timedelta(days=14))
        self.days=[str(d.date()) for d in dates]
        eligible=[d for d in dates if d.date()<self.clock.date() or (d.date()==self.clock.date() and self.clock.time()>=time(15,5))]
        closes=[20+i*.1 for i in range(100)];ds=eligible[-100:]
        self.rows=[{'date':str(d.date()),'open':c,'close':c,'high':c+.2,'low':c-.2,'volume':10000} for d,c in zip(ds,closes)]
        runtime.write(self.out/'strategy_calendar.json',{'verified':True,'dates':self.days})
        self.save_rows()
        self.refresh=patch('strategy_runtime.refresh_evidence_async');self.refresh.start();self.addCleanup(self.refresh.stop)

    def save_cfg(self):
        (self.root/'config.json').write_text(json.dumps(self.cfg),encoding='utf-8')

    def save_rows(self):
        runtime.write(self.out/'strategy_evidence'/f'{self.code}.json',{'history':self.rows,'fetched_at':datetime.now(runtime.CST).isoformat()})

    def get(self):
        result=self.client.get('/api/exit-suggestion/'+self.code,headers=self.headers)
        self.assertEqual(result.status_code,200,result.json)
        return result.json

    def adopt(self,token='',**extra):
        return self.client.post('/api/exit-suggestion/'+self.code+'/adopt',headers=self.headers,json={'suggestion_token':token,**extra})

    def test_signed_adoption_preserves_account_and_unknown_execution(self):
        before=deepcopy(self.cfg);draft=self.get()
        self.assertTrue(draft['valid'],draft);self.assertTrue(draft['adoptable'],draft)
        result=self.adopt(draft['suggestion_token'],stop_loss=.001,legacy_exit_only=False,setup_verified=True)
        self.assertEqual(result.status_code,200,result.json)
        saved=json.loads((self.root/'config.json').read_text());plan=saved['trade_plans'][self.code]
        self.assertEqual(plan['stop_loss'],draft['plan']['stop_loss'])
        self.assertTrue(plan['legacy_exit_only']);self.assertFalse(plan['setup_verified'])
        self.assertEqual(plan['sector'],'')
        for key in ('buy_date','actual_entry','initial_quantity'):
            self.assertNotIn(key,plan)
        for key in ('positions','cash','baseline_equity'):
            self.assertEqual(saved[key],before[key])
        self.assertEqual(plan['reference_kind'],'remaining_position_snapshot')
        from risk_engine import compute
        risk=compute(saved,{str(p['code']):p['baseline_price'] for p in saved['positions']},fresh=True,strategy_account=runtime.get_account(self.out,saved))
        self.assertEqual(risk['available_new_risk'],0)
        self.assertTrue(any('行业' in str(x) for x in risk['strategy']['blockers']))

    def test_missing_forged_changed_config_and_changed_cache_tokens_rejected(self):
        draft=self.get();token=draft['suggestion_token']
        self.assertEqual(self.adopt().status_code,400)
        self.assertEqual(self.adopt('forged').status_code,400)
        self.cfg['cash']+=1;self.save_cfg()
        self.assertEqual(self.adopt(token).status_code,400)
        token=self.get()['suggestion_token'];self.rows[-1]['volume']+=1;self.save_rows()
        self.assertEqual(self.adopt(token).status_code,400)
        self.assertEqual(json.loads((self.root/'config.json').read_text())['trade_plans'],{})

    def test_wide_stop_remains_research_and_server_refuses_adoption(self):
        self.rows[95]['low']=20.;self.save_rows();draft=self.get()
        self.assertTrue(draft['valid']);self.assertFalse(draft['adoptable'])
        self.assertEqual(draft['status'],'risk_mismatch');self.assertNotIn('suggestion_token',draft)
        self.assertLess(draft['plan']['stop_loss'],20.)
        self.assertEqual(self.adopt('anything').status_code,400)

    def test_old_fixed_plan_preserved_even_without_fresh_evidence(self):
        prior={'entry_price':26.,'stop_loss':25.,'take_profit_1':28.,'take_profit_2':29.,'initial_stop_loss':24.,'legacy_exit_only':True,'actual_entry':25.9,'buy_date':'2026-10-01'}
        self.cfg['trade_plans'][self.code]=prior;self.save_cfg()
        (self.out/'strategy_evidence'/f'{self.code}.json').unlink()
        draft=self.get();self.assertTrue(draft['preserved']);self.assertEqual(draft['plan'],prior)
        result=self.adopt('anything');self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(json.loads((self.root/'config.json').read_text())['trade_plans'][self.code],prior)

    def test_missing_daily_is_pending_not_fabricated(self):
        (self.out/'strategy_evidence'/f'{self.code}.json').unlink()
        draft=self.get();self.assertFalse(draft['valid']);self.assertFalse(draft['adoptable'])
        self.assertEqual(draft['plan'],{});self.assertEqual(self.adopt('anything').status_code,400)

    def test_exit_api_rejects_unheld_and_unknown_sector_still_blocks_new_plan(self):
        result=self.client.get('/api/exit-suggestion/600001',headers=self.headers)
        self.assertEqual(result.status_code,400)
        with self.assertRaises(ValueError):
            runtime.validate_metadata(self.out,self.cfg,'600001',{'legacy_exit_only':True,'sector':''},{'entry_price':10,'stop_loss':9.5,'take_profit_1':11,'take_profit_2':11.5})

    def test_one_year_window_bounds_provider_and_chart_for_live_and_demo(self):
        ds=pd.bdate_range(end=self.clock.date()+timedelta(days=7),periods=600)
        frame=pd.DataFrame({'date':ds,'open':30.,'close':30.,'high':30.2,'low':29.8,'volume':10000})
        with patch('web_dashboard.history_by_asset',return_value=frame) as provider,patch('web_dashboard.snapshot_selected_akshare',return_value=({},[])):
            result=self.client.get('/api/analyze/'+self.code,headers=self.headers)
        self.assertEqual(result.status_code,200,result.json)
        expected=(self.clock.date()-timedelta(days=365)).isoformat()
        self.assertEqual(result.json['analysis_window']['start'],expected)
        self.assertEqual(provider.call_args.args[2],expected.replace('-',''))
        self.assertEqual(result.json['analysis_window']['label'],'最近1年')
        self.assertGreater(len(result.json['history']),100)
        for row in result.json['history']:
            self.assertGreaterEqual(row['date'],expected)
            self.assertLessEqual(row['date'],result.json['analysis_window']['end'])
        demo=self.client.get('/api/analyze/'+self.code+'?mode=demo',headers=self.headers)
        self.assertEqual(demo.status_code,200,demo.json)
        self.assertTrue(all(r['date']>=expected for r in demo.json['history']))


if __name__=='__main__':unittest.main()
