"""Local T-observation journals. Actual fills are user supplied, never executed here."""
from datetime import datetime
from pathlib import Path
import secrets
import threading
import strategy_runtime as storage

CST=storage.CST
_LOCK=threading.Lock()
_LAST={}


def settings(cfg):
    raw=cfg.get('t_settings',{})
    return {'enabled':raw.get('enabled') is True,'mode':'sell_first',
            'fee_reserve':max(20.,storage.number(raw.get('fee_reserve',20),0,100000)),
            'max_fraction':min(1/3,storage.number(raw.get('max_fraction',1/3),0,1)),
            'max_cycles_per_code':1,'last_open_time':'14:45',
            'note':'仅条件观察；不自动下单。先买后卖暂不给可执行提示，不能使用当日新买股票充当可卖底仓。'}


def ledger(output,cfg,now=None):
    now=now or datetime.now(CST)
    data=storage.read(Path(output)/'t_ledger.json',{'availability':{},'cycles':[]})
    if not isinstance(data,dict) or not isinstance(data.get('availability'),dict) or not isinstance(data.get('cycles'),list):
        raise ValueError('做T日志损坏，暂停做T复核，请恢复备份')
    positions={str(p['code']):p for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0}
    for cycle in data['cycles']:
        code=cycle['code'];p=positions.get(code,{})
        expected=int(cycle['base_quantity'])-int(cycle['quantity'])+int(cycle.get('buyback_quantity',0))
        cycle['expected_quantity']=expected
        cycle['ledger_sync_required']=int(p.get('quantity',0))!=expected
        cycle['expired']=cycle['date']!=str(now.date())
        paired=int(cycle.get('buyback_quantity',0));cost=float(cycle.get('buyback_cost',0))
        cycle['unbought_quantity']=int(cycle['quantity'])-paired
        cycle['paired_gross_estimate']=round(paired*float(cycle['sell_price'])-cost,2) if paired else None
        cycle['paired_net_estimate']=round(cycle['paired_gross_estimate']-float(cycle['fee_reserve']),2) if paired else None
        # An unmatched sale is a reduction of holdings, not an earned T spread.
    return data


def _save(output,data):
    data['revision']=secrets.token_hex(12)
    storage.write(Path(output)/'t_ledger.json',data)


def _position(cfg,code):
    code=str(code).strip()
    p=next((x for x in cfg.get('positions',[]) if str(x['code'])==code and int(x.get('quantity',0))>0),None)
    if not p or p.get('type') not in ('stock','etf'):raise ValueError('只支持本地持仓中的股票或股票ETF')
    return p


def _quantity(value):
    q=storage.number(value,1,100000000)
    if q!=int(q):raise ValueError('实际成交数量需为正整数；机会提示仍按100股/份取整')
    return int(q)


def confirm_available(output,cfg,body,now=None):
    now=now or datetime.now(CST);p=_position(cfg,body.get('code'));code=str(p['code'])
    available=storage.number(body.get('available_quantity'),0,int(p['quantity']))
    if available!=int(available):raise ValueError('可卖数量需为整数，以券商今日显示为准')
    with storage.locked(output):
        data=ledger(output,cfg,now)
        if any(x['code']==code and x['date']==str(now.date()) and x['status']=='open' for x in data['cycles']):
            raise ValueError('本轮T尚未结束，保留原底仓基准；请先核对持仓与成交日志')
        row={'date':str(now.date()),'available_quantity':int(available),'base_quantity':int(p['quantity']),
             'confirmed_at':now.isoformat(),'name':p['name']}
        data['availability'][code]=row;_save(output,data)
    return row


def record_sell(output,cfg,body,now=None):
    now=now or datetime.now(CST);today=str(now.date())
    if body.get('sell_filled') is not True:raise ValueError('仅登记已在券商实际成交的做T卖出；计划或普通减仓不能登记为T成交')
    p=_position(cfg,body.get('code'));code=str(p['code']);qty=_quantity(body.get('quantity'))
    price=storage.number(body.get('sell_price'),0.0001,1e7)
    with storage.locked(output):
        data=ledger(output,cfg,now);available=data['availability'].get(code,{})
        if available.get('date')!=today:raise ValueError('先确认今日券商可卖数量；历史可卖数不能沿用')
        if any(x['code']==code and x['date']==today for x in data['cycles']):raise ValueError('同一证券每天只登记一轮T；不要重复登记同一笔卖出')
        base=int(available['base_quantity']);cap=int(base*settings(cfg)['max_fraction']//100)*100
        if qty>int(available['available_quantity']) or qty>cap:raise ValueError('超过今日确认可卖数或T仓上限（最多原底仓约1/3，至少保留2/3底仓）；其他减仓请登记普通成交')
        if int(p['quantity']) not in (base,base-qty):raise ValueError('持仓数量已发生其他变化，重新核对底仓后再登记')
        cycle={'id':secrets.token_hex(10),'code':code,'name':p['name'],'date':today,'quantity':qty,
               'sell_price':price,'base_quantity':base,'buyback_quantity':0,'buyback_cost':0.,'status':'open',
               'fee_reserve':settings(cfg)['fee_reserve'],'recorded_at':now.isoformat(),'fills':[]}
        data['cycles'].append(cycle);_save(output,data)
    return cycle


def record_buyback(output,cfg,cycle_id,body,now=None):
    now=now or datetime.now(CST)
    if body.get('buy_filled') is not True:raise ValueError('只登记已经实际成交的接回，不把触价当成交')
    qty=_quantity(body.get('quantity'));price=storage.number(body.get('price'),0.0001,1e7)
    with storage.locked(output):
        data=ledger(output,cfg,now);cycle=next((x for x in data['cycles'] if x['id']==cycle_id),None)
        if not cycle or cycle['status']!='open':raise ValueError('未找到待接回的实际T卖出')
        if cycle['date']!=str(now.date()):raise ValueError('本轮T已跨日，不沿用昨日接回计划；请按普通成交记录并重新评估')
        remaining=int(cycle['quantity'])-int(cycle['buyback_quantity'])
        if qty>remaining:raise ValueError('接回数量不能超过本轮实际卖出后尚未接回的数量')
        cycle['fills'].append({'quantity':qty,'price':price,'at':now.isoformat()})
        cycle['buyback_quantity']+=qty;cycle['buyback_cost']=round(cycle['buyback_cost']+qty*price,4)
        if cycle['buyback_quantity']==cycle['quantity']:cycle['status']='closed';cycle['closed_at']=now.isoformat()
        _save(output,data)
    return cycle


def close_cycle(output,cfg,cycle_id,body,now=None):
    if body.get('leave_cash') is not True:raise ValueError('请明确选择保留卖出现金；这不会自动买回')
    now=now or datetime.now(CST)
    with storage.locked(output):
        data=ledger(output,cfg,now);cycle=next((x for x in data['cycles'] if x['id']==cycle_id),None)
        if not cycle or cycle['status']!='open':raise ValueError('未找到待结束的T记录')
        cycle.update(status='cash',closed_at=now.isoformat());_save(output,data)
    return cycle


def refresh_minutes_async(output,cfg,now=None):
    if not settings(cfg)['enabled']:return
    now=now or datetime.now(CST);key=str(Path(output).resolve())
    prior=_LAST.get(key)
    if prior and (now-prior).total_seconds()<45:return
    if not _LOCK.acquire(False):return
    _LAST[key]=now
    def run():
        try:
            from t_quotes import fetch_t_minutes
            minutes=fetch_t_minutes(cfg)
            storage.write(Path(output)/'t_minutes.json',{'fetched_at':datetime.now(CST).isoformat(),'by_code':minutes})
        except Exception as exc:
            storage.write(Path(output)/'t_minutes.json',{'fetched_at':datetime.now(CST).isoformat(),'by_code':{},'error':type(exc).__name__})
        finally:_LOCK.release()
    threading.Thread(target=run,daemon=True).start()


def observe(output,cfg,quotes,risk,*,now=None,market_session=False,calendar_ok=False):
    from t_engine import build_t_observations
    now=now or datetime.now(CST)
    if not settings(cfg)['enabled']:return {'enabled':False,'rows':[],'signals':[],'status':'做T观察未启用'}
    try:
        book=ledger(output,cfg,now);cache=storage.read(Path(output)/'t_minutes.json')
        minutes=cache.get('by_code',{})
        result=build_t_observations(cfg,quotes,risk,minutes,book,now=now,market_session=market_session,calendar_ok=calendar_ok)
        result.update(enabled=True,asof=now.isoformat(),settings=settings(cfg),cycles=book['cycles'][-30:],availability=book['availability'],ledger_revision=book.get('revision'),
                      status='做T条件观察；不自动交易。普通风险减仓不会产生接回提醒。')
        return result
    except Exception as exc:
        return {'enabled':True,'asof':now.isoformat(),'rows':[],'signals':[],'settings':settings(cfg),'status':'做T数据待核验：'+type(exc).__name__}
