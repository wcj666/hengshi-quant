"""Offline V3.6.1 tests: bounded timeouts, signal gating, measured duration, source preservation."""
from __future__ import annotations
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic,sleep
from unittest.mock import patch
import json
from zoneinfo import ZoneInfo
from quote_health import assess
from portfolio_quotes import snapshot_selected_akshare
import intraday_monitor as mon
from portfolio_test_fixture import account_fixture

root=Path(__file__).resolve().parent
cfg=account_fixture()
prices={'300274':81.97,'159929':1.348,'159562':1.886,'123290':132.5}
feed={(p['type'],p['code']):(prices[p['code']],p['name'],p['baseline_price']) for p in cfg['positions']}
now=datetime(2026,11,9,10,15,tzinfo=ZoneInfo('Asia/Shanghai'))
quality=assess(cfg,feed,now=now.astimezone(timezone.utc))
assert quality['all_held_usable'] is True
assert quality['block_new_signals'] is False
assert quality['block_trade_signals'] is True
assert set(quality['unverified_source'])=={x['code'] for x in cfg['positions']}

verified_feed={k:(v[0],v[1],v[2],now.isoformat()) for k,v in feed.items()}
q=assess(cfg,verified_feed,now=now.astimezone(timezone.utc))
assert q['provider_timestamps_verified'] and not q['block_trade_signals']

# Unit test the scheduler directly with a deliberately stalled provider.
class SlowProvider:
 def __init__(self):self.calls=0;self.gate=Event()
 def stock_bid_ask_em(self,symbol):
  self.calls+=1
  self.gate.wait(timeout=3)
  import pandas as pd
  return pd.DataFrame({'item':['最新','昨收'],'value':[81.97,82.0]})
provider=SlowProvider()
cfg_only={'positions':[{'type':'stock','code':'300274','quantity':300}], 'watchlist':[]}
start=monotonic()
quotes,errors=snapshot_selected_akshare(cfg_only,ak=provider,deadline_seconds=.15)
assert not quotes and errors and monotonic()-start<1.3
assert provider.calls==1
q2,e2=snapshot_selected_akshare(cfg_only,ak=provider,deadline_seconds=.15)
assert not q2 and e2 and provider.calls==1,'must not submit a duplicate while previous call is unfinished'
provider.gate.set()
# Poll next round only after old task exits, without reusing its previous-cycle output.
sleep(.08)
q3,e3=snapshot_selected_akshare(cfg_only,ak=provider,deadline_seconds=1.0)
assert q3[('stock','300274')][0]==81.97 and provider.calls==2

# End-to-end mock: unverified source may display indicative equity, but never alerts.
with TemporaryDirectory() as td:
 out=Path(td)
 with patch.object(mon,'now_cn',return_value=now),\
      patch.object(mon,'load_config',return_value=cfg),\
      patch.object(mon,'verified_trading_date',return_value=(True,'已验证交易日')),\
      patch.object(mon,'refresh_context_async',return_value=None),\
      patch.object(mon,'snapshot_selected_akshare',return_value=(feed,[])),\
      patch.object(mon,'STATUS_FILE',out/'intraday_status.json'),\
      patch.object(mon,'SENT_FILE',out/'sent.json'),\
      patch.object(mon,'HISTORY_FILE',out/'history.jsonl'),\
      patch.object(mon,'SIGNAL_SENT',out/'signals.json'),\
      patch.object(mon,'SIGNAL_HISTORY',out/'signal_history.jsonl'),\
      patch.object(mon,'EQUITY_JOURNAL',out/'equity.jsonl'),\
      patch.object(mon,'CONTEXT_FILE',out/'context.json'),\
      patch.object(mon,'ROOT',root):
  cycle=mon.single_cycle(mode='live',interval=10)
  assert cycle['risk']['valid'] is True
  assert cycle['quote_health']['block_trade_signals'] is True
  assert cycle['alerts']==[] and cycle['trade_signals']==[] and cycle['new_trade_signals']==[]
  assert cycle['fetch_duration_seconds']>=0
  assert '仅显示账户估算' in cycle['status']
  cycle2=mon.single_cycle(mode='live',interval=10)
  assert cycle2['actual_cycle_gap_seconds']==0.0
  realtime_feed={k:(v[0],v[1],v[2],datetime.now(timezone.utc).isoformat()) for k,v in feed.items()}
  with patch.object(mon,'snapshot_selected_akshare',return_value=(realtime_feed,[])):
   cycle3=mon.single_cycle(mode='live',interval=10)
  assert cycle3['quote_health']['provider_timestamps_verified'] is True
  assert cycle3['quote_health']['block_trade_signals'] is False

js=(root/'static/dashboard.js').read_text(encoding='utf-8')
assert 'block_trade_signals' in js and '参考估算·暂停新增' in js
assert '源报价时间未全部核验' in js
# Backup function is non-destructive and copies the journal only when it exists.
import setup_autostart as installer
with TemporaryDirectory() as td:
 base=Path(td); installed=base/'app'; installed.mkdir()
 out=installed/'output';out.mkdir()
 original=json.dumps({'positions':[{'code':'300274','quantity':300}], 'cash':5891.40},ensure_ascii=False)
 (installed/'config.json').write_text(original,encoding='utf-8')
 (out/'manual_trade_log.jsonl').write_text('{"sample":true}\n',encoding='utf-8')
 with patch.object(installer,'BASE',base),patch.object(installer,'APP',installed),patch.object(installer,'OUTPUT',out):
  backup=installer.backup_current_config()
 assert (backup/'config.json').read_text(encoding='utf-8')==original
 assert (backup/'manual_trade_log.jsonl').is_file()
 assert (installed/'config.json').read_text(encoding='utf-8')==original

inst=(root/'setup_autostart.py').read_text(encoding='utf-8')
assert 'backup_current_config' in inst and "EXPECTED_VERSION = '3.6.1'" in inst
assert '3.6.1' in (root/'templates/dashboard.html').read_text()
assert (root/'VERSION.txt').read_text().strip()=='3.6.1'
print('PASS V3.6.1: bounded quote wait, no duplicate in-flight calls, no stale reuse, provider-time verification, no unverified signals, positive duration, pre-upgrade backup, correct UI/install versions')
