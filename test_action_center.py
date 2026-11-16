from copy import deepcopy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest,json
from action_center import build_actions,allowed_signals
from web_dashboard import create_app,CSRF
from portfolio_test_fixture import account_fixture

class ActionTests(unittest.TestCase):
 def setUp(self):
  self.now=datetime(2026,11,16,10,30,tzinfo=ZoneInfo('Asia/Shanghai'))
  self.cfg=account_fixture();self.cfg['positions']=[{'code':'159971','type':'etf','name':'测试ETF','quantity':1000,'cost':1.,'baseline_price':1.}];self.cfg['cash']=1000
  self.book={'availability':{'159971':{'date':'2026-11-16','base_quantity':1000,'available_quantity':300}},'cycles':[]}
  self.state={'mode':'live','market_session':True,'calendar_ok':True,'stale':False,'fetch_finished_at':self.now.isoformat(),'risk':{'valid':True,'equity':2000},'quote_health':{'all_held_usable':True,'block_trade_signals':False},'quotes':[{'code':'159971','price':1.,'source_timestamp':self.now.isoformat()}],'trade_signals':[{'id':'a'*20,'code':'159971','category':'reduce','shares':1000,'reason':'触发固定止损','level':1.,'trigger':'stop'}]}
 def run_action(self):return build_actions(self.cfg,self.state,self.book,now=self.now)
 def test_sell_quantity_capped_and_cash_conservative(self):
  d=self.run_action();c=d['cards'][0]
  self.assertTrue(d['valid']);self.assertEqual(c['quantity'],300);self.assertEqual(c['cash_after'],1280)
 def test_stale_quotes_and_inventory_do_not_offer_sizes(self):
  self.book['availability']['159971']['date']='2026-11-13'
  self.assertIsNone(self.run_action()['cards'][0]['quantity'])
  self.state['fetch_finished_at']=(self.now-timedelta(seconds=46)).isoformat()
  d=self.run_action();self.assertFalse(d['valid']);self.assertEqual(d['signals'],[]);self.assertIsNone(d['cards'][0]['price'])
 def test_future_quote_is_not_actionable(self):
  self.state['quotes'][0]['source_timestamp']=(self.now+timedelta(seconds=1)).isoformat()
  self.assertIsNone(self.run_action()['cards'][0]['quantity'])
 def test_no_signal_is_wait_not_recommendation(self):
  self.state['trade_signals']=[];self.assertEqual(self.run_action()['cards'][0]['state'],'wait')
 def test_exit_only_filters_entries_and_all_t_actions(self):
  self.cfg['execution_preferences']={'mode':'exit_only'}
  s=[{'category':x} for x in ['reduce','add','t','switch']]
  self.assertEqual(allowed_signals(self.cfg,s),[{'category':'reduce'}])
 def test_api_mode_is_explicit_csrf_and_preserves_account(self):
  with TemporaryDirectory() as t:
   root=Path(t);(root/'config.json').write_text(json.dumps(self.cfg));client=create_app(root).test_client()
   self.assertEqual(client.get('/api/action-center').status_code,200)
   self.assertEqual(client.get('/api/action-center').json['mode'],'normal')
   body={'mode':'exit_only','native_sound':True}
   self.assertEqual(client.put('/api/execution-preferences',json=body).status_code,403)
   r=client.put('/api/execution-preferences',json=body,headers={'X-CSRF-Token':CSRF})
   self.assertEqual(r.status_code,200)
   cfg=json.loads((root/'config.json').read_text());pref=cfg.pop('execution_preferences');self.assertEqual(cfg,self.cfg);self.assertEqual(pref,body)
   self.assertEqual(client.get('/api/action-center').json['mode'],'exit_only')
   self.assertEqual(client.get('/api/t-performance').status_code,200)
   self.assertEqual(client.post('/api/action-feedback/'+'a'*20,json={'action':'filled'},headers={'X-CSRF-Token':CSRF}).status_code,400)

if __name__=='__main__':unittest.main()
