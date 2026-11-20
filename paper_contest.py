"""Private, snapshot-based PAPER trading. Never submits broker orders.

Only contemporaneously observed quotes can produce virtual fills. An opening
broker snapshot is preserved verbatim; contest profit starts at zero. All money,
holdings and files here are separate from config.json and the real V1 account.
"""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, date, time, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR
import fcntl
import hashlib
import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from strategy_defaults import SIMULATED_COSTS, SIM_PARTICIPATION, initial_v1_rules

CN=ZoneInfo('Asia/Shanghai')
ROOT=Path(__file__).resolve().parent
DEFAULT_STORE=ROOT/'output'/'paper_contest_20261113_20261119'
UNIVERSE=[('159971','创业100','etf'),('159929','医药ETF','etf'),
          ('159562','黄金股ETF','etf'),('300274','阳光电源','stock'),
          ('600641','先导基电','stock'),('002463','沪电股份','stock'),
          ('600406','国电南瑞','stock'),('000682','东方电子','stock'),
          ('002714','牧原股份','stock')]


def number(value,minimum=None):
    if isinstance(value,bool):raise ValueError('布尔值不是金额或数量')
    value=float(value)
    if not math.isfinite(value) or minimum is not None and value<minimum:raise ValueError('数值无效')
    return value


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))


def clock_now():return datetime.now(CN)


def fees(state,kind,side,notional):
    model=state['fees']
    commission=money(max(model['minimum_commission'],notional*model['commission_rate']))
    stamp=money(notional*model['stock_sell_stamp_rate']) if kind=='stock' and side=='sell' else 0.
    transfer=money(notional*model['stock_transfer_rate']) if kind=='stock' else 0.
    return {'commission':commission,'stamp_tax':stamp,'transfer':transfer,'total':money(commission+stamp+transfer)}


def _opening_number(row, field, minimum=None):
    value = row.get(field)
    if type(value) not in (int, float):
        raise ValueError(f'开赛快照的 {field} 须为有效数值')
    try:
        return number(value, minimum)
    except (ValueError, OverflowError) as error:
        raise ValueError(f'开赛快照的 {field} 须为有效数值') from error


def _opening_day(value):
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError('比赛日历须为 YYYY-MM-DD 格式的已核验交易日') from error
    if day.isoformat() != value or day.weekday() >= 5:
        raise ValueError('比赛日历须为 YYYY-MM-DD 格式的已核验工作日')
    return day


def initialize(snapshot,calendar,start,end,*,now=None):
    now = now or clock_now()
    if now.tzinfo is None:
        raise ValueError('初始化时刻须带时区')
    start_date, end_date = _opening_day(start), _opening_day(end)
    days = sorted({_opening_day(day).isoformat() for day in calendar})
    if start not in days or end not in days or start_date > end_date:
        raise ValueError('比赛日期须经真实交易日历核验')
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('positions'), list):
        raise ValueError('开赛快照须包含完整持仓列表')
    from quote_health import parse_provider_time
    stamp = parse_provider_time(snapshot.get('asof'))
    if not stamp or stamp > now or stamp >= datetime.combine(start_date, time(9, 30), CN):
        raise ValueError('开赛快照须带时区，且早于开市、不晚于初始化时刻')
    cash = _opening_number(snapshot, 'available_cash', 0)
    equity = _opening_number(snapshot, 'total_assets', .01)
    market_value = _opening_number(snapshot, 'total_market_value', 0)
    opening = deepcopy(snapshot)
    positions = {}
    for p in opening['positions']:
        if not isinstance(p, dict):
            raise ValueError('开赛持仓格式不完整')
        qty = _opening_number(p, 'quantity', 1)
        available = _opening_number(p, 'available_quantity', 0)
        if qty != int(qty) or available != int(available) or available > qty:
            raise ValueError('开赛数量不一致')
        code = p.get('code')
        if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code) or code in positions
                or p.get('type') not in ('stock', 'etf') or not isinstance(p.get('name'), str) or not p['name'].strip()):
            raise ValueError('开赛持仓须有合法且不重复的代码、名称和证券类别')
        _opening_number(p, 'cost')
        price = _opening_number(p, 'screenshot_price', .000001)
        if abs(money(qty * price) - _opening_number(p, 'market_value', 0)) > .011:
            raise ValueError(f'{code} 开赛价、数量和市值不一致')
        positions[code]={**deepcopy(p),'contest_basis':p['screenshot_price'],'remaining_entry_fee':0.,
                         'opening_available':int(available),'opening_inventory':True,
                         'virtual_bought_on':None,'realized_contest_pnl':0.,'initial_risk':None}
    holdings_value = sum(p['market_value'] for p in positions.values())
    total = money(cash + sum(p['quantity'] * p['screenshot_price'] for p in positions.values()))
    if abs(money(holdings_value) - market_value) > .011 or abs(total - equity) > .011:
        raise ValueError('开赛持仓、现金和净值不一致')
    universe = {code: {'code': code, 'name': name, 'type': kind} for code, name, kind in UNIVERSE}
    for code, position in positions.items():
        universe[code] = {key: position[key] for key in ('code', 'name', 'type')}
    state={'version':1,'mode':'paper_only','start':start,'end':end,'calendar':days,
            'created_at':now.isoformat(),'opening_snapshot':opening,'initial_equity':total,
            'cash':opening['available_cash'],'positions':positions,'plans':{},'trades':[],
            'observations':[],'closed_trades':[],'high_water':total,'maximum_drawdown':0.,
            'last_equity':total,'last_valuation_asof':opening['asof'],'paused':False,
            'universe':list(universe.values()),
            'policy':initial_v1_rules(),
            'fees':{**SIMULATED_COSTS, 'minimum_commission_confirmed': False, 'book_fraction': SIM_PARTICIPATION,
                    'note':'本账本按模拟参数估算佣金、最低佣金、税费和单边滑点，实际费用以交割单为准。'},
            'real_updates':[],'events':[],'notes':[
                f'{start} 起才允许虚拟成交。比赛收益从开赛快照净值起算。',
                '复制开赛券商快照，不连续覆盖模拟账户；之后的虚拟交易导致持仓自然分化。',
                '开赛可卖数量照录；截图未确认可卖的份额不擅自解锁。新虚拟买入按T+1处理。',
                '仅以巡检时实际获取的有效盘口估算成交，不补记两次巡检间错过的价格。',
                '限价、跳空和流动性可能导致未成交；截止日按收盘市值结算，不伪造卖出。']}
    state['fixed_rules_hash']=fixed_rules_hash(state)
    return state


def fixed_rules_hash(state):
    fields={k:state[k] for k in ['mode','start','end','calendar','opening_snapshot','initial_equity','fees','policy']}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def continuation_active(state):
    return isinstance(state.get('continuation'), dict) and state['continuation'].get('enabled') is True


def _continuation_hash(continuation):
    return hashlib.sha256(json.dumps(continuation,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def effective_calendar(state):
    if continuation_active(state):
        previous = [day for day in state['calendar'] if day <= state['end']]
        return sorted(set(previous) | set(state['continuation']['calendar']))
    return state['calendar']


def date_allowed(state, day):
    if day < state['start'] or day not in effective_calendar(state):
        return False
    if day <= state['end']:
        return True
    return continuation_active(state) and day >= state['continuation']['activated_at'][:10]


def _verified_calendar(payload, now):
    if not isinstance(payload, dict) or payload.get('verified') is not True or not isinstance(payload.get('dates'), list):
        raise ValueError('持续模拟须使用已核验的交易日历')
    days = sorted({_opening_day(day).isoformat() for day in payload['dates']})
    if not days or days[-1] < now.astimezone(CN).date().isoformat():
        raise ValueError('交易日历尚未覆盖当前日期')
    return days


def continue_account(state, calendar, reason, now):
    """Append the authorized date extension while retaining the original fixed rules."""
    validate_state(state)
    if now.tzinfo is None or not isinstance(reason, str) or not reason.strip():
        raise ValueError('持续模拟须记录带时区的生效时间和授权依据')
    if continuation_active(state):
        return {'ongoing': True, 'already_enabled': True}
    days = _verified_calendar(calendar, now)
    continuation = {'version': 1, 'enabled': True, 'activated_at': now.astimezone(CN).isoformat(),
                    'reason': reason.strip(), 'calendar': [day for day in days if day >= state['start']],
                    'original_end': state['end'], 'original_rules_hash': state['fixed_rules_hash']}
    state['continuation'] = continuation
    state['continuation_hash'] = _continuation_hash(continuation)
    event = {'at': now.isoformat(), 'kind': 'continuous_simulation_enabled',
             'original_end': state['end'], 'reason': reason.strip(),
             'continuation_hash': state['continuation_hash']}
    state['events'].append(event)
    return {'ongoing': True, 'effective_at': continuation['activated_at'],
            'calendar_through': days[-1]}


def refresh_continuation_calendar(state, store, now):
    """Use the locally verified exchange calendar, without changing the real account."""
    if not continuation_active(state):
        return False
    calendar_path = Path(store).parent/'strategy_calendar.json'
    try:
        days = _verified_calendar(json.loads(calendar_path.read_text(encoding='utf-8')), now)
    except (OSError, ValueError, TypeError):
        return False
    prior = state['continuation']['calendar']
    today = now.astimezone(CN).date().isoformat()
    updated = sorted({day for day in prior if day < today}
                     | {day for day in days if day >= state['start']})
    if updated == prior:
        return False
    previous_hash = state['continuation_hash']
    state['continuation']['calendar'] = updated
    state['continuation_hash'] = _continuation_hash(state['continuation'])
    state['events'].append({'at': now.isoformat(), 'kind': 'simulation_calendar_updated',
                            'previous_hash': previous_hash, 'continuation_hash': state['continuation_hash'],
                            'calendar_through': updated[-1]})
    return True


def validate_state(state):
    if state.get('mode')!='paper_only' or state.get('fixed_rules_hash')!=fixed_rules_hash(state):
        raise ValueError('初始账户、日期或固定规则发生变化，禁止隐式重置比赛')
    if 'continuation' in state:
        continuation = state['continuation']
        from quote_health import parse_provider_time
        if (not continuation_active(state) or continuation.get('version') != 1
                or continuation.get('original_rules_hash') != state['fixed_rules_hash']
                or continuation.get('original_end') != state['end']
                or not parse_provider_time(continuation.get('activated_at'))
                or not continuation.get('reason')
                or continuation.get('calendar') != sorted(set(continuation.get('calendar', [])))
                or state.get('continuation_hash') != _continuation_hash(continuation)):
            raise ValueError('持续模拟的授权或交易日历记录无效')
        for day in continuation['calendar']:
            _opening_day(day)
    cash=state['opening_snapshot']['available_cash']
    qty={p['code']:p['quantity'] for p in state['opening_snapshot']['positions']}
    for i,t in enumerate(state['trades'],1):
        if t.get('sequence')!=i or t.get('mode')!='virtual_snapshot_fill':raise ValueError('虚拟成交序列不一致')
        sign=1 if t['side']=='buy' else -1
        qty[t['code']]=qty.get(t['code'],0)+sign*t['quantity']
        if qty[t['code']]<0:raise ValueError('虚拟成交导致负持仓')
        cash=money(cash-sign*t['quantity']*t['price']-t['fee']['total'])
    if abs(cash-state['cash'])>.011 or {k:v for k,v in qty.items() if v}!={k:v['quantity'] for k,v in state['positions'].items()}:
        raise ValueError('现金、股数与完整虚拟成交流水不一致')


def _quote_number(quote, key, message, minimum=.000001):
    try:
        return number(quote.get(key), minimum)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(message) from None


def _source(quote,now,*,closing=False):
    from quote_health import parse_provider_time
    stamp=parse_provider_time(quote.get('source_timestamp'))
    if not stamp or stamp.astimezone(CN).date()!=now.date() or stamp>now.astimezone(timezone.utc):
        raise ValueError('缺少当日已核验源报价')
    if closing:
        if now.time()<time(15,5) or stamp.astimezone(CN).time()<time(15):raise ValueError('完整收盘价尚未核验')
    elif (now.astimezone(timezone.utc)-stamp).total_seconds()>90:raise ValueError('源报价超过90秒')
    _quote_number(quote, 'price', '缺少有效的最新成交价')
    return stamp


def valuation(state,quotes,now,*,closing=False):
    total=state['cash'];clocks=[]
    for code,p in state['positions'].items():
        q=quotes.get(code,{})
        clocks.append(_source(q,now,closing=closing));total+=p['quantity']*q['price']
    return money(total),min(clocks).isoformat() if clocks else now.isoformat()


def observe(state,quotes,now,*,closing=False):
    from pnl_engine import valid_price

    equity,asof=valuation(state,quotes,now,closing=closing)
    state['last_equity']=equity;state['last_valuation_asof']=asof
    state['last_position_marks']={c:{'quantity':p['quantity'],'price':quotes[c]['price'],
        'value':money(p['quantity']*quotes[c]['price']),'source_timestamp':quotes[c]['source_timestamp'],
        'previous_close':valid_price(quotes[c].get('previous_close'))}
        for c,p in state['positions'].items()}
    state['last_mark_closing']=bool(closing)
    state['high_water']=max(state['high_water'],equity)
    dd=money(state['high_water']-equity);state['maximum_drawdown']=max(state['maximum_drawdown'],dd)
    if dd>=state['policy']['pause_drawdown']:state['paused']=True
    record={'at':now.isoformat(),'source_asof':asof,'equity':equity,'cash':state['cash'],
            'net_pnl':money(equity-state['initial_equity']),'drawdown':dd,'closing':closing}
    state['observations'].append(record)
    return record


def sellable(position,now):
    if position.get('opening_inventory'):return int(position['opening_available'])
    return int(position['quantity']) if position.get('virtual_bought_on','')<str(now.date()) else 0


def _session(state,now,side):
    day=str(now.date())
    if now.weekday()>=5 or not date_allowed(state,day):
        raise ValueError('当前日期不在已核验交易日历内，或尚未开放模拟操作')
    if not (time(9,45)<=now.time()<time(11,30) or time(13)<=now.time()<time(14,57)):
        raise ValueError('模拟成交时段为 9:45–11:30、13:00–14:57（北京时间）')
    if side=='buy' and now.time()>=time(14,45):raise ValueError('14:45后不新增')


def _virtual_config(state):
    positions=[{k:p[k] for k in ('code','name','type','quantity','cost')} for p in state['positions'].values()]
    for p in positions:p['baseline_price']=state['positions'][p['code']]['contest_basis']
    return {'baseline_equity':state['initial_equity'],'max_additional_loss':state['policy']['loss_budget'],'cash':state['cash'],
            'positions':positions,'watchlist':state['universe'],'trade_plans':state['plans'],
            'strategy_v1':{'enabled':True,'version':'1.0',**state['policy']}}


def execute(state,code,side,quantity,quotes,now,reason):
    """Atomic caller persists a successful copied state; invalid orders change nothing."""
    _session(state,now,side)
    if side not in ('buy','sell') or len(str(code))!=6 or not str(code).isdigit():raise ValueError('订单无效')
    qty=number(quantity,1)
    if qty!=int(qty):raise ValueError('数量须为整数')
    qty=int(qty);reason=str(reason).strip()
    if not reason:raise ValueError('必须记录决策依据')
    q=quotes.get(code,{})
    stamp=_source(q,now)
    item=next((x for x in state['universe'] if x['code']==code),None)
    if not item:raise ValueError('不在已记录的研究范围')
    old=state['positions'].get(code)
    if side=='buy' and (not q.get('name') or 'ST' in q['name'].upper() or '退' in q['name']):
        raise ValueError('证券实时名称未核验或有风险警示，不新开仓')
    if qty%100 and not (side=='sell' and old and qty==old['quantity']):raise ValueError('按100股/份整数手执行')
    if q.get('limit_status_verified') is not True:raise ValueError('涨跌停范围未核验')
    upper = _quote_number(q, 'upper_limit', '涨停价未核验')
    lower = _quote_number(q, 'lower_limit', '跌停价未核验')
    if not lower<upper:raise ValueError('涨跌停范围未核验')
    if side=='buy' and (q['price']>=upper or q.get('at_limit_up')):raise ValueError('涨停不假定买得到')
    if side=='sell' and (q['price']<=lower or q.get('at_limit_down')):raise ValueError('跌停不假定卖得出')
    key='ask1' if side=='buy' else 'bid1'
    reference = _quote_number(q, key, '缺少有效的最优档盘口价格')
    book = _quote_number(q, key + '_shares', '缺少有效的最优档可见数量', minimum=1)
    if q.get('bid1') and q.get('ask1') and q['bid1']>q['ask1']:raise ValueError('盘口倒挂')
    if not lower <= reference <= upper:
        raise ValueError('盘口价格超出已核验涨跌停范围')
    depth_cap = math.floor(book * state['fees']['book_fraction'])
    if qty > depth_cap:
        raise ValueError(f'超过最优档10%可见数量，本次盘口上限 {depth_cap:,} 股/份')
    tick=Decimal('.001') if item['type']=='etf' else Decimal('.01')
    raw=Decimal(str(reference))*(Decimal(1)+Decimal(str(state['fees']['slippage_rate']))*(1 if side=='buy' else -1))
    price=float((raw/tick).to_integral_value(rounding=ROUND_CEILING if side=='buy' else ROUND_FLOOR)*tick)
    if not lower<price<upper:raise ValueError('计滑点后触及涨跌停，不模拟成交')
    charge=fees(state,item['type'],side,price*qty)
    slip=money(abs(price-reference)*qty)
    plan=state['plans'].get(code,{})
    initial_risk=None
    if side=='sell':
        if not old or qty>sellable(old,now):raise ValueError('可卖数量不足，未知冻结部分不解锁；新买入按T+1')
    else:
        if old:raise ValueError('不加仓、不摊平')
        if state['paused']:raise ValueError('模拟账户已暂停新增')
        equity,_=valuation(state,quotes,now)
        cfg=_virtual_config(state)
        context={'initialized':True,'verified':True,'needs_reconcile':False,'paused':False,
                 'drawdown':max(0.,money(state['high_water']-equity)),'high_water':max(state['high_water'],equity),
                 'net_external_flow':0.,'adjusted_equity':equity,
                 'opens_today':sum(t['side']=='buy' and t['at'][:10]==str(now.date()) for t in state['trades'])}
        from risk_engine import compute
        from strategy_setup import evaluate_entry
        prices={c:quotes[c]['price'] for c in state['positions']}
        prices[code]=q['price']
        risk=compute(cfg,prices,fresh=True,watch_quotes=[{'code':code,'name':item['name'],'price':q['price']}],strategy_account=context)
        snap={'mode':'live','market_session':True,'calendar_ok':True,'config_hash':hashlib.sha256(json.dumps(cfg,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
              'fetch_finished_at':now.astimezone(timezone.utc).isoformat(),'risk':risk,'strategy_account':context,
              'trading_dates':effective_calendar(state),'quote_health':{'provider_timestamps_verified':True,'block_trade_signals':False}}
        check=evaluate_entry(cfg,code,q,snap,setup=plan.get('setup'),now=now)
        if not check.get('allowed'):raise ValueError('新增未通过：'+'；'.join(check.get('blockers',[])))
        if qty>int(check.get('max_buy_shares') or 0):raise ValueError('超过V1最大股数')
        if price>plan['entry_price'] or not plan['stop_loss']<price:raise ValueError('计滑点后超Ecap或固定止损')
        policy = state['policy']
        initial_risk = money((price - plan['stop_loss']) * qty + max(policy['fee_reserve'], charge['total'] * 3))
        if initial_risk > policy['per_trade_risk']:
            raise ValueError(f"含费用的初始风险超过 {policy['per_trade_risk']:g} 元")
        if state['cash']<price*qty+charge['total']:raise ValueError('现金不足')
    trade={'sequence':len(state['trades'])+1,'mode':'virtual_snapshot_fill','at':now.isoformat(),
           'source_timestamp':stamp.isoformat(),'code':code,'name':item['name'],'side':side,'quantity':qty,
           'price':price,'reference_book_price':reference,'fee':charge,'slippage_estimate':slip,
           'reason':reason,'note':'虚拟估计成交，非交易所回报；手续费/滑点模型已预先记录。'}
    if side=='buy':
        state['cash']=money(state['cash']-price*qty-charge['total'])
        state['positions'][code]={'code':code,'name':item['name'],'type':item['type'],'quantity':qty,
            'cost':price,'contest_basis':price,'remaining_entry_fee':charge['total'],
            'opening_inventory':False,'virtual_bought_on':str(now.date()),'initial_risk':initial_risk,
            'realized_contest_pnl':0.}
        plan.update(buy_date=str(now.date()),actual_entry=price,initial_quantity=qty)
    else:
        allocation=money(old['remaining_entry_fee']*qty/old['quantity'])
        net=money((price-old['contest_basis'])*qty-charge['total']-allocation)
        state['cash']=money(state['cash']+price*qty-charge['total'])
        old['quantity']-=qty;old['remaining_entry_fee']=money(old['remaining_entry_fee']-allocation)
        old['realized_contest_pnl']=money(old['realized_contest_pnl']+net)
        if old.get('opening_inventory'):old['opening_available']-=qty
        trade['net_realized_contest_pnl']=net
        if not old['quantity']:
            risk=old.get('initial_risk');total=old['realized_contest_pnl']
            state['closed_trades'].append({'code':code,'closed_at':now.isoformat(),'net_pnl':total,
                'initial_risk':risk,'net_r':round(total/risk,4) if risk else None,
                'note':'开赛旧仓初始风险未知，不补造R' if not risk else '新虚拟交易的固定初始风险'})
            del state['positions'][code]
            if len(state['closed_trades'])>=3 and all(t['net_pnl']<0 for t in state['closed_trades'][-3:]):state['paused']=True
    state['trades'].append(trade)
    return trade


def preview_trade(state, code, side, quantity, quotes, now, reason):
    """Project one requested fill using current execution rules on a copy."""
    holding = state['positions'].get(code)
    preview = {
        'allowed': False,
        'code': code,
        'side': side,
        'requested_quantity': quantity,
        'fillable_quantity': 0,
        'checked_at': now.isoformat(),
        'cash_before': state['cash'],
        'position_before': holding['quantity'] if holding else 0,
        'sellable_before': sellable(holding, now) if holding else 0,
    }
    projected = deepcopy(state)
    try:
        fill = execute(projected, code, side, quantity, quotes, now, reason)
    except ValueError as error:
        return {**preview, 'error': str(error)}
    holding_after = projected['positions'].get(code)
    return {
        **preview,
        'allowed': True,
        'name': fill['name'],
        'fillable_quantity': fill['quantity'],
        'quote_asof': fill['source_timestamp'],
        'price': fill['price'],
        'reference_book_price': fill['reference_book_price'],
        'notional': money(fill['price'] * fill['quantity']),
        'fee': fill['fee'],
        'slippage_estimate': fill['slippage_estimate'],
        'cash_after': projected['cash'],
        'cash_delta': money(projected['cash'] - state['cash']),
        'position_after': holding_after['quantity'] if holding_after else 0,
        'sellable_after': sellable(holding_after, now) if holding_after else 0,
        'realized_contest_pnl': fill.get('net_realized_contest_pnl'),
        'reason': fill['reason'],
    }


def fetch_quotes(state):
    from portfolio_quotes import snapshot_selected_tencent
    cfg={'positions':list(state['positions'].values()),'watchlist':state['universe']}
    feed,errors=snapshot_selected_tencent(cfg,deadline_seconds=7.5)
    quotes={}
    for (kind,code),q in feed.items():
        quotes[code]={'price':q[0],'name':q[1],'previous_close':q[2],'source_timestamp':q[3],**(q[4] if len(q)>4 else {})}
    return quotes,errors


def prepare(state,code,sector,review,now):
    from asset_history import history_by_asset,benchmark_history
    from strategy_setup import build_setup
    from suggested_exit import build_exit_suggestion
    item=next((x for x in state['universe'] if x['code']==code),None)
    if not item or not sector.strip():raise ValueError('研究范围或行业未核验')
    if not isinstance(review,dict) or not review.get('sources') or review.get('checked_on')!=str(now.date()):raise ValueError('需要当日复核记录和真实来源链接')
    begin=(now.date().replace(year=now.year-1)).strftime('%Y%m%d')
    history=history_by_asset(code,item['type'],begin)
    position=state['positions'].get(code)
    if position:
        result=build_exit_suggestion(history,position,trading_dates=effective_calendar(state),now=now,existing_plan=state['plans'].get(code))
        if not result.get('adoptable'):raise ValueError(result.get('reason') or '旧仓退出条件待核验')
        plan={**result['plan'],'sector':sector,'review_sources':review['sources']}
        state['plans'][code]=plan;return plan
    required=['fundamentals_ok','event_clear','corporate_action_clear','resistance_clear']
    if any(review.get(k) is not True for k in required):raise ValueError('财务/基金、公告、除权与压力空间不得自动勾选')
    setup=build_setup(history,benchmark_history(begin),item['type'],item['name'],now=now)
    if not setup.get('eligible'):raise ValueError('；'.join(setup.get('blockers',[])))
    following=[d for d in effective_calendar(state) if d>setup['signal_date']]
    if not following:raise ValueError('下一交易日未核验')
    plan={**setup['levels'],'strategy_version':'1.0','setup_verified':True,'legacy_exit_only':False,
          'signal_date':setup['signal_date'],'valid_on':following[0],'sector':sector,'setup':setup,
          'review':{k:True for k in required},'review_sources':review['sources']}
    state['plans'][code]=plan
    return plan


def report(state):
    net=money(state['last_equity']-state['initial_equity'])
    period = f"持续模拟：{state['start']} 起，按已核验交易日开放。" if continuation_active(state) else f"比赛：{state['start']} 至 {state['end']}。"
    lines=['# 模拟账户账本','',period,
           f"初始净值：{state['initial_equity']:,.2f}；最近估值：{state['last_equity']:,.2f}；比赛净盈亏：{net:+,.2f}。",
           f"估值源时间：{state['last_valuation_asof']}；开赛前历史总盈亏另存，不计入比赛。",
           f"现金：{state['cash']:,.2f}；虚拟成交：{len(state['trades'])}笔；最大观察回撤：{state['maximum_drawdown']:,.2f}。",
           '', '|证券|代码|数量|开赛/虚拟成本|当前可卖上限|','|---|---|---:|---:|---:|']
    for c,p in state['positions'].items():lines.append(f"|{p['name']}|{c}|{p['quantity']}|{p['cost']:.3f}|{sellable(p,clock_now())}|")
    lines+=['','## 成本模型',state['fees']['note'],'','## 固定约定',*['- '+s for s in state['notes']]]
    if continuation_active(state):
        lines+=['','## 持续运行',
                f"自 {state['continuation']['activated_at']} 取消结束日期。后续按已核验交易日及模拟成交时段操作。",
                f"授权依据：{state['continuation']['reason']}"]
    lines+=['','## 最近虚拟成交']
    for t in state['trades'][-20:]:lines.append(f"- {t['at']} {t['side']} {t['code']} × {t['quantity']} @ {t['price']}，费用{t['fee']['total']:.2f}，额外滑点估算{t['slippage_estimate']:.2f}；{t['reason']}")
    if not state['trades']:lines.append(f"尚无虚拟成交。首个可操作日为 {state['start']}。")
    lines+=['','## 实盘比较','期末须使用用户提供的收盘净值和已确认净转账；缺失时不判胜负。']
    for r in state['real_updates']:
        pnl=money(r['equity']-state['initial_equity']-r['net_external_flow'])
        lines.append(f"- {r['asof']} 实盘净值{r['equity']:,.2f}，比赛期净转账{r['net_external_flow']:+,.2f}，现金流调整净盈亏{pnl:+,.2f}。")
    return '\n'.join(lines)+'\n'


def _write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n');temporary.replace(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store',type=Path,default=DEFAULT_STORE)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('init');p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--calendar',type=Path,required=True)
    p.add_argument('--start',default='2026-11-13');p.add_argument('--end',default='2026-11-19')
    sub.add_parser('status');p=sub.add_parser('observe');p.add_argument('--closing',action='store_true')
    p=sub.add_parser('continue');p.add_argument('--calendar',type=Path,required=True);p.add_argument('--reason',required=True)
    p=sub.add_parser('prepare');p.add_argument('code');p.add_argument('--sector',required=True);p.add_argument('--review',type=Path,required=True)
    p=sub.add_parser('trade');p.add_argument('side',choices=['buy','sell']);p.add_argument('code');p.add_argument('quantity',type=int);p.add_argument('--reason',required=True)
    p=sub.add_parser('real-update');p.add_argument('--equity',type=float,required=True);p.add_argument('--net-external-flow',type=float,required=True);p.add_argument('--asof',required=True)
    p=sub.add_parser('confirm-availability');p.add_argument('code');p.add_argument('quantity',type=int);p.add_argument('--evidence',required=True)
    args=parser.parse_args();store=args.store.resolve();store.mkdir(parents=True,exist_ok=True)
    with (store/'paper.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);path=store/'state.json';now=clock_now()
        if args.action=='init':
            if path.exists():raise ValueError('已存在模拟账本，不允许重新初始化或清零收益')
            snapshot=json.loads(args.snapshot.read_text());calendar=json.loads(args.calendar.read_text())
            if calendar.get('verified') is not True:raise ValueError('交易日历未核验')
            state=initialize(snapshot,calendar['dates'],args.start,args.end,now=now)
            output={'initialized':True,'initial_equity':state['initial_equity'],'holdings':len(state['positions']),'first_allowed_trade':state['start']}
        else:
            state=json.loads(path.read_text());validate_state(state);working=deepcopy(state)
            refresh_continuation_calendar(working,store,now)
            if args.action=='status':output={'initial_equity':state['initial_equity'],'last_equity':state['last_equity'],'cash':state['cash'],'positions':state['positions'],'trades':state['trades'],'notes':state['notes']}
            elif args.action=='continue':
                output=continue_account(working,json.loads(args.calendar.read_text()),args.reason,now)
            elif args.action=='confirm-availability':
                position=working['positions'].get(args.code)
                if not position or not position.get('opening_inventory') or not 0<=args.quantity<=position['quantity'] or not args.evidence.strip():raise ValueError('只能按用户明确确认更新剩余期初库存可卖数，并记录依据')
                event={'at':now.isoformat(),'kind':'opening_availability_confirmed','code':args.code,
                       'before':position['opening_available'],'after':args.quantity,'evidence':args.evidence}
                position['opening_available']=args.quantity;working['events'].append(event);output=event
            elif args.action=='real-update':
                moment=datetime.fromisoformat(args.asof)
                if not moment.tzinfo or not date_allowed(working,str(moment.astimezone(CN).date())) or moment>now:raise ValueError('实盘对账时间无效')
                working['real_updates'].append({'asof':args.asof,'equity':number(args.equity,.01),'net_external_flow':number(args.net_external_flow),'recorded_at':now.isoformat()});output=working['real_updates'][-1]
            elif args.action=='prepare':output=prepare(working,args.code,args.sector,json.loads(args.review.read_text()),now)
            else:
                quotes,errors=fetch_quotes(state)
                if args.action=='observe':output=observe(working,quotes,now,closing=args.closing)
                else:
                    output=execute(working,args.code,args.side,args.quantity,quotes,now,args.reason)
                    try:observe(working,quotes,now)
                    except ValueError as exc:
                        working['events'].append({'at':now.isoformat(),'kind':'valuation_pending','reason':str(exc)})
                        output['valuation_pending']=str(exc)
                output={**output,'quote_errors':errors}
            state=working
        validate_state(state);_write(path,state);(store/'REPORT.md').write_text(report(state))
        print(json.dumps(output,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
