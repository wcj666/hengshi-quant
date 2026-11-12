"""Deterministic decision-gate tests; no profitability claims."""
from copy import deepcopy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import hashlib,json
import pandas as pd
from swing_analysis import analyze_swing
from technical_engine import analyze_technical
from chan_history_engine import analyze_chan_history
from web_dashboard import create_app

now=datetime(2026,11,10,10,tzinfo=ZoneInfo('Asia/Shanghai'))
dates=pd.bdate_range(end='2026-11-09',periods=140)
price=[25+i*.02 for i in range(139)]+[28.2]
x=pd.DataFrame({'date':dates,'open':price,'close':price,'high':[p+.05 for p in price],'low':[p-.05 for p in price],'volume':[100]*139+[200]})
t={'valid':True,'asof':'2026-11-09','atr14':1,'rsi14':60,
   'macd':{'dif':1,'dea':.5,'bar':1,'prev_bar':.8},'volume':{'ratio5':2,'ratio20':2}}
c={'valid':True,'interpretation':{'basis_date':t['asof'],'action':'偏买入观察'},'points':[
   {'date':'2026-11-06','confirmed_on':'2026-11-09','kind':'二买候选'}]}
cfg={'baseline_equity':100000,'cash':100000,'max_additional_loss':2000,'positions':[],
     'trade_plans':{'600406':{'entry_price':28,'stop_loss':27,'take_profit_1':31,'take_profit_2':33}},
     'risk_settings':{'per_trade_risk':500},'watchlist':[]}
def snapshot(config):
 return {'mode':'live','calendar_ok':True,'market_session':True,'fetch_finished_at':now.isoformat(),
         'config_hash':hashlib.sha256(json.dumps(config,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
         'risk':{'valid':True},'quote_health':{'provider_timestamps_verified':True},'quotes':[]}
quote={'price':28.2,'source_timestamp':now.isoformat()}
def run(config=cfg,tech=t,chan=c,q=quote,snap=None,position=None,frame=x,mode='live'):
 return analyze_swing(frame,tech,chan,config,'600406',position=position,quote=q,snapshot=snapshot(config) if snap is None else snap,now=now,mode=mode)
r=run();assert r['action']=='buy' and r['max_buy_shares']>=100,r
assert run(snap={})['action']=='wait'
assert run(q={'price':28.2,'source_timestamp':(now-timedelta(minutes=3)).isoformat()})['action']=='wait'
assert run(q={'price':30,'source_timestamp':now.isoformat()})['action']=='wait'
missing=deepcopy(cfg);missing['trade_plans']={}
assert run(config=missing)['action']=='wait'
hot=deepcopy(t);hot['rsi14']=76
assert run(tech=hot)['action']=='wait'
shrink=deepcopy(t);shrink['volume']['ratio5']=.8
assert run(tech=shrink)['action']=='wait'
weak=deepcopy(t);weak['macd']['bar']=.7
assert run(tech=weak)['action']=='wait'
old=deepcopy(c);old['points'][0]['confirmed_on']=str(dates[-6].date());old['points'][0]['date']=str(dates[-7].date())
assert run(chan=old)['action']=='wait'
wrong=deepcopy(c);wrong['interpretation']['basis_date']='2026-11-06'
assert run(chan=wrong)['action']=='wait'
assert run(mode='demo')['action']=='wait'
position={'code':'600406','type':'stock','quantity':100,'cost':28,'name':'测试','baseline_price':28}
holdcfg=deepcopy(cfg);holdcfg['positions']=[position];holdcfg['cash']=97200
snap=snapshot(holdcfg);snap['quotes']=[{'code':'600406','held':True,'price':28.2}]
assert run(config=holdcfg,position=position,snap=snap)['action']=='add'
assert run(config=holdcfg,position=position,q={'price':26,'source_timestamp':now.isoformat()},snap=snap)['action']=='exit'
assert run(config=holdcfg,position=position,q={'price':32,'source_timestamp':now.isoformat()},snap=snap)['action']=='reduce'
bad=x.copy();bad.loc[139,'close']=float('inf')
assert not analyze_technical(bad)['valid']
# Real engine integration, no partial-day candle used.
tech=analyze_technical(x);chan=analyze_chan_history(x)
assert run(tech=tech,chan=chan)['action'] in ('wait','buy')
assert all(p['confirmed_on']<=chan['asof'] for p in chan['recent_points'])
# Read-only HTTP route with a synthetic history and quote; no live data required.
from unittest.mock import patch
with patch('web_dashboard.history_by_asset',return_value=x),patch('web_dashboard.snapshot_selected_akshare',return_value=({},[])):
 response=create_app().test_client().get('/api/analyze/301313',headers={'Host':'127.0.0.1:8765'})
 assert response.status_code==200,response.data
 assert response.json['swing']['horizon']=='1～2周 / 5～10个交易日'
print('PASS: swing entry/exit guards, stale/partial/demo data, old Chan candidates, source and account gating, HTTP integration')
