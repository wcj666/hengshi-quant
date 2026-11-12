"""Executable, offline V3.6 regression: source quality, tier risk, simulation, journal and UI."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from html.parser import HTMLParser
from unittest.mock import patch
import copy, json, subprocess
from zoneinfo import ZoneInfo
from portfolio_test_fixture import account_fixture
from quote_health import assess,parse_provider_time
from risk_engine import compute
from risk_extensions import tier_alerts,risk_level,simulate_reduction
from portfolio_journal import log_manual_trade,record_equity,read_lines,daily_curve
from signal_review import inspect
import intraday_monitor as mon

root=Path(__file__).resolve().parent
cfg=account_fixture()
prices={'300274':81.97,'159929':1.348,'159562':1.886,'123290':132.5}
feed={(p['type'],p['code']):(prices[p['code']],p['name'],p['baseline_price']) for p in cfg['positions']}
now=datetime(2026,11,9,10,15,tzinfo=ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)

# Time-only and naive datetimes do not validate an exchange's update time.
assert parse_provider_time('10:15:00') is None
assert parse_provider_time('2026-11-09T10:15:00') is None
assert parse_provider_time('2026-11-09T10:15:00+08:00') is not None
ok=assess(cfg,feed,now=now)
assert ok['all_held_usable'] and not ok['block_new_signals']
assert not ok['provider_timestamps_verified'] and ok['verified_count']==0
missing=dict(feed);del missing[('etf','159929')]
health=assess(cfg,missing,now=now)
assert not health['all_held_usable'] and health['block_new_signals'] and '159929' in health['missing']
pre={'quote_health':{'by_code':{'300274':{'price':81.97,'last_changed_at':(now-timedelta(seconds=250)).isoformat(),'consecutive_same':15}}}}
health=assess(cfg,feed,previous=pre,now=now)
assert '300274' in health['suspected_freeze'] and health['block_new_signals']
jump=dict(feed);jump[('stock','300274')]=(130.0,'阳光电源',80.0)
prev={'quote_health':{'by_code':{'300274':{'price':81.97,'last_changed_at':now.isoformat(),'consecutive_same':0}}}}
health=assess(cfg,jump,previous=prev,now=now)
assert '300274' in health['invalid_jump']

risk=compute(cfg,prices,fresh=True)
assert risk['valid'] and abs(risk['equity']-cfg['baseline_equity'])<.01
assert risk_level(risk)['level'] in ('normal','critical')
for pct,level in ((50,'watch'),(80,'high'),(100,'critical')):
    test=dict(risk,used=10*pct,budget=1000,existing_planned_stress=100,remaining=1000-10*pct,over_budget=False)
    assert risk_level(test)['level']==level
    assert any(x['tier']==pct for x in tier_alerts(test))
assert not tier_alerts({'valid':False})

scenario=simulate_reduction(cfg,prices,'300274',100)
assert scenario['planned_stress_after'] < scenario['planned_stress_before']
assert scenario['equity_after']==risk['equity']
assert scenario['budget_used_after']==risk['used']
assert scenario['stress_reduction']>0
assert cfg['positions'][0]['quantity']==300 and cfg['cash']==5891.40
try:simulate_reduction(cfg,prices,'300274',101)
except ValueError:pass
else:raise AssertionError('nonstandard lot should not be accepted unless entire position')

with TemporaryDirectory() as td:
    log=Path(td)/'trades.jsonl'
    rec=log_manual_trade(log,{'date':'2026-11-06','code':'300274','side':'sell','quantity':100,'price':81.97,'fee':5.0,'note':'真实成交示例'})
    assert rec['gross']==8197 and rec['net_cash_change']==8192
    assert len(read_lines(log))==1 and read_lines(log)[0]['id']==rec['id']
    try:log_manual_trade(log,{'date':'2026-11-06','code':'300274','side':'sell','quantity':1.5,'price':81,'fee':0})
    except ValueError:pass
    else:raise AssertionError('fractional quantity must be rejected')
    hist=Path(td)/'curve.jsonl'
    assert record_equity(hist,at=now.isoformat(),config_hash='a',amount=60000.0,source_status='未验证')
    assert not record_equity(hist,at=(now+timedelta(seconds=10)).isoformat(),config_hash='a',amount=54115,source_status='未验证')
    assert record_equity(hist,at=(now+timedelta(minutes=6)).isoformat(),config_hash='a',amount=54101,source_status='未验证')
    assert record_equity(hist,at=(now+timedelta(days=1)).isoformat(),config_hash='b',amount=54200,source_status='未验证')
    curve=daily_curve(hist)
    assert len(curve)==2 and curve[0]['equity_estimate']==54101
    assert curve[1]['config_hash']=='b'

# An unconfirmed Chan/indicator mismatch must not be represented as a firm buy.
unknown=inspect({'valid':False},{'valid':False},{})
assert not unknown['verified'] and '证据不足' in unknown['decision']
conflict=inspect({'valid':True,'interpretation':{'basis_date':'2026-11-05','phase':'反弹'}},
                 {'valid':True,'asof':'2026-11-06','macd':{'status':'空头柱'},'volume':{'status':'放量下跌'},'trend':'偏弱'},
                 {'action':'买入条件观察','alignment':'信号明显冲突'})
assert not conflict['verified'] and '继续观察' in conflict['decision']

# End-to-end polling cycle with mocked market clock/calendar and fake AKShare feed.
with TemporaryDirectory() as td:
    out=Path(td)
    fixed_now=datetime(2026,11,9,10,15,tzinfo=ZoneInfo('Asia/Shanghai'))
    with patch.object(mon,'now_cn',return_value=fixed_now),\
         patch.object(mon,'load_config',return_value=cfg),\
         patch.object(mon,'verified_trading_date',return_value=(True,'测试日历已核验')),\
         patch.object(mon,'refresh_context_async',return_value=None),\
         patch.object(mon,'snapshot_selected_akshare',return_value=(feed,[])),\
         patch.object(mon,'STATUS_FILE',out/'status.json'),\
         patch.object(mon,'SENT_FILE',out/'sent.json'),\
         patch.object(mon,'HISTORY_FILE',out/'events.jsonl'),\
         patch.object(mon,'SIGNAL_SENT',out/'signal_sent.json'),\
         patch.object(mon,'SIGNAL_HISTORY',out/'signal_events.jsonl'),\
         patch.object(mon,'EQUITY_JOURNAL',out/'equity.jsonl'),\
         patch.object(mon,'CONTEXT_FILE',out/'context.json'),\
         patch.object(mon,'ROOT',root):
        state=mon.single_cycle(mode='live',notify=False,interval=10)
    assert state['risk']['valid'] and state['quote_health']['all_held_usable']
    assert state['risk']['alert_level']['used_pct']==0
    assert len(read_lines(out/'equity.jsonl'))==1
    assert json.loads((out/'status.json').read_text())['interval_seconds']==10

class IDs(HTMLParser):
 def __init__(self):super().__init__();self.ids=[]
 def handle_starttag(self,tag,attrs):self.ids.extend(v for k,v in attrs if k=='id')

html=(root/'templates/dashboard.html').read_text();parser=IDs();parser.feed(html)
assert len(parser.ids)==len(set(parser.ids)), 'duplicate HTML id'
for elem in ['risk-tier-banner','reduction-form','reduction-result','manual-trade-form','trades-table',
             'quote-health-table','quote-health-banner','equity-curve-chart','signal-review']:
    assert elem in parser.ids,elem
assert 'V3.6.1' in html and (root/'VERSION.txt').read_text().strip()=='3.6.1'
assert "EXPECTED_VERSION = '3.6.1'" in (root/'setup_autostart.py').read_text()
assert '3.6.1' in (root/'检查网页版本.command').read_text()
subprocess.run(['node','--check',str(root/'static/dashboard.js')],check=True)
print('PASS: quote provenance, missing/freeze/jump, 50/80/100 risk tiers, reduction scenarios, manual trade journal, forward-only equity series, Chan/MACD crosscheck, mocked polling, DOM IDs, syntax and installer version')
