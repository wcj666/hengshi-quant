"""Read-only projection of the private paper ledger, with no quote/order I/O."""
from datetime import datetime
import json
from pathlib import Path
from paper_contest import CN, validate_state, sellable, money
from pnl_engine import valid_price
from quote_health import parse_provider_time


def daily_baseline(state, day):
    previous = max((date for date in state['calendar'] if date < day), default=None)
    if not previous:
        return None
    for record in reversed(state['observations']):
        if record.get('closing') is not True:
            continue
        source = parse_provider_time(record.get('source_asof'))
        value = valid_price(record.get('equity'))
        if source and value:
            source = source.astimezone(CN)
            if source.date().isoformat() == previous and source.hour >= 15:
                return value, source, '上个交易日收盘净值'
    opening = parse_provider_time(state['opening_snapshot'].get('asof'))
    if opening and day == state['start']:
        opening = opening.astimezone(CN)
        if opening.date().isoformat() == previous and opening.hour >= 15:
            return state['initial_equity'], opening, '开赛快照'
    return None


def day_trade_totals(trades, day):
    result = {}
    for fill in trades:
        executed = parse_provider_time(fill.get('at'))
        if not executed or executed.astimezone(CN).date().isoformat() != day:
            continue
        qty, cash = result.get(fill['code'], (0, 0.0))
        delta = fill['quantity'] if fill['side'] == 'buy' else -fill['quantity']
        result[fill['code']] = qty + delta, cash - delta * fill['price'] - fill['fee']['total']
    return result


def daily_position_pnl(row, previous_close, trade_totals):
    change, cash = trade_totals.get(row['code'], (0, 0.0))
    opening_quantity = row['quantity'] - change
    if opening_quantity < 0 or (opening_quantity and previous_close is None):
        return None
    return money(row['value'] - opening_quantity * (previous_close or 0) + cash)


def load_snapshot(store,now=None):
    state=json.loads((Path(store)/'state.json').read_text(encoding='utf-8'))
    validate_state(state)
    now=now or datetime.now(CN)
    opening=state['opening_snapshot'];marks=state.get('last_position_marks',{})
    rows=[]
    for code,p in state['positions'].items():
        mark=marks.get(code,{})
        if mark.get('quantity')!=p['quantity']:mark={}
        if not mark and not state['trades']:
            original=next((x for x in opening['positions'] if x['code']==code),{})
            mark={'price':original.get('screenshot_price'),'value':original.get('market_value'),
                  'source_timestamp':opening['asof']}
        price=mark.get('price');value=mark.get('value')
        rows.append({'code':code,'name':p['name'],'type':p['type'],'quantity':p['quantity'],
                     'sellable':sellable(p,now),'cost':p['cost'],'contest_basis':p['contest_basis'],
                     'price':price,'value':value,'source_timestamp':mark.get('source_timestamp'),
                     'contest_pnl':money((price-p['contest_basis'])*p['quantity']-p['remaining_entry_fee']) if price is not None else None,
                     'opening_inventory':bool(p.get('opening_inventory')),
                     'stop_loss':state['plans'].get(code,{}).get('stop_loss')})
    asof=state['last_valuation_asof'];stamp=datetime.fromisoformat(asof.replace('Z','+00:00')).astimezone(CN)
    age=(now-stamp).total_seconds();day=str(now.date())
    before=day<state['start'];ended=day>state['end']
    stale=stamp.date()!=now.date() or age<0 or age>90
    if state.get('last_mark_closing') and stamp.date()==now.date() and stamp.hour>=15:
        stale=False
    label='开赛快照' if before else '已结束 · 最近估值' if ended else '等待更新估值' if stale else '收盘估值' if state.get('last_mark_closing') else '盘中估值'
    from paper_runtime import read_status, refresh_phase
    phase,phase_message=refresh_phase(state,now)
    if not before and not ended and phase=='idle' and not state.get('last_mark_closing'):
        label=phase_message+' · 最近估值'
    if not state.get('last_mark_closing') and now.hour>=15 and phase!='idle':
        stale=True;label='等待当日完整收盘估值'
    pnl=money(state['last_equity']-state['initial_equity'])
    # Source clocks can differ slightly. Never put old quantities/prices next
    # to a new post-trade portfolio and call that a current complete valuation.
    complete=(all(marks.get(r['code'],{}).get('quantity')==r['quantity'] for r in rows)
              if marks else not rows or not state['trades'] and asof==opening['asof'])
    if state['trades']:
        observed = parse_provider_time(state['observations'][-1].get('at')) if state['observations'] else None
        executed = parse_provider_time(state['trades'][-1].get('at'))
        complete = bool(complete and observed and executed and observed >= executed)
    if not complete:stale=True;label='等待完整持仓估值'
    exposure=money(sum(r['value'] for r in rows)) if complete else None
    valuation_day = stamp.date().isoformat()
    in_contest = state['start'] <= valuation_day <= state['end'] and valuation_day in state['calendar']
    usable = bool(complete and in_contest and age >= 0 and (not stale or phase == 'idle'))
    baseline = daily_baseline(state, valuation_day) if usable else None
    daily = {'date': valuation_day if in_contest else None, 'pnl': None, 'return_pct': None,
             'basis_asof': None, 'basis_label': None,
             'message': '等待有效行情' if not usable else '缺少上个交易日收盘净值'}
    if baseline:
        baseline_value, baseline_stamp, basis_label = baseline
        daily_pnl = money(state['last_equity'] - baseline_value)
        daily.update(pnl=daily_pnl,return_pct=round(daily_pnl/baseline_value*100,4),
                     basis_asof=baseline_stamp.isoformat(),basis_label=basis_label,message=None)
    trade_totals = day_trade_totals(state['trades'], valuation_day) if usable else {}
    for row in rows:
        mark = marks.get(row['code'], {})
        previous_close = valid_price(mark.get('previous_close')) if usable else None
        row['previous_close'] = previous_close
        row['daily_pnl'] = (daily_position_pnl(row, previous_close, trade_totals)
                            if usable and row['value'] is not None else None)
    return {'mode':'paper_only','start':state['start'],'end':state['end'],'label':label,
            'before_start':before,'ended':ended,'stale':stale,'asof':asof,
            'source':'开赛券商截图' if asof==opening['asof'] else '腾讯公开行情 · 提供方时间',
            'initial_equity':state['initial_equity'],'equity':state['last_equity'],'cash':state['cash'],
            'net_pnl':pnl,'return_pct':round(pnl/state['initial_equity']*100,4),
            'daily':daily,
            'exposure':exposure,'exposure_pct':round(exposure/state['last_equity']*100,2) if exposure is not None else None,
            'maximum_drawdown':state['maximum_drawdown'],'paused':state['paused'],
            'positions':rows,'trade_count':len(state['trades']),'trades':state['trades'][-100:],
            'refresh':read_status(store),'phase':phase,'phase_message':phase_message,
            'fees':state['fees'],'notes':state['notes'],
            'opening_broker_total_pnl':opening.get('broker_total_pnl'),
            'real_updates':state.get('real_updates',[])}
