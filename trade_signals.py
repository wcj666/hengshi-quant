"""Conditional research signals. Never submit orders or guarantee outcomes.

Only the regular A-share trading session can generate live trade-review signals.
Indicative pre-open prices, stale snapshots and demo prices cannot drive alerts.
"""
from __future__ import annotations
import hashlib
import math
from risk_extensions import tier_alerts
from datetime import datetime, timezone
from quote_health import parse_provider_time
from risk_engine import risk_prefs


def _num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) and x>0 else None
    except (TypeError,ValueError):
        return None


def build_signals(cfg, quotes, risk, *, trading_day, market_session, calendar_ok, demo=False,
                  snapshot=None, now=None):
    if (cfg.get('strategy_v1') or {}).get('enabled') is True:
        return _build_v1_signals(cfg, quotes, risk, trading_day=trading_day,
                                 market_session=market_session, calendar_ok=calendar_ok,
                                 demo=demo, snapshot=snapshot, now=now)
    return _build_legacy_signals(cfg, quotes, risk, trading_day=trading_day,
                                market_session=market_session, calendar_ok=calendar_ok, demo=demo)


def _build_v1_signals(cfg, quotes, risk, *, trading_day, market_session, calendar_ok,
                      demo=False, snapshot=None, now=None):
    from strategy_setup import evaluate_entry, exit_review, _now
    clock = _now(now)
    if demo or not (market_session and calendar_ok) or str(trading_day)[:10] != clock.date().isoformat():
        return []
    rows = []
    held = {str(p['code']).zfill(6): p for p in cfg.get('positions', []) if int(p.get('quantity', 0)) > 0}
    def emit(category, code, trigger, level, price, title, reason, shares=None, priority='normal'):
        level = _num(level) or 0.0
        ident = hashlib.sha256(f'{trading_day}|{category}|{code}|{trigger}|{level:.4f}'.encode()).hexdigest()[:20]
        rows.append({'id': ident, 'category': category, 'code': code,
                     'name': quotes.get(code, {}).get('name', held.get(code, {}).get('name', '全账户')),
                     'title': title, 'reason': reason, 'trigger': trigger, 'price': price,
                     'level': level, 'priority': priority, 'shares': shares,
                     'held': code in held, 'day': str(trading_day),
                     'data_note': 'V1规则复核；可卖数量、T+1和成交均须在券商核验；提醒不是委托。'})
    for code, position in held.items():
        quote = quotes.get(code, {})
        source = parse_provider_time(quote.get('source_timestamp'))
        price = _num(quote.get('price'))
        if not price or not source or not 0 <= (clock.astimezone(timezone.utc)-source).total_seconds() <= 90:
            continue
        plan = cfg.get('trade_plans', {}).get(code) or {}
        # Monitor injects daily lifecycle results. Never fetch a calendar or invent
        # elapsed sessions inside an alert function.
        review = quote.get('strategy_exit')
        if not isinstance(review, dict):
            review = exit_review(cfg, position, plan, price, now=clock)
        if review.get('action') not in ('exit', 'reduce','review'):
            continue
        trigger = review.get('trigger')
        if trigger == 'stop' and not _num(plan.get('stop_loss')):
            continue
        level = (review.get('level') if review.get('action')=='review' else review.get('stop_loss') if trigger == 'stop' else
                 plan.get('take_profit_1') if trigger == 'take_profit_1' else
                 plan.get('take_profit_2') if trigger == 'take_profit_2' else price)
        reason = review.get('reason', '')
        if review.get('blockers'):
            reason += ' ' + '；'.join(review['blockers'])
        emit('reduce', code, trigger or 'exit_review', level, price, '量价结构观察' if review.get('action')=='review' else 'V1退出复核', reason,
             shares=review.get('quantity'), priority='high' if trigger in ('stop', 'time_10', 'time_5') else 'normal')
    for event in tier_alerts(risk):
        pressure = event['kind'] == '持仓压力损失超剩余预算'
        emit('reduce', 'PORT', 'portfolio_stress' if pressure else f'account_budget_{event["tier"]}',
             risk.get('budget', event['level']) if pressure else event['level'], event['price'], event['kind'],
             '账户预算触发复核，暂停新增；资金、持仓固定退出计划与调流后高水位须重新核验。',
             priority='high' if event['tier'] >= 80 else 'normal')
    for candidate in cfg.get('watchlist', []):
        code = str(candidate.get('code')).zfill(6)
        if code in held:
            continue
        quote = quotes.get(code, {})
        entry = evaluate_entry(cfg, code, quote, snapshot, now=clock)
        if entry['allowed']:
            plan = cfg['trade_plans'][code]
            emit('add', code, 'v1_entry', plan['trigger_price'], quote['price'], 'V1新增买入复核',
                 f'仅信号日下一交易日有效，固定触发区间与最新账户约束通过；最多复核{entry["max_buy_shares"]}股。成交前在券商核对报价、公告和股数。',
                 shares=entry['max_buy_shares'])
    return rows


def _build_legacy_signals(cfg, quotes, risk, *, trading_day, market_session, calendar_ok, demo=False):
    """Return active *review prompts*, not trade recommendations.

    quotes: {code: {name, price, asset_type, held, stop_loss, ...}}
    risk: result of risk_engine.compute with sizing from watch quotes.
    No claim of exchange-level freshness can be made about AKShare public snapshots.
    """
    if demo or not (market_session and calendar_ok):
        return []
    held={str(p['code']).zfill(6):p for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0}
    watch={str(p['code']).zfill(6) for p in cfg.get('watchlist',[])}
    alert_levels={str(x['code']).zfill(6):x for x in cfg.get('alert_levels',[])}
    sizing={str(x['code']).zfill(6):x for x in risk.get('sizing',[])}
    rows=[]

    def emit(category, code, trigger, level, price, title, detail, priority='normal', shares=None, is_held=False):
        # Fixed identity means a persistent threshold condition only creates one popup per day.
        key=f'{trading_day}|{category}|{code}|{trigger}|{level:.4f}'
        ident=hashlib.sha256(key.encode()).hexdigest()[:20]
        rows.append({'id':ident, 'category':category,'code':code,
            'name':quotes.get(code,{}).get('name',held.get(code,{}).get('name','全账户')),
            'title':title,'reason':detail,'trigger':trigger,
            'price':round(price,4) if price is not None else None,
            'level':round(level,4),'priority':priority,'shares':shares,
            'held':is_held,'day':str(trading_day),
            'data_note':'第三方公开行情的源时间戳未经交易所级校验，务必在券商核对；信号不是委托或买卖指令。'})

    # All positions can produce reduction-review prompts, but only with a valid quote.
    # Avoid claiming that a profit target or a breakdown *requires* a transaction.
    for code,p in held.items():
        r=quotes.get(code,{})
        price=_num(r.get('price'))
        if price is None: continue
        stop=_num(r.get('stop_loss'))
        tp1=_num(r.get('take_profit_1'))
        tp2=_num(r.get('take_profit_2'))
        lower=_num(alert_levels.get(code,{}).get('below'))
        # Prioritize stop > breakdown > profit-taking to avoid conflicting prompts.
        if stop is not None and price<=stop:
            emit('reduce',code,'stop',stop,price,
                 '减仓风险复核：触及止损参考线',
                 f'持仓{p["quantity"]}单位，参考价{price:g}已触及止损{stop:g}；检查实盘报价、流动性和全账户亏损预算。',
                 priority='high',is_held=True)
        elif lower is not None and price<=lower:
            emit('reduce',code,'breakdown',lower,price,
                 '减仓风险复核：跌破观察位',
                 f'参考价{price:g}低于观察位{lower:g}；需确认是否持续破位及是否放量。',
                 priority='high',is_held=True)
        elif tp2 is not None and price>=tp2:
            emit('reduce',code,'take_profit_2',tp2,price,
                 '止盈复核：触及第二止盈参考',
                 f'参考价{price:g}达到第二止盈{tp2:g}；可研究分批兑现或上移保护线，不代表必须卖出。',is_held=True)
        elif tp1 is not None and price>=tp1:
            emit('reduce',code,'take_profit_1',tp1,price,
                 '止盈复核：触及第一止盈参考',
                 f'参考价{price:g}达到第一止盈{tp1:g}；考虑止盈与趋势延续的取舍。',is_held=True)

    # Distinct tier identities preserve 50 -> 80 -> 100 escalation in daily dedup.
    for event in tier_alerts(risk):
        pressure=event['kind']=='持仓压力损失超剩余预算'
        trigger='portfolio_stress' if pressure else f'account_budget_{event["tier"]}'
        # Stress is a changing number, not a changing alert identity.
        threshold=float(risk['budget']) if pressure else event['level']
        emit('reduce','PORT',trigger,threshold,event['price'],event['kind'],
             (f'计划止损压力{event["price"]:.2f}元，剩余预算{risk["remaining"]:.2f}元；暂停新增并复核整体仓位。'
              if pressure else f'已用回撤预算{event["tier"]}%：{event["price"]:.2f} / {risk["budget"]:.2f}元；请核对账户。'),
             priority='high' if event['tier']>=80 else 'normal')

    # Add prompts require *positive computed capacity* and a configured breakout.
    # No add signals when quotes are missing, held stops are breached or budget is used up.
    candidates=[]
    switch_candidates=[]
    settings=risk_prefs(cfg)
    for code in sorted(watch):
        r=quotes.get(code,{})
        price=_num(r.get('price'))
        hi=_num(alert_levels.get(code,{}).get('above'))
        size=sizing.get(code,{})
        qty=size.get('max_buy_shares')
        if price is None or hi is None or price<hi or not risk.get('valid'):
            continue
        if code in held and _num(r.get('stop_loss')) is not None and price<=r['stop_loss']:
            continue
        # Dynamic percentage lines move with every quote; only fixed plans can
        # qualify as entry candidates. Sizing alone is not entry confirmation.
        source_time=parse_provider_time(r.get('source_timestamp'))
        if not source_time or not 0<=(datetime.now(timezone.utc)-source_time).total_seconds()<=90:
            continue
        if not cfg.get('trade_plans',{}).get(code) or not (size.get('levels') or {}).get('valid_order'):
            continue
        if price>hi*(1+settings['max_breakout_extension_pct']/100):
            continue
        if price>=size['levels']['take_profit_1'] or (size.get('reward_risk') or 0)<settings['min_reward_risk']:
            continue
        if code not in held:
            switch_candidates.append((code,hi,price))
        if risk.get('over_budget') or qty is None or qty<100:
            continue
        emit('add',code,'breakout',hi,price,
             '加仓观察：突破价位且预算测算允许',
             f'参考价{price:g}达到观察位{hi:g}，距第一止盈的收益/止损风险比{size["reward_risk"]:.2f}；按现金、单笔与全账户风险约束，理论最多可研究{int(qty)}股。还需确认真实成交量、行情与现有持仓。',
             shares=int(qty),is_held=code in held)
        candidates.append((code,hi,price))

    # Switch is only a *comparison*, never a coupled sell-buy order. It cannot
    # presume that proceeds of an unconfirmed sale are already available.
    reduces=[x for x in rows if x['category']=='reduce' and x['code'] in held]
    if reduces and switch_candidates and risk.get('valid') and not risk.get('stop_breaches'):
        out=sorted(reduces,key=lambda x:(0 if x['priority']=='high' else 1,x['code']))[0]
        into=next(((c,hi,px) for c,hi,px in switch_candidates if c!=out['code']),None)
        if into:
            c,hi,px=into
            emit('switch',c,f'compare_{out["code"]}',hi,px,
                 f'换仓比较：{out["name"]} → {quotes[c]["name"]}',
                 f'{out["name"]}出现减仓复核条件，而{quotes[c]["name"]}触及突破观察位。仅供对照，当前并不代表有加仓额度；必须先在券商确认卖出成交并更新现金/仓位后重新计算，不应机械一卖一买。')
    return rows
