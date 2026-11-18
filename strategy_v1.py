"""V1 planned-risk constraints. No orders, invented stops or account resets."""
from __future__ import annotations
import math
from trade_levels import config_plan, make_levels, num
from pnl_engine import calculate as calculate_pnl
from strategy_defaults import V1_DEFAULTS


def enabled(cfg):
    value=cfg.get('strategy_v1',{})
    return isinstance(value, dict) and value.get('enabled') is True


def _finite(value, minimum=None):
    if isinstance(value,bool):return None
    try:value=float(value)
    except (TypeError,ValueError):return None
    if not math.isfinite(value) or (minimum is not None and value<minimum):return None
    return value


def strategy_prefs(cfg):
    """V1 ceilings cannot be relaxed through legacy percentage settings."""
    raw=cfg.get('strategy_v1',{})
    raw=raw if isinstance(raw,dict) else {}
    effective = dict(V1_DEFAULTS)
    for key, default in V1_DEFAULTS.items():
        value = _finite(raw.get(key), 0)
        if value is not None:
            effective[key] = max(value, default) if key in ('min_fees', 'min_reward_risk') else min(value, default)
    # Legacy and canonical names sometimes coexist in imported configurations.
    # A conflict must not enlarge the usable budget or reduce the fee reserve.
    fee = _finite(raw.get('fee_reserve'), 0)
    budget = _finite(raw.get('account_risk_budget'), 0)
    if fee is not None:
        effective['min_fees'] = max(fee, effective['min_fees'])
    if budget is not None:
        effective['loss_budget'] = min(budget, effective['loss_budget'])
    effective['reduce_drawdown'] = min(effective['reduce_drawdown'], effective['loss_budget'])
    effective['pause_drawdown'] = min(effective['pause_drawdown'], effective['reduce_drawdown'])
    for key in ('max_positions', 'max_daily_opens'):
        effective[key] = int(effective[key])
    return {**effective, 'fee_reserve': effective['min_fees'],
            'account_risk_budget': effective['loss_budget']}


def _sector(plan,position=None):
    value=plan.get('sector')
    if isinstance(value,str) and value.strip():return value.strip()
    value=(position or {}).get('sector')
    return value.strip() if isinstance(value,str) and value.strip() else None


def asset_type_for(code, item=None):
    item=item or {}
    value=item.get('type',item.get('asset_type'))
    if value in ('stock','etf','convertible'):return value
    return 'etf' if str(code).startswith(('15','51','56','58')) else 'stock'


def plan_blockers(cfg,code,asset_type='stock',name=''):
    """Entry-only checks; existing fixed stops remain visible without these passes."""
    code=str(code).zfill(6);plan=config_plan(cfg,code);out=[]
    levels=make_levels(cfg,code,asset_type=asset_type)
    if str(plan.get('strategy_version',''))!='1.0':out.append('交易计划版本未核验为1.0')
    if not levels or not levels.get('valid_order') or not levels.get('fixed'):
        out.append('缺少有效固定建仓、止损和两级目标')
    if not _sector(plan):out.append('缺少行业分类')
    if plan.get('setup_verified') is not True:out.append('回调转强形态未核验')
    review=plan.get('review') if isinstance(plan.get('review'),dict) else {}
    for key,label in (('fundamentals_ok','财报或ETF属性未核验'),
                      ('event_clear','公告与风险事项未核验'),
                      ('corporate_action_clear','除权除息口径未核验'),
                      ('resistance_clear','附近压力位的2R上行空间未核验')):
        if review.get(key) is not True:out.append(label)
    if asset_type=='stock':
        if not code.startswith(('00','60')):out.append('新开仓范围仅沪深主板普通A股')
        if 'ST' in str(name).upper() or '退' in str(name):out.append('风险警示证券不新开仓')
    elif asset_type=='etf':
        if plan.get('leveraged') is True or any(x in str(name) for x in ('杠杆','分级','反向')):
            out.append('杠杆、分级或反向产品不在策略范围')
        if plan.get('equity_etf') is False or any(x in str(name) for x in ('债券','货币','原油','黄金ETF')):
            out.append('新开ETF须为非杠杆股票ETF')
    else:out.append('该品种不在V1新开仓范围')
    if levels:
        entry=levels['reference'];stop=levels['stop_loss']
        if (entry-stop)/entry>.060000001:out.append('初始止损距离超过6%')
        if levels['take_profit_1']+0.0001<entry+2*(entry-stop):out.append('第一目标不足固定初始价差的2倍')
        if levels['take_profit_2']+0.0001<entry+3*(entry-stop):out.append('第二目标不足固定初始价差的3倍')
        if plan.get('resistance_price') not in (None,''):
            resistance=num(plan['resistance_price'])
            if resistance is None or resistance+0.0001<max(levels['take_profit_1'],entry+2*(entry-stop)):
                out.append('已记录压力位不足固定第一目标与2R空间')
    return out


def _account_state(context,settings):
    account=dict(context) if isinstance(context,dict) else {}
    blockers=[]
    if account.get('initialized') is not True:blockers.append('V1账户基准尚未初始化')
    if account.get('verified') is not True:blockers.append('账户、资金流水和高水位尚未核验')
    required={'drawdown':0,'high_water':0,'net_external_flow':None,'adjusted_equity':None,'opens_today':0}
    for key,minimum in required.items():
        value=_finite(account.get(key),minimum)
        if value is None:
            blockers.append('账户统计不完整：'+key)
        else:account[key]=value
    opens=account.get('opens_today')
    if isinstance(opens,(float,int)) and opens!=int(opens):blockers.append('当日新开数量须为整数')
    if account.get('needs_reconcile') is not False:blockers.append('账户与成交、资金流水待对账')
    if account.get('paused') is not False or account.get('pause_until'):
        blockers.append('策略暂停中，须复盘确认恢复')
    if _finite(account.get('drawdown'),0) is not None and float(account['drawdown'])>=settings['pause_drawdown']:
        blockers.append(f'调流后回撤已达{settings["pause_drawdown"]:g}元，暂停新开并复盘')
    verified=bool(account.get('initialized') is True and account.get('verified') is True
                  and not any(x.startswith('账户统计') or x.startswith('当日新开') for x in blockers)
                  and account.get('needs_reconcile') is False)
    return account,blockers,verified


def _fees(plan,settings,remaining=False):
    # A remaining reserve may be separately recorded after an actual partial exit.
    key='remaining_fee_reserve' if remaining else 'fee_reserve'
    reserve=_finite(plan.get(key),0)
    if remaining and reserve is not None:return reserve
    if reserve is None:reserve=_finite(plan.get('fee_reserve'),0)
    return max(settings['fee_reserve'],reserve or 0)


def compute_strategy(cfg,quotes,fresh=False,watch_quotes=None,previous_closes=None,strategy_account=None,legacy_settings=None):
    from risk_engine import cash_balances
    settings=dict(legacy_settings or {});settings.update(strategy_prefs(cfg))
    quotes=quotes or {};cash,frozen_cash=cash_balances(cfg);base=float(cfg['baseline_equity'])
    cap=settings['account_risk_budget'];rows=[];missing=[];breaches=[];unknown_stops=[];unknown_sectors=[]
    exposure=0.;stress=0.;sector_values={};single_excess=[]
    for position in cfg.get('positions',[]):
        quantity=int(position.get('quantity',0))
        if quantity<=0:continue
        code=str(position['code']).zfill(6);kind=position.get('type','stock')
        plan=config_plan(cfg,code);sector=_sector(plan,position)
        price=num(quotes.get(code)) if fresh else None
        levels=make_levels(cfg,code,price=price,position=position,asset_type=kind)
        fixed_stop=levels['stop_loss'] if levels and levels.get('valid_order') and levels.get('fixed') else None
        if fixed_stop is None:unknown_stops.append(code)
        if not sector:unknown_sectors.append(code)
        value=price*quantity if price is not None else None
        breached=bool(price is not None and fixed_stop is not None and price<=fixed_stop)
        if breached:breaches.append(code)
        if price is None:missing.append(code)
        else:
            exposure+=value
            if sector:sector_values[sector]=sector_values.get(sector,0)+value
        fee=_fees(plan,settings,remaining=True)
        planned=max(price-fixed_stop,0)*quantity+fee if price is not None and fixed_stop is not None else None
        if planned is not None:stress+=planned
        rows.append({'code':code,'name':position.get('name',code),'type':kind,'quantity':quantity,
                     'cost':position.get('cost'),'price':round(price,4) if price else None,
                     'value':round(value,2) if value is not None else None,'levels':levels,
                     'sector':sector,'stop_breached':breached,'planned_stop_pct':None,
                     'future_fee_reserve':fee,'planned_stress':round(planned,2) if planned is not None else None})
    valid=bool(fresh and not missing)
    equity=round(cash+frozen_cash+exposure,2) if valid else None
    pnl=calculate_pnl(cfg,quotes,previous_closes,valid=valid)
    for row in rows:row.update(pnl.get('positions',{}).get(row['code'],{}))
    account,blockers,account_verified=_account_state(strategy_account,settings)
    used=_finite(account.get('drawdown'),0) if account_verified else None
    remaining=cap-used if used is not None else None
    planned_stress=round(stress,2) if valid and not unknown_stops else None
    available=max(0,remaining-planned_stress) if remaining is not None and planned_stress is not None else 0.
    if not valid:blockers.append('持仓行情缺失或过期')
    if unknown_stops:blockers.append('已有持仓缺少固定止损：'+','.join(unknown_stops))
    if unknown_sectors:blockers.append('已有持仓缺少行业分类：'+','.join(unknown_sectors))
    if breaches:blockers.append('已有持仓触及或跌破固定止损：'+','.join(breaches))
    count=len(rows);exposure_pct=100*exposure/equity if valid and equity>0 else None
    sector_exposure={key:{'value':round(value,2),'pct':round(100*value/equity,2) if valid and equity>0 else None}
                     for key,value in sector_values.items()}
    transition=[]
    if valid and equity>0:
        single_excess=[row['code'] for row in rows if row['value']>equity*settings['max_position_pct']/100+1e-8]
        if single_excess:transition.append('单一证券超过15%上限：'+','.join(single_excess))
        if exposure_pct>settings['max_exposure_pct']+1e-8:transition.append('权益总仓位超过60%上限')
        if any(value>equity*settings['max_sector_pct']/100+1e-8 for value in sector_values.values()):
            transition.append('同行业仓位超过30%上限')
    elif valid:blockers.append('账户净值必须大于0')
    if count>settings['max_positions']:transition.append('现有持仓超过4只上限')
    transition.extend(['已有持仓待补固定止损'] if unknown_stops else [])
    transition.extend(['已有持仓待补行业分类'] if unknown_sectors else [])
    blockers.extend(transition)
    if count>=settings['max_positions']:blockers.append('已达同时持仓数量上限')
    if account_verified and account['opens_today']>=settings['max_daily_opens']:blockers.append('已达每日新开1只上限')
    if remaining is not None and (remaining<=0 or (planned_stress is not None and planned_stress>=remaining)):
        blockers.append('账户剩余计划风险预算不足')
    if blockers:available=0.
    blockers=list(dict.fromkeys(blockers))
    over_budget=bool(unknown_stops or not account_verified or breaches or remaining is not None and
                     (remaining<=0 or planned_stress is not None and planned_stress>remaining))
    summary={'status':blockers[0] if blockers else 'V1风险核验通过；仍须核验时段、形态与券商价格',
             'valid':valid,'baseline':base,'budget':cap,'equity':equity,
             'change':round(equity-base,2) if valid else None,'used':round(used,2) if used is not None else None,
             'remaining':round(remaining,2) if remaining is not None else None,
             'existing_planned_stress':planned_stress,'available_new_risk':round(available,2),
             'cash':cash,'frozen_cash':frozen_cash,'total_cash':round(cash+frozen_cash,2),
             'exposure':round(exposure,2) if valid else None,'missing':missing,
             'stop_breaches':breaches,'over_budget':over_budget,'positions':rows,'settings':settings,
             'sizing':[],'pnl':pnl,
             'strategy':{'enabled':True,'version':'1.0','blockers':blockers,'transition':transition,
                         'new_entries_allowed':not blockers,
                         'exposure_pct':round(exposure_pct,2) if exposure_pct is not None else None,
                         'position_count':count,'sector_exposure':sector_exposure,
                         'unknown_stops':unknown_stops,'unknown_sectors':unknown_sectors,
                         'single_excess':single_excess,'account':account,'account_verified':account_verified,
                         'budget_occupancy':round(used+planned_stress,2) if used is not None and planned_stress is not None else None}}
    held={row['code']:row for row in rows}
    for item in watch_quotes or []:
        code=str(item['code']).zfill(6);price=num(item.get('price'));plan=config_plan(cfg,code)
        kind=asset_type_for(code,item);levels=make_levels(cfg,code,price=price,asset_type=kind)
        reasons=list(blockers)+plan_blockers(cfg,code,kind,item.get('name',''))
        if code in held:reasons.append('V1不在已有仓位上加仓')
        if price is None:reasons.append('缺少有效证券行情')
        if price is not None and levels:
            if price<=levels['stop_loss']:reasons.append('现价已触及或跌破固定止损')
            if price>levels['reference']:reasons.append('现价超过允许最高成交价Ecap')
            trigger=num(plan.get('trigger_price'))
            if trigger is None:reasons.append('缺少固定触发下沿')
            elif trigger>levels['reference']:reasons.append('触发下沿高于Ecap，计划区间为空')
            elif price<trigger:reasons.append('现价尚未达到固定触发下沿')
        quantity=0;net_rr=None;fee=_fees(plan,settings)
        price_risk=total_risk=notional=None
        if not reasons and levels and valid and equity and price:
            entry=levels['reference'];stop=levels['stop_loss'];distance=entry-stop
            sector_value=sector_values.get(_sector(plan),0)
            money=min(cash-fee,equity*settings['max_position_pct']/100,
                      equity*settings['max_exposure_pct']/100-exposure,
                      equity*settings['max_sector_pct']/100-sector_value)
            shares=min(money/entry,(settings['per_trade_risk']-fee)/distance,(available-fee)/distance)
            quantity=max(0,math.floor((shares+1e-10)/100)*100)
            if quantity<100:reasons.append('费用、现金、仓位或风险额度不足100股')
            else:
                price_risk=distance*quantity;total_risk=price_risk+fee;notional=entry*quantity
                net_rr=((levels['take_profit_1']-entry)*quantity-fee)/total_risk
                if net_rr+1e-10<settings['min_reward_risk']:
                    reasons.append('全仓假设在TP1退出的净价差筛选值不足1.5');quantity=0
        summary['sizing'].append({'code':code,'name':item.get('name',code),'price':price,
             'max_buy_shares':quantity,'levels':levels,'reward_risk':round(net_rr,4) if net_rr is not None else None,
             'fee_reserve':fee,'price_risk':round(price_risk,2) if price_risk is not None else None,
             'total_planned_risk':round(total_risk,2) if total_risk is not None else None,
             'max_notional':round(notional,2) if notional is not None else None,
             'blockers':list(dict.fromkeys(reasons)),
             'reason':'；'.join(dict.fromkeys(reasons)) if reasons else '按Ecap、费用及账户全部上限估算；须在券商核验后手工执行'})
    return summary
