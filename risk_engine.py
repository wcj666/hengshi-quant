"""Account-level risk scenarios; reference levels are not stop orders."""
from __future__ import annotations
import math
from trade_levels import make_levels
from pnl_engine import calculate as calculate_pnl


def risk_prefs(cfg):
    v=cfg.get('risk_settings',{})
    return {
        'stop_stock':float(v.get('stop_stock',5.0)),
        'stop_etf':float(v.get('stop_etf',4.0)),
        'stop_convertible':float(v.get('stop_convertible',10.0)),
        'tp1_pct':float(v.get('tp1_pct',10.0)),
        'tp2_pct':float(v.get('tp2_pct',15.0)),
        'per_trade_risk':float(v.get('per_trade_risk',150.0)),
        'max_position_pct':float(v.get('max_position_pct',15.0)),
        'min_reward_risk':float(v.get('min_reward_risk',1.5)),
        'max_breakout_extension_pct':float(v.get('max_breakout_extension_pct',3.0)),
    }


def compute(cfg, quotes:dict[str,float]|None,fresh=False,watch_quotes=None,previous_closes=None,strategy_account=None):
    """Value complete quotes independently from V1's permission to add risk."""
    from strategy_v1 import enabled, compute_strategy
    if enabled(cfg):
        return compute_strategy(cfg,quotes,fresh=fresh,watch_quotes=watch_quotes,
                                previous_closes=previous_closes,strategy_account=strategy_account,
                                legacy_settings=risk_prefs(cfg))
    return _compute_legacy(cfg,quotes,fresh=fresh,watch_quotes=watch_quotes,previous_closes=previous_closes)


def _compute_legacy(cfg, quotes:dict[str,float]|None,fresh=False,watch_quotes=None,previous_closes=None):
    """Missing/stale quotes => suppress risk capacity and sizing. Uses whole portfolio."""
    settings=risk_prefs(cfg)
    base=float(cfg['baseline_equity'])
    cap=float(cfg['max_additional_loss'])
    cash=float(cfg.get('cash',0))
    rows=[];missing=[];breaches=[];stress=0.;exposed=0.
    quotes=quotes or {}
    for p in cfg.get('positions',[]):
        if int(p.get('quantity',0))<=0:continue
        code=str(p['code']).zfill(6)
        price=quotes.get(code) if fresh else None
        if price is not None:
            try:price=float(price)
            except (TypeError,ValueError):price=None
        if price is None or not math.isfinite(price) or price<=0:
            missing.append(code)
            price=None; value=None;plan=make_levels(cfg,code,position=p,asset_type=p['type'])
            planned_loss=None;breached=False
        else:
            value=price*int(p['quantity'])
            exposed+=value
            plan=make_levels(cfg,code,position=p,price=price,asset_type=p['type'])
            stop=plan['stop_loss'] if plan else 0
            breached=bool(price<=stop and int(p['quantity'])>0)
            if breached: breaches.append(code)
            planned_loss=max(0.,price-stop)*int(p['quantity'])
            stress+=planned_loss
        rows.append({'code':code,'name':p['name'],'type':p['type'],'quantity':p['quantity'],
            'cost':p.get('cost'),'price':round(price,4) if price else None,
            'value':round(value,2) if value is not None else None,
            'levels':plan,'stop_breached':breached,
            'planned_stop_pct':settings['stop_'+p['type']],
            'planned_stress':round(planned_loss,2) if planned_loss is not None else None})
    valid=bool(fresh and not missing)
    equity=round(cash+exposed,2) if valid else None
    used=max(0.,base-equity) if valid else None
    remaining=cap-used if valid else None
    # A triggered stop is an urgent review condition, never considered 'risk free'.
    available=max(0.,remaining-stress) if valid and not breaches else (0. if valid else None)
    pnl=calculate_pnl(cfg,quotes,previous_closes,valid=valid)
    for r in rows:
        r.update(pnl.get('positions',{}).get(r['code'],{}))
    summary={
        'status':('已有持仓跌破参考止损；新增仓位设为0并请检查实际行情' if breaches
            else '可估算（行情快照仍可能延迟）' if valid else '行情缺失/过期，禁止据此计算新增仓位'),
        'valid':valid,'baseline':base,'budget':cap,'equity':equity,
        'change':round(equity-base,2) if valid else None,
        'used':round(used,2) if valid else None,
        'remaining':round(remaining,2) if valid else None,
        'existing_planned_stress':round(stress,2) if valid else None,
        'available_new_risk':round(available,2) if available is not None else None,
        'cash':cash,'exposure':round(exposed,2) if valid else None,
        'missing':missing,'stop_breaches':breaches,
        'over_budget':bool(valid and (used>=cap or stress>remaining or breaches)),
        'positions':rows,'settings':settings,'sizing':[], 'pnl':pnl,
    }
    held={r['code']:r for r in rows}
    for item in (watch_quotes or []):
        code=str(item['code']).zfill(6)
        price=item.get('price')
        try:price=float(price)
        except (TypeError,ValueError):price=None
        if price is not None and (not math.isfinite(price) or price<=0):price=None
        pos=next((p for p in cfg.get('positions',[]) if str(p['code']).zfill(6)==code and int(p.get('quantity',0))>0),None)
        plan=make_levels(cfg,code,price=price,position=pos,asset_type=pos['type'] if pos else 'stock',price_label='当次行情快照价')
        qty=None;reward_risk=None;reason='等待有效账户和证券行情'
        if valid and price is not None and plan and plan['valid_order']:
            stop=plan['stop_loss']
            reward_risk=(plan['take_profit_1']-price)/(price-stop) if price>stop else None
            if price<=stop:
                qty=0;reason='当前价已触及/跌破止损参考，不新增'
            elif price>=plan['take_profit_1']:
                qty=0;reason='已到第一止盈参考，不追买'
            elif reward_risk<settings['min_reward_risk']:
                qty=0;reason=f'距第一止盈的收益/止损风险比{reward_risk:.2f}，低于{settings["min_reward_risk"]:g}'
            elif breaches:
                qty=0;reason='现有持仓有参考止损被触发，先检查风险'
            elif available<=0:
                qty=0;reason='组合剩余风险预算不足'
            else:
                per_share=price-stop
                own=held.get(code,{}).get('value',0) or 0
                cap_position=max(0.,equity*settings['max_position_pct']/100-own)
                limits=[cash/price,cap_position/price,available/per_share,settings['per_trade_risk']/per_share]
                qty=max(0,math.floor(min(limits)/100)*100)
                reason='按现金、单股上限、组合与单笔预算综合估算' if qty else '资金或仓位限额不足100股'
        summary['sizing'].append({'code':code,'name':item['name'],'price':price,
            'max_buy_shares':qty,'levels':plan,'reward_risk':round(reward_risk,2) if reward_risk is not None else None,'reason':reason})
    return summary
