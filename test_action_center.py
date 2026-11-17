from copy import deepcopy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest,json
from unittest.mock import patch
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
 def test_waiting_entry_shows_distance_and_ceiling_and_drops_outside_signal(self):
  code='588170'
  self.cfg['watchlist']=[{'code':code,'name':'观察ETF','type':'etf'}]
  self.cfg['trade_plans']={code:{'strategy_version':'1.0','trigger_price':.700,'entry_price':.710,'valid_on':str(self.now.date())}}
  quote={'code':code,'price':.690,'open':.685,'source_timestamp':self.now.isoformat()}
  self.state['quotes'].append(quote)
  self.state['trade_signals']=[{'id':'entry','code':code,'category':'add','trigger':'v1_entry','level':.700,'shares':100}]
  def card():return self.run_action()['cards'][1]
  self.assertEqual(card()['thresholds'][0]['distance'],.010)
  self.assertEqual(card()['thresholds'][0]['max_price'],.710)
  self.assertIsNone(card()['quantity'])
  quote['price']=.705
  self.assertEqual(card()['quantity'],100)
  quote['price']=.711
  self.assertIsNone(card()['quantity'])
  self.assertIsNone(card()['alert_id'])
  quote.update(price=.705,open=.711)
  self.assertIsNone(card()['quantity'])
 def test_t_cap_expires_with_minutes_while_actual_remaining_is_preserved(self):
  code='159971'
  self.book.update(revision='new',cycles=[{'code':code,'status':'open','sell_price':1.1,'unbought_quantity':120}])
  self.state['trade_signals']=[{'id':'buyback','code':code,'category':'t','trigger':'buyback_ready',
    'level':1.,'tradable':True,'shares':100}]
  row={'code':code,'cycle_id':'actual','target_buy':1.,'status':'做T回补条件观察',
       'minute_asof':(self.now-timedelta(seconds=60)).isoformat(),'minute_source_at':self.now.isoformat()}
  self.state['t_observe']={'ledger_revision':'new','asof':self.now.isoformat(),'rows':[row]}
  card=self.run_action()['cards'][0]
  self.assertEqual(card['t']['buyback_cap'],1.)
  self.assertEqual(card['t']['remaining_quantity'],120)
  self.assertEqual(card['quantity'],100)
  row['minute_source_at']=(self.now-timedelta(seconds=91)).isoformat()
  card=self.run_action()['cards'][0]
  self.assertIsNone(card['quantity'])
  self.assertNotIn('buyback_cap',card['t'])
  self.assertEqual(card['t']['remaining_quantity'],120)
 def test_exit_only_filters_entries_and_all_t_actions(self):
  self.cfg['execution_preferences']={'mode':'exit_only'}
  s=[{'category':x} for x in ['reduce','add','t','switch']]
  self.assertEqual(allowed_signals(self.cfg,s),[{'category':'reduce'}])
 def test_desktop_test_reports_submit_failure_and_honors_security(self):
  with TemporaryDirectory() as t:
   root=Path(t);(root/'config.json').write_text(json.dumps(self.cfg));client=create_app(root).test_client()
   path='/api/action-desktop-test';headers={'X-CSRF-Token':CSRF}
   with patch('intraday_monitor.native_notice',return_value=False) as send:
    self.assertEqual(client.post(path).status_code,403)
    send.assert_not_called()
    self.assertEqual(client.post(path,headers=headers).status_code,503)
    self.assertEqual(client.post(path,headers=headers).status_code,429)
    send.assert_called_once()
 def test_held_t_observation_is_visible_without_executable_quantity(self):
  with TemporaryDirectory() as t:
   root=Path(t);(root/'config.json').write_text(json.dumps(self.cfg));client=create_app(root).test_client()
   signal={'id':'a'*20,'code':'159971','held':True,'category':'t','trigger':'sell_watch',
           'price':1.,'level':1.,'blockers':['退出计划待核'],'reason':'高抛条件观察'}
   card={'alert_id':signal['id'],'quantity':None,'expires_at':self.now.isoformat()}
   original={'id':signal['id'],'native':False,'active':True,'status':'pending','side':None}
   payload={'cards':[card],'signals':[signal],'mode':'normal','expires_at':self.now.isoformat()}
   with patch('action_center.build_actions',return_value=payload),patch('alert_journal.load_journal',return_value={'rows':[original]}):
    result=client.get('/api/action-center')
   self.assertEqual(result.status_code,200)
   row=result.json['alerts'][0]
   self.assertTrue(row['native']);self.assertFalse(row['repeatable'])
   self.assertIsNone(row['side']);self.assertIsNone(row['quantity'])
   self.assertEqual(row['reason'],'高抛条件观察')
 def test_structure_card_does_not_hide_concurrent_t_observation(self):
  code='159971';self.book['revision']='same'
  structure={**self.state['trade_signals'][0],'trigger':'structure_support','held':True,'shares':None}
  watch={'id':'b'*20,'code':code,'held':True,'category':'t','trigger':'sell_watch',
         'price':1.,'level':1.,'reason':'等待高抛复核','tradable':False}
  self.state['trade_signals']=[structure,watch]
  minute=self.now-timedelta(seconds=130)
  self.state['t_observe']={'ledger_revision':'same','asof':self.now.isoformat(),
      'rows':[{'code':code,'minute_source_at':self.now.isoformat(),'minute_asof':minute.isoformat()}]}
  payload=self.run_action()
  self.assertEqual(payload['cards'][0]['alert_id'],structure['id'])
  self.assertEqual(payload['signal_expires_at'][watch['id']],(self.now+timedelta(seconds=20)).isoformat())
  rows=[{'id':s['id'],'active':True,'status':'pending','native':False,'side':None} for s in [structure,watch]]
  with TemporaryDirectory() as t:
   root=Path(t);(root/'config.json').write_text(json.dumps(self.cfg));client=create_app(root).test_client()
   with patch('action_center.build_actions',return_value=payload),patch('alert_journal.load_journal',return_value={'rows':rows}):
    result=client.get('/api/action-center').json
  self.assertEqual({r['id'] for r in result['alerts']},{structure['id'],watch['id']})
  self.assertTrue(all(r['native'] and r['quantity'] is None and r['side'] is None for r in result['alerts']))
  t_alert=next(r for r in result['alerts'] if r['id']==watch['id'])
  self.assertEqual(t_alert['expires_at'],payload['signal_expires_at'][watch['id']])
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
