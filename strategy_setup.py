"""Version 1.0's one daily setup and recorded-position exits. No I/O or orders."""
from __future__ import annotations

from datetime import datetime, date, time, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import pandas as pd
from quote_health import parse_provider_time

CN = ZoneInfo('Asia/Shanghai')


def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n > 0 else None
    except (TypeError, ValueError):
        return None


def _now(value=None):
    value = value or datetime.now(CN)
    return value.replace(tzinfo=CN) if value.tzinfo is None else value.astimezone(CN)


def _day(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _floor(value, tick):
    return float((Decimal(str(value)) / Decimal(str(tick))).to_integral_value(rounding=ROUND_FLOOR) * Decimal(str(tick)))


def completed_history(history, now=None, *, amount=False):
    """Validate bars, excluding today's incomplete bar without filling missing data."""
    clock = _now(now)
    try:
        x = pd.DataFrame(history).copy()
        fields = ['date', 'open', 'high', 'low', 'close', 'volume'] + (['amount'] if amount else [])
        if any(k not in x for k in fields):
            return None, '缺少完整日K或可核验成交额字段'
        x['date'] = pd.to_datetime(x['date'], errors='coerce').dt.date
        if x['date'].isna().any() or x['date'].duplicated().any():
            return None, '日K日期无效或重复'
        x = x.sort_values('date')
        x = x[x.date <= clock.date()]
        if clock.time() < time(15, 5):
            x = x[x.date < clock.date()]
        if 'complete' in x:
            x = x[x['complete'].eq(True)]
        for key in fields[1:]:
            x[key] = pd.to_numeric(x[key], errors='coerce')
            if x[key].isna().any() or not x[key].map(math.isfinite).all() or (x[key] <= 0).any():
                return None, '日K价格、成交量或成交额缺失/无效'
        if ((x.high < x[['open', 'close', 'low']].max(axis=1)) |
                (x.low > x[['open', 'close', 'high']].min(axis=1))).any():
            return None, '日K高低价关系异常'
        return x.reset_index(drop=True), None
    except (TypeError, ValueError, AttributeError):
        return None, '日K格式无效'


def build_setup(hist, benchmark_hist, asset_type='stock', name='', tick_size=None, *, now=None):
    """Fixed T-3..T-1 pullback, complete signal-day bars and fixed Ecap levels.

    Amount must already be in RMB; it is never inferred from volume. Financial,
    announcement and corporate-action reviews remain explicit manual plan fields.
    """
    result = {'version': '1.0', 'valid': False, 'eligible': False, 'signal_date': None,
              'checks': [], 'blockers': [], 'levels': {}, 'evidence': {}}
    def check(name, passed, detail):
        result['checks'].append({'name': name, 'passed': bool(passed), 'detail': detail})
        if not passed:
            result['blockers'].append(detail)
    x, error = completed_history(hist, now, amount=True)
    b, berror = completed_history(benchmark_hist, now)
    if error or berror:
        result['blockers'] = [v for v in (error, berror and '沪深300：' + berror) if v]
        return result
    if len(x) < 120 or len(b) < 120:
        result['blockers'] = ['标的与沪深300均需至少120根完整日K，上市天数不足时只观察']
        return result
    result['signal_date'] = x.date.iloc[-1].isoformat()
    if x.date.iloc[-1] != b.date.iloc[-1] or list(x.date.iloc[-9:]) != list(b.date.iloc[-9:]):
        result['blockers'] = ['标的与沪深300完整交易日不一致，不能拼接不同日期的信号']
        return result
    result['valid'] = True
    tick = _number(tick_size) or (0.001 if asset_type == 'etf' else 0.01)
    check('品种范围', asset_type in ('stock', 'etf') and 'ST' not in name.upper() and '退' not in name,
          '仅普通主板A股、非杠杆股票ETF可进入V1，ST/退市风险品种只观察')
    c, h, low, v = x.close, x.high, x.low, x.volume
    ma5, ma10, ma20, ma60 = [c.rolling(n).mean() for n in (5, 10, 20, 60)]
    bc = b.close
    bm20 = bc.rolling(20).mean()
    check('沪深300市场闸门', bc.iloc[-1] > bm20.iloc[-1] and bm20.iloc[-1] > bm20.iloc[-6],
          '沪深300完整日收盘须高于MA20，MA20须高于5个交易日前')
    check('趋势', ma20.iloc[-1] > ma60.iloc[-1] and ma20.iloc[-1] > ma20.iloc[-6],
          '标的MA20须在MA60之上且高于5个交易日前')
    pull = x.iloc[-4:-1]
    touches = any(low.iloc[i] <= ma10.iloc[i] and c.iloc[i] > ma20.iloc[i] for i in range(len(x)-4, len(x)-1))
    above = all(c.iloc[i] > ma20.iloc[i] for i in range(len(x)-4, len(x)-1))
    check('固定3日回调', touches and above,
          'T-3至T-1至少一次最低价触及MA10，且三日收盘均守住MA20')
    pv3, pv5 = float(v.iloc[-4:-1].mean()), float(v.iloc[-9:-4].mean())
    check('回调缩量', pv3 < pv5, '固定3日回调均量须小于此前5日均量')
    prev5 = float(v.iloc[-6:-1].mean())
    check('信号日重新转强', c.iloc[-1] > h.iloc[-2] and c.iloc[-1] > ma5.iloc[-1] and v.iloc[-1] >= 1.2 * prev5,
          '信号收盘须突破前一日最高价、站回MA5且成交量至少为前5日均量1.2倍')
    amount20 = float(x.amount.iloc[-20:].mean())
    minimum = 10_000_000 if asset_type == 'etf' else 100_000_000
    check('20日成交额', amount20 >= minimum, f'可核验20日平均成交额须至少{minimum:,.0f}元')
    # Main-board 10% is only a conservative rejection rule. Ex-rights and special
    # listing/ETF limits require the separately recorded corporate-action review.
    def limit_hit(i):
        target = float((Decimal(str(c.iloc[i-1])) * Decimal('1.10') / Decimal(str(tick))).to_integral_value(rounding=ROUND_HALF_UP) * Decimal(str(tick)))
        return c.iloc[i] >= target - tick * 0.1
    hit = limit_hit(len(x)-1)
    consecutive = limit_hit(len(x)-2) and limit_hit(len(x)-3)
    check('不追涨停', not hit and not consecutive, '信号日涨停或连续涨停后直接追入，取消；特殊涨跌幅及除权需人工核验')
    tr = pd.concat([h-low, (h-c.shift()).abs(), (low-c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.iloc[-14:].mean())
    stop = _floor(float(pull.low.min()) - 0.2 * atr, tick)
    trigger = float(Decimal(str(h.iloc[-1])) + Decimal(str(tick)))
    ecap = _floor(float(h.iloc[-1]) + 0.25 * atr, tick)
    d = ecap - stop
    check('固定价格区间', 0 < stop < trigger <= ecap and 0 < d / ecap <= 0.06,
          '固定触发区间须非空，初始结构止损距离不得超过Ecap的6%')
    result['levels'] = {'trigger_price': trigger, 'entry_price': ecap, 'stop_loss': stop,
                        'take_profit_1': round(ecap + 2*d, 8), 'take_profit_2': round(ecap + 3*d, 8),
                        'initial_stop': stop, 'initial_risk_per_share': round(d, 8), 'tick_size': tick}
    result['evidence'] = {'signal_high': float(h.iloc[-1]), 'signal_close': float(c.iloc[-1]),
                          'atr14': atr, 'pullback_low': float(pull.low.min()),
                          'pullback_dates': [d.isoformat() for d in pull.date],
                          'pullback_volume3': pv3, 'prior_volume5': pv5,
                          'signal_volume_ratio5': float(v.iloc[-1]) / prev5,
                          'amount20': amount20, 'benchmark_date': b.date.iloc[-1].isoformat(),
                          'benchmark_close': float(bc.iloc[-1]), 'benchmark_ma20': float(bm20.iloc[-1]),
                          'completed_bars': len(x), 'price_limit_review_required': True}
    result['eligible'] = not result['blockers']
    return result


def _calendar(value, today):
    verified, through = True, None
    if isinstance(value, dict):
        verified = value.get('verified') is True
        through = _day(value.get('through'))
        value = value.get('dates', [])
    days = sorted(set(d for d in (_day(v) for v in (value or [])) if d))
    if not verified or not days or (through or days[-1]) < today:
        return None
    return days


def exit_review(cfg, position, plan, price, history=None, trading_dates=None, now=None):
    """Review recorded fills. A touched target is never treated as a fill."""
    clock = _now(now)
    plan = plan or {}
    quantity = int(position.get('quantity', 0) or 0)
    current = _number(price)
    stop = _number(plan.get('stop_loss'))
    actual = _number(plan.get('actual_entry'))
    initial_stop = _number(plan.get('initial_stop_loss')) or _number(plan.get('initial_stop')) or stop
    sold = max(0, int(plan.get('tp1_sold_quantity', 0) or 0))
    initial_qty = int(plan.get('initial_quantity', quantity) or quantity)
    effective = max(stop or 0, actual or 0) if sold > 0 else stop
    buy = _day(plan.get('buy_date'))
    price_first=cfg.get('strategy_v1',{}).get('exit_mode')=='price_first'
    days = _calendar(trading_dates, clock.date())
    age = len([d for d in days if buy <= d <= clock.date()]) if days and buy in days and buy <= clock.date() else None
    result = {'action': 'hold', 'trigger': None, 'reason': '继续按已记录的固定计划复核。',
              'quantity': 0, 'days_held': age, 'pending': [], 'blockers': [],
              'stop_loss': effective, 'raise_stop_to': actual if sold > 0 and actual and (not stop or stop < actual) else None,
              'broker_sellable_required': True, 'sellable': None,
              'note': '仅复核提醒；可卖数量、T+1、停牌、跌停及实际成交必须在券商核验。'}
    if not quantity:
        return result
    if not stop:
        result['pending'].append('固定风控止损未设置：结构观察仍可用，新增额度仍暂停' if price_first else '未定义固定止损；旧仓不套用成本减百分比止损')
    if not buy and not price_first:
        result['pending'].append('买入日期待补，不推算持有天数')
    elif buy and age is None and not price_first:
        result['pending'].append('可信交易日历不足或买入日期不在日历内，不估算第5/10日')
    if sold > 0 and not (_number(plan.get('initial_stop_loss')) or _number(plan.get('initial_stop'))):
        result['pending'].append('已分批但初始止损未留档，不能用抬升后止损改写初始r')
        initial_stop = None
    # Only confirmed broker remaining quantity can say how much is sellable.
    available = position.get('available_quantity', position.get('available'))
    if available is not None:
        try:
            result['sellable'] = max(0, min(quantity, int(available)))
        except (TypeError, ValueError):
            pass
    t1 = buy == clock.date() and position.get('t0_verified') is not True
    def action(trigger, reason, shares=quantity):
        result.update(action='exit' if shares >= quantity else 'reduce', trigger=trigger,
                      reason=reason, quantity=min(shares, quantity))
        if t1:
            result['blockers'].append('买入当日按T+1处理，次日且券商确认可卖后执行；ETF回转规则须单独核验')
            result['sellable'] = 0
        if result['sellable'] is not None and result['sellable'] < result['quantity']:
            result['blockers'].append('券商已记录可卖数量不足，不能把计划退出记为成交')
        return result
    if current and effective and current <= effective:
        return action('stop', '触及固定止损（分批后剩余止损至少抬至原成交价），优先退出复核。')
    if current and _number(plan.get('take_profit_2')) and current >= float(plan['take_profit_2']):
        return action('take_profit_2', '触及盘前固定第二目标，退出剩余仓位复核。')
    if current and _number(plan.get('take_profit_1')) and current >= float(plan['take_profit_1']):
        target_qty = initial_qty if initial_qty < 200 else (initial_qty // 200) * 100
        outstanding = max(0, target_qty - sold)
        if outstanding:
            return action('take_profit_1', '触及固定第一目标；100股全退，至少200股约半仓向下取整到100股。仅成交记录可标记已完成。', outstanding)
    if price_first:
        from price_review import review_price
        observation=review_price(history,current,now=clock,trading_dates=trading_dates)
        result['price_review']=observation
        result['holding_period_note']='持有天数仅作复盘参考，不触发强制退出。'
        if observation.get('action')=='review':
            result.update(action='review',trigger=observation['trigger'],reason=observation['reason'],quantity=None,level=observation['level'])
        elif not observation['valid']:result['pending'].append('完整日线或交易日历待核验，结构提醒暂不可用')
        if not buy:result['holding_period_note']+=' 买入日期未知，券商可卖数量和T+1需人工确认。'
        return result
    if age is not None and age >= 10:
        return action('time_10', '已到第10个交易日，收盘前结束剩余仓位；无法成交要记录并继续处理。')
    if age is not None and age >= 5:
        if not actual or not initial_stop or actual <= initial_stop:
            result['pending'].append('缺实际成交均价或原始S0，第5日1r条件待补')
        else:
            day5 = days[days.index(buy) + 4]
            frame, error = completed_history(history, clock)
            row = frame[frame.date == day5] if frame is not None else None
            if error or row is None or row.empty:
                result['pending'].append('第5日完整收盘K缺失；不以盘中价或其他日期收盘替代')
            elif float(row.close.iloc[0]) - actual < actual - initial_stop:
                if clock.date() > day5:
                    return action('time_5', '第5日完整收盘浮盈不足初始实际1r，下一交易日起退出剩余仓位复核。')
                result['pending'].append('第5日收盘不足初始实际1r，下一交易日退出复核')
    return result


def evaluate_entry(cfg, code, quote, snapshot, *, setup=None, now=None):
    """Shared fail-closed execution gates for swing UI and monitor alerts."""
    from risk_engine import compute
    clock = _now(now)
    snapshot, quote = snapshot or {}, quote or {}
    blockers = []
    plan = cfg.get('trade_plans', {}).get(code) or {}
    price = _number(quote.get('price'))
    source = parse_provider_time(quote.get('source_timestamp'))
    stamp = parse_provider_time(snapshot.get('fetch_finished_at'))
    digest = hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    strategy = (snapshot.get('risk') or {}).get('strategy') or {}
    result = {'allowed': False, 'blockers': blockers, 'max_buy_shares': None, 'reward_risk': None}
    if any(str(p.get('code')).zfill(6) == code and int(p.get('quantity', 0)) > 0 for p in cfg.get('positions', [])):
        blockers.append('V1不在已有仓位加仓')
    if not price or not source or not 0 <= (clock.astimezone(timezone.utc)-source).total_seconds() <= 90 or source.astimezone(CN).date() != clock.date():
        blockers.append('缺少本证券当日90秒内源报价')
    if (not stamp or not 0 <= (clock.astimezone(timezone.utc)-stamp).total_seconds() <= 45
            or snapshot.get('mode') != 'live' or not snapshot.get('calendar_ok') or not snapshot.get('market_session')
            or snapshot.get('config_hash') != digest or snapshot.get('quote_health', {}).get('block_trade_signals')
            or not (snapshot.get('risk') or {}).get('valid')):
        blockers.append('账户快照、配置、交易日历或行情状态尚未核验')
    if clock.time() < time(9, 45) or not (time(9, 45) <= clock.time() <= time(11, 30) or time(13) <= clock.time() <= time(15)):
        blockers.append('仅交易日9:45以后连续竞价时段复核新增')
    if not strategy or strategy.get('can_open', strategy.get('new_entries_allowed', False)) is not True:
        blockers.append('V1账户状态未允许新增')
    account = snapshot.get('strategy_account')
    if not isinstance(account, dict) or account.get('opens_today', 0) >= 1 or account.get('paused') or account.get('needs_reconcile'):
        blockers.append('当日新增已用、策略暂停或账户对账待完成')
    if plan.get('strategy_version') != '1.0' or plan.get('setup_verified') is not True:
        blockers.append('缺少已核验V1固定计划')
    if not plan.get('sector') or not all((plan.get('review') or {}).get(k) is True for k in ('fundamentals_ok', 'event_clear', 'corporate_action_clear', 'resistance_clear')):
        blockers.append('行业、财报/基金、公告、除权口径或2r可见压力空间待人工核验')
    calendar = _calendar(snapshot.get('trading_dates', snapshot.get('calendar_dates')), clock.date())
    signal = _day(plan.get('signal_date'))
    if not calendar or signal not in calendar or clock.date() not in calendar or calendar.index(clock.date()) != calendar.index(signal)+1:
        blockers.append('计划仅信号日下一交易日有效，交易日历或计划日期不符')
    evidence = setup or quote.get('strategy_setup')
    if not evidence or not evidence.get('eligible') or evidence.get('signal_date') != plan.get('signal_date'):
        blockers.append('完整日K形态证据缺失或与固定计划日期不一致')
    else:
        for k in ('trigger_price', 'entry_price', 'stop_loss', 'take_profit_1', 'take_profit_2'):
            if not _number(plan.get(k)) or abs(float(plan[k])-float((evidence.get('levels') or {}).get(k, math.inf))) > 1e-6:
                blockers.append('固定计划价位与该日形态计算不一致')
                break
    lower, cap = _number(plan.get('trigger_price')), _number(plan.get('entry_price'))
    if not lower or not cap or not price or not lower <= price <= cap:
        blockers.append('当前价不在固定触发下沿至Ecap内')
    opened = _number(quote.get('open', quote.get('open_price')))
    if not opened:
        blockers.append('当日开盘价缺失，不能排除跳空超Ecap')
    elif cap and opened > cap:
        blockers.append('当日高开超过Ecap，全天取消原计划')
    if quote.get('at_limit_up') is True:
        blockers.append('涨停排队不进入新增观察')
    if quote.get('limit_status_verified') is False:
        blockers.append('当日涨停价状态未核验，暂停新增')
    if any(int(p.get('quantity', 0)) > 0 for p in cfg.get('positions', [])) and snapshot.get('quote_health', {}).get('provider_timestamps_verified') is not True:
        blockers.append('已有持仓的源时间戳未全部核验')
    if blockers:
        return result
    prices = {str(r.get('code')).zfill(6): r.get('price') for r in snapshot.get('quotes', []) if r.get('held') and _number(r.get('price'))}
    computed = compute(cfg, prices, fresh=True, watch_quotes=[{'code': code, 'name': quote.get('name', code), 'price': price,
                                                             'asset_type': quote.get('asset_type', 'stock')}], strategy_account=account)
    gate = computed.get('strategy') or {}
    size = next((s for s in computed.get('sizing', []) if str(s.get('code')).zfill(6) == code), {})
    if not computed.get('valid') or computed.get('over_budget') or gate.get('can_open', gate.get('new_entries_allowed', False)) is not True or (size.get('max_buy_shares') or 0) < 100:
        blockers.append(size.get('reason') or '最新账户重算未通过新增约束')
    else:
        result.update(allowed=True, max_buy_shares=size['max_buy_shares'], reward_risk=size.get('reward_risk'))
    return result
