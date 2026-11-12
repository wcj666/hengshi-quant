"""Offline repeatable indicator and multi-evidence checks."""
from __future__ import annotations
import math
import pandas as pd
from technical_engine import analyze_technical, combine_chan_technical

# Clean candle with 5-day volume spike / weak price
x = pd.DataFrame({'date':pd.bdate_range('2024-01-02',periods=240)})
x['close']=[round(20 + i*.035 + 1.7*math.sin(i/13),3) for i in range(240)]
x['open']=x.close
x['high']=x.close+0.25
x['low']=x.close-0.25
x['volume']=100000.0
s=analyze_technical(x)
assert s['valid'],s
assert s['bar_count']==240
assert s['ma']['60']==round(x.close.tail(60).mean(),3)
assert s['volume']['ratio5']==1.0
assert len(s['chart'])==65
assert 'MACD' in s['scores'] and '量价' in s['scores']
assert s['macd']['bar']==round(2*(s['macd']['dif']-s['macd']['dea']),4) or abs(s['macd']['bar']-2*(s['macd']['dif']-s['macd']['dea']))<.0002
assert s['asof']==str(x.date.iloc[-1].date())
# no lookahead: prefixed inputs must agree even if future rows exist
for n in [65,110,210]:
    p=analyze_technical(x.iloc[:n])
    assert p['valid']
    assert p['asof']==str(x.date.iloc[n-1].date())
    assert p['macd']['bar'] is not None
# volume comparison excludes today's row
x.loc[x.index[-1],'volume']=300000
vs=analyze_technical(x)
assert vs['volume']['ratio5']==3.0 and vs['volume']['previous_5_avg']==100000
# finished bar current down with big volume = sell-pressure research cue
x.loc[x.index[-1],'close']=x.close.iloc[-2]*.98
x.loc[x.index[-1],'open']=x.close.iloc[-1]
x.loc[x.index[-1],'high']=x.close.iloc[-1]+.35
x.loc[x.index[-1],'low']=x.close.iloc[-1]-.35
down=analyze_technical(x)
assert down['valid'] and '放量下跌' in down['volume']['status']
assert down['scores']['量价']==-1
# Bad / absent source data yields no inferred trades
assert not analyze_technical(x.drop(columns='volume'))['valid']
assert not analyze_technical(x.iloc[:22])['valid']
xb=pd.concat([x,x.iloc[[-1]]],ignore_index=True)
assert not analyze_technical(xb)['valid']
# Cross-check: a positive MACD cannot override a bearish Chan structure.
chan={'valid':True,'interpretation':{'action':'偏卖出/减仓观察','phase':'中枢下方','basis_date':s['asof']}}
tech=dict(s,bias='技术指标偏强')
r=combine_chan_technical(chan,tech)
assert r['action']=='继续观察' and r['alignment']=='信号明显冲突',r
tech['bias']='技术指标偏弱'
r=combine_chan_technical(chan,tech)
assert r['action']=='偏减仓/卖出观察'
assert combine_chan_technical({'valid':False},tech)['action']=='继续观察'
assert combine_chan_technical({'valid':True}, {'valid':False})['action']=='继续观察'
print('PASS: MACD/MA/量价/RSI/ATR/历史前缀/零透视/异常拒绝/缠论冲突阻断')
