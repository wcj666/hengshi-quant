"""Intraday T observations. Pure research logic; no orders or invented fills.

The initial thresholds below are design defaults, not a backtested strategy.
A-share/stock ETF same-day buying does not create same-day sellable inventory.
"""
from __future__ import annotations
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
import hashlib
import math
from quote_health import parse_provider_time
from strategy_defaults import T_DEFAULTS
from strategy_v1 import strategy_prefs
from t_costs import money, roundtrip, sold_fee_for_quantity, decimal, estimate_leg, nonnegative, TICKS, ROUND_CEILING, ROUND_FLOOR

CN=ZoneInfo('Asia/Shanghai')
T_CUTOFF = time.fromisoformat(T_DEFAULTS['last_open_time'])
RISK_EXITS={'stop','take_profit_1','take_profit_2','structure_support','structure_weakness','time_5','time_10'}


def _number(value,minimum=None):
    try:value=float(value)
    except (TypeError,ValueError):return None
    if not math.isfinite(value) or (minimum is not None and value<minimum):return None
    return value


def _positive(value):
    x=_number(value,0)
    return x if x and x>0 else None


def _integer(value):
    if isinstance(value, bool):return None
    x=_number(value,0)
    return int(x) if x is not None and int(x)==x else None


def _clock(now=None):
    now=now or datetime.now(CN)
    return now.replace(tzinfo=CN) if now.tzinfo is None else now.astimezone(CN)


def _fresh(value,clock,max_age):
    stamp=parse_provider_time(value)
    return bool(stamp and stamp.astimezone(CN).date()==clock.date() and 0<=(clock.astimezone(timezone.utc)-stamp).total_seconds()<=max_age)


def _settings(cfg):
    """Numerical rules shared by live signals, the ledger and historical replay."""
    raw = cfg.get('t_settings')
    if not isinstance(raw, dict):
        raw = {}
    reserve = _positive(raw.get('fee_reserve')) or T_DEFAULTS['fee_reserve']
    maximum = _number(raw.get('max_fraction'), 0)
    if maximum is None:
        maximum = T_DEFAULTS['max_fraction']
    return {
        'fee_reserve': max(T_DEFAULTS['fee_reserve'], reserve),
        'max_fraction': min(T_DEFAULTS['max_fraction'], maximum),
        'stock_deviation_pct': max(.3, _positive(raw.get('stock_deviation_pct')) or T_DEFAULTS['stock_deviation_pct']),
        'etf_deviation_pct': max(.15, _positive(raw.get('etf_deviation_pct')) or T_DEFAULTS['etf_deviation_pct']),
    }


def _minutes(data,clock):
    if not isinstance(data,dict) or data.get('valid') is not True:return None,(data or {}).get('error','分时数据尚未核验') if isinstance(data,dict) else '分时数据尚未核验'
    if not _fresh(data.get('source_timestamp'),clock,90):return None,'分时源时间缺失、过期或与今天不一致'
    rows=data.get('rows')
    if not isinstance(rows,list) or len(rows)<30:return None,'至少需要30根已完成且有效的一分钟数据'
    parsed=[];last=None;volume=-1;amount=-1
    for row in rows:
        stamp=parse_provider_time(row.get('timestamp')) if isinstance(row,dict) else None
        p=_positive(row.get('price')) if isinstance(row,dict) else None
        v=_number(row.get('cum_volume'),0) if isinstance(row,dict) else None
        a=_number(row.get('cum_amount'),0) if isinstance(row,dict) else None
        if not stamp or not p or v is None or a is None:return None,'分时价格、累计量额或时间无效'
        when=stamp.astimezone(CN)
        if when.date()!=clock.date() or when>=clock.replace(second=0,microsecond=0):return None,'分时包含跨日、未来或尚未完成的一分钟'
        if not (time(9,30)<=when.time()<=time(11,30) or time(13)<=when.time()<time(15)):return None,'分时包含非连续交易时段'
        if last and when<=last:return None,'分时日期重复或乱序'
        if v<volume or a<amount:return None,'分时累计成交量/成交额回退'
        if (v>volume and last and a<=amount) or (a>amount and last and v<=volume):return None,'分时累计量额增量不匹配'
        parsed.append((when,p,v,a));last=when;volume=v;amount=a
    if (clock-last).total_seconds()>150:return None,'最新已完成分时超过150秒，暂停做T观察'
    if volume<=0 or amount<=0:return None,'缺少有效累计量额，不能计算分时均价'
    prices=[x[1] for x in parsed];vwap=amount/(volume*100)
    # Tencent CN stock/stock-ETF volume is in lots. Reject mismatched units rather
    # than synthesizing VWAP from prices or silently switching a multiplier.
    if not min(prices)*.99<=vwap<=max(prices)*1.01:return None,'分时累计量额单位未通过均价校验'
    consecutive=all((parsed[i][0]-parsed[i-1][0]).total_seconds()==60 for i in (-2,-1))
    return {'prices':prices,'vwap':vwap,'high':max(prices),'low':min(prices),'consecutive':consecutive,
            'asof':last.isoformat(),'volume':volume,'amount':amount},None


def _risk_conflict(cfg,position,quote,risk,price):
    code=str(position['code']).zfill(6);review=quote.get('strategy_exit') or {};trigger=review.get('trigger')
    if trigger in RISK_EXITS or review.get('action') in ('exit','reduce'):
        return '固定退出或结构风险提醒优先；本次减仓不作为做T并默认接回'
    stop=_positive((cfg.get('trade_plans') or {}).get(code,{}).get('stop_loss'))
    if stop is not None and price<=stop:return '已触及固定止损，不用做T代替退出处理'
    if code in (risk.get('stop_breaches') or []):return '已触及固定止损，不生成做T回补观察'
    return None


def _signal(row,clock,cycle_id=''):
    ident=hashlib.sha256(f'{clock.date()}|t|{row["code"]}|{row["phase"]}|{cycle_id}'.encode()).hexdigest()[:20]
    result = {'id':ident,'category':'t','code':row['code'],'name':row['name'],
            'title':row['status'],'reason':row['reason'],'trigger':row['phase'],
            'price':row['price'],'level':row.get('target_buy') if row['phase']=='buyback_ready' else row.get('target_sell'),
            'shares':row.get('quantity'),'held':True,'day':str(clock.date()),'priority':'normal',
            'tradable':row.get('tradable',False),'blockers':row.get('blockers',[]),
            'data_note':'做T条件观察，不自动下单；须核对券商可卖数及实际成交。价差与费用为估算，不能保证收益。'}
    if cycle_id:
        result['cycle_id'] = cycle_id
    if row['phase'] == 'carry_cash':
        result.update(ledger_notice=True, source_kind='actual_t_ledger', cycle_id=cycle_id,
                      closeout=row['closeout'], expires_at=clock.replace(hour=15, minute=0, second=0, microsecond=0).isoformat(),
                      held=row.get('held', True),
                      data_note='仅核对已登记实际T成交；未接回部分保留现金，不生成买卖指令或虚构接回。')
    return result


def closeout_summary(cycle, model=None, samples=None):
    """Summarize recorded T cash flow without requiring or substituting a quote.

    This is a cycle cash flow, not account buying power and not realized profit
    for the still-unmatched sale. Missing fees always keep actual net cash null.
    """
    if cycle.get('kind', 'stock') not in ('stock', 'etf'):
        return None
    sold = _integer(cycle.get('quantity'))
    bought = _integer(cycle.get('buyback_quantity', 0))
    sell_price = _positive(cycle.get('sell_price'))
    buy_cost = nonnegative(cycle.get('buyback_cost', 0))
    if not sold or bought is None or bought > sold or not sell_price or buy_cost is None:
        return None
    fills = cycle.get('fills') or []
    if not isinstance(fills, list):
        return None
    parsed = []
    for fill in fills:
        if not isinstance(fill, dict):
            return None
        qty, price = _integer(fill.get('quantity')), _positive(fill.get('price'))
        if not qty or not price:
            return None
        parsed.append((qty, price, nonnegative(fill.get('fee'))))
    covered = sum(qty for qty, _, _ in parsed)
    recorded_cost = sum(decimal(qty) * decimal(price) for qty, price, _ in parsed)
    matched = covered == bought and abs(recorded_cost - decimal(buy_cost)) <= decimal('.01')
    sell_gross = decimal(sold) * decimal(sell_price)
    gross_cash = sell_gross - decimal(buy_cost)
    sell_fee = nonnegative(cycle.get('sell_fee'))
    known = decimal(sell_fee or 0) + sum(decimal(fee) for _, _, fee in parsed if fee is not None)
    complete = matched and sell_fee is not None and all(fee is not None for _, _, fee in parsed)
    estimated, bases, unknown_fee = known, [], False
    reserve = max(T_DEFAULTS['fee_reserve'], _positive(cycle.get('fee_reserve')) or 0.)
    kind = cycle.get('kind', 'stock')
    if sell_fee is None:
        fee, basis = estimate_leg(kind, 'sell', float(sell_gross), model or {}, samples or [])
        unknown_fee |= fee is None
        estimated += decimal(fee if fee is not None else reserve)
        bases.append(basis)
    for qty, price, actual_fee in parsed:
        if actual_fee is None:
            fee, basis = estimate_leg(kind, 'buy', qty * price, model or {}, samples or [])
            unknown_fee |= fee is None
            estimated += decimal(fee if fee is not None else reserve)
            bases.append(basis)
    # Aggregate buybacks without their individual legs cannot prove order fees.
    # Do not present a net estimate as complete when those fills are absent.
    estimate = money(gross_cash - estimated) if matched and not unknown_fee else None
    return {'cycle_id': cycle.get('id'), 'sold_quantity': sold, 'buyback_quantity': bought,
            'remaining_quantity': sold - bought, 'sell_gross': money(sell_gross),
            'buyback_gross': money(buy_cost), 'gross_cash_flow': money(gross_cash),
            'actual_fee': money(known) if complete else None,
            'estimated_fee': money(estimated) if matched and not complete and not unknown_fee else None,
            'net_cash_actual': money(gross_cash - known) if complete else None,
            'net_cash_estimate': estimate if not complete else None,
            'cash_basis': 'actual' if complete else 'estimated' if matched and not unknown_fee else 'pending',
            'conservative_cash_delta': money(gross_cash - estimated) if matched else None,
            'fee_basis': bases, 'fills_verified': matched,
            'ledger_sync_required': cycle.get('ledger_sync_required') is True,
            'cash_note': '本轮净现金流包含未接回份额的卖出本金，不是全部已实现做T利润，也不是券商可用或可取余额。',
            'fee_note': '实际成交费用已齐全' if complete else '费用或接回分笔记录未齐，净现金仅按已知资料估算；未知项保留待核。'}


def _closeout_rows(cfg, cycles, ledger, clock):
    positions = {str(p.get('code')).zfill(6): p for p in cfg.get('positions', [])}
    result = []
    for cycle in cycles:
        if cycle.get('status') != 'open' or cycle.get('date') != str(clock.date()):
            continue
        ident = cycle.get('id')
        if not isinstance(ident, str) or not ident:
            continue
        summary = closeout_summary(cycle, ledger.get('fee_model'), ledger.get('fee_samples'))
        if not summary or summary['remaining_quantity'] <= 0:
            continue
        code = str(cycle.get('code')).zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        p = positions.get(code, {})
        result.append({'code': code, 'name': cycle.get('name') or p.get('name', code),
                       'cycle_id': ident, 'closeout': summary, 'phase': 'carry_cash',
                       'status': '尾盘做T收尾：尚有份额未接回',
                       'reason': f'本轮已卖{summary["sold_quantity"]}、已接回{summary["buyback_quantity"]}、'
                                 f'尚未接回{summary["remaining_quantity"]}股/份。核对成交后可确认保留现金结束本轮；不为完成做T追买。',
                       'price': None, 'vwap': None, 'high': None, 'low': None,
                       'target_buy': None, 'target_sell': None, 'quantity': None,
                       'remaining_quantity': summary['remaining_quantity'], 'sell_price': cycle.get('sell_price'),
                       'tradable': False, 'sell_first': True, 'held': (_integer(p.get('quantity')) or 0) > 0,
                       'blockers': [], 'checks': [],
                       'note': '仅账本收尾，不依赖行情生成买卖决策；保留现金不等于发生接回成交。'})
    return result


def closeout_signals(cfg, book, *, now, calendar_ok, market_session):
    """Rebuild quote-independent closeout notices from the current actual ledger."""
    clock = _clock(now)
    if not (calendar_ok and market_session and clock.weekday() < 5
            and T_CUTOFF <= clock.time() < time(15)):
        return []
    return [_signal(row, clock, row['cycle_id'])
            for row in _closeout_rows(cfg, book.get('cycles') or [], book, clock)]


def _restore_blockers(cfg, risk, base, price, code):
    blockers=[];strategy=risk.get('strategy') or {};equity=_positive(risk.get('equity'))
    account=strategy.get('account') or {};limits=strategy_prefs(cfg)
    if account.get('verified') is not True or account.get('needs_reconcile') is not False or account.get('paused'):
        blockers.append('账户未核对或暂停，当前不允许承诺接回')
    drawdown=_number(account.get('drawdown'),0)
    if drawdown is None or drawdown >= limits['pause_drawdown']:
        blockers.append('账户回撤未核验或已达暂停线')
    exposure=_number(strategy.get('exposure_pct'),0)
    if exposure is None or exposure > limits['max_exposure_pct']:
        blockers.append('当前总仓位超限，接回将恢复高仓位')
    if equity is None or base * price > equity * limits['max_position_pct'] / 100:
        blockers.append('原底仓单一证券仓位超限，接回受限')
    if not _positive((cfg.get('trade_plans') or {}).get(code,{}).get('stop_loss')) or risk.get('existing_planned_stress') is None:
        blockers.append('固定止损或组合风险未知，接回条件未通过')
    return blockers


def _tick(value,kind,rounding):
    tick=TICKS[kind]
    return float((decimal(value)/tick).to_integral_value(rounding=rounding)*tick)


def _cost_thresholds(kind, sell_price, quantity, reserve, model, samples, *, sell_fee=None):
    costs = roundtrip(kind, sell_price, quantity, reserve, model, samples, sell_fee=sell_fee)
    fees = costs['estimated_roundtrip_fee']
    reserve = max(reserve, fees)
    minimum_gross = 2 * reserve
    minimum_spread = _tick(decimal(minimum_gross) / quantity, kind, ROUND_CEILING)
    return {**costs, 'fee_reserve': reserve, 'minimum_gross': minimum_gross,
            'minimum_spread': minimum_spread, 'extra_cost_buffer': money(minimum_gross - fees)}


def _explain(row,position,cfg,risk,quote,minutes,ledger,clock,session):
    """Explain waiting and blocking states without manufacturing a new signal."""
    checks=[];row['checks']=checks
    def check(label,passed,detail):checks.append({'label':label,'passed':passed,'detail':detail})
    code=row['code'];kind=position['type'];held=int(position['quantity']);prefs=_settings(cfg)
    entry=(ledger.get('availability') or {}).get(code,{})
    available=_integer(entry.get('available_quantity'))
    confirmed=(entry.get('date')==str(clock.date()) and _integer(entry.get('base_quantity'))==held
               and available is not None and available<=held)
    cap=int(held*prefs['max_fraction']//100)*100
    capacity=min(cap,available//100*100) if confirmed else None
    row.update(held_quantity=held,available_quantity=available if confirmed else None,
               availability_asof=entry.get('confirmed_at') if confirmed else None,
               availability_source=entry.get('source','manual') if confirmed else None,
               t_capacity=capacity,quote_asof=quote.get('source_timestamp'))
    check('交易时段',session and (bool(row.get('cycle_id')) or clock.time() < T_CUTOFF),
          f'{T_DEFAULTS["last_open_time"]}后不开新T；休市保留记录，下一交易日重新核验')
    check('当日可卖底仓',confirmed and capacity>=100 if confirmed else False,
          f'已记录可卖{available}份；保留至少2/3底仓，本轮测算上限{capacity}份' if confirmed else '需要今天的券商可卖数量；不由T+1自动推算')
    fresh=bool(row['price'] and _fresh(quote.get('source_timestamp'),clock,90) and risk.get('valid') is True)
    data,error=_minutes(minutes,clock)
    check('完整行情与分时',fresh and data is not None,error or ('行情源时间已核验' if fresh else '报价或全账户行情待核验'))
    if row['phase']=='exit_only':
        check('账户模式',False,row['reason'])
        return
    if not fresh or not data or not session:return
    if row.get('cycle_id'):
        check('回补风控',not row['blockers'],'；'.join(row['blockers']) or '仅对实际T卖出后的剩余数量复核，不能超额接回')
        check('回补触发',row['phase']=='buyback_ready',row['reason'])
        return
    price=row['price'];vwap=data['vwap'];prices=data['prices']
    deviation=prefs['etf_deviation_pct'] if kind=='etf' else prefs['stock_deviation_pct']
    peak=max(prices[-12:]);fall=data['consecutive'] and prices[-3]>prices[-2]>prices[-1] and price<=prices[-1] and price<sum(prices[-5:])/5
    row['deviation_pct']=round((price/vwap-1)*100,3)
    check('冲高至均价上方',peak>=vwap*(1+deviation/100) and price>vwap,
          f'最近12分钟需冲高至均价上方{deviation:g}%，当前偏离均价{row["deviation_pct"]:+.3f}%')
    check('冲高后回落',fall,'需连续3个已完成分钟逐步回落，最新价不高于末分钟且低于5分钟平均价')
    if capacity and capacity>=100:
        costs = _cost_thresholds(kind, price, capacity, prefs['fee_reserve'],
                                 ledger.get('fee_model') or {}, ledger.get('fee_samples') or [])
        row.update(costs)
        gross = (price - vwap) * capacity
        check('价差覆盖成本', gross >= costs['minimum_gross'],
              f'假设回落至分时均价，毛价差{money(gross):.2f}元；规则门槛{costs["minimum_gross"]:.2f}元（含费用及滑点缓冲）')
        row['opening_price_floor'] = _tick(decimal(vwap) + decimal(costs['minimum_gross']) / capacity, kind, ROUND_CEILING)
        if row['phase'] in ('wait', 'sell_watch'):
            row['target_sell'] = row['opening_price_floor']
    else:check('价差覆盖成本',None,'可卖数量未核验或不足一手，暂不计算净收益和费用门槛')
    restore=_restore_blockers(cfg,risk,held,price,code)
    if row['phase']=='risk_first':restore=[row['reason']]+restore
    check('卖出后是否允许接回',not restore,'；'.join(restore) or '当前通过，实际卖出后仍须重新核对现金、仓位及风险')
    row['blockers']=list(dict.fromkeys(row['blockers']+restore+([] if confirmed else ['请先确认今天券商可卖数量'])))
    # No actual T sale means there is no executable buyback task.
    row['target_buy']=None
    if row['phase']=='wait':
        row['status']='暂不做T：当前低于分时均价' if price<=vwap else '等待冲高回落确认'
        row['reason']='；'.join(x['detail'] for x in checks if x['passed'] is not True)


def _buyback_blockers(cfg,position,quote,risk,cycle,ledger,quantity,price,fee):
    blockers=[];code=str(position['code']).zfill(6)
    current=int(position.get('quantity',0));sold=_integer(cycle.get('quantity'));bought=_integer(cycle.get('buyback_quantity',0))
    base=_integer(cycle.get('base_quantity'))
    availability=(ledger.get('availability') or {}).get(code,{})
    if base is None:base=_integer(availability.get('base_quantity'))
    if base is None:blockers.append('本轮原底仓数量未核验')
    elif sold is not None and bought is not None and current!=base-sold+bought:blockers.append('先按实际T成交同步持仓和现金，再核对账户')
    if cycle.get('ledger_sync_required') or ledger.get('ledger_sync_required'):blockers.append('T成交与当前台账尚未同步')
    if quantity<100:blockers.append('剩余可回补不足100股/份，不凑整扩大回补')
    if sold is None or bought is None or bought>sold or quantity>sold-bought:blockers.append('回补数量超过已实际T卖出且尚未买回数量')
    if base is not None and current+quantity>base:blockers.append('回补后将超过原始底仓数量')
    if risk.get('valid') is not True:blockers.append('完整账户行情未核验')
    strategy=risk.get('strategy') or {};account=strategy.get('account') or risk.get('strategy_account') or {}
    if account.get('initialized') is not True or account.get('verified') is not True or account.get('needs_reconcile') is not False:blockers.append('账户、成交与资金流水待核对')
    limits = strategy_prefs(cfg)
    drawdown=_number(account.get('drawdown'),0)
    if drawdown is None or drawdown >= limits['pause_drawdown']:
        blockers.append(f'账户回撤未核验或达到{limits["pause_drawdown"]:g}元暂停线')
    if account.get('paused') or account.get('budget_exhausted') or account.get('pause_until'):blockers.append('账户仍处于暂停状态')
    equity=_positive(risk.get('equity'));cash=_number(cfg.get('cash'),0)
    if cash is None or cash<quantity*price+fee:blockers.append('现金不足以覆盖回补及费用预留')
    pos_cap = limits['max_position_pct']
    total_cap = limits['max_exposure_pct']
    exposure_pct=_number(strategy.get('exposure_pct'),0)
    if equity is None:blockers.append('账户净值未核验')
    else:
        if (current+quantity)*price>equity*pos_cap/100+1e-8:blockers.append(f'回补后单一证券超过{pos_cap:g}%上限')
        if exposure_pct is None or exposure_pct+quantity*price/equity*100>total_cap+1e-8:blockers.append(f'回补后权益总仓位未核验或超过{total_cap:g}%上限')
    stop=_positive((cfg.get('trade_plans') or {}).get(code,{}).get('stop_loss'))
    available=_number(risk.get('available_new_risk'),0)
    if stop is None or risk.get('existing_planned_stress') is None or available is None:
        blockers.append('固定止损或全账户剩余风险未知，不能用做T绕过风控')
    elif price<=stop:blockers.append('回补价不高于固定止损，不能接回')
    else:
        planned=(price-stop)*quantity+fee
        single = limits['per_trade_risk']
        if planned>single+1e-8:blockers.append('含费用的回补计划风险超过单笔上限')
        if planned>available+1e-8:blockers.append('回补计划风险超过账户可用额度')
    return list(dict.fromkeys(blockers))


def build_t_observations(cfg,quotes,risk,minute_by_code,ledger,now=None,market_session=True,calendar_ok=True):
    """Return always-visible rows and explicitly conditional T observations.

    Sell-then-buy only. No buyback exists without an actual, same-day T-sale
    record. General reduction fills never become implicit T-sale records.
    """
    clock=_clock(now);day=str(clock.date());prefs=_settings(cfg);ledger=ledger or {};risk=risk or {};quotes=quotes or {}
    result={'rows':[],'signals':[],'asof':clock.isoformat(),'mode':T_DEFAULTS['mode'],
            'note':'先卖后买为主；先买后卖受现有V1不加仓及T+1限制，当前不提供可执行提示。阈值尚未回测。'}
    cycles=ledger.get('cycles') or [];availability=ledger.get('availability') or {}
    fee_model=ledger.get('fee_model') or {};fee_samples=ledger.get('fee_samples') or []
    session=bool(market_session and calendar_ok and clock.weekday()<5 and
                 (time(9,30)<=clock.time()<time(11,30) or time(13)<=clock.time()<time(15)))
    # Closing an actual record is a ledger decision, not a new market trade.
    # Keep it visible even in exit-only mode or when minute/quote data is absent.
    closeouts = _closeout_rows(cfg, cycles, ledger, clock) if session and clock.time() >= T_CUTOFF else []
    result['rows'].extend(closeouts)
    result['signals'].extend(_signal(row, clock, row['cycle_id']) for row in closeouts)
    closeout_codes = {row['code'] for row in closeouts}
    for position in cfg.get('positions',[]):
        held=_integer(position.get('quantity'));kind=position.get('type','stock')
        if not held or kind not in ('stock','etf'):continue
        code=str(position['code']).zfill(6);quote=quotes.get(code) or {};price=_positive(quote.get('price'))
        if code in closeout_codes:continue
        row={'code':code,'name':position.get('name',code),'phase':'wait','status':'等待分时条件',
             'reason':'等待偏离均价后的冲高回落或回踩企稳，不因时间到点做T。','blockers':[],
             'price':price,'vwap':None,'high':None,'low':None,'target_buy':None,'target_sell':None,
             'quantity':None,'gross_spread_estimate':None,'fee_reserve':prefs['fee_reserve'],
             'net_spread_estimate':None,'tradable':False,'sell_first':True,
             'buy_first_blockers':['当前V1不向已有持仓追加；先买后卖需要另行核验新增风险及原有可卖底仓，当前未启用'],
             'note':'仅研究提醒，不是委托；触价不是成交，不能承诺接回。'}
        result['rows'].append(row)
        if (cfg.get('execution_preferences') or {}).get('mode')=='exit_only':
            row.update(phase='exit_only',status='退出休息：暂停做T',reason='仅保留已有仓位的退出复核，不新开T，也不提示接回。')
            continue
        if not session:row.update(phase='inactive',status='非有效盘中',reason='仅在已核验交易日的连续交易时段做T观察');continue
        if not price or not _fresh(quote.get('source_timestamp'),clock,90):row.update(phase='blocked',status='报价待核验',reason='当前价格或源时间未通过90秒新鲜度校验');continue
        if risk.get('valid') is not True:row.update(phase='blocked',status='账户行情待核验',reason='完整持仓行情不可用，暂停做T观察');continue
        data,error=_minutes((minute_by_code or {}).get(code),clock)
        if error:row.update(phase='blocked',status='分时数据待核验',reason=error);continue
        vwap=data['vwap'];prices=data['prices'];row.update(vwap=round(vwap,4),high=data['high'],low=data['low'],minute_asof=data['asof'],
            minute_source_at=(minute_by_code or {})[code].get('source_timestamp'))
        if abs(price/prices[-1]-1)>.03:row.update(phase='blocked',status='分时与最新报价差异异常',reason='最新价与已完成分钟价差超过3%，等待下一轮核验');continue
        if quote.get('at_limit_up') is True or quote.get('at_limit_down') is True or quote.get('limit_status_verified') is False:
            row.update(phase='blocked',status='涨跌停状态异常或未核验',reason='不在涨跌停排队或价格限制未核验时假设可成交');continue
        conflict=_risk_conflict(cfg,position,quote,risk,price)
        if conflict:row.update(phase='risk_first',status='优先处理退出风险',reason=conflict);continue
        todays=[x for x in cycles if str(x.get('code')).zfill(6)==code and str(x.get('date'))==day]
        opens=[x for x in todays if x.get('status')=='open']
        older=[x for x in cycles if str(x.get('code')).zfill(6)==code and str(x.get('date'))<day and x.get('status')=='open']
        if older or any(x.get('expired') for x in opens):
            row.update(phase='expired',status='历史T未完成，待人工复盘',reason='跨日T不自动延续为当日接回指令，先核对实际持仓和成交');continue
        if len(opens)>1:row.update(phase='blocked',status='T记录冲突',reason='同一证券存在多个未完成T记录，先核对台账');continue
        if opens:
            cycle=opens[0];row['cycle_id']=cycle.get('id');sold=_integer(cycle.get('quantity'));bought=_integer(cycle.get('buyback_quantity',0));sell_price=_positive(cycle.get('sell_price'))
            if sold is None or bought is None or bought>sold or not sell_price:
                row.update(phase='blocked',status='T成交记录无效',reason='实际卖出价格或剩余回补数量未核验');continue
            remaining=sold-bought;quantity=(remaining//100)*100;fee=max(prefs['fee_reserve'],_positive(cycle.get('fee_reserve')) or 0.)
            row.update(fee_reserve=fee,remaining_quantity=remaining,sell_price=sell_price)
            if remaining<=0:row.update(phase='closed',status='本轮T已完成',reason='当天同一证券最多一轮，不重复追做');continue
            if quantity >= 100:
                costs = _cost_thresholds(kind, sell_price, quantity, fee, fee_model, fee_samples,
                                         sell_fee=sold_fee_for_quantity(cycle, quantity))
                row.update(costs)
                fee = costs['fee_reserve']
            if clock.time() >= T_CUTOFF:
                row.update(phase='blocked',status='T收尾资料待核',reason='实际T成交记录未通过收尾核对，先核对分笔成交，不为完成做T追买。');continue
            if quantity<100:row.update(phase='blocked',status='剩余不足一手',reason='实际未接回数量不足100股/份，不凑整扩大回补');continue
            target = _tick(decimal(sell_price) - decimal(costs['minimum_spread']), kind, ROUND_FLOOR)
            row.update(target_buy=target if target > 0 else None,target_sell=sell_price)
            gross=(sell_price-price)*quantity
            row.update(gross_spread_estimate=money(gross),net_spread_estimate=money(gross-costs['estimated_roundtrip_fee']))
            blockers=_buyback_blockers(cfg,position,quote,risk,cycle,ledger,quantity,price,fee)
            deviation=prefs['etf_deviation_pct'] if kind=='etf' else prefs['stock_deviation_pct']
            low=min(prices[-12:]);bounce=data['consecutive'] and prices[-3]<prices[-2]<prices[-1] and price>=prices[-1]
            cheap=target > 0 and price<=target and low<=vwap*(1-deviation/100)
            if blockers:row.update(phase='buyback_blocked',status='回补风控未通过',reason='；'.join(blockers),blockers=blockers);continue
            if not (bounce and cheap):row.update(phase='buyback_wait',status='等待价差与企稳确认',reason='须在扣费后有足够价差，并出现连续3个完成分钟回升；跌了不等于可以买回');continue
            row.update(phase='buyback_ready',status='做T回补条件观察',reason='实际T卖出记录、剩余数量、扣费价差、回踩企稳和账户风控均通过；仍须在券商确认价格与成交。',quantity=quantity,tradable=True)
            result['signals'].append(_signal(row,clock,str(cycle.get('id',''))));continue
        if todays:row.update(phase='closed',status='当天本轮已结束',reason='同一证券每天最多一轮T，不因新波动反复交易');continue
        if clock.time() >= T_CUTOFF:
            row.update(phase='inactive', status='尾盘不开新T',
                       reason=f'{T_DEFAULTS["last_open_time"]}后不新开做T，避免尾盘强行追回')
            continue
        base=held;available=availability.get(code) or {};confirmed=available.get('date')==day
        if confirmed:
            b=_integer(available.get('base_quantity'));a=_integer(available.get('available_quantity'))
            if b is not None:base=b
            if b!=held or a is None or a>held:
                row.update(phase='blocked',status='可卖数量与台账待核对',reason='当日券商可卖数量、原底仓和当前持仓不一致，重新确认后再观察');continue
        else:a=None
        cap=int(base*prefs['max_fraction']//100)*100
        if cap<100:row.update(phase='too_small',status='底仓不足，默认不做T',reason='保留至少2/3底仓并按100股/份取整，当前底仓不足以分出一手做T');continue
        quantity=min(cap,(a//100)*100) if a is not None else None
        if quantity is not None and quantity<100:row.update(phase='blocked',status='当日可卖不足一手',reason='股票及股票ETF不能把当天新买入数量当作可卖底仓');continue
        deviation=prefs['etf_deviation_pct'] if kind=='etf' else prefs['stock_deviation_pct']
        peak=max(prices[-12:]);fall=data['consecutive'] and prices[-3]>prices[-2]>prices[-1] and price<=prices[-1] and price<sum(prices[-5:])/5
        target_buy=vwap;row.update(target_buy=round(target_buy,4),target_sell=price)
        if quantity is not None:
            row.update(_cost_thresholds(kind,price,quantity,prefs['fee_reserve'],fee_model,fee_samples))
        pattern=peak>=vwap*(1+deviation/100) and price>vwap and fall
        if not pattern:continue
        screening_quantity=quantity if quantity is not None else cap
        gross=(price-target_buy)*screening_quantity
        if gross<2*row['fee_reserve']:
            row.update(status='价差不足，跳过做T',reason='假设回落至分时均价的毛价差不足费用预留的2倍，不为小波动频繁交易');continue
        restore=_restore_blockers(cfg,risk,base,price,code)
        if restore:
            row.update(phase='sell_watch',status='高抛/减仓观察（接回受限）',reason='冲高回落条件出现，但当前风控不允许确认接回；只作高抛/减仓观察，不承诺完成做T。',blockers=restore+(['请先确认今天券商可卖数量'] if quantity is None else []),quantity=None,tradable=False)
        elif quantity is None:
            row.update(phase='sell_watch',status='高抛条件观察，待核可卖数量',reason='分时偏离均价后连续3个完成分钟回落；尚未核实当日可卖数量，仅观察，不提供下单股数。',blockers=['请先确认今天券商可卖数量'])
        else:
            row.update(phase='sell_ready',status='做T高抛条件观察',reason='分时偏离均价后连续3个完成分钟回落，假设价差覆盖费用门槛；仅可复核已确认的旧底仓。卖出后可能无法低价接回。',quantity=quantity,tradable=True,gross_spread_estimate=money(gross),net_spread_estimate=money(gross-row['estimated_roundtrip_fee']))
        result['signals'].append(_signal(row,clock))
    positions={str(p['code']).zfill(6):p for p in cfg.get('positions',[])}
    for row in result['rows']:
        if row.get('closeout'):continue
        _explain(row,positions[row['code']],cfg,risk,quotes.get(row['code']) or {},
                 (minute_by_code or {}).get(row['code']),ledger,clock,session)
    return result
