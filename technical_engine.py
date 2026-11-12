"""Completed daily OHLCV indicators and conservative Chan/indicator cross-check.

Research only. MACD bars use 2*(DIF-DEA) (Chinese quote convention).
Vol ratios compare completed daily volume with PREVIOUS 5 or 20 complete days.
No intra-session volume extrapolation, no chart-based automatic orders.
"""
from __future__ import annotations
import math
import pandas as pd

MIN_HISTORY = 60


def _num(v, digits=3):
    try:
        x=float(v)
        return round(x,digits) if math.isfinite(x) else None
    except (ValueError,TypeError):
        return None


def _as_num(v):
    x=_num(v,6)
    return x if x is not None else 0.0


def analyze_technical(df:pd.DataFrame)->dict:
    """Return factual indicators derived only from rows in the supplied date interval."""
    keys=('date','open','high','low','close','volume')
    if not set(keys).issubset(df.columns):
        return {'valid':False,'reason':'历史数据缺少真实日期、开高低收或成交量字段，技术指标无法核验'}
    try:
        x=df[list(keys)].copy().sort_values('date')
        x['date']=pd.to_datetime(x['date'],errors='coerce')
        for k in keys[1:]:x[k]=pd.to_numeric(x[k],errors='coerce')
        if x.isna().any().any() or x.date.duplicated().any() or not x[list(keys[1:])].apply(lambda col:col.map(math.isfinite)).all().all():
            return {'valid':False,'reason':'OHLCV存在空值或重复交易日，暂停技术判断'}
        if (x[['open','high','low','close']]<=0).any().any() or (x.volume<0).any() or (x.high<x.low).any() or (x.high<x[['open','close']].max(axis=1)).any() or (x.low>x[['open','close']].min(axis=1)).any():
            return {'valid':False,'reason':'历史K线价格或成交量异常，暂停技术判断'}
        n=len(x)
        if n<MIN_HISTORY:
            return {'valid':False,'reason':f'完整OHLCV仅{n}根，综合指标至少需要{MIN_HISTORY}根；不补假数据', 'bar_count':n}
        x=x.reset_index(drop=True)
        close=x['close']; vol=x['volume']; high=x['high']; low=x['low']
        # Typical China-market EMA convention, finite-warmup from earliest fetched bar.
        ema12=close.ewm(span=12,adjust=False).mean()
        ema26=close.ewm(span=26,adjust=False).mean()
        dif=ema12-ema26
        dea=dif.ewm(span=9,adjust=False).mean()
        hist=2*(dif-dea)
        ma={str(p):_num(close.tail(p).mean()) for p in (5,10,20,60)}
        # previous N complete sessions; current-day volume not part of its own baseline
        vol5=float(vol.iloc[-6:-1].mean());vol20=float(vol.iloc[-21:-1].mean())
        last_vol=float(vol.iloc[-1]); vr5=last_vol/vol5 if vol5>0 else None
        vr20=last_vol/vol20 if vol20>0 else None
        delta=close.diff().fillna(0)
        up=delta.clip(lower=0); down=(-delta).clip(lower=0)
        gain=up.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        loss=down.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
        if gain.iloc[-1]+loss.iloc[-1] <= 0:rsi=50.0
        elif loss.iloc[-1]==0:rsi=100.0
        else:rsi=100-100/(1+gain.iloc[-1]/loss.iloc[-1])
        prev_close=close.shift(1)
        tr=pd.concat([(high-low),(high-prev_close).abs(),(low-prev_close).abs()],axis=1).max(axis=1)
        atr14=float(tr.tail(14).mean())
        chg_pct=100*(close.iloc[-1]/close.iloc[-2]-1)
        hnow=float(hist.iloc[-1]);hprev=float(hist.iloc[-2]);dnow=float(dif.iloc[-1]);enow=float(dea.iloc[-1]);
        golden=hprev<=0 and hnow>0
        death=hprev>=0 and hnow<0
        macd_state='金叉（本根已完成日线）' if golden else '死叉（本根已完成日线）' if death else '多头柱（DIF在DEA上方）' if hnow>0 else '空头柱（DIF在DEA下方）' if hnow<0 else '零轴附近'
        trend_up=close.iloc[-1]>ma['20'] and ma['20']>ma['60'] and ma['5']>=ma['20']
        trend_down=close.iloc[-1]<ma['20'] and ma['20']<ma['60'] and ma['5']<=ma['20']
        trend_state='均线偏强' if trend_up else '均线偏弱' if trend_down else '均线交织/过渡'
        if vr5 is None: vol_state='无有效前5日均量，无法计算量比'
        elif vr5>=1.2 and chg_pct>0.15:vol_state='放量上涨（量能支持，但不是资金净流入）'
        elif vr5>=1.2 and chg_pct<-0.15:vol_state='放量下跌（卖压风险，仍需复核）'
        elif vr5<0.8:vol_state='缩量（方向需看价格结构）'
        else:vol_state='量能接近常态'
        if rsi>=70:rsi_state='偏热，防追高/回撤'
        elif rsi<=30:rsi_state='偏弱，反弹不等于见底'
        else:rsi_state='中性'
        reasons=[]
        scores={}
        scores['均线']=1 if trend_up else -1 if trend_down else 0
        reasons.append(f'均线：{trend_state}；收盘 {close.iloc[-1]:.3f}，MA20 {ma["20"]:.3f}，MA60 {ma["60"]:.3f}')
        scores['MACD']=1 if (dnow>enow and hnow>hprev and dnow>0) else -1 if (dnow<enow and hnow<hprev and dnow<0) else 0
        reasons.append(f'MACD：{macd_state}；DIF {dnow:.3f} / DEA {enow:.3f} / 柱 {hnow:.3f}（两倍口径）')
        scores['量价']=1 if (vr5 is not None and vr5>=1.2 and chg_pct>0.15) else -1 if (vr5 is not None and vr5>=1.2 and chg_pct<-.15) else 0
        reasons.append(f'量价：{vol_state}；成交量为过去5日均量的 {vr5:.2f}倍' if vr5 is not None else f'量价：{vol_state}')
        scores['RSI']= -1 if rsi>=75 else -1 if rsi<30 else 1 if 45<=rsi<=65 and chg_pct>0 else 0
        reasons.append(f'RSI14：{rsi:.1f}（{rsi_state}；仅辅助）')
        total=sum(scores.values());up_votes=sum(v>0 for v in scores.values());down_votes=sum(v<0 for v in scores.values())
        bias='技术指标偏强' if total>=2 and up_votes>=2 else '技术指标偏弱' if total<=-2 and down_votes>=2 else '指标方向不一致'
        rows=[]
        start=max(0,len(x)-65)
        for i in range(start,len(x)):
            rows.append({'date':x.date.iloc[i].strftime('%Y-%m-%d'),
               'close':_num(close.iloc[i]),'volume':_num(vol.iloc[i],0),
               'volume_ma5':_num(vol.iloc[max(0,i-4):i+1].mean(),0) if i>=4 else None,
               'dif':_num(dif.iloc[i],4),'dea':_num(dea.iloc[i],4),'macd_bar':_num(hist.iloc[i],4)})
        return {'valid':True,'asof':x.date.iloc[-1].strftime('%Y-%m-%d'),'bar_count':n,
          'close':_num(close.iloc[-1]),'change_pct':_num(chg_pct,2),
          'ma':ma, 'trend':trend_state,
          'macd':{'dif':_num(dnow,4),'dea':_num(enow,4),'bar':_num(hnow,4),'prev_bar':_num(hprev,4),
                  'status':macd_state,'golden_cross':bool(golden),'death_cross':bool(death),'convention':'MACD柱=2×(DIF−DEA)，EMA12/26，DEA9'},
          'volume':{'latest':_num(last_vol,0),'previous_5_avg':_num(vol5,0),'previous_20_avg':_num(vol20,0),
                    'ratio5':_num(vr5,2),'ratio20':_num(vr20,2),'status':vol_state,
                    'unit':'来源成交量原单位；不同股票/ETF/转债不直接比较绝对量'},
          'rsi14':_num(rsi,1),'rsi_status':rsi_state,
          'atr14':_num(atr14,3),'atr_pct':_num(atr14/float(close.iloc[-1])*100,2),
          'scores':scores,'bias':bias,'reasons':reasons,'chart':rows,
          'note':'仅使用本次请求的完整历史日K。MACD对复权与初始样本敏感；量比为当日成交量/此前5个完整交易日均量，不是盘口量比；RSI是Wilder式平滑近似；ATR14为最近14日真实波幅简单平均。'}
    except (ValueError,TypeError,OverflowError,KeyError,IndexError) as exc:
        return {'valid':False,'reason':f'技术指标计算失败：{type(exc).__name__}，暂停判断'}


def combine_chan_technical(chan:dict,technical:dict)->dict:
    """Conservative joint *research* interpretation, not a trading classifier."""
    if not technical.get('valid') or not chan.get('valid'):
        why='缠论结构未达确认要求' if not chan.get('valid') else '技术指标数据不足'
        return {'action':'继续观察','alignment':'证据不足','reasons':[why],
                'summary':'至少一个分析模块证据不足；不输出买卖倾向。', 'risk_note':'不依据未确认结构加仓。'}
    c=chan.get('interpretation') or {}
    if not c.get('basis_date') or c['basis_date']!=technical.get('asof'):
        return {'action':'继续观察','alignment':'分析日期不一致','reasons':['缠论与技术指标必须使用同一已完成交易日'],
                'summary':'日期缺失或不一致，暂停买卖倾向。','risk_note':'等待同日完整K线。'}
    ca=c.get('action','继续观察')
    bias=technical['bias']
    reasons=[f'缠论：{ca}；{c.get("phase","阶段待核验")}',
             f'量化指标：{bias}；{technical["macd"]["status"]}',technical['volume']['status']]
    bear=ca=='偏卖出/减仓观察'
    bull=ca=='偏买入观察'
    strong=bias=='技术指标偏强'
    weak=bias=='技术指标偏弱'
    overhot=(technical.get('rsi14') or 0)>=75
    if bear and weak:
        action,alignment='偏减仓/卖出观察','偏弱证据一致'
        summary='缠论走弱，均线/MACD/量价等技术指标也偏弱；优先复核持仓风险，而不是补仓。'
    elif bull and strong and not overhot:
        action,alignment='买入条件观察','偏强证据一致'
        summary='缠论买入结构候选与技术指标偏强相互支持；仍须等确认、检查仓位和风险预算，不能直接视为买点。'
    elif (bull and weak) or (bear and strong):
        action,alignment='继续观察','信号明显冲突'
        summary='缠论与技术指标出现相反倾向，暂不把单个金叉、分型或中枢形态当作交易依据。'
    elif overhot and bull:
        action,alignment='继续观察','技术偏热'
        summary='缠论偏强但RSI14过热，警惕追高；等待结构和量能进一步确认。'
    elif bear:
        action,alignment='偏减仓/卖出观察','结构警示，其他指标未确认'
        summary='缠论风险仍需关注，但其他指标未完全确认。结合价格失效位与持仓比例复核。'
    else:
        action,alignment='继续观察','确认条件不足'
        summary='分型、中枢、MACD、均线和成交量尚未一致，当前以观察为主。'
    return {'action':action,'alignment':alignment,'summary':summary,'reasons':reasons,
            'risk_note':'严格缠论一/二/三类买卖点尚未验证次级别线段与背驰。所有结论只是已完成日K的条件性观察，不能替代券商报价或止损执行。'}
