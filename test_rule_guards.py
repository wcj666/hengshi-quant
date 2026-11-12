"""Offline behavioral regressions for entry guards and risk escalation."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from risk_engine import compute
from trade_signals import build_signals
from risk_extensions import risk_level, simulate_reduction
from signal_review import inspect
from technical_engine import combine_chan_technical
from portfolio_quotes import snapshot_selected_akshare
import intraday_monitor as monitor

cfg={'baseline_equity':10000,'max_additional_loss':1000,'cash':8000,
     'positions':[{'code':'300274','name':'持仓','type':'stock','quantity':100,'cost':20}],
     'watchlist':[{'code':'600406','name':'候选'}],
     'alert_levels':[{'code':'600406','above':24}],
     'trade_plans':{'600406':{'entry_price':24,'stop_loss':23,'take_profit_1':28,'take_profit_2':30}},
     'risk_settings':{'max_position_pct':50,'per_trade_risk':200}}

def candidate(c,price,stamp=None):
    r=compute(c,{'300274':20},fresh=True,watch_quotes=[{'code':'600406','name':'候选','price':price}])
    q={'600406':{'name':'候选','price':price,'source_timestamp':stamp or datetime.now(timezone.utc).isoformat()}}
    s=build_signals(c,q,r,trading_day='2026-11-10',market_session=True,calendar_ok=True)
    return r,s

r,s=candidate(cfg,24.5)
assert any(x['category']=='add' for x in s)
# A higher print remains affordable, but the chase guard rejects it.
r,s=candidate(cfg,25)
assert r['sizing'][0]['max_buy_shares']>=100 and not any(x['category']=='add' for x in s)
# Near-target and above-target prices cannot receive new capacity.
for price in [27.9,28,30]:
    r,s=candidate(cfg,price)
    assert r['sizing'][0]['max_buy_shares']==0 and not any(x['category']=='add' for x in s)
# A moving simulated plan, stale source or missing source must not produce entries.
c=deepcopy(cfg);c['trade_plans']={}
assert not candidate(c,24.5)[1]
assert not candidate(cfg,24.5,(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat())[1]
assert not candidate(cfg,24.5,'missing')[1]
# An exited position needs no quote; closing the final holding frees cash, not losses.
c=deepcopy(cfg);c['positions'].append({'code':'000001','name':'已清仓','type':'stock','quantity':0,'cost':10})
assert compute(c,{'300274':20},fresh=True)['valid']
after=simulate_reduction(c,{'300274':20},'300274',100)
assert after['equity_after']==10000 and after['planned_stress_after']==0
# Tier escalation produces a different identity at every severity; pressure is independent.
ids=[]
for used,tier in [(500,50),(800,80),(1000,100)]:
    risk=dict(r,valid=True,used=used,budget=1000,remaining=1000-used,existing_planned_stress=0)
    signals=build_signals(cfg,{},risk,trading_day='2026-11-10',market_session=True,calendar_ok=True)
    event=next(x for x in signals if x['trigger']==f'account_budget_{tier}')
    ids.append(event['id'])
assert len(set(ids))==3
with TemporaryDirectory() as tmp, patch.object(monitor,'SENT_FILE',Path(tmp)/'seen.json'):
    for remaining in [600,590]:
        events=[{'code':'PORT','name':'全账户','kind':'持仓压力损失超剩余预算','level':remaining,'price':1000,'tier':100}]
        assert len(monitor.dedup_events(events,'2026-11-10',demo=False))==(1 if remaining==600 else 0)
assert risk_level(dict(r,valid=True,used=100,budget=1000,remaining=900,existing_planned_stress=1000,over_budget=True))['label']=='持仓压力超剩余预算'
# Missing or mismatched dates cannot be reported as aligned evidence.
chan={'valid':True,'interpretation':{'action':'偏买入观察'}}
tech={'valid':True,'asof':'2026-11-09'}
assert combine_chan_technical(chan,tech)['action']=='继续观察'
assert not inspect(chan,tech,{'action':'买入条件观察','alignment':'偏强证据一致'})['verified']
# Real adapter path preserves raw provider time for stocks and Shanghai ETFs.
provider_time=datetime.now(timezone.utc).replace(microsecond=0)
def response(url,*,params,headers,timeout):
    assert url=='https://qt.gtimg.cn/'
    symbols=params['q'].split(',')
    assert set(symbols)=={'sh600406','sh510300'}
    lines=[]
    for symbol in symbols:
        fields=['']*31
        fields[1:5]=['测试证券',symbol[2:],'20','19']
        fields[30]=provider_time.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')
        lines.append(f'v_{symbol}="'+ '~'.join(fields)+'";')
    return Mock(content='\n'.join(lines).encode('gb18030'))
with patch('portfolio_quotes.requests.get',side_effect=response):
    quotes,errors=snapshot_selected_akshare({'positions':[{'type':'stock','code':'600406','quantity':100},{'type':'etf','code':'510300','quantity':100}]})
    assert not errors and len(quotes)==2
    assert all(q[3]==provider_time.isoformat() for q in quotes.values())
print('PASS: entry/chase/reward-risk guards; zero holdings; tier escalation/dedup; date agreement; raw provider timestamps')
