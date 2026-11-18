"""Daily research shortlist — not buy recommendations or orders."""
from __future__ import annotations
import math
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def select_research(verified_rows, cfg, *, expected_day):
    """Rank completed stock bars for observation, independently of V1 entry eligibility."""
    if not expected_day:
        return []
    exclude = {str(code).zfill(6) for code in cfg.get('daily_excludes', [])}
    observed = set()
    rows = []
    for source in verified_rows:
        code = str(source.get('股票代码', '')).zfill(6)
        name = str(source.get('股票名称', '')).strip()
        if (str(source.get('日期')) != expected_day or code in exclude or code in observed
                or not re.fullmatch(r'(?:00|60)\d{4}', code) or not name
                or re.search(r'ST|退', name, re.IGNORECASE)):
            continue
        try:
            close, change = float(source['收盘']), float(source['涨跌幅%'])
            if not math.isfinite(close) or close <= 0 or not -4 <= change <= 8:
                continue
            setup = json.loads(source['V1形态证据'])
            checks = {row['name'] for row in setup['checks'] if row['passed'] is True}
            evidence = setup.get('evidence', {})
            # Old completed scans carry equivalent price/volume evidence in V1.
            research = source.get('研究证据')
            if isinstance(research, str):
                evidence = json.loads(research)
            amount = float(evidence['amount20'])
            volume = float(evidence['signal_volume_ratio5'])
            if not math.isfinite(amount) or amount < 100_000_000 or not math.isfinite(volume) or volume <= 0:
                continue
            trend = evidence.get('research_uptrend', '趋势' in checks)
            breakout = evidence.get('research_breakout', '信号日重新转强' in checks)
            if not (trend or breakout):
                continue
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        reasons = []
        if breakout:
            reasons.append('放量越过前一日高点，收在5日均线上方')
        if trend:
            reasons.append('20日均线向上，且高于60日均线')
        if '固定3日回调' in checks:
            reasons.append('近三日回调守住20日均线')
        if '回调缩量' in checks:
            reasons.append('回调期间成交量收缩')
        if not breakout:
            reasons.append('等待新的放量突破')
        score = (4 * breakout + 3 * trend + 2 * ('固定3日回调' in checks)
                 + ('回调缩量' in checks) + ('固定价格区间' in checks) - (abs(change) > 5))
        observed.add(code)
        rows.append({'code': code, 'name': name, 'asof': expected_day,
                     'close': round(close, 3), 'pct': round(change, 2),
                     'volume_ratio': round(volume, 2), 'amount20': amount,
                     'total_score': score, 'reasons': reasons,
                     'research_only': True, 'can_buy_now': False})
    rows.sort(key=lambda row: (-row['total_score'], -row['amount20'], row['code']))
    shortlist = rows[:5]
    for rank, row in enumerate(shortlist, 1):
        row['rank'] = rank
    return shortlist


def _select_v1(verified,cfg,mode,generated_at):
    """Display server-derived setup evidence, never create an executable plan."""
    exclude={str(x).zfill(6) for x in cfg.get('daily_excludes',[])}
    rows=[];rejections=[]
    for _,r in verified.iterrows():
        code=str(r.get('股票代码','')).zfill(6)
        if code in exclude:continue
        try:
            setup=r.get('V1形态证据')
            if isinstance(setup,str):setup=json.loads(setup)
            if not isinstance(setup,dict):continue
            if mode!='live' or not code.startswith(('00','60')) or setup.get('eligible') is not True or setup.get('blockers'):
                rejections.extend(setup.get('blockers') or [])
                continue
            levels=setup.get('levels',{});entry=float(levels['entry_price']);stop=float(levels['stop_loss'])
            trigger=float(levels['trigger_price']);tp1=float(levels['take_profit_1']);tp2=float(levels['take_profit_2'])
            if not all(math.isfinite(v) and v>0 for v in (entry,stop,trigger,tp1,tp2)) or not stop<trigger<=entry<tp1<tp2:continue
            close=float(r['收盘']);pct=float(r['涨跌幅%']);evidence=setup.get('evidence',{})
            if not all(math.isfinite(v) for v in (close,pct)) or close<=0:continue
            checks=sum(x.get('passed') is True for x in setup.get('checks',[]))
            rows.append({'code':code,'name':str(r['股票名称']),'asof':setup['signal_date'],
                         'close':round(close,3),'pct':round(pct,2),'total_score':checks,
                         'baseline_score':None,'chan_score':0,'chan_condition':'V1仅回调后转强；缠论不另开买点',
                         'volume_ratio':evidence.get('signal_volume_ratio5'),
                         'reasons':['V1固定3日缩量回调与信号日转强检查通过','须生成服务器计划并完成人工核验后才可复核开仓'],
                         'strategy_version':'1.0','setup':setup,'setup_evidence':evidence,
                         'trigger_price':trigger,'entry_price':entry,'stop_loss':stop,
                         'take_profit_1':tp1,'take_profit_2':tp2,'can_buy_now':False,
                         'research_only':True,'amount20':evidence.get('amount20',0)})
        except (KeyError,ValueError,TypeError):continue
    latest=max((row['asof'] for row in rows),default=None)
    rows=[row for row in rows if row['asof']==latest]
    rows.sort(key=lambda row:(-float(row.get('amount20') or 0),row['code']))
    top=rows[:5]
    for index,row in enumerate(top):row['rank']=index+1
    return {'mode':mode,'strategy_version':'1.0','generated_at':generated_at,'asof':latest,
            'selection_scope':'沪深主板；V1唯一回调后转强形态与沪深300完整日线闸门',
            'count':len(top),'shortlist':top,'blockers':list(dict.fromkeys(rejections))[:20],
            'status':'V1技术研究候选；仍须服务器prepare与人工审核' if top else '没有通过完整V1形态的候选；不补足名单',
            'disclaimer':'仅展示已完成日K的V1形态与固定候选价；不是已保存交易计划，不产生实时买入额度。财务、公告、行业、压力位及全账户预算仍须复核。'}


def select_five(verified, cfg, mode, generated_at=None):
    """Use only *verified* historic daily candidates; return at most five.

    Do not fill gaps with unverified snapshot-only names. The rank represents
    a research queue, never a claim of positive future returns.
    """
    generated_at=generated_at or datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    if cfg.get('strategy_v1',{}).get('enabled') is True:
        return _select_v1(verified,cfg,mode,generated_at)
    rows=[]
    exclude={str(x).zfill(6) for x in cfg.get('daily_excludes',[])}
    for _,r in verified.iterrows():
        try:
            code=str(r['股票代码']).zfill(6)
            if code in exclude:continue
            price=float(r['收盘']); change=float(r['涨跌幅%']); ma5=float(r['MA5']); ma20=float(r['MA20'])
            vol=float(r['量比5日']); score=float(r['观察评分']); chan=int(r.get('缠论调整分',0))
            if not all(map(math.isfinite,(price,change,ma5,ma20,vol,score))):continue
            if price<=0 or score<45 or price<ma5 or ma5<=ma20 or not (-4<=change<=8) or vol<0.75:
                continue
            # Penalize chasing a highly extended close, not predictive alpha.
            extension=(price/ma5-1)*100
            if extension>13:continue
            total=score*.75+chan+max(-10,min(10,(vol-1)*5))-max(0,extension-5)*2
            levels={'stop_loss':r.get('止损参考'),'take_profit_1':r.get('第一止盈'),'take_profit_2':r.get('第二止盈')}
            def f(x):
                try:
                    z=float(x)
                    return round(z,3) if math.isfinite(z) and z>0 else None
                except (ValueError,TypeError):return None
            levels={k:f(v) for k,v in levels.items()}
            if any(x is None for x in levels.values()) or not (levels['stop_loss']<price<levels['take_profit_1']<levels['take_profit_2']):
                continue
            reason=[]
            if r.get('20日突破')=='是':reason.append('日线收盘突破此前20日高点')
            if vol>=1.3:reason.append('成交量高于前5日均量')
            reason.append('MA5位于MA20之上')
            cond=str(r.get('缠论结构','结构未确认'))
            if chan>0:reason.append('缠论结构辅助加分（简化口径）')
            rows.append({'code':code,'name':str(r['股票名称']),'asof':str(r['日期']),
                         'close':round(price,3),'pct':round(change,2),'ma5':round(ma5,3),'ma20':round(ma20,3),
                         'volume_ratio':round(vol,2),'baseline_score':round(score,2),'chan_score':chan,
                         'chan_condition':cond,'total_score':round(total,1),'reasons':reason,
                         'stop_loss':levels['stop_loss'],'take_profit_1':levels['take_profit_1'],
                         'take_profit_2':levels['take_profit_2'],
                         'structural_support':f(r.get('缠论支撑')), 'structural_resistance':f(r.get('缠论压力'))})
        except (KeyError,ValueError,TypeError):continue
    # Do not mix incompatible trading-day dates in the same list.
    if rows:
        latest=max(x['asof'] for x in rows)
        rows=[x for x in rows if x['asof']==latest]
    else:latest=None
    rows.sort(key=lambda x:(x['total_score'],x['baseline_score']),reverse=True)
    top=rows[:5]
    for i,item in enumerate(top):item['rank']=i+1
    return {'mode':mode,'generated_at':generated_at,'asof':latest,'selection_scope':'沪深主板及创业板；快照初筛+完成日线复核',
            'count':len(top),'shortlist':top,'disclaimer':'盘后技术研究候选，不是最佳买入保证；缠论为简化结构辅助，未计算完整三类买点；须核对行情及全账户风险预算。'}
