"""Explainable 5–10 session research decisions. No fitted win-rate or orders."""
from __future__ import annotations
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo
import hashlib
import json
import math
from quote_health import parse_provider_time
from risk_engine import compute, risk_prefs
from trade_levels import make_levels


def number(value):
    try:
        x=float(value)
        return x if math.isfinite(x) and x>0 else None
    except (TypeError,ValueError):return None


def analyze_swing(hist, technical, chan, cfg, code, *, position=None, quote=None,
                  snapshot=None, now=None, mode='live', benchmark_hist=None,
                  trading_dates=None):
    if (cfg.get('strategy_v1') or {}).get('enabled') is True:
        return _analyze_swing_v1(hist, cfg, str(code).zfill(6), position=position,
                                 quote=quote, snapshot=snapshot, now=now, mode=mode,
                                 benchmark_hist=benchmark_hist, trading_dates=trading_dates)
    return _analyze_swing_legacy(hist, technical, chan, cfg, code, position=position,
                                 quote=quote, snapshot=snapshot, now=now, mode=mode)


def _analyze_swing_v1(hist, cfg, code, *, position=None, quote=None, snapshot=None,
                      now=None, mode='live', benchmark_hist=None, trading_dates=None):
    from strategy_setup import build_setup, evaluate_entry, exit_review, _now
    now = _now(now)
    snapshot, quote = snapshot or {}, quote or {}
    held = bool(position and int(position.get('quantity', 0)) > 0)
    candidate = position or next((p for p in cfg.get('watchlist', []) if str(p.get('code')).zfill(6) == code), {})
    if benchmark_hist is None:
        benchmark_hist = snapshot.get('benchmark_history')
    setup = build_setup(hist, benchmark_hist, candidate.get('type', quote.get('asset_type', 'stock')),
                        candidate.get('name', quote.get('name', '')), now=now)
    plan = cfg.get('trade_plans', {}).get(code) or {}
    source = parse_provider_time(quote.get('source_timestamp'))
    fresh = bool(number(quote.get('price')) and source and
                 0 <= (now.astimezone(timezone.utc)-source).total_seconds() <= 90 and
                 source.astimezone(ZoneInfo('Asia/Shanghai')).date() == now.date())
    levels = plan if held else plan or setup.get('levels', {})
    result = {'action': 'hold' if held else 'wait', 'label': '持仓观望' if held else '暂不买入',
              'tone': 'neutral', 'horizon': '1～2周 / 5～10个交易日',
              'asof': setup.get('signal_date'), 'summary': '', 'reasons': [],
              'checks': setup.get('checks', []), 'blockers': list(setup.get('blockers', [])),
              'levels': {'reference': (setup.get('evidence') or {}).get('signal_close'),
                         'current': quote.get('price') if fresh else None,
                         'stop': levels.get('stop_loss'), 'target1': levels.get('take_profit_1'),
                         'target2': levels.get('take_profit_2'), 'trigger': levels.get('trigger_price'),
                         'entry_cap': levels.get('entry_price'), 'source': 'V1固定结构计划' if plan else 'V1完整日K研究草案',
                         'quote_asof': source.isoformat() if fresh else None},
              'strategy_setup': setup, 'max_buy_shares': None, 'reward_risk': None,
              'note': 'V1只做固定3日回调后转强；MACD、缠论仅辅助。人工核对券商，软件不下单。'}
    if mode != 'live':
        result['summary'] = '演示数据仅检查界面，不提供实盘操作结论。'
        return result
    if held:
        review = exit_review(cfg, position, plan, quote.get('price') if fresh else None,
                             history=hist,
                             trading_dates=trading_dates if trading_dates is not None else snapshot.get('trading_dates', snapshot.get('calendar_dates')),
                             now=now)
        result['strategy_exit'] = review
        result['blockers_title']='持仓退出管理待核验'
        if cfg.get('strategy_v1',{}).get('exit_mode')=='price_first':
            result['horizon']='价格与趋势优先 · 1～2周仅作参考'
            result['checks']=[]
            if not plan:result['levels']['source']='固定风控退出计划待补；结构观察位不替代固定止损'
            result['note']+=' '+review.get('holding_period_note','')
        result['blockers'] = review['pending'] + review['blockers']
        if not fresh:
            result['blockers'].append('盘中价格未核验，不据历史价格触发价格退出')
        if review['action'] in ('exit', 'reduce'):
            result.update(action=review['action'], label='退出复核' if review['action'] == 'exit' else '分批止盈复核', tone='bear')
            result['summary'] = review['reason']
        elif review['action']=='review':
            result.update(action='review',label='量价结构观察',summary=review['reason'])
        else:
            result['summary'] = ('按价格、趋势与量价结构复核；持有天数仅作参考，不到期强制卖出。' if cfg.get('strategy_v1',{}).get('exit_mode')=='price_first' else '按原固定退出计划和第5/10交易日条件管理；V1不在已有仓位加仓。')
        if review.get('price_review'):result['price_review']=review['price_review']
        return result
    if not setup.get('eligible'):
        result['summary'] = '完整日K尚未满足V1唯一形态；等待下一次收盘复核。'
        return result
    entry = evaluate_entry(cfg, code, quote, snapshot, setup=setup, now=now)
    result['blockers'] = entry['blockers']
    if entry['allowed']:
        result.update(action='buy', label='买入复核', tone='bull', max_buy_shares=entry['max_buy_shares'],
                      reward_risk=entry['reward_risk'], summary='完整日K形态、固定次日计划、实时账户与触发区间均通过；在券商核对后按限价和核定股数复核。')
    else:
        result['summary'] = '形态成立，执行条件尚未全部通过；按下列原因等待或取消当天计划。'
    return result


def _analyze_swing_legacy(hist, technical, chan, cfg, code, *, position=None, quote=None,
                          snapshot=None, now=None, mode='live'):
    now=now or datetime.now(ZoneInfo('Asia/Shanghai'))
    snapshot=snapshot or {};quote=quote or {}
    held=bool(position and int(position.get('quantity',0))>0)
    result={'action':'hold' if held else 'wait','label':'持仓观望' if held else '暂不买入',
            'tone':'neutral','horizon':'1～2周 / 5～10个交易日','asof':technical.get('asof'),
            'summary':'等待完整数据再判断。','reasons':[],'checks':[], 'blockers':[],
            'levels':{},'max_buy_shares':None,'reward_risk':None,
            'note':'规则观察，尚未验证胜率；不自动交易。每日收盘后复核，盘中主要检查失效价。'}
    def finish(summary):
        result['summary']=summary
        return result
    if mode!='live':return finish('演示数据只检查界面，不提供实盘操作结论。')
    if not technical.get('valid') or not chan.get('valid'):
        result['blockers']=[technical.get('reason') or chan.get('reason') or '历史数据不足']
        return finish('数据还不够，先别凭单个指标做决定。')
    interp=chan.get('interpretation') or {}
    if not interp.get('basis_date') or interp['basis_date']!=technical.get('asof'):
        return finish('缠论与技术指标日期不一致，等数据对齐再看。')
    asof=datetime.fromisoformat(technical['asof']).date()
    if not 0<=(now.date()-asof).days<=4 or (asof==now.date() and now.time()<time(15,5)):
        result['blockers']=['日线过旧、日期异常或今天尚未收盘']
        return finish('这批日线不能代表最新完整交易日，先观望。')
    x=hist.sort_values('date').reset_index(drop=True)
    close=x.close.astype(float);high=x.high.astype(float);vol=x.volume.astype(float)
    ma5=close.rolling(5).mean();ma10=close.rolling(10).mean();ma20=close.rolling(20).mean()
    ma60=close.rolling(60).mean();price=float(close.iloc[-1]);atr=number(technical.get('atr14'))
    dates=[str(d.date() if hasattr(d,'date') else d)[:10] for d in x.date]
    # Count from the date the pattern became knowable, never from its earlier trough.
    recent=[]
    for p in chan.get('points',[]):
        if p.get('confirmed_on') not in dates or p.get('date') not in dates:continue
        age=len(dates)-1-dates.index(p['confirmed_on'])
        point_age=len(dates)-1-dates.index(p['date'])
        if 0<=age<=3 and 0<=point_age<=10:
            recent.append({**p,'confirmed_bars_ago':age})
    recent.sort(key=lambda p:(p['confirmed_on'],p['date']))
    latest=recent[-1] if recent else None
    pivot=chan.get('pivot') or {}
    pivot_recent=0<=len(x)-1-pivot.get('last_stroke_end',-10000)<=20
    chan_bear=interp.get('action')=='偏卖出/减仓观察' or bool(latest and '卖候选' in latest['kind'])
    chan_bull=not chan_bear and (bool(latest and '买候选' in latest['kind']) or
                                  (pivot_recent and price>float(pivot.get('upper',math.inf))))
    trend=bool(price>ma20.iloc[-1] and ma5.iloc[-1]>=ma10.iloc[-1]>=ma20.iloc[-1]
               and ma20.iloc[-1]>ma20.iloc[-6] and price>ma60.iloc[-1])
    macd=technical['macd']
    momentum=bool(macd['dif']>macd['dea'] and macd['bar']>0 and macd['bar']>=macd['prev_bar'])
    volume=technical['volume'];ratio=volume.get('ratio5')
    volume_ok=bool(ratio is not None and ratio>=1.2 and (volume.get('ratio20') or 0)>=1
                   and vol.iloc[-1]>0 and price>close.iloc[-2])
    breakout=price>float(high.iloc[-21:-1].max())
    pullback=bool(any(close.iloc[i]<=ma10.iloc[i]*1.01 and close.iloc[i]>=ma20.iloc[i]*.98
                       and vol.iloc[i]<vol.iloc[max(0,i-5):i].mean() for i in range(len(x)-4,len(x)-1))
                  and price>high.iloc[-2] and price>ma5.iloc[-1])
    overhot=bool((technical.get('rsi14') or 0)>=70 or (atr and price-ma20.iloc[-1]>2*atr))
    checks=[('日线趋势',trend,'短中期均线向上，股价在20日、60日均线上方' if trend else '均线还没形成稳定向上排列'),
            ('MACD动能',momentum,'上涨动能仍在增强' if momentum else '上涨动能没有同步增强'),
            ('成交量',volume_ok,f'成交量是前5日均量的{ratio:g}倍' if ratio is not None else '成交量基准不足'),
            ('价格位置',breakout or pullback,'突破前20日高点' if breakout else '缩量回踩后重新走强' if pullback else '还没有突破或回踩转强的确认'),
            ('缠论结构',chan_bull,'近期结构偏强，仍为候选' if chan_bull else '结构偏弱' if chan_bear else '近期买入结构尚未确认')]
    result['checks']=[{'name':a,'passed':bool(b),'detail':c} for a,b,c in checks]
    result['reasons']=[c for _,_,c in checks]
    result['chan_confirmation']=({'kind':latest['kind'],'confirmed_on':latest['confirmed_on'],
                                  'bars_ago':latest['confirmed_bars_ago']} if latest else None)
    source=parse_provider_time(quote.get('source_timestamp'))
    current=number(quote.get('price'))
    quote_ok=bool(current and source and 0<=(now.astimezone(timezone.utc)-source).total_seconds()<=90)
    basis=current if quote_ok else price
    plan=make_levels(cfg,code,price=price,position=position if held else None,
                     asset_type=position['type'] if held else 'stock',price_label='最近完整日线收盘')
    result['levels']={'reference':price,'current':current if quote_ok else None,'stop':plan['stop_loss'] if plan else None,
                      'target1':plan['take_profit_1'] if plan else None,'target2':plan['take_profit_2'] if plan else None,
                      'support':chan.get('support'),'resistance':chan.get('resistance'),
                      'quote_asof':source.isoformat() if quote_ok else None,
                      'source':'手动锁定' if plan and plan['manual'] else '按成本或最近完整收盘价模拟，非结构止损'}
    # Risk and known failure prices take precedence over bullish pattern votes.
    if held and plan and basis<=plan['stop_loss']:
        result.update(action='exit',label='卖出 / 止损复核',tone='bear')
        return finish(f'{"最新参考价" if quote_ok else "最近日线收盘"}已在止损参考线下方。先核对券商价格和可卖数量，优先处理风险，不补仓摊低成本。')
    if held and plan and basis>=plan['take_profit_1']:
        result.update(action='reduce',label='减仓 / 止盈观察',tone='bear')
        return finish('已到止盈参考区，可以复核分批止盈；若继续持有，先确定利润回撤的保护价。')
    broken=bool(close.iloc[-1]<ma20.iloc[-1] and close.iloc[-2]<ma20.iloc[-2])
    weak_macd=bool(macd['dif']<macd['dea'] and macd['bar']<macd['prev_bar'])
    volume_sell=bool(ratio is not None and ratio>=1.2 and price<close.iloc[-2])
    if broken and weak_macd and (volume_sell or chan_bear):
        result.update(action='reduce' if held else 'avoid',label='减仓 / 卖出观察' if held else '暂不买入',tone='bear')
        return finish('连续两天收在20日均线下，MACD转弱，成交量或缠论也在提示风险。持仓先评估减仓，空仓先回避。')
    technical_buy=all(b for _,b,_ in checks) and not overhot
    if not technical_buy:
        if overhot:result['blockers'].append('涨得偏急或远离均线，暂不追高')
        if chan_bear:result['blockers'].append('缠论结构走弱，与买入条件冲突')
        return finish('已有持仓先观望，不急着加仓；等待趋势、量能和结构同步转强。' if held else '先不买。趋势、量能或结构还没同时确认，等下一次收盘再看。')
    blockers=result['blockers'];settings=risk_prefs(cfg)
    if not quote_ok:blockers.append('缺少本只证券90秒内的源报价时间，先核价')
    if not plan or not plan['manual'] or not plan['valid_order']:blockers.append('先设置并锁定止损、两级止盈方案')
    if quote_ok and current>price*(1+settings['max_breakout_extension_pct']/100):blockers.append('盘中价已明显高于日线信号价，暂不追高')
    if quote_ok and current<ma20.iloc[-1]:blockers.append('盘中已跌回20日均线下，原偏强条件失效')
    config_hash=hashlib.sha256(json.dumps(cfg,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    stamp=parse_provider_time(snapshot.get('fetch_finished_at'))
    account_ok=bool(stamp and 0<=(now.astimezone(timezone.utc)-stamp).total_seconds()<=45
                    and snapshot.get('mode')=='live' and snapshot.get('calendar_ok') and snapshot.get('market_session')
                    and snapshot.get('config_hash')==config_hash and (snapshot.get('risk') or {}).get('valid')
                    and (not any(int(p.get('quantity',0))>0 for p in cfg.get('positions',[]))
                         or snapshot.get('quote_health',{}).get('provider_timestamps_verified'))
                    and not snapshot.get('quote_health',{}).get('block_trade_signals'))
    if not account_ok:blockers.append('账户行情、交易时段或风险状态尚未通过核验')
    if account_ok and quote_ok:
        prices={r['code']:r['price'] for r in snapshot.get('quotes',[]) if r.get('held') and r.get('price') is not None}
        if held:prices[code]=current
        risk=compute(cfg,prices,fresh=True,watch_quotes=[{'code':code,'name':code,'price':current}])
        size=risk['sizing'][0]
        result['reward_risk']=size.get('reward_risk')
        if not risk['valid'] or risk['over_budget'] or not size.get('max_buy_shares'):blockers.append(size['reason'] if risk['valid'] else '持仓报价不完整')
        elif not blockers:result['max_buy_shares']=size['max_buy_shares']
    if blockers:return finish('技术条件偏强，但现在先不加仓。先处理下方未通过的条件。')
    result.update(action='add' if held else 'buy',label='加仓观察' if held else '买入观察',tone='bull')
    return finish('趋势向上、量能配合，价格与缠论结构也偏强。可进入买入复核，成交前再核对报价；跌破固定失效价则取消计划。')
