"""Conservative, *simplified* Chan-style daily structure study.

This is NOT a canonical implementation of Chan's full segment-level analysis or
of class-1/2/3 buy points. All extrema are confirmed using a right-side bar,
and the final unsettled K-line is never turned into an entry signal.
"""
from __future__ import annotations
import math
from typing import Any



def describe_chan_in_plain_chinese(raw, fractals, strokes, pivot):
    """Conservative explanations of a simplified confirmed daily pattern.

    These are deterministic descriptions, *not* complete Chan buy/sell points.
    No forward bars or intraday price estimates enter the judgment.
    """
    close = raw[-1]['close']
    last_fractal = fractals[-1] if fractals else None
    age = len(raw) - 1 - last_fractal['index'] if last_fractal else None
    latest_stroke = strokes[-1] if strokes else None
    change5 = close - raw[-6]['close'] if len(raw) >= 6 else 0
    recent_rising = change5 > close * 0.002  # small buffer to avoid noise
    recent_falling = change5 < -close * 0.002
    latest_direction = latest_stroke['direction'] if latest_stroke else '笔尚未形成'
    stroke_age = len(raw)-1-latest_stroke['end_index'] if latest_stroke and 'end_index' in latest_stroke else 0
    zone = '暂无明确顶底分型区'
    if last_fractal:
        which = last_fractal['kind']
        zone = (f'{which}附近（已确认，不等于市场底部/顶部）'
                if age is not None and age <= 12 else f'最近确认{which}较远（{age}根日K线前）')
    pivot_age = (len(raw) - 1 - pivot.get('last_stroke_end', len(raw)-1)) if pivot else None
    pivot_is_recent = bool(pivot) and pivot_age <= 30
    if pivot_is_recent:
        if close > pivot['upper']:
            phase = '中枢上方：上升结构' if recent_rising else '中枢上方：回踩观察'
        elif close < pivot['lower']:
            phase = '中枢下方：下跌结构' if recent_falling else '中枢下方：反抽观察'
        else:
            phase = '中枢内部：震荡整理'
    elif latest_stroke:
        if latest_direction == '上笔':
            phase = '上升笔后短线回调' if recent_falling else '上升笔阶段'
        else:
            phase = '下跌笔后短线反抽' if recent_rising else '下跌笔阶段'
        phase += '（旧中枢仅供参考）' if pivot else '（中枢未确认）'
    else:
        phase = '结构不充分：上升/下跌尚难判断'
    reasons=[]
    if last_fractal:
        reasons.append(f'最近确认的{last_fractal["kind"]}在{last_fractal["date"]}，距离分析日线末端约{age}根K线')
    else:
        reasons.append('尚未识别出可靠的已确认顶底分型')
    if latest_stroke:
        reasons.append(f'最近一笔为{latest_direction}，但不代表最后几根日K线也沿同一方向运行')
    else:
        reasons.append('有效笔数量不足，不适合据此判断买卖')
    if pivot:
        reasons.append(f'笔级重叠区参考范围 {pivot["lower"]:.3f}—{pivot["upper"]:.3f}元，最新日线收盘 {close:.3f}元')
        if not pivot_is_recent: reasons.append(f'该重叠区距最近完成日线已有{pivot_age}根K线，仅作历史参考，不产生买卖观察信号')
    else:
        reasons.append('暂未形成可验证的三笔重叠区；信号依据较弱')

    # Deliberately strict; a standalone fractal is NEVER a buy/sell instruction.
    action = '继续观察'
    trigger = '等待至少一个方向出现更多日线收盘确认，不因单个分型就下单'
    invalidation = '结构尚不充分；先观察已确认分型、后续笔和量价变化'
    if (pivot_is_recent and close > pivot['upper'] and recent_rising and latest_direction == '上笔' and stroke_age <= 20
            and not (last_fractal and last_fractal['kind'] == '顶分型' and age is not None and age <= 8)):
        action = '偏买入观察'
        trigger = f'后续日线连续站稳中枢上沿 {pivot["upper"]:.3f}元，回踩不破并结合量价确认'
        invalidation = f'若重新跌回中枢上沿 {pivot["upper"]:.3f}元下方，转为等待；不是确认的缠论三买'
        reasons.append('最新收盘高于中枢上沿、近5日日线偏强，且最近确认笔向上')
    elif pivot_is_recent and close < pivot['lower'] and recent_falling:
        action = '偏卖出/减仓观察'
        trigger = f'若后续继续收于 {pivot["lower"]:.3f}元下方，关注控制下跌风险'
        invalidation = f'若重新收回中枢下沿 {pivot["lower"]:.3f}元上方，重新评估弱势判断'
        reasons.append('最新收盘低于中枢下沿且近5日方向走弱')
    elif last_fractal and age is not None and age <= 8 and last_fractal['kind'] == '顶分型' and recent_falling and latest_direction == '下笔' and not pivot:
        action = '偏卖出/减仓观察'
        trigger = '观察后续是否继续走弱及是否跌破最近确认的结构支撑'
        invalidation = '若重新形成上笔并收复关键压力位，重新评估'
        reasons.append('近期确认顶分型、近5日回落且最后确认笔向下；此为风险复核，并非精准顶部')
    elif pivot_is_recent:
        trigger = f'关注收盘能否有效离开 {pivot["lower"]:.3f}—{pivot["upper"]:.3f}元区间'
        invalidation = '中枢内来回穿越并不构成可靠交易信号'

    if action == '偏买入观察':
        summary = '日线结构偏强，但不代表马上买：先等突破位收盘确认，再结合量价和账户风险预算。'
    elif action == '偏卖出/减仓观察':
        summary = '日线结构偏弱，持仓者应先复核止损和仓位风险；不是断言股价还会继续跌。'
    else:
        summary = '目前没有足够一致的缠论结构证据支持买入或卖出，更适合继续观察。'
    return {
        'summary':summary, 'action': action, 'phase': phase, 'zone': zone,
        'last_fractal': ({'kind':last_fractal['kind'], 'date':last_fractal['date'],
                          'price':last_fractal['price'], 'bars_ago':age} if last_fractal else None),
        'latest_stroke': latest_direction,
        'reasons': reasons,
        'trigger': trigger, 'invalidation': invalidation,
        'basis_close': round(close, 3), 'basis_date': raw[-1]['date'],
        'disclaimer': '简化日线缠论辅助观察；不是完整的一二三买卖点，也不是盘中实时信号。',
    }

def analyze_chan(hist) -> dict[str, Any]:
    """Return JSON-safe, explainable daily Chan-style structures from OHLC.

    Uses past/finished rows only. Never fills a missing low with a guessed low.
    Candlestick inclusion is simplified and may differ from Chan practitioners.
    """
    need = {'date', 'high', 'low', 'close'}
    if not need.issubset(hist.columns):
        return {'valid': False, 'reason': '原始日线缺少最低价或日期，无法分析缠论结构', 'note': '简化缠论辅助，不提供严格一二三买认定'}
    raw = []
    for r in hist.itertuples(index=False):
        try:
            high, low, close = float(r.high), float(r.low), float(r.close)
            if not all(map(math.isfinite, (high, low, close))) or low <= 0 or low > high or not (low - 1e-7 <= close <= high + 1e-7):
                continue
            date = str(r.date.date() if hasattr(r.date, 'date') else r.date)
            raw.append({'high': high, 'low': low, 'close': close, 'date': date})
        except (TypeError, ValueError, AttributeError):
            continue
    if len(raw) < 35:
        return {'valid': False, 'reason': f'有效日线只有{len(raw)}根，至少需要35根', 'note': '简化缠论辅助，不提供严格一二三买认定'}

    # The inclusion merge is a reference approximation. Track original index so
    # the derived strokes can be drawn against chronological close points.
    merged = []
    for idx, k in enumerate(raw):
        new = dict(k, index=idx)
        if not merged:
            merged.append(new)
            continue
        last = merged[-1]
        includes = (last['high'] >= new['high'] and last['low'] <= new['low']) or (new['high'] >= last['high'] and new['low'] <= last['low'])
        if not includes:
            merged.append(new)
            continue
        prev = merged[-2] if len(merged) >= 2 else None
        upward = True if prev is None else (last['high'] + last['low'] >= prev['high'] + prev['low'])
        if upward:
            last['high'] = max(last['high'], new['high'])
            last['low'] = max(last['low'], new['low'])
        else:
            last['high'] = min(last['high'], new['high'])
            last['low'] = min(last['low'], new['low'])
        last['index'] = idx
        last['date'] = k['date']
    fractals=[]
    # Exclude the rightmost (potentially unsettled) merged bar and require an
    # actual bar to the right of any fractal centre.
    for i in range(1, len(merged) - 1):
        a,b,c=merged[i-1], merged[i], merged[i+1]
        top = b['high']>a['high'] and b['high']>c['high'] and b['low']>a['low'] and b['low']>c['low']
        bottom = b['high']<a['high'] and b['high']<c['high'] and b['low']<a['low'] and b['low']<c['low']
        if top or bottom:
            fractals.append({'kind': '顶分型' if top else '底分型', 'index': b['index'], 'merged_index': i, 'date': b['date'], 'price': round(b['high'] if top else b['low'], 3)})
    # Alternating extrema with >=4 independent merged-bar centres apart.
    endpoints=[]
    for f in fractals:
        if not endpoints:
            endpoints.append(f)
            continue
        last=endpoints[-1]
        if last['kind']==f['kind']:
            stronger=(f['price']>last['price']) if f['kind']=='顶分型' else (f['price']<last['price'])
            if stronger:endpoints[-1]=f
        elif f['merged_index']-last['merged_index']>=4:
            if f['kind']=='顶分型' and f['price']>last['price']:
                endpoints.append(f)
            elif f['kind']=='底分型' and f['price']<last['price']:
                endpoints.append(f)
    strokes=[]
    for a,b in zip(endpoints, endpoints[1:]):
        strokes.append({'start_index':a['index'],'end_index':b['index'], 'start_date':a['date'], 'end_date':b['date'],
                        'start_price':a['price'], 'end_price':b['price'], 'direction':'上笔' if b['price']>a['price'] else '下笔',
                        'low':min(a['price'],b['price']),'high':max(a['price'],b['price'])})
    pivot=None
    if len(strokes)>=3:
        # Latest confirmed three-stroke overlap (simplified stroke-level zone).
        for j in range(len(strokes)-3, -1, -1):
            tail=strokes[j:j+3]
            low=max(s['low'] for s in tail)
            high=min(s['high'] for s in tail)
            if high>low:
                pivot={'lower':round(low,3),'upper':round(high,3), 'start_date':tail[0]['start_date'],
                       'end_date':tail[-1]['end_date'],'last_stroke_end':tail[-1]['end_index']}
                break
    close=raw[-1]['close']
    last_bottom=next((f for f in reversed(fractals) if f['kind']=='底分型' and f['price']<close),None)
    last_top=next((f for f in reversed(fractals) if f['kind']=='顶分型' and f['price']>close),None)
    condition='未形成足够的已确认笔结构'
    boost=0
    if pivot:
        if close>pivot['upper']:
            condition='价格在近期笔级中枢上方（仅趋势观察，非严格三买）'
            boost=12
            if strokes and strokes[-1]['direction']=='下笔' and strokes[-1]['low']>pivot['upper']:
                condition='中枢上方回踩结构候选（非严格三买，需次级别验证）'
                boost=18
        elif close<pivot['lower']:
            condition='价格在近期笔级中枢下方（弱势）'
            boost=-15
        else:
            condition='价格处于近期笔级中枢震荡区'
            boost=0
    elif last_bottom and len(strokes)>=2:
        condition='已有底分型及笔结构，尚无有效中枢确认'
        boost=3
    interpretation=describe_chan_in_plain_chinese(raw,fractals,strokes,pivot)
    return {
        'valid': True, 'interpretation':interpretation, 'asof': raw[-1]['date'], 'raw_count': len(raw), 'merged_count':len(merged),
        'fractals_count':len(fractals), 'strokes_count':len(strokes),
        'condition':condition, 'score_adjustment':boost,
        'recent_fractals':fractals[-9:], 'strokes':strokes[-15:], 'pivot':pivot,
        'chart_bars':[{'index':i,'date':k['date'],'close':round(k['close'],3)} for i,k in enumerate(raw) if i>=max(0,len(raw)-80)],
        'support':last_bottom['price'] if last_bottom else None,
        'resistance':last_top['price'] if last_top else None,
        'note':'仅简化日线包含K/分型/笔/笔级重叠区，不计算线段、背驰或严格一二三买；最后结构会随新K线改变。',
    }
