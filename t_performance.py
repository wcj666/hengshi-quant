"""Review user-recorded T fills, separating realized pairs from open exposure.

This module neither fetches prices nor creates trades. Paired net spread is the
incremental cash difference versus leaving those shares untouched, not the
portfolio's investment profit or evidence of a profitable strategy.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from collections import Counter
import re
from zoneinfo import ZoneInfo

from quote_health import parse_provider_time
from t_costs import decimal, money, nonnegative, summarize_cycle_costs

CN = ZoneInfo('Asia/Shanghai')


def _number(value, *, positive=False):
    value = nonnegative(value)
    if value is None or (positive and value <= 0):
        raise ValueError('invalid amount')
    return value


def _quantity(value):
    number = _number(value)
    if number != int(number):
        raise ValueError('invalid quantity')
    return int(number)


def _fee(value):
    return None if value in (None, '') else _number(value)


def _record_time(value, now, day):
    if value in (None, ''):
        return
    stamp = parse_provider_time(value)
    if stamp is None or stamp > now or stamp.astimezone(CN).date() < day:
        raise ValueError('invalid or future record time')


def _cycle(raw, now, day):
    if not isinstance(raw.get('id'), str) or not raw['id']:
        raise ValueError('missing cycle identity')
    if not isinstance(raw.get('code'), str) or not re.fullmatch(r'\d{6}', raw['code']):
        raise ValueError('invalid security')
    if raw.get('status') not in ('open', 'closed', 'cash'):
        raise ValueError('invalid cycle status')
    for key in ('sold_at', 'recorded_at', 'closed_at'):
        _record_time(raw.get(key), now, day)
    sale_stamp = parse_provider_time(raw.get('sold_at'))
    if sale_stamp and sale_stamp.astimezone(CN).date() != day:
        raise ValueError('sale date does not match cycle date')
    sold = _quantity(raw.get('quantity'))
    paired = _quantity(raw.get('buyback_quantity', 0))
    if not sold or paired > sold or (raw['status'] == 'closed' and paired != sold):
        raise ValueError('inconsistent cycle quantities')
    price = _number(raw.get('sell_price'), positive=True)
    cost = _number(raw.get('buyback_cost', 0))
    fills = raw.get('fills', [])
    if not isinstance(fills, list):
        raise ValueError('invalid fills')
    normalized = []
    for fill in fills:
        if not isinstance(fill, dict):
            raise ValueError('invalid fill')
        qty = _quantity(fill.get('quantity'))
        unit_price = _number(fill.get('price'), positive=True)
        if not qty:
            raise ValueError('zero fill')
        for key in ('at', 'filled_at'):
            _record_time(fill.get(key), now, day)
        sold_at = parse_provider_time(raw.get('sold_at'))
        bought_at = parse_provider_time(fill.get('filled_at'))
        if bought_at and (bought_at.astimezone(CN).date() != day or sold_at and bought_at < sold_at):
            raise ValueError('buyback outside actual T sale date')
        normalized.append({'quantity': qty, 'price': unit_price, 'fee': _fee(fill.get('fee'))})
    actual_cost = sum((decimal(f['price']) * f['quantity'] for f in normalized), Decimal(0))
    if sum(f['quantity'] for f in normalized) != paired or abs(actual_cost - decimal(cost)) > Decimal('.0001'):
        raise ValueError('fills do not reconcile to totals')
    return {**raw, 'quantity': sold, 'buyback_quantity': paired, 'sell_price': price,
            'buyback_cost': float(actual_cost), 'fills': normalized, 'sell_fee': _fee(raw.get('sell_fee')),
            'fee_reserve': max(20., _number(raw.get('fee_reserve', 20)))}


def _mark(quote, now):
    if not isinstance(quote, dict):
        return None
    stamp = parse_provider_time(quote.get('source_timestamp'))
    price = nonnegative(quote.get('price'))
    if (stamp is None or not price or stamp.astimezone(CN).date() != now.date()
            or not 0 <= (now - stamp).total_seconds() <= 90):
        return None
    return price, stamp.astimezone(CN).isoformat(timespec='seconds')


def _sum(rows, key):
    values = [decimal(row[key]) for row in rows if row.get(key) is not None]
    return money(sum(values, Decimal(0))) if values else None


def build_review(book, quotes=None, now=None):
    """Return a seven-calendar-day report attributed to each T sale's date.

    Quotes map security codes to ``price`` and ``source_timestamp``. Only fresh,
    same-day provider timestamps can mark shares which have not been bought back.
    Missing marks/fees remain unknown and never enter realized paired profit.
    """
    now = now or datetime.now(CN)
    now = now.replace(tzinfo=CN) if now.tzinfo is None else now.astimezone(CN)
    since = now.date() - timedelta(days=6)
    rows = []
    invalid = 0
    outside = 0
    cycles = book.get('cycles', []) if isinstance(book, dict) else []
    if not isinstance(cycles, list):
        cycles = []
        invalid += 1
    # Duplicate identities are all excluded; choosing either could conceal a
    # conflicting fill history and inflate the reported sample count.
    ids = [r.get('id') for r in cycles if isinstance(r, dict) and isinstance(r.get('id'), str)]
    duplicates = {ident for ident, count in Counter(ids).items() if count > 1}
    for raw in cycles:
        try:
            if not isinstance(raw, dict):
                raise ValueError('invalid cycle')
            day = date.fromisoformat(raw['date'])
            if day > now.date():
                raise ValueError('future cycle')
            if day < since:
                outside += 1
                continue
            if raw.get('id') in duplicates:
                raise ValueError('duplicate cycle identity')
            cycle = _cycle(raw, now, day)
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        costs = summarize_cycle_costs(cycle)
        remaining = cycle['quantity'] - cycle['buyback_quantity']
        row = {
            'id': cycle['id'], 'code': cycle['code'], 'name': cycle.get('name', cycle['code']),
            'date': day.isoformat(), 'status': cycle['status'], 'quantity': cycle['quantity'],
            'paired_quantity': cycle['buyback_quantity'], 'unmatched_quantity': remaining,
            'sell_price': cycle['sell_price'], **costs,
            'comparison_status': 'fully_paired' if not remaining else 'quote_missing_or_stale',
            'unmatched_price': None, 'unmatched_quote_at': None,
            'unmatched_gross_comparison': None, 'unmatched_comparison': None,
            'comparison_note': '未接回余量相对继续持有的市值差额；不是已完成做T盈利，不含假设接回费用。',
        }
        mark = _mark((quotes or {}).get(cycle['code']), now) if remaining and isinstance(quotes, (dict, type(None))) else None
        if mark:
            current, stamp = mark
            gross = money((decimal(cycle['sell_price']) - decimal(current)) * remaining)
            row.update(unmatched_price=current, unmatched_quote_at=stamp, unmatched_gross_comparison=gross)
            fee = costs['unmatched_sell_fee_actual']
            if fee is None:
                row['comparison_status'] = 'sell_fee_unknown'
            else:
                row['unmatched_comparison'] = money(decimal(gross) - decimal(fee))
                row['comparison_status'] = 'marked_comparison'
        rows.append(row)
    rows.sort(key=lambda row: (row['date'], row['id']), reverse=True)
    complete = [row for row in rows if row['status'] == 'closed' and row['unmatched_quantity'] == 0
                and row['paired_net_actual'] is not None]
    wins = sum(row['paired_net_actual'] > 0 for row in complete)
    losses = sum(row['paired_net_actual'] < 0 for row in complete)
    actual = [row for row in rows if row['paired_net_actual'] is not None]
    estimated = [row for row in rows if row['paired_net_estimate'] is not None]
    actual_gross = _sum(actual, 'paired_gross_estimate')
    actual_fees = _sum(actual, 'paired_fee_actual')
    summary = {
        'cycle_count': len(rows), 'completed_cycles': sum(r['status'] == 'closed' for r in rows),
        'open_cycles': sum(r['status'] == 'open' for r in rows), 'cash_cycles': sum(r['status'] == 'cash' for r in rows),
        'actual_paired_count': len(actual), 'estimated_paired_count': len(estimated),
        'actual_paired_net': _sum(actual, 'paired_net_actual'),
        'estimated_paired_net': _sum(estimated, 'paired_net_estimate'),
        'actual_paired_gross': actual_gross, 'actual_paired_fees': actual_fees,
        'cost_ratio_pct': round(actual_fees / actual_gross * 100, 2) if actual_fees is not None and actual_gross and actual_gross > 0 else None,
        'closed_actual_samples': len(complete), 'wins': wins, 'losses': losses,
        'flat': len(complete) - wins - losses,
        'win_rate': round(wins / len(complete) * 100, 2) if complete else None,
        'average_closed_net': money(decimal(_sum(complete, 'paired_net_actual')) / len(complete)) if complete else None,
        'unmatched_mark_comparison': _sum(rows, 'unmatched_comparison'),
        'unmatched_marked_count': sum(r['comparison_status'] == 'marked_comparison' for r in rows),
        'unmatched_unpriced_count': sum(r['unmatched_quantity'] > 0 and r['comparison_status'] != 'marked_comparison' for r in rows),
        'excluded_invalid_cycles': invalid, 'outside_window_cycles': outside,
        'sample_status': 'unvalidated',
        'sample_note': f'本期仅有{len(complete)}轮完整成交且费用齐全的样本；短期结果不能证明策略有效，仍需覆盖不同市场环境的样本外验证。',
    }
    return {
        'asof': now.isoformat(timespec='seconds'), 'window_start': since.isoformat(), 'window_end': now.date().isoformat(),
        'window_label': '最近7个自然日，按本轮T卖出日期归属；不是最近5个交易日',
        'summary': summary, 'rows': rows,
        'notes': [
            '已配对净价差以真实卖出与接回成交、分摊实付费用计算；只衡量相对未做T的增量，不是账户总收益。',
            '费用待补的估算单独列示，不并入实付净价差或胜率；无样本显示空值。',
            '未接回余量的市值比较单列，已结束但留现金的余量也不算完成一轮T；过期报价不估值。',
            '没有完整资金曲线，不能据此计算账户最大回撤或年化收益；模拟、样本外验证仍不可省略。',
        ],
    }
