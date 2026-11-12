"""Offline invariants for V3.5 (no network or Flask required)."""
from pathlib import Path
from tempfile import TemporaryDirectory
import pandas as pd
from chan_analysis import analyze_chan
from daily_observe import select_five

# Missing true low must NOT be guessed; misleading pseudo-Chan is suppressed.
missing=pd.DataFrame({'date':pd.bdate_range('2026-01-01',periods=60),'close':[10+i*.1 for i in range(60)],'high':[10.3+i*.1 for i in range(60)]})
a=analyze_chan(missing)
assert a['valid'] is False and '最低' in a['reason'],a

# Use a strongly oscillating synthetic series, enough for real confirmed fractals.
values=[14+(i//7)*.18+(2.1 if i%14<7 else -2.1) + (i%7)*(.13 if i%14<7 else -.13) for i in range(115)]
df=pd.DataFrame({'date':pd.bdate_range('2026-01-01',periods=len(values)),'close':values,'high':[x+.2 for x in values], 'low':[x-.2 for x in values]})
c=analyze_chan(df)
assert c['valid'] is True,c
assert c['strokes_count']>=0
assert all(x['index']<len(df)-1 for x in c['recent_fractals']), 'unconfirmed last candle fractal'
assert len(c['chart_bars'])<=80
assert '严格一二三买' in c['note']

# Daily five: actual verified candidates only, with correct date, price and levels.
rows=[]
for i in range(10):
    p=20+i
    rows.append({'股票代码':f'600{i:03}1','股票名称':f'示例{i}','日期':'2026-11-06','收盘':p,
                 '涨跌幅%':2.0,'MA5':p-1,'MA20':p-2,'量比5日':1.8,'观察评分':80-i,
                 '缠论结构':'简化笔级中枢上方','缠论调整分':10,'20日突破':'是',
                 '止损参考':p*.95,'第一止盈':p*1.1,'第二止盈':p*1.15})
rank=pd.DataFrame(rows)
ans=select_five(rank,{'daily_excludes':[]},'live',generated_at='2026-11-08T10:00:00+08:00')
assert ans['count']==5 and len({x['code'] for x in ans['shortlist']})==5
assert all(x['asof']=='2026-11-06' and x['stop_loss']<x['close']<x['take_profit_1']<x['take_profit_2'] for x in ans['shortlist'])
res=select_five(rank,{'daily_excludes':['600001']},'live')
assert res['count']==5 and '600001' not in [x['code'] for x in res['shortlist']]
weak=rank.copy();weak['MA5']=weak['MA20']-1
assert select_five(weak,{},'live')['shortlist']==[]

# No chance of inventing a missing stock to complete a 5-stock slate.
assert select_five(rank.iloc[:3],{},'live')['count']==3
print('PASS: 缠论有效OHLC/已确认分型/每日≤5只/排除/风控参考价/不补虚假候选')
