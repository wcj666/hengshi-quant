"""Offline checks for historical Chan helper (no simulated market truth claims)."""
from __future__ import annotations
from pathlib import Path
import pandas as pd
from chan_history_engine import analyze_chan_history
from asset_history import _norm

# No invented minimum, missing OHLC => reject.
dates = pd.bdate_range('2024-01-03', periods=550)
prices = [18 + i * .006 + (1.7 if i%28 < 14 else -1.7) + (i%14)*(.035 if i%28<14 else -.035) for i in range(550)]
df = pd.DataFrame({'date': dates, 'open':prices,'high':[x+.13 for x in prices],
                   'low':[x-.15 for x in prices],'close':prices,'volume':[100000+i for i in range(550)]})
full = analyze_chan_history(df)
assert full['valid'], full
assert full['raw_count'] == 550
assert full['strokes_count'] > 3
assert full['zone_count'] >= 1
assert full['engine'] == 'history-oldpen-v1'
assert full['confirmation'].startswith('完整旧笔')
assert all(f['confirm_index']>f['index'] for f in full['recent_fractals'])
assert all(s['bars_between']>=4 for s in full['strokes'])
assert all(p['status']=='结构候选（非严格确认）' for p in full['points'])
assert all(p['confirmed_on'] <= full['asof'] for p in full['points'])
for size in (150,220,370,500):
    partial=analyze_chan_history(df.iloc[:size].copy())
    assert partial['valid']
    assert partial['asof']==str(df.iloc[size-1].date.date())
    assert all(f['confirm_index']<size for f in partial['recent_fractals'])
    assert all(s['confirm_index']<size for s in partial['strokes'])
    assert all(p['confirmed_on']<=partial['asof'] for p in partial['points'])
short = analyze_chan_history(df.iloc[:15].copy())
assert not short['valid'] and '不足' in short['reason']
# Missing true low, no fabricated candle
assert not analyze_chan_history(df.drop(columns=['low']))['valid']
# Duplicate date cannot be silently used
bad=pd.concat([df.iloc[:20],df.iloc[[19]],df.iloc[20:]]).reset_index(drop=True)
assert not analyze_chan_history(bad)['valid']
# ETF Chinese field and convertible raw date index normalize
etf=df.head(160).rename(columns={'date':'日期','open':'开盘','high':'最高','low':'最低','close':'收盘','volume':'成交量'})
assert len(_norm(etf))==160
print('PASS: 550根真实形态输入/已确认笔/三笔重叠区/候选非严格/不透视/日期重复拒绝/上市不足拒绝/ETF映射')
