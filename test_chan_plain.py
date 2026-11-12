"""Unit tests: deterministic, explicitly cautious Chan explanatory labels."""
import pandas as pd
from chan_analysis import analyze_chan, describe_chan_in_plain_chinese

raw=[{'date':f'2026-09-{i+1:02d}', 'close':10., 'high':10.2, 'low':9.8} for i in range(30)]
for i in range(25,30):raw[i]['close']=11.0+0.25*(i-25)
fractals=[{'kind':'底分型','index':24,'date':'2026-09-25','price':10.2}]
up=[{'direction':'上笔'}]
pivot={'lower':10.4,'upper':10.9,'last_stroke_end':26}
res=describe_chan_in_plain_chinese(raw,fractals,up,pivot)
assert res['action']=='偏买入观察' and '不代表马上买' in res['summary'],res
assert '中枢上方' in res['phase']
assert '底分型' in res['zone']
assert '不是' in res['disclaimer']

recent_top=describe_chan_in_plain_chinese(raw,[{'kind':'顶分型','index':27,'date':'2026-09-28','price':12.7}],up,pivot)
assert recent_top['action']=='继续观察', recent_top

for i in range(25,30):raw[i]['close']=10.2-0.30*(i-25)
down=describe_chan_in_plain_chinese(raw,[{'kind':'顶分型','index':25,'date':'2026-09-26','price':11.2}], [{'direction':'下笔'}], pivot)
assert down['action']=='偏卖出/减仓观察',down
assert '下跌' in down['phase']
assert '顶分型' in down['zone']

for i in range(25,30):raw[i]['close']=10.7
flat=describe_chan_in_plain_chinese(raw,fractals,up,pivot)
assert flat['action']=='继续观察',flat
assert '震荡' in flat['phase']

old=describe_chan_in_plain_chinese(raw,fractals,up,{**pivot,'last_stroke_end':-20})
assert old['action']=='继续观察' and '旧中枢' in old['phase'],old

no_structure=describe_chan_in_plain_chinese(raw,[],[],None)
assert no_structure['action']=='继续观察' and no_structure['latest_stroke']=='笔尚未形成'

invalid=analyze_chan(pd.DataFrame({'date':pd.bdate_range('2026-10-01',periods=35),'close':[1]*35}))
assert invalid['valid'] is False
print('PASS: 上升观察/下跌风险/中枢震荡/无结构/行情不足防止伪信号')
