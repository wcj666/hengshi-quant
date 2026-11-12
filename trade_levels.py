"""Transparent, non-executable take-profit/stop-loss reference levels.

Manual plans have absolute fixed prices. Defaults are *illustrative* levels
anchored to holding cost or a chosen reference price, NOT stop orders.
"""
from __future__ import annotations
import math


def num(v):
    if v is None: return None
    try:
        x=float(v)
        return x if math.isfinite(x) and x>0 else None
    except (TypeError, ValueError): return None


def config_plan(cfg, code):
    plans=cfg.get('trade_plans', {})
    p=plans.get(str(code).zfill(6)) if isinstance(plans,dict) else None
    return p if isinstance(p,dict) else {}


def make_levels(cfg, code, *, price=None, position=None, asset_type='stock', price_label='最近历史收盘价'):
    """Generate levels with explicit basis/source. Don't silently shift manual values.

    For held positions, auto levels use cost (NOT today's market price).
    For unheld stocks, auto levels use latest daily close; move on next refresh.
    """
    code=str(code).zfill(6)
    p=config_plan(cfg, code)
    # V1 only budgets against a complete, explicitly fixed plan. In particular,
    # a legacy holding's cost less a percentage is not its structural stop.
    v1=cfg.get('strategy_v1',{})
    if isinstance(v1,dict) and v1.get('enabled'):
        fixed={k:num(p.get(k)) for k in ('entry_price','stop_loss','take_profit_1','take_profit_2')}
        if any(value is None for value in fixed.values()):return None
    defaults=cfg.get('risk_settings',{})
    stop_pct=float(defaults.get('stop_'+asset_type,5.0))
    tp1=float(defaults.get('tp1_pct',10.0))
    tp2=float(defaults.get('tp2_pct',15.0))
    if position is not None and num(position.get('cost')) is not None:
        base=num(position.get('cost'))
        base_label='持仓成本'
    else:
        base=num(price)
        base_label=price_label
    manual_entry=num(p.get('entry_price'))
    if manual_entry is not None:
        base=manual_entry
        base_label='手工锁定参考价'
    if base is None: return None
    stop=num(p.get('stop_loss')) or (base*(1-stop_pct/100))
    first=num(p.get('take_profit_1')) or (base*(1+tp1/100))
    second=num(p.get('take_profit_2')) or (base*(1+tp2/100))
    initial_stop=num(p.get('initial_stop_loss'))
    raised_stop=bool(isinstance(v1,dict) and v1.get('enabled') and position is not None
                     and num(p.get('tp1_sold_quantity')) is not None
                     and initial_stop is not None and initial_stop<base
                     and base<=stop<first<second)
    return {
        'code':code,'reference':round(base,4),'reference_source':base_label,
        'stop_loss':round(stop,4),'take_profit_1':round(first,4),
        'take_profit_2':round(second,4),
        'stop_source':'手动设置' if num(p.get('stop_loss')) else f'{stop_pct:g}%模拟止损',
        'tp1_source':'手动设置' if num(p.get('take_profit_1')) else f'{tp1:g}%第一止盈',
        'tp2_source':'手动设置' if num(p.get('take_profit_2')) else f'{tp2:g}%第二止盈',
        'manual':bool(p),'fixed':all(num(p.get(k)) is not None for k in ('entry_price','stop_loss','take_profit_1','take_profit_2')),
        'valid_order':bool(stop < base < first < second or raised_stop),
        'initial_stop_loss':round(initial_stop,4) if initial_stop is not None else round(stop,4),
        'raised_stop':raised_stop,
        'current_price':round(float(price),4) if num(price) else None,
        'disclaimer':'仅为参考价位，不是委托单；跳空、停牌和涨跌停可能无法按价成交。',
    }


def validate_plan(values,allow_raised_stop=False):
    """Validate a complete fixed plan. Partial values intentionally disallowed."""
    keys=('entry_price','stop_loss','take_profit_1','take_profit_2')
    parsed={k:num(values.get(k)) for k in keys}
    if any(v is None or v>1000000 for v in parsed.values()):
        raise ValueError('参考价、止损价、两级止盈价都要填写大于0且有效的数字')
    x=parsed
    ordered=x['stop_loss'] < x['entry_price'] < x['take_profit_1'] < x['take_profit_2']
    raised=bool(allow_raised_stop and x['entry_price']<=x['stop_loss']<x['take_profit_1']<x['take_profit_2'])
    if not (ordered or raised):
        raise ValueError('价格顺序必须为：止损 < 建仓参考 < 止盈一 < 止盈二')
    return {k:round(v,4) for k,v in parsed.items()}
