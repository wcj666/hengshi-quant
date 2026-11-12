"""Risk tier alerts and hypothetical position reductions; never places trades."""
from __future__ import annotations
from copy import deepcopy
import math
from risk_engine import compute

TIERS=(.5,.8,1.0)


def _number(value):
    try:value=float(value)
    except (TypeError,ValueError):return None
    return value if math.isfinite(value) else None


def tier_alerts(risk):
    if not risk or not risk.get('valid'):return []
    budget=_number(risk.get('budget'))
    used=_number(risk.get('used'))
    if budget is None or used is None or budget<=0:return []
    messages=[]
    # Report only the highest reached tier per cycle. On a sudden gap-down, do
    # not send three popups for 50/80/100% at the same moment.
    reached=[ratio for ratio in TIERS if used>=budget*ratio]
    if reached:
        ratio=max(reached)
        messages.append({'code':'PORT','name':'全账户','kind':f'回撤预算使用{round(ratio*100)}%预警',
                         'level':round(budget*ratio,2),'price':round(used,2),'tier':int(ratio*100)})
    stress=_number(risk.get('existing_planned_stress'));remaining=_number(risk.get('remaining'))
    if stress is not None and remaining is not None and stress>remaining:
        messages.append({'code':'PORT','name':'全账户','kind':'持仓压力损失超剩余预算',
                         'level':round(remaining,2),'price':round(stress,2),'tier':100})
    return messages


def risk_level(risk):
    if not risk or not risk.get('valid'):return {'level':'unavailable','label':'行情不足：风险预算未核验','used_pct':None}
    used=_number(risk.get('used'));budget=_number(risk.get('budget'))
    stress=_number(risk.get('existing_planned_stress'));remaining=_number(risk.get('remaining'))
    if used is None or budget is None or budget<=0 or stress is None or remaining is None:
        return {'level':'unavailable','label':'固定止损或账户统计未核验；暂停新增','used_pct':None}
    raw_pct=100*max(0,used)/budget
    pct=round(raw_pct,1)
    drawdown_level='critical' if raw_pct>=100 else 'high' if raw_pct>=80 else 'watch' if raw_pct>=50 else 'normal'
    pressure=stress>remaining
    breached=bool(risk.get('stop_breaches'))
    level='critical' if pressure or breached or risk.get('over_budget') else drawdown_level
    label=('持仓已触及止损参考' if breached else '持仓压力超剩余预算' if pressure
           else {'critical':'回撤预算已用尽','high':'回撤已用80%以上','watch':'回撤已用50%以上','normal':'未触及回撤预警'}[drawdown_level])
    return {'level':level,'label':label,'drawdown_level':drawdown_level,'pressure_exceeded':pressure,
            'used_pct':pct,'thresholds':[50,80,100]}



def simulate_reduction(cfg, quotes, code, quantity, fee=0,strategy_account=None):
    """Hypothetical mark-to-market sell at supplied snapshot. Does not modify cfg."""
    code=str(code).zfill(6)
    p=next((p for p in cfg.get('positions',[]) if str(p['code']).zfill(6)==code),None)
    if not p:raise ValueError('该证券不在现有持仓')
    try:quantity=int(quantity)
    except (TypeError,ValueError):raise ValueError('减仓数量应为整数')
    held=int(p['quantity'])
    if quantity<=0 or quantity>held:raise ValueError('模拟减仓数量必须大于0且不超过当前持有数量')
    lot=10 if p['type']=='convertible' else 100
    if quantity%lot!=0 and quantity!=held:
        raise ValueError(f'非清仓模拟按{lot}单位整数倍计算；零股/不足一手可选择全部卖出')
    px=quotes.get(code)
    try:px=float(px)
    except (TypeError,ValueError):px=0
    if not math.isfinite(px) or px<=0:raise ValueError('缺少当前有效行情，不可模拟')
    fee=_number(fee)
    if fee is None or fee<0:raise ValueError('模拟费用应为有限非负数')
    before=compute(cfg,quotes,fresh=True,strategy_account=strategy_account)
    if not before['valid']:raise ValueError('持仓价格不完整，不可计算账户风险')
    new=deepcopy(cfg)
    x=next(y for y in new['positions'] if str(y['code']).zfill(6)==code)
    x['quantity']=held-quantity
    new['cash']=round(float(new.get('cash',0))+quantity*px-fee,2)
    projected_account=deepcopy(strategy_account)
    if isinstance(projected_account,dict) and fee:
        adjusted=_number(projected_account.get('adjusted_equity'))
        high=_number(projected_account.get('high_water'))
        if adjusted is not None and high is not None:
            projected_account['adjusted_equity']=adjusted-fee
            projected_account['drawdown']=max(0,high-(adjusted-fee))
    after=compute(new,quotes,fresh=True,strategy_account=projected_account)
    if not after['valid']:raise ValueError('模拟后账户结构不完整')
    return {'code':code,'name':p['name'],'quantity':quantity,'held':held,'price':px,'sale_proceeds':round(quantity*px,2),
            'fee_assumed':fee,'cash_after':after['cash'],'equity_before':before['equity'],'equity_after':after['equity'],
            'budget_used_before':before['used'],'budget_used_after':after['used'],
            'planned_stress_before':before['existing_planned_stress'],'planned_stress_after':after['existing_planned_stress'],
            'stress_reduction':round(before['existing_planned_stress']-after['existing_planned_stress'],2)
                if before['existing_planned_stress'] is not None and after['existing_planned_stress'] is not None else None,
            'available_new_risk_before':before['available_new_risk'],'available_new_risk_after':after['available_new_risk'],
            'remaining_after':after['remaining'],
            'note':'仅假设全部按本轮公开快照价成交并扣除所填费用，未建模额外滑点、税费、价格冲击；卖出不会消除已发生的回撤。没有执行或登记真实卖出。'}
