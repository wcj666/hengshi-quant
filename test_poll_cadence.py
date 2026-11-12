"""Offline regression test for V3.5.9 polling and global-API throttling."""
import datetime as dt
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import intraday_monitor as monitor

ROOT=Path(__file__).resolve().parent
script=(ROOT/'setup_autostart.py').read_text(encoding='utf-8')
js=(ROOT/'static/dashboard.js').read_text(encoding='utf-8')
web=(ROOT/'web_dashboard.py').read_text(encoding='utf-8')
assert "'--interval', '10', '--notify'" in script
assert 'setInterval(refreshMonitor,6000)' in js
assert 'setInterval(refreshMarketContext,6000)' in js
assert "state.get('config_hash')!=json_hash(current_cfg)" in web
assert "'positions':risk['positions'],'sizing':risk['sizing']" in (ROOT/'intraday_monitor.py').read_text(encoding='utf-8')
assert 'renderSizing(r.sizing||[])' in js

now=dt.datetime(2026,11,9,10,0,tzinfo=monitor.CST)
with tempfile.TemporaryDirectory() as tmp:
    path=Path(tmp)/'market_context.json'
    fetched=now-dt.timedelta(seconds=120)
    path.write_text(json.dumps({'mode':'live','global':{'fetched_at':fetched.isoformat(),'rows':[],'source':'cached'}}),encoding='utf-8')
    with patch.object(monitor,'CONTEXT_FILE',path),patch.object(monitor,'get_global',side_effect=AssertionError('should use cache')):
        assert monitor.get_global_throttled(now)['source']=='cached'
    later=now+dt.timedelta(minutes=6)
    with patch.object(monitor,'CONTEXT_FILE',path),patch.object(monitor,'get_global',return_value={'source':'fresh'}) as lookup:
        assert monitor.get_global_throttled(later)['source']=='fresh'
        lookup.assert_called_once()
print('PASS: 10秒后台/6秒页面/持仓风控同批快照/旧配置拒绝/海外接口限速')
