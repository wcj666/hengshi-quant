"""Local T-observation journals. Actual fills are user supplied, never executed here."""
from datetime import datetime, time
from pathlib import Path
import hashlib
import json
import secrets
import threading
import strategy_runtime as storage
from strategy_defaults import T_DEFAULTS
from quote_health import parse_provider_time
from t_costs import money, nonnegative, summarize_cycle_costs
from t_engine import T_CUTOFF, _settings as signal_settings

CST=storage.CST
_LOCK=threading.Lock()
_LAST={}


def settings(cfg, book=None):
    raw = cfg.get('t_settings')
    enabled = isinstance(raw, dict) and raw.get('enabled') is True
    return {**T_DEFAULTS, **signal_settings(cfg), 'enabled':enabled,
            'fee_model':(book or {}).get('fee_model',{}),
            'note':'仅条件观察；不自动下单。先买后卖暂不给可执行提示，不能使用当日新买股票充当可卖底仓。'}


def ledger(output,cfg,now=None):
    now=now or datetime.now(CST)
    data=storage.read(Path(output)/'t_ledger.json',{'availability':{},'cycles':[]})
    if not isinstance(data,dict) or not isinstance(data.get('availability'),dict) or not isinstance(data.get('cycles'),list):
        raise ValueError('做T日志损坏，暂停做T复核，请恢复备份')
    positions={str(p['code']):p for p in cfg.get('positions',[]) if int(p.get('quantity',0))>0}
    # Reuse user-supplied, internally reconciled screenshots only on their own
    # trading date. This is inventory evidence, never a broker live connection.
    from broker_snapshot import latest_broker_snapshot
    snapshot=latest_broker_snapshot(output,cfg)
    stamp=parse_provider_time((snapshot or {}).get('asof'))
    data['availability']={code:row for code,row in data['availability'].items()
                          if code in positions and row.get('source')!='broker_screenshot'}
    if stamp and stamp.astimezone(CST).date()==now.astimezone(CST).date() and stamp<=now:
        for item in snapshot['positions']:
            code=item['code'];value=item.get('available_quantity')
            if isinstance(value,bool) or not isinstance(value,int) or not 0<=value<=item['quantity']:continue
            if any(c.get('code')==code and c.get('date')==str(now.date()) for c in data['cycles']):continue
            if any(str(f.get('code'))==code and (parse_provider_time(f.get('executed_at')) or stamp)>stamp
                   for f in cfg.get('reconciled_trades',[])):continue
            old=data['availability'].get(code,{})
            prior=parse_provider_time(old.get('confirmed_at'))
            if prior and prior>=stamp:continue
            data['availability'][code]={'date':str(stamp.astimezone(CST).date()),
                'available_quantity':value,'base_quantity':item['quantity'],'confirmed_at':stamp.isoformat(),
                'name':item['name'],'source':'broker_screenshot'}
    from trade_reconciliation import matches_receipt
    receipt = cfg.get('trade_reconciliation') or {}
    checked_at = parse_provider_time(receipt.get('broker_asof'))
    if (matches_receipt(cfg, receipt) and checked_at and checked_at <= now
            and checked_at.astimezone(CST).date() == now.astimezone(CST).date()):
        for code, checked in receipt.get('positions', {}).items():
            p = positions.get(code)
            available = checked.get('available_quantity')
            if not p or checked.get('quantity') != p['quantity'] or type(available) is not int or not 0 <= available <= p['quantity']:
                continue
            old = data['availability'].get(code) or {}
            prior = parse_provider_time(old.get('confirmed_at'))
            if prior and prior > checked_at:
                continue
            data['availability'][code] = {'date': str(checked_at.astimezone(CST).date()),
                'available_quantity': available, 'base_quantity': p['quantity'],
                'confirmed_at': checked_at.isoformat(), 'name': p['name'], 'source': 'broker_balance'}
    # Evidence changes must invalidate previously computed T observations even
    # when quantities/cost/cash happen to be unchanged.
    data['revision']=hashlib.sha256(json.dumps([data.get('revision'),data['availability']],
        sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    for cycle in data['cycles']:
        code=cycle['code'];p=positions.get(code,{})
        expected=int(cycle['base_quantity'])-int(cycle['quantity'])+int(cycle.get('buyback_quantity',0))
        cycle['expected_quantity']=expected
        cycle['ledger_sync_required']=int(p.get('quantity',0))!=expected
        cycle['expired']=cycle['date']!=str(now.date())
        paired=int(cycle.get('buyback_quantity',0))
        cycle['unbought_quantity']=int(cycle['quantity'])-paired
        cycle.update(summarize_cycle_costs(cycle))
        from t_engine import closeout_summary
        cycle['closeout'] = closeout_summary(cycle, data.get('fee_model'), fee_samples(cfg, data))
    return data


def _save(output,data):
    data['revision']=secrets.token_hex(12)
    storage.write(Path(output)/'t_ledger.json',data)


def save_fee_model(output, cfg, body):
    """Save fee assumptions independently of the actual portfolio ledger."""
    kind = body.get('kind')
    if kind not in ('stock', 'etf'):
        raise ValueError('费率需选择股票或股票ETF')
    updated = {}
    for key, maximum in (('commission_per_10k', 30), ('minimum_commission', 100_000)):
        value = body.get(key)
        amount = nonnegative(value)
        if value not in (None, '') and (amount is None or amount > maximum):
            raise ValueError('佣金或最低费用超出有效范围')
        updated[key] = amount
    with storage.locked(output):
        book = ledger(output, cfg)
        model = book.setdefault('fee_model', {})
        model[kind] = updated
        _save(output, book)
    return model


def fee_samples(cfg, book):
    """Use recent confirmed fees, with explicitly linked fills counted once."""
    kinds = {str(p['code']):p.get('type') for p in cfg.get('positions', [])}
    samples = []
    seen = set()

    def add(kind, side, qty, price, fee):
        actual, quantity, unit_price = map(nonnegative, (fee, qty, price))
        if actual is not None and quantity and unit_price and kind in ('stock', 'etf') and side in ('buy', 'sell'):
            samples.append({'kind':kind,'side':side,'notional':quantity*unit_price,'fee':actual})

    for fill in cfg.get('reconciled_trades', [])[-120:]:
        add(fill.get('asset_type') or fill.get('type') or kinds.get(str(fill.get('code'))),
            fill.get('side'),fill.get('quantity'),fill.get('price'),fill.get('fee'))
        seen.add(str(fill.get('id') or fill.get('fill_id') or ''))
    for cycle in book.get('cycles', [])[-30:]:
        kind = cycle.get('kind') or kinds.get(str(cycle.get('code')))
        if cycle.get('sell_fill_id') not in seen:
            add(kind, 'sell', cycle['quantity'], cycle['sell_price'], cycle.get('sell_fee'))
        for fill in cycle.get('fills', []):
            if fill.get('broker_fill_id') not in seen:
                add(kind, 'buy', fill['quantity'], fill['price'], fill.get('fee'))
    return samples


def _position(cfg,code):
    code=str(code).strip()
    p=next((x for x in cfg.get('positions',[]) if str(x['code'])==code and int(x.get('quantity',0))>0),None)
    if not p or p.get('type') not in ('stock','etf'):raise ValueError('只支持本地持仓中的股票或股票ETF')
    return p


def _quantity(value):
    q=storage.number(value,1,100000000)
    if q!=int(q):raise ValueError('实际成交数量需为正整数；机会提示仍按100股/份取整')
    return int(q)


def _fee(value):
    if value in (None, ''):
        return None
    amount = nonnegative(value)
    if amount is None or amount > 1e8 or money(amount) != amount:
        raise ValueError('实际费用须为非负金额，最多保留两位小数')
    return amount


def _confirmed_fill(cfg, data, body, code, day, side):
    requested = body.get('broker_fill_id')
    if requested in (None, ''):
        return None, None
    if not isinstance(requested, str) or len(requested) > 180:
        raise ValueError('已对账成交编号无效')
    requested = requested.strip()
    if not requested:
        raise ValueError('已对账成交编号无效')
    candidates = [fill for fill in cfg.get('reconciled_trades', [])
                  if requested in (str(fill.get('id') or ''), str(fill.get('fill_id') or ''))
                  and str(fill.get('code')) == code and fill.get('date') == day and fill.get('side') == side]
    if len(candidates) != 1:
        raise ValueError('未匹配到唯一的当日同证券同方向已对账成交')
    fill = candidates[0]
    identifier = str(fill.get('id') or fill.get('fill_id'))
    if any(cycle.get('sell_fill_id') == identifier or
           any(leg.get('broker_fill_id') == identifier for leg in cycle.get('fills', []))
           for cycle in data['cycles']):
        raise ValueError('该已对账成交已关联做T，不重复登记')
    return fill, identifier


def _fill_fields(body, fill, price_key):
    if fill is None:
        return _quantity(body.get('quantity')), storage.number(body.get(price_key),0.0001,1e7), _fee(body.get('fee'))
    return _quantity(fill['quantity']), storage.number(fill['price'],0.0001,1e7), _fee(fill.get('fee'))


def _check_linked_quantity(fill, before, after):
    if (fill.get('quantity_before'), fill.get('quantity_after')) != (before, after):
        raise ValueError('该成交前后持仓与本轮底仓不一致，请核对是否关联了正确成交')


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
    p=_position(cfg,body.get('code'));code=str(p['code'])
    with storage.locked(output):
        data=ledger(output,cfg,now);available=data['availability'].get(code,{})
        fill,fill_id=_confirmed_fill(cfg,data,body,code,today,'sell')
        qty,price,fee=_fill_fields(body,fill,'sell_price')
        if any(x['code']==code and x['date']==today for x in data['cycles']):raise ValueError('同一证券每天只登记一轮T；不要重复登记同一笔卖出')
        if fill:
            base=_quantity(fill.get('quantity_before'))
            _check_linked_quantity(fill,base,base-qty)
        else:
            if available.get('date')!=today:raise ValueError('先确认今日券商可卖数量；历史可卖数不能沿用')
            base=int(available['base_quantity'])
            if qty>int(available['available_quantity']):raise ValueError('超过今日确认的可卖数量')
            if int(p['quantity']) not in (base,base-qty):raise ValueError('持仓数量已发生其他变化，重新核对底仓后再登记')
        cap=int(base*settings(cfg)['max_fraction']//100)*100
        if qty>cap:raise ValueError('超过T仓上限（最多原底仓约1/3，至少保留2/3底仓）；其他减仓请登记普通成交')
        cycle={'id':secrets.token_hex(10),'code':code,'name':p['name'],'kind':p['type'],'date':today,'quantity':qty,
               'sell_price':price,'base_quantity':base,'buyback_quantity':0,'buyback_cost':0.,'status':'open',
               'sell_fee':fee,'sell_fill_id':fill_id,'sold_at':fill.get('executed_at') if fill else None,
               'fee_reserve':settings(cfg)['fee_reserve'],'recorded_at':now.isoformat(),'fills':[]}
        cycle.update(summarize_cycle_costs(cycle))
        data['cycles'].append(cycle);_save(output,data)
    return cycle


def record_buyback(output,cfg,cycle_id,body,now=None):
    now=now or datetime.now(CST)
    if body.get('buy_filled') is not True:raise ValueError('只登记已经实际成交的接回，不把触价当成交')
    with storage.locked(output):
        data=ledger(output,cfg,now);cycle=next((x for x in data['cycles'] if x['id']==cycle_id),None)
        if not cycle or cycle['status']!='open':raise ValueError('未找到待接回的实际T卖出')
        if cycle['date']!=str(now.date()):raise ValueError('本轮T已跨日，不沿用昨日接回计划；请按普通成交记录并重新评估')
        fill,fill_id=_confirmed_fill(cfg,data,body,cycle['code'],cycle['date'],'buy')
        qty,price,fee=_fill_fields(body,fill,'price')
        remaining=int(cycle['quantity'])-int(cycle['buyback_quantity'])
        if qty>remaining:raise ValueError('接回数量不能超过本轮实际卖出后尚未接回的数量')
        if fill:
            before=int(cycle['base_quantity'])-remaining
            _check_linked_quantity(fill,before,before+qty)
        sold_at=parse_provider_time(cycle.get('sold_at'))
        filled_at=parse_provider_time(fill.get('executed_at')) if fill else None
        if sold_at and filled_at and filled_at<sold_at:raise ValueError('接回成交时间早于本轮实际卖出')
        cycle['fills'].append({'quantity':qty,'price':price,'at':now.isoformat(),'fee':fee,
                               'broker_fill_id':fill_id,'filled_at':fill.get('executed_at') if fill else None})
        cycle['buyback_quantity']+=qty;cycle['buyback_cost']=round(cycle['buyback_cost']+qty*price,4)
        if cycle['buyback_quantity']==cycle['quantity']:cycle['status']='closed';cycle['closed_at']=now.isoformat()
        cycle.update(summarize_cycle_costs(cycle))
        _save(output,data)
    return cycle


def close_cycle(output,cfg,cycle_id,body,now=None):
    if body.get('leave_cash') is not True:raise ValueError('请明确选择保留卖出现金；这不会自动买回')
    now=now or datetime.now(CST)
    with storage.locked(output):
        data=ledger(output,cfg,now);cycle=next((x for x in data['cycles'] if x['id']==cycle_id),None)
        if not cycle or cycle['status'] not in ('open', 'cash'):raise ValueError('未找到待结束的T记录')
        remaining=int(cycle['quantity'])-int(cycle.get('buyback_quantity',0))
        requested=body.get('remaining_quantity')
        if requested is not None and (type(requested) is not int or requested != remaining):
            raise ValueError('尚未接回数量已变化，请刷新并核对最新成交后再结束本轮')
        if remaining <= 0:raise ValueError('本轮已全部接回，无需按保留现金结束')
        if cycle['status']=='cash':return cycle
        cycle.update(status='cash',closed_at=now.isoformat(),closeout_confirmed_quantity=remaining,
                     closeout_snapshot=cycle.get('closeout'),closure_kind='retain_cash')
        _save(output,data)
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
    try:
        book=ledger(output,cfg,now);cache=storage.read(Path(output)/'t_minutes.json')
        enabled=settings(cfg)['enabled']
        closeout_due=T_CUTOFF <= now.astimezone(CST).time() < time(15) and any(
            cycle.get('status')=='open' and cycle.get('date')==str(now.date()) for cycle in book['cycles'])
        if not enabled and not closeout_due:
            return {'enabled':False,'rows':[],'signals':[],'status':'做T观察未启用'}
        minutes=cache.get('by_code',{})
        result=build_t_observations(cfg,quotes,risk,minutes,{**book,'fee_samples':fee_samples(cfg,book)},now=now,market_session=market_session,calendar_ok=calendar_ok)
        result.update(enabled=enabled,asof=now.isoformat(),settings=settings(cfg,book),cycles=book['cycles'][-30:],availability=book['availability'],ledger_revision=book.get('revision'),
                      status='做T条件观察；不自动交易。普通风险减仓不会产生接回提醒。')
        return result
    except Exception as exc:
        return {'enabled':True,'asof':now.isoformat(),'rows':[],'signals':[],'settings':settings(cfg),'status':'做T数据待核验：'+type(exc).__name__}
