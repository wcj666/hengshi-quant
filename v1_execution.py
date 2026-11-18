"""Shared cost and risk estimates for live V1 sizing and historical fills."""
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from strategy_defaults import SIMULATED_COSTS
from t_costs import TICKS, decimal, money, nonnegative


def normalize_execution_costs(broker_terms=None, slippage_rate=SIMULATED_COSTS['slippage_rate'], *,
                              stock_sell_stamp_rate=SIMULATED_COSTS['stock_sell_stamp_rate'],
                              stock_transfer_rate=SIMULATED_COSTS['stock_transfer_rate'],
                              fee_rounding='ceil'):
    terms = {}
    for kind in TICKS:
        raw = broker_terms.get(kind) if isinstance(broker_terms, dict) else None
        if not isinstance(raw, dict):
            continue
        rate = nonnegative(raw.get('commission_per_10k'))
        minimum = nonnegative(raw.get('minimum_commission'))
        if rate is not None and minimum is not None:
            terms[kind] = {'commission_per_10k': rate, 'minimum_commission': minimum}
    return {'broker_terms': terms, 'slippage_rate': slippage_rate,
            'stock_sell_stamp_rate': stock_sell_stamp_rate, 'stock_transfer_rate': stock_transfer_rate,
            'fee_rounding': fee_rounding}


def execution_price(reference, kind, side, slippage):
    tick = TICKS[kind]
    factor = 1 + decimal(slippage) if side == 'buy' else 1 - decimal(slippage)
    rounding = ROUND_CEILING if side == 'buy' else ROUND_FLOOR
    return float((decimal(reference) * factor / tick).to_integral_value(rounding=rounding) * tick)


def execution_fee(kind, side, notional, execution_costs):
    terms = execution_costs['broker_terms'].get(kind)
    if terms is None:
        return None
    amount = decimal(notional)
    commission = max(decimal(terms['minimum_commission']), amount * decimal(terms['commission_per_10k']) / 10000)
    transfer = amount * decimal(execution_costs['stock_transfer_rate']) if kind == 'stock' else 0
    stamp = amount * decimal(execution_costs['stock_sell_stamp_rate']) if kind == 'stock' and side == 'sell' else 0
    if execution_costs['fee_rounding'] == 'half_up':
        return money(sum(money(item, ROUND_HALF_UP) for item in (commission, transfer, stamp)))
    return money(commission + transfer + stamp, ROUND_CEILING)


def entry_economics(kind, quantity, price, stop, target, reserve, execution_costs=None):
    """The quoted buy price is an execution bound, already inclusive of entry slippage."""
    costs = execution_costs if execution_costs is not None else normalize_execution_costs()
    stop_fill = execution_price(stop, kind, 'sell', costs['slippage_rate'])
    target_fill = execution_price(target, kind, 'sell', costs['slippage_rate'])
    buy_fee = execution_fee(kind, 'buy', decimal(price) * quantity, costs)
    stop_fee = execution_fee(kind, 'sell', decimal(stop_fill) * quantity, costs)
    target_fee = execution_fee(kind, 'sell', decimal(target_fill) * quantity, costs)
    known = buy_fee is not None and stop_fee is not None and target_fee is not None
    stop_slippage = (decimal(stop) - decimal(stop_fill)) * quantity
    target_slippage = (decimal(target) - decimal(target_fill)) * quantity
    loss = (decimal(price) - decimal(stop)) * quantity
    profit = (decimal(target) - decimal(price)) * quantity
    floor = decimal(reserve)
    stop_fees = decimal(buy_fee) + decimal(stop_fee) if known else None
    target_fees = decimal(buy_fee) + decimal(target_fee) if known else None
    stop_costs = stop_fees + stop_slippage if known else max(floor, stop_slippage)
    target_costs = target_fees + target_slippage if known else max(floor, target_slippage)
    protected_costs = max(floor, stop_costs)
    stop_net_risk = loss + stop_costs
    target_net_reward = profit - target_costs
    return {
        'estimated_costs_known': known,
        'expected_entry_fee': buy_fee,
        'expected_stop_fees': money(stop_fees) if known else None,
        'expected_target_fees': money(target_fees) if known else None,
        'stop_slippage': money(stop_slippage),
        'target_slippage': money(target_slippage),
        'risk_cost_allowance': money(protected_costs, ROUND_CEILING),
        'extra_risk_reserve': money(protected_costs - stop_costs) if known else None,
        'price_risk': money(loss),
        'total_planned_risk': money(loss + protected_costs, ROUND_CEILING),
        'stop_net_risk': money(stop_net_risk, ROUND_CEILING),
        'target_net_reward': money(target_net_reward, ROUND_FLOOR),
        'reward_risk': float(target_net_reward / stop_net_risk) if stop_net_risk > 0 and stop_fill > 0 else None,
    }
