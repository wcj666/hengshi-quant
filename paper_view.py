"""Read-only projection of the private paper ledger, with no quote/order I/O."""
from datetime import datetime
import json
from pathlib import Path
from paper_contest import CN, validate_state, sellable, money


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
    if not complete:stale=True;label='等待完整持仓估值'
    exposure=money(sum(r['value'] for r in rows)) if complete else None
    return {'mode':'paper_only','start':state['start'],'end':state['end'],'label':label,
            'before_start':before,'ended':ended,'stale':stale,'asof':asof,
            'source':'开赛券商截图' if asof==opening['asof'] else '腾讯公开行情 · 提供方时间',
            'initial_equity':state['initial_equity'],'equity':state['last_equity'],'cash':state['cash'],
            'net_pnl':pnl,'return_pct':round(pnl/state['initial_equity']*100,4),
            'exposure':exposure,'exposure_pct':round(exposure/state['last_equity']*100,2) if exposure is not None else None,
            'maximum_drawdown':state['maximum_drawdown'],'paused':state['paused'],
            'positions':rows,'trade_count':len(state['trades']),'trades':state['trades'][-100:],
            'refresh':read_status(store),'phase':phase,'phase_message':phase_message,
            'fees':state['fees'],'notes':state['notes'],
            'opening_broker_total_pnl':opening.get('broker_total_pnl'),
            'real_updates':state.get('real_updates',[])}
