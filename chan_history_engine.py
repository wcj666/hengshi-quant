"""Audit-friendly, no-lookahead historical Chan-pattern research engine.

Fixed, conservative OLD-pen rules. This is not a claim to a canonical implementation
of segment feature sequences or recursive multi-timeframe Chan buy/sell points.
Signals are structural *candidates* only and require second-level validation.
"""
from __future__ import annotations
from typing import Any
import math
import pandas as pd

MIN_BARS = 120
OLD_PEN_CENTER_GAP = 4  # centers at least four standardized bars apart => at least 5 total


def _r(v: float) -> float:
    return round(float(v), 3)


def _as_ohlc(df: pd.DataFrame) -> list[dict]:
    names={'date', 'open', 'high', 'low', 'close'}
    if not names.issubset(df.columns):
        raise ValueError('缺少真实OHLC日期/开高低收字段；不能推测K线高低点')
    out=[]
    for x in df.sort_values('date').itertuples(index=False):
        try:
            dt=str(x.date.date() if hasattr(x.date,'date') else x.date)[:10]
            o,h,l,c=(float(getattr(x,k)) for k in ('open','high','low','close'))
            if not all(math.isfinite(v) for v in (o,h,l,c)) or l<=0 or h<l or not (l<=o<=h and l<=c<=h):
                continue
            if out and dt==out[-1]['date']:
                # Data vendors occasionally duplicate a day; reject rather than use lookahead.
                raise ValueError('历史日线存在重复日期，暂停结构分析')
            out.append({'date':dt,'open':o,'high':h,'low':l,'close':c,'index':len(out),
                        'volume':max(0,float(getattr(x,'volume',0) or 0))})
        except (AttributeError, TypeError, ValueError) as exc:
            if '重复日期' in str(exc): raise
            continue
    return out


def _merge(raw: list[dict]) -> list[dict]:
    merged=[]
    direction=0
    for x in raw:
        a={'date':x['date'],'high':x['high'],'low':x['low'],'close':x['close'],
           'start_index':x['index'],'end_index':x['index'],'index':x['index'],
           'start_date':x['date'],'source_count':1}
        if not merged:
            merged.append(a);continue
        prev=merged[-1]
        incl=(prev['high']>=a['high'] and prev['low']<=a['low']) or (a['high']>=prev['high'] and a['low']<=prev['low'])
        if incl:
            # Determine direction from last distinct non-contained bar; if undecidable,
            # retain prior direction, otherwise defer to conservative upward default.
            if len(merged)>1:
                older=merged[-2]
                if prev['high']>older['high'] and prev['low']>older['low']:direction=1
                elif prev['high']<older['high'] and prev['low']<older['low']:direction=-1
            if direction>=0:
                prev['high']=max(prev['high'],a['high']);prev['low']=max(prev['low'],a['low'])
            else:
                prev['high']=min(prev['high'],a['high']);prev['low']=min(prev['low'],a['low'])
            prev['end_index']=a['end_index'];prev['index']=a['end_index'];prev['date']=a['date'];prev['close']=a['close'];prev['source_count']+=1
        else:
            if a['high']>prev['high'] and a['low']>prev['low']:direction=1
            elif a['high']<prev['high'] and a['low']<prev['low']:direction=-1
            merged.append(a)
    for i,x in enumerate(merged):x['merged_index']=i
    return merged


def _fractals(bars: list[dict]) -> list[dict]:
    result=[]
    for i in range(1,len(bars)-1):
        a,b,c=bars[i-1:i+2]
        top=b['high']>max(a['high'],c['high']) and b['low']>max(a['low'],c['low'])
        bottom=b['high']<min(a['high'],c['high']) and b['low']<min(a['low'],c['low'])
        if top or bottom:
            result.append({'kind':'顶分型' if top else '底分型', 'merged_index':i,
              'index':b['end_index'],'date':b['date'],'price':_r(b['high'] if top else b['low']),
              'confirmed_on':c['date'],'confirm_index':c['end_index'],
              'raw_start_index':a['start_index'],'raw_end_index':c['end_index']})
    return result


def _strokes(fx: list[dict], bars: list[dict]) -> list[dict]:
    endpoints=[]
    for f in fx:
        if not endpoints:
            endpoints.append(f);continue
        prev=endpoints[-1]
        if f['kind']==prev['kind']:
            stronger=f['price']>prev['price'] if f['kind']=='顶分型' else f['price']<prev['price']
            if stronger:endpoints[-1]=f
            continue
        # old-pen: centers 4+ standardized bars apart and two 3-bar fractals
        # must not overlap (three bars each) .
        gap=f['merged_index']-prev['merged_index']
        if gap<OLD_PEN_CENTER_GAP:continue
        if prev['kind']=='底分型' and f['price']<=prev['price']:continue
        if prev['kind']=='顶分型' and f['price']>=prev['price']:continue
        endpoints.append(f)
    strokes=[]
    for a,b in zip(endpoints,endpoints[1:]):
        assert a['merged_index']+OLD_PEN_CENTER_GAP<=b['merged_index']
        strokes.append({'start_index':a['index'],'end_index':b['index'],
          'start_date':a['date'],'end_date':b['date'], 'confirm_date':b['confirmed_on'],
          'confirm_index':b['confirm_index'], 'start_price':a['price'],'end_price':b['price'],
          'direction':'上笔' if a['kind']=='底分型' else '下笔',
          'low':min(a['price'],b['price']),'high':max(a['price'],b['price']),
          'bars_between':b['merged_index']-a['merged_index']})
    return strokes


def _zones(strokes: list[dict]) -> list[dict]:
    zones=[]
    # Each 3 consecutive confirmed pens forms a pen-level overlap candidate.
    # Overlapping consecutive triples extend the SAME zone.  Only fully finished
    # pen triples count; this is NOT a confirmed recursive segment-level centre.
    for i in range(len(strokes)-2):
        trio=strokes[i:i+3]
        zd=max(s['low'] for s in trio)
        zg=min(s['high'] for s in trio)
        if zg<=zd+1e-9:continue
        start_idx=i
        if zones and i<=zones[-1]['stroke_end_index'] and zd<zones[-1]['upper'] and zg>zones[-1]['lower']:
            z=zones[-1]
            z['lower']=_r(max(z['lower'],zd));z['upper']=_r(min(z['upper'],zg))
            z['stroke_end_index']=i+2;z['end_date']=trio[-1]['end_date']
            z['last_stroke_end']=trio[-1]['end_index'];z['confirm_date']=trio[-1]['confirm_date']
        else:
            zones.append({'lower':_r(zd),'upper':_r(zg),'stroke_start_index':start_idx,
              'stroke_end_index':i+2,'start_date':trio[0]['start_date'],
              'end_date':trio[-1]['end_date'],'last_stroke_end':trio[-1]['end_index'],
              'confirm_date':trio[-1]['confirm_date']})
    return [z for z in zones if z['upper']>z['lower']]


def _strength(stroke: dict, raw: list[dict]) -> float:
    a,b=stroke['start_index'],stroke['end_index']
    if b<=a:return 0.0
    returns=[abs(math.log(raw[i]['close']/raw[i-1]['close'])) for i in range(a+1,min(len(raw),b+1)) if raw[i]['close']>0 and raw[i-1]['close']>0]
    return sum(returns)/max(1,len(returns))


def _point_candidates(strokes: list[dict], zones: list[dict], raw: list[dict]) -> list[dict]:
    """Class 1/2/3 STRUCTURAL CANDIDATES. Not validated strict Chan points.

    Level-dependent segment and lower-timeframe divergence checks are deliberately
    not implied. Every candidate is labelled as such in its payload.
    """
    pts=[]
    if not strokes or not zones:return pts
    def add(kind, at, price, why, z, recognized_on=None):
        pts.append({'kind':kind,'date':at['end_date'],'confirmed_on':recognized_on or at['confirm_date'],
          'price':_r(price),'status':'结构候选（非严格确认）','why':why,
          'zone_upper':z['upper'],'zone_lower':z['lower'],'end_index':at['end_index']})
    for z in zones:
        end=z['stroke_end_index']
        after=strokes[end+1:]
        if len(after)<2:continue
        # III: exit a COMPLETED pen from a finished pen-zone, then completed
        # opposite pullback remains beyond the same boundary. The point itself is
        # only marked after the pullback pen is confirmed by its right-hand K-line.
        for i in range(0,len(after)-1):
            a,b=after[i:i+2]
            if a['direction']=='上笔' and b['direction']=='下笔' and a['end_price']>z['upper'] and b['end_price']>z['upper']:
                add('三买候选',b,b['end_price'],'向上离开笔级中枢，已确认回踩低点高于中枢上沿；需次级别离开/回试确认',z)
            if a['direction']=='下笔' and b['direction']=='上笔' and a['end_price']<z['lower'] and b['end_price']<z['lower']:
                add('三卖候选',b,b['end_price'],'向下离开笔级中枢，已确认反抽高点低于中枢下沿；需次级别离开/回抽确认',z)
        # I: directional exhaustion heuristic + confirmed counter pen.
        # Cannot claim actual Chan divergence without confirmed recursive segments.
        for i in range(2,len(after)):
            x,y,t=after[i-2:i+1]
            if x['direction']=='下笔' and y['direction']=='上笔' and t['direction']=='下笔' and i+1<len(after):
                confirm=after[i+1]
                if confirm['direction']=='上笔' and t['end_price']<x['end_price'] and t['end_price']<z['lower'] and _strength(t,raw)<0.85*_strength(x,raw):
                    add('一买候选',t,t['end_price'],'两次下笔创新低但平均绝对对数波动减弱；只是力度代理，非严格背驰',z,recognized_on=confirm['confirm_date'])
            if x['direction']=='上笔' and y['direction']=='下笔' and t['direction']=='上笔' and i+1<len(after):
                confirm=after[i+1]
                if confirm['direction']=='下笔' and t['end_price']>x['end_price'] and t['end_price']>z['upper'] and _strength(t,raw)<0.85*_strength(x,raw):
                    add('一卖候选',t,t['end_price'],'两次上笔创新高但平均绝对对数波动减弱；只是力度代理，非严格背驰',z,recognized_on=confirm['confirm_date'])
    # II: a later pullback higher than 1-buy, or rebound lower than 1-sell,
    # requiring two more COMPLETE alternating pens.
    early=list(pts)
    for p in early:
        if p['kind'] not in ('一买候选','一卖候选'):continue
        following=[s for s in strokes if s['start_index']>=p['end_index']]
        if len(following)<2:continue
        a,b=following[:2]
        z={'lower':p['zone_lower'],'upper':p['zone_upper']}
        if p['kind']=='一买候选' and a['direction']=='上笔' and b['direction']=='下笔' and b['end_price']>p['price']:
            add('二买候选',b,b['end_price'],'一买结构后的首次完整回落未创新低，仍需次级别确认',z)
        if p['kind']=='一卖候选' and a['direction']=='下笔' and b['direction']=='上笔' and b['end_price']<p['price']:
            add('二卖候选',b,b['end_price'],'一卖结构后的首次完整反弹未创新高，仍需次级别确认',z)
    return sorted(pts,key=lambda x:(x['end_index'],x['kind']))


def analyze_chan_history(df: pd.DataFrame) -> dict[str,Any]:
    """Use >=120 finished historical daily OHLC bars, with zero lookahead."""
    try:raw=_as_ohlc(df)
    except (ValueError,KeyError) as exc:
        return {'valid':False,'reason':str(exc),'engine':'history-oldpen-v1','points':[]}
    if len(raw)<MIN_BARS:
        return {'valid':False,'reason':f'真实完整日K线不足：仅{len(raw)}根；历史结构要求至少{MIN_BARS}根。上市时间短的转债不做缠论买卖点判断。',
                'raw_count':len(raw),'engine':'history-oldpen-v1','points':[],
                'interpretation':{'action':'继续观察','phase':'历史K线不足','summary':'历史样本不足，不做顶底或一二三类点判断。'}}
    merged=_merge(raw)
    fractals=_fractals(merged)
    strokes=_strokes(fractals,merged)
    zones=_zones(strokes)
    points=_point_candidates(strokes,zones,raw)
    last=raw[-1];last_fractal=fractals[-1] if fractals else None
    recent=strokes[-1] if strokes else None
    z=zones[-1] if zones else None
    is_recent=z and last['index']-z['last_stroke_end']<=60
    support=next((f['price'] for f in reversed(fractals) if f['kind']=='底分型' and f['price']<last['close']),None)
    resistance=next((f['price'] for f in reversed(fractals) if f['kind']=='顶分型' and f['price']>last['close']),None)
    risk='继续观察';phase='结构尚未充分确认';why=[];trigger='等待分型、完整笔和中枢形成，并与实际量价复核';invalidation='条件尚不充分，不能用单根K线判定趋势反转'
    if is_recent:
        if last['close']>z['upper']:
            phase='笔级中枢上方（偏强，仍可能回调）'
            trigger=f'等待回试不进入 {z["upper"]:.3f} 元上方中枢后再看走强'
            invalidation=f'重新跌入 {z["lower"]:.3f}—{z["upper"]:.3f} 元中枢须降低强势判断'
        elif last['close']<z['lower']:
            phase='笔级中枢下方（偏弱，仍可能反弹）'
            risk='偏卖出/减仓观察'
            trigger=f'跌破中枢下沿 {z["lower"]:.3f} 元后无法收复，重点复核持仓风险'
            invalidation=f'重新收回中枢 {z["lower"]:.3f} 元上方需重新评估'
        else:
            phase='笔级中枢震荡（方向未确认）'
            trigger=f'观察能否有效离开 {z["lower"]:.3f}—{z["upper"]:.3f} 元'
        why.append(f'最近已完成笔级重叠区 {z["lower"]:.3f}—{z["upper"]:.3f} 元')
    elif recent:
        phase='最后已确认上笔（当前走势不一定仍上涨）' if recent['direction']=='上笔' else '最后已确认下笔（当前走势不一定仍下跌）'
        why.append('没有足够近期的笔级中枢证据，不能认定三买/三卖')
    if last_fractal:
        why.append(f'最近分型：{last_fractal["kind"]}，{last_fractal["date"]}，右侧日K于{last_fractal["confirmed_on"]}确认')
    if recent:
        why.append(f'最近确认完整笔为{recent["direction"]}（终点 {recent["end_date"]}，确认于 {recent["confirm_date"]}）')
    date_index={r['date']:r['index'] for r in raw}
    fresh_pts=[p for p in points if 0<=last['index']-date_index.get(p.get('confirmed_on'),-10000)<=5
               and 0<=last['index']-p['end_index']<=10]
    fresh_pts.sort(key=lambda p:(p['confirmed_on'],p['date']))
    if fresh_pts:
        p=fresh_pts[-1]
        why.append(f'最近出现{p["kind"]}：{p["date"]}，仅为规则定义的结构候选')
        if p['kind'].endswith('买候选') and risk!='偏卖出/减仓观察':
            risk='偏买入观察'
        elif p['kind'].endswith('卖候选'):
            risk='偏卖出/减仓观察'
    summary={'偏买入观察':'历史结构有潜在买点，但未完成严格次级别和走势力度确认，不能据此直接买入。',
             '偏卖出/减仓观察':'历史结构存在走弱或卖点候选，持仓者优先核对风险与支撑。',
             '继续观察':'尚未出现足够可靠且近期的完整结构，优先等待，不把分型直接当买卖点。'}[risk]
    f_age=last['index']-last_fractal['index'] if last_fractal else None
    zone='近期无明确顶底分型' if not last_fractal else f'{last_fractal["kind"]}附近（已确认，不等于真正顶部或底部）' if f_age<=12 else f'最近确认{last_fractal["kind"]}距今{f_age}根日K线'
    interp={'summary':summary,'action':risk,'phase':phase,'zone':zone,'latest_stroke':recent['direction'] if recent else '无完整笔',
        'last_fractal':({'kind':last_fractal['kind'],'date':last_fractal['date'],'price':last_fractal['price'],
                         'bars_ago':f_age,'confirmed_on':last_fractal['confirmed_on']} if last_fractal else None),
        'reasons':why,'trigger':trigger,'invalidation':invalidation,'basis_close':_r(last['close']),
        'basis_date':last['date'],'disclaimer':'固定旧笔规则＋已完成日K；笔级中枢为近似，六类点仅标结构候选，未完成严格次级别走势/线段/背驰确认。'}
    if z:
        pivot=dict(z)
    else:pivot=None
    adj=12 if is_recent and last['close']>z['upper'] else -15 if is_recent and last['close']<z['lower'] else 0
    return {'valid':True,'engine':'history-oldpen-v1','rule_name':'固定旧笔：分型不重叠、中心间隔≥4根标准K线、右侧K线确认；三笔区间重叠为笔级中枢候选',
      'min_history':MIN_BARS,'asof':last['date'],'raw_count':len(raw),'merged_count':len(merged),
      'fractals_count':len(fractals),'strokes_count':len(strokes),'zone_count':len(zones),
      'condition':phase,'score_adjustment':adj,'interpretation':interp,
      'signal_window':{'max_confirmation_bars':5,'max_point_bars':10},
      'recent_fractals':fractals[-9:],'strokes':strokes[-20:],'zones':zones[-8:],'pivot':pivot,
      'points':points[-18:],'recent_points':fresh_pts[-6:],
      'chart_bars':[{'index':x['index'],'date':x['date'],'close':_r(x['close'])} for x in raw[-100:]],
      'support':support,'resistance':resistance,
      'confirmation':'完整旧笔已核验；递归线段、走势级别和严格背驰未核验；一二三类标记仅为候选',
      'note':'只使用截至分析日的历史OHLC；右侧K线收盘后才确认分型。笔级中枢区间会随新笔延伸修订，旧候选不可当作无重绘回测信号；严格买卖点需要次级别与背驰核验。'}
