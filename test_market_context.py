"""Offline checks for A-share auction and US/Japan/Korea temporal integrity."""
from __future__ import annotations
import datetime as dt
import pandas as pd
from zoneinfo import ZoneInfo
from market_context import current_auction_phase, parse_auction, parse_global, match_index
from intraday_monitor import single_cycle

CST=ZoneInfo('Asia/Shanghai')

# Sunday/weekday and auction timing boundaries
for h,m,expected in [(9,14,'closed'),(9,15,'cancellable'),(9,19,'cancellable'),(9,20,'locked'),(9,24,'locked'),(9,25,'confirmation'),(9,29,'confirmation'),(9,30,'closed')]:
    assert current_auction_phase(dt.datetime(2026,11,9,h,m,tzinfo=CST))==expected,(h,m)
assert current_auction_phase(dt.datetime(2026,11,8,9,20,tzinfo=CST))=='closed'

now=dt.datetime(2026,11,9,9,24,tzinfo=CST)
bars=pd.DataFrame([{'时间':'2026-11-06 09:22:00','收盘':100},
 {'时间':'2026-11-09 09:21:00','收盘':24.1},
 {'时间':'2026-11-09 09:23:00','收盘':24.2},
 {'时间':'2026-11-09 09:26:00','收盘':24.3}])
res=parse_auction(bars,'600406','国电南瑞',now)
assert res['price']==24.2 and res['usable'] and res['indicative_change_pct'] is None
assert res['auction_volume_lots'] is None and not res['matching_volume_verified']
res=parse_auction(bars,'600406','国电南瑞',dt.datetime(2026,11,9,9,27,tzinfo=CST))
assert res['price']==24.3 and '非交易所认证' in res['quality']
res=parse_auction(bars,'600406','国电南瑞',dt.datetime(2026,11,10,9,24,tzinfo=CST))
assert res['price'] is None and not res['usable']  # reject prior-day quote

# Beijing 9:24 Mon is Sunday evening New York: latest completed cash session is Friday.
global_frame=pd.DataFrame([
 {'名称':'标普500指数','代码':'SPX','最新价':6000,'涨跌幅':1.2,'最新行情时间':'2026-11-06 16:01:00'},
 {'名称':'日经225','代码':'N225','最新价':40000,'涨跌幅':-1.0,'最新行情时间':'2026-11-09 10:22:00'},
 {'名称':'韩国综合指数','代码':'KOSPI','最新价':3000,'涨跌幅':0.7,'最新行情时间':'2026-11-09 10:21:00'},
 {'名称':'纳斯达克指数','代码':'IXIC','最新价':22000,'涨跌幅':1.5,'最新行情时间':'2026-10-01 16:00:00'},
])
vals={x['key']:x for x in parse_global(global_frame,now)}
assert vals['SP500']['usable'] and vals['SP500']['source_time'].startswith('2026-11-06')
assert vals['NIKKEI']['usable'] and vals['KOSPI']['usable']
assert not vals['NASDAQ']['usable']
assert vals['TOPIX']['price'] is None
assert match_index('纳斯达克100指数','NDX') is None
assert match_index('韩国KOSPI200','KOSPI200') is None

# No stale prior-day auction data can trigger risk alarms in demonstration mode.
state=single_cycle(mode='demo',interval=180)
assert state['mode']=='demo' and not state.get('notifications_attempted')
print('PASS: auction schedule, date/stale exclusion, global timezone, missing indices, demo safety')
