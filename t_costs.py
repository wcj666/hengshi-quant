"""Fee estimates and confirmed fill costs for local stock and equity ETF T cycles."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from math import isfinite


CENT = Decimal('0.01')
TICKS = {'stock': Decimal('0.01'), 'etf': Decimal('0.001')}
# Published A-share transfer fee and sell-side stock stamp duty. Broker
# commission is supplied separately. Equity ETF estimates use their own broker terms.
STOCK_EXTRA_RATES = {'buy': Decimal('0.00001'), 'sell': Decimal('0.00051')}


def nonnegative(value):
    """Keep missing values distinct from confirmed zero."""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if isfinite(parsed) and parsed >= 0 else None


def decimal(value):
    return Decimal(str(value))


def money(value, rounding=ROUND_HALF_UP):
    return float(decimal(value).quantize(CENT, rounding=rounding))


def estimate_leg(kind, side, notional, model, samples):
    """Return a leg estimate and its basis using same-side comparable actual costs."""
    terms = model.get(kind, {})
    rate = nonnegative(terms.get('commission_per_10k'))
    minimum = nonnegative(terms.get('minimum_commission'))
    expected = None
    if rate is not None and minimum is not None:
        amount = decimal(notional)
        commission = max(decimal(minimum), amount * decimal(rate) / 10000)
        surcharge = amount * STOCK_EXTRA_RATES[side] if kind == 'stock' else 0
        expected = money(commission + surcharge, ROUND_CEILING)
    comparable = []
    for fill in samples:
        if fill['kind'] != kind or fill['side'] != side:
            continue
        previous = fill['notional']
        if .8 * notional <= previous <= 1.25 * notional:
            comparable.append(money(decimal(fill['fee']) * max(Decimal(1), decimal(notional) / decimal(previous)),
                                    ROUND_CEILING))
    if comparable and (expected is None or max(comparable) >= expected):
        return max(comparable), 'comparable_fills'
    return (expected, 'broker_terms') if expected is not None else (None, 'reserve')


def cash_impact(kind, side, notional, reserve, model=None, samples=()):
    """Separate estimated charges from cash held aside for a planned fill.

    ``reserve`` is the minimum total cash deduction budget, not a brokerage
    charge. Unknown terms remain unknown; only the conservative cash delta may
    use the reserve. No value returned here is an actual execution or fee.
    """
    amount, floor = nonnegative(notional), nonnegative(reserve)
    if amount is None or floor is None or side not in ('buy', 'sell'):
        raise ValueError('成交金额、费用预留或买卖方向无效')
    if not amount:
        estimate, basis = 0., 'no_trade'
    elif kind in ('stock', 'etf'):
        estimate, basis = estimate_leg(kind, side, amount, model or {}, samples or ())
    else:
        estimate, basis = None, 'reserve'
    reserved = max(decimal(floor), decimal(estimate or 0)) if amount else Decimal(0)
    reserved = reserved.quantize(CENT, rounding=ROUND_CEILING)
    extra = reserved - decimal(estimate or 0)
    signed_amount = decimal(amount) * (1 if side == 'sell' else -1)
    return {
        'estimated_fee': estimate,
        'fee_basis': basis,
        'safety_reserve': money(extra, ROUND_CEILING),
        'reserved_cost': money(reserved, ROUND_CEILING),
        'estimated_cash_delta': money(signed_amount - decimal(estimate)) if estimate is not None else None,
        'conservative_cash_delta': money(signed_amount - reserved),
    }


def roundtrip(kind, sell_price, quantity, reserve, model, samples, *, sell_fee=None):
    """Estimate a cost-covering buyback cap at the instrument's price tick."""
    notional = float(decimal(sell_price) * quantity)
    if sell_fee is None:
        sell_fee, sell_basis = estimate_leg(kind, 'sell', notional, model, samples)
    else:
        sell_basis = 'actual_sell'
    # At break-even the buyback amount cannot exceed the sell amount. Using the
    # latter to price the buy fee is conservative for a positive commission rate.
    buy_fee, buy_basis = estimate_leg(kind, 'buy', notional, model, samples)
    unknown = (sell_fee is None) + (buy_fee is None)
    fees = money(decimal(sell_fee or 0) + decimal(buy_fee or 0) + decimal(reserve) * unknown / 2, ROUND_CEILING)
    if unknown:
        fees = max(fees, reserve)
    tick = TICKS[kind]
    cap = decimal(sell_price) - decimal(fees) / quantity
    cap = (cap / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    return {
        'estimated_roundtrip_fee': fees, 'fee_basis': [sell_basis, buy_basis],
        'break_even_quantity': quantity, 'break_even_buy_price': float(cap) if cap > 0 else None,
        'break_even_gap': float(decimal(sell_price) - cap) if cap > 0 else None,
    }


def sold_fee_for_quantity(cycle, quantity):
    actual = nonnegative(cycle.get('sell_fee'))
    return money(decimal(actual) * quantity / cycle['quantity']) if actual is not None else None


def summarize_cycle_costs(cycle):
    paired = int(cycle.get('buyback_quantity', 0))
    cost = decimal(cycle.get('buyback_cost', 0))
    sell_fee = sold_fee_for_quantity(cycle, paired)
    total_sell_fee = nonnegative(cycle.get('sell_fee'))
    buy_fees = [nonnegative(fill.get('fee')) for fill in cycle.get('fills', [])]
    covered_quantity = sum(fill['quantity'] for fill in cycle.get('fills', []) if nonnegative(fill.get('fee')) is not None)
    gross = money(paired * decimal(cycle['sell_price']) - cost) if paired else None
    known_fees = decimal(sell_fee or 0) + sum(decimal(fee) for fee in buy_fees if fee is not None)
    complete = paired and sell_fee is not None and covered_quantity == paired and None not in buy_fees
    actual_fee = money(known_fees) if complete else None
    estimate = None
    if paired and not complete:
        reserve = decimal(cycle['fee_reserve'])
        unknown_fraction = Decimal(sell_fee is None) + Decimal(paired - covered_quantity) / paired
        estimate = money(max(reserve, known_fees + reserve * unknown_fraction / 2), ROUND_CEILING)
    return {
        'paired_gross_estimate': gross,
        'paired_net_estimate': money(decimal(gross) - decimal(estimate)) if estimate is not None else None,
        'paired_fee_actual': actual_fee,
        'paired_net_actual': money(decimal(gross) - decimal(actual_fee)) if actual_fee is not None else None,
        'unmatched_sell_fee_actual': money(decimal(total_sell_fee) - decimal(sell_fee)) if sell_fee is not None else None,
        'fee_status': 'actual' if actual_fee is not None else 'pending' if paired else 'unpaired',
    }
