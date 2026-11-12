"""Conservative approximate unrealized and same-session P&L calculations.

Never infer previous close from portfolio baseline or from a price already in the cost
basis. Mixed/missing previous closes must leave daily totals unavailable.
"""
from __future__ import annotations
import math


def valid_price(x):
    try:
        x = float(x)
    except (ValueError, TypeError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def calculate(cfg, current: dict, previous: dict | None = None, *, valid: bool = False):
    """Keys are zero-padded strings, values numeric. Does not account for prior-day trades.

    Returns aggregate and per-security details. 'valid' is true only for a current
    verified-by-application complete *snapshot* (not exchange-verified real-time).
    """
    previous = previous or {}
    positions = [p for p in cfg.get('positions', []) if int(p.get('quantity', 0)) > 0]
    rows = {}
    if not valid:
        return {'valid': False, 'floating_pnl': None, 'floating_pct': None,
                'daily_pnl': None, 'daily_pct': None, 'daily_valid': False,
                'missing_previous': [], 'positions': rows,
                'note': '未获取有效完整持仓行情；盈亏暂不计算'}
    floating = 0.0
    daily = 0.0
    total_cost = 0.0
    previous_value = 0.0
    missing_prices = []
    missing_previous = []
    for p in positions:
        code = str(p['code']).zfill(6)
        qty = int(p['quantity'])
        price = valid_price(current.get(code))
        cost = valid_price(p.get('cost'))
        prev = valid_price(previous.get(code))
        if price is None or cost is None:
            missing_prices.append(code)
            continue
        item_float = (price - cost) * qty
        floating += item_float
        total_cost += cost * qty
        item_daily = None
        if prev is None:
            missing_previous.append(code)
        else:
            item_daily = (price - prev) * qty
            daily += item_daily
            previous_value += prev * qty
        rows[code] = {
            'floating_pnl': round(item_float, 2),
            'daily_pnl': round(item_daily, 2) if item_daily is not None else None,
            'previous_close': prev,
        }
    if missing_prices:
        return {'valid': False, 'floating_pnl': None, 'floating_pct': None,
                'daily_pnl': None, 'daily_pct': None, 'daily_valid': False,
                'missing_previous': missing_previous, 'positions': rows,
                'note': '持仓现价或成本缺失：' + '、'.join(missing_prices)}
    daily_valid = len(missing_previous) == 0 and previous_value > 0
    return {
        'valid': True,
        'floating_pnl': round(floating, 2),
        'floating_pct': round(floating / total_cost * 100, 2) if total_cost > 0 else None,
        'daily_pnl': round(daily, 2) if daily_valid else None,
        'daily_pct': round(daily / previous_value * 100, 2) if daily_valid else None,
        'daily_valid': daily_valid,
        'missing_previous': missing_previous,
        'positions': rows,
        'note': ('按台账成本与昨收估算，不含费用、分红、当日交易及资金划转；券商口径可能不同'
                 if daily_valid else '缺少部分持仓的昨收价：' + '、'.join(missing_previous) + '；当日参考盈亏暂不计算'),
    }
