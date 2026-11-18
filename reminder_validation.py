"""Compare fixed V1 and T reminder rules across chronological replay windows."""
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from strategy_v1 import strategy_prefs
from v1_backtest import costs_for, money, round_lot, run_backtest, try_price
from v1_backtest_report import _drawdowns


TRADE_KINDS = {'breakout': '买入突破', 'stop_loss': '固定止损',
               'take_profit_1': '第一止盈', 'take_profit_2': '第二止盈'}
T_KINDS = {'sell_ready': '做 T 高抛', 'buyback_ready': '做 T 接回'}
GAPS = ('missing_sessions', 'partial_minute_sessions', 'corporate_action_unverified_sessions',
        'daily_unready_sessions', 'benchmark_unready_days', 'universe_missing_days')
MIN_DAYS = 20
MIN_OUT_TRADES = 30
MIN_TOTAL_TRADES = 100
MIN_OUT_TRIGGERS = 20


def no_data():
    return {
        'meta': {'demo': None, 'warnings': []},
        'status': '暂无真实历史回放报告，买卖和做 T 提醒待验证。',
        'periods': [], 'recommendations': [
            {'key': key, 'label': label, 'status': '待验证', 'reason': '缺少回放数据或评估结果'}
            for key, label in {**TRADE_KINDS, **T_KINDS}.items()
        ],
    }


def empty_metric():
    return {'net_return_pct': None, 'net_pnl': None, 'max_drawdown_pct': None, 'fees': None}


def compare_curve(initial, curve, fees):
    if not curve or any(value is None for value in curve):
        return {**empty_metric(), 'fees': fees}
    pnl = money(curve[-1] - initial)
    return {'net_pnl': pnl, 'net_return_pct': round(pnl * 100 / initial, 4),
            'max_drawdown_pct': _drawdowns(initial, curve)[1], 'fees': fees}


def baseline_window(bundle, start, end, initial, costs, code, *, rules=None):
    """Use the same cost and capacity model for a single preselected buy-and-hold."""
    if code is None:
        return compare_curve(initial, [initial], 0), True, []
    item = bundle['securities'][code]
    kind = item['type']
    cash, quantity = initial, 0
    fee, last_close = 0, None
    cap = initial * strategy_prefs({'strategy_v1': rules})['max_position_pct'] / 100
    calendar = bundle['calendar']
    curve = []
    for day in calendar[calendar.index(start):calendar.index(end) + 1]:
        session = item['sessions'].get(day)
        if not session or session.get('corporate_action_clear') is not True:
            return empty_metric(), False, [f'{day} 基准标的场次或公司行动待核验']
        if session['status'] == 'suspended':
            curve.append(money(cash + quantity * last_close) if quantity else cash)
            continue
        bars = {bar['time']: bar for bar in session['minutes']}
        close = bars.get('15:00')
        if close is None:
            return empty_metric(), False, [f'{day} 基准未取得完整收盘估值']
        if not quantity:
            first, second = bars.get('09:31'), bars.get('09:32')
            if first is None or second is None:
                return empty_metric(), False, [f'{day} 基准缺少首两个分钟，不能复现入场']
            reference = second['open']
            price = try_price(reference, kind, 'buy', costs['slippage_rate'])
            if session['lower_limit'] < reference < session['upper_limit'] and price < session['upper_limit']:
                proposed = min(round_lot(cap / price),
                               round_lot(first['volume'] * costs['prior_minute_participation']))
                if proposed:
                    fee = costs_for(kind, 'buy', proposed * price, costs)['fees']
                    if proposed * price + fee <= cash:
                        quantity = proposed
                        cash = money(cash - quantity * price - fee)
                    else:
                        fee = 0
        last_close = close['close']
        curve.append(money(cash + quantity * last_close))
    if not quantity:
        return compare_curve(initial, curve, fee), False, ['基准因价格限制、资金或深度未成交，不能用作筛选证据']
    return compare_curve(initial, curve, fee), True, []


def index_reference(bundle, start, end):
    """Passive index movement is context only, with no executable price/fee claim."""
    days = [day for day in bundle['calendar'] if start <= day <= end]
    by_day = {row['date']: row for row in bundle['benchmark'] if row.get('complete', True)}
    if any(day not in by_day for day in days):
        return None
    initial = by_day[start]['open']
    prices = [by_day[day]['close'] for day in days]
    return {'return_pct': round((prices[-1] / initial - 1) * 100, 4),
            'max_drawdown_pct': _drawdowns(initial, prices)[1]}


def reminder_counts(rows, kinds):
    result = {}
    for kind in kinds:
        triggers = [row for row in rows if row.get('kind') == kind]
        filled = sum(row.get('filled') is True for row in triggers)
        result[kind] = {'triggers': len(triggers), 'filled': filled,
                        'fill_rate_pct': round(100 * filled / len(triggers), 2) if triggers else None,
                        'partial': sum(row.get('status') == 'partial' for row in triggers),
                        'blocked_reasons': dict(Counter(reason for row in triggers if not row.get('filled')
                                                        for reason in row.get('blocked_reasons', [])))}
    return result


def stock_metrics(report):
    summary = report['summary']
    return {
        'net_return_pct': summary['return_pct'] if summary['valuations_complete'] else None,
        'net_pnl': summary['net_pnl'] if summary['valuations_complete'] else None,
        'max_drawdown_pct': summary['max_drawdown_pct'], 'fees': summary['fee_total'],
    }


def stock_coverage(report):
    summary, meta = report['summary'], report['meta']
    return bool(summary['valuations_complete'] and summary['execution_coverage_complete']
                and not any(meta.get('input_coverage', {}).get(key) for key in GAPS))


def decision(periods, kind, group, demo, baseline_code, dated_pool):
    if kind == 'stop_loss':
        return '保留风控', '固定止损是风险约束，样本不足时仍待验证。触发和可成交比例照样统计。'
    if demo:
        return '待验证', '本次是合成样例，不能判断实盘效果。'
    if group == 'trade' and not dated_pool:
        return '待验证', '缺少按信号日留存的历史股票池，无法排除当前名单带来的偏差。'
    if kind == 'breakout' and not baseline_code:
        return '待验证', '现金基准可做初筛。需补预先指定的可交易持有基准后才能筛选。'
    out = [period[group] for period in periods if period['key'] in ('validation', 'holdout')]
    if len(out) != 2 or not all(p['covered'] for p in out):
        return '待验证', '验证或留出区间缺少必要的行情、成交或底仓证据。'
    if (sum(period[group]['sample_count'] for period in periods if period[group]['covered']) < MIN_TOTAL_TRADES
            or any(p['sample_count'] < MIN_OUT_TRADES or p['session_count'] < MIN_DAYS
                   or (p['reminders'][kind]['triggers'] or 0) < MIN_OUT_TRIGGERS for p in out)):
        return '待验证', (f'需合计至少 {MIN_TOTAL_TRADES} 笔/轮完整样本；验证及留出各至少 {MIN_DAYS} 个交易日、'
                         f'{MIN_OUT_TRADES} 笔/轮和 {MIN_OUT_TRIGGERS} 次本项触发。')
    references = []
    for sample in out:
        comparator = sample['ablation'].get(kind) if group == 'trade' and kind.startswith('take_profit') else sample['baseline']
        strategy = sample['strategy']
        if (not comparator or comparator.get('covered') is False
                or any(metric.get(key) is None for metric in (strategy, comparator)
                       for key in ('net_return_pct', 'max_drawdown_pct'))):
            return '待验证', '用于比较的基准或单项移除回放不完整。'
        references.append((strategy['net_return_pct'] - comparator['net_return_pct'],
                           strategy['max_drawdown_pct'] - comparator['max_drawdown_pct']))
    basis = '与同底仓不动组合' if group == 't' else '与单项移除组' if kind.startswith('take_profit') else '与指定持有基准'
    if all(net <= 0 and drawdown >= 0 for net, drawdown in references):
        return '移出强提醒候选', f'在验证与留出两段，{basis}相比扣费收益和日终回撤均未改善。'
    if all(net > 0 and drawdown <= 0 for net, drawdown in references):
        return '验证通过', f'本次验证与留出两段，{basis}相比扣费收益均改善，日终回撤未上升。'
    return '待验证', f'{basis}的分段结果不一致，或收益与回撤存在取舍。'


def evaluate_reminders(bundle, result, *, baseline_code=None):
    from t_history_replay import evaluate_t_window

    meta = result['meta']
    demo = meta['demo']
    validation_files = ('reminder_validation.py', 't_history_replay.py', 't_engine.py',
                        't_costs.py', 'price_review.py', 'v1_validation.py')
    root = Path(__file__).resolve().parent
    validator_digest = sha256(b''.join((root/name).read_bytes() for name in validation_files)).hexdigest()
    initial, costs = meta['initial_cash'], meta['costs']
    if baseline_code is not None and baseline_code not in bundle['securities']:
        raise ValueError('预先指定的持有基准必须是历史数据包中的证券代码')
    periods = []
    for original in result['periods']:
        key, start, end = (original[x] for x in ('key', 'start', 'end'))
        main = result['reports'][key]
        baseline, baseline_ready, baseline_warnings = baseline_window(
            bundle, start, end, initial, costs, baseline_code, rules=meta['rules'])
        by_kind = reminder_counts(main['reminders'], TRADE_KINDS)
        scoped_coverage = stock_coverage(main)
        ablation = {}
        for kind in ('take_profit_1', 'take_profit_2'):
            if by_kind[kind]['triggers']:
                variant = run_backtest(bundle, start, end, initial, bundle_sha256=meta['bundle_sha256'],
                                       costs=costs, rules=meta['rules'], disabled_profit_targets=(kind,))
                ablation[kind] = {**stock_metrics(variant), 'covered': stock_coverage(variant)}
        trade = {
            'strategy': stock_metrics(main), 'baseline': {**baseline, 'covered': baseline_ready},
            'reminders': by_kind, 'ablation': ablation, 'index_reference': index_reference(bundle, start, end),
            'covered': scoped_coverage, 'sample_count': main['summary']['closed_trade_count'],
            'session_count': main['summary']['session_count'], 'warnings': original['warnings'] + baseline_warnings,
        }
        t = evaluate_t_window(bundle, start, end, initial, costs, rules=meta['rules'], t_settings=meta['t_settings'])
        t['session_count'] = main['summary']['session_count']
        periods.append(dict(key=key, label=original['label'], start=start, end=end, trade=trade, t=t))
    recommendations = []
    for group, items in (('trade', TRADE_KINDS), ('t', T_KINDS)):
        for kind, label in items.items():
            status, reason = decision(periods, kind, group, demo, baseline_code, meta.get('universe_source'))
            recommendations.append(dict(key=kind, label=label, status=status, reason=reason))
    return {
        'meta': {
            'demo': demo, 'source': meta['source'], 'generated_at': datetime.now(timezone.utc).isoformat(),
            'baseline_code': baseline_code, 'bundle_sha256': meta['bundle_sha256'],
            'engine_sha256': meta['engine_sha256'], 'validator_sha256': validator_digest,
            'baseline_label': (f'{baseline_code} 买入持有（最多 {meta["rules"]["max_position_pct"]:g}% 仓位）'
                               if baseline_code else '现金不动'),
            't_baseline_label': '相同底仓不动', 'costs': costs, 'rules': meta['rules'],
            't_settings': meta['t_settings'], 'fee_source_note': meta['fee_source_note'],
            'warnings': [
                '各区间独立估值。可成交是触发后至少有一笔模拟成交，包含部分成交；没有触发显示空值。',
                '突破项比较整套 V1 与持有基准，不能单独归因于突破。两档止盈分别移除再回放。',
                '时间切分只证明各段不重叠。若留出段已用于调参，需另取未查看的数据再验证。',
                meta['fee_source_note'],
                '佣金、税费与滑点已计入回放，最大回撤按日终净值计算。模拟成交受上一分钟的成交量约束，不能还原真实排队。',
                '筛选使用验证和留出两段，仅影响报告的强提醒候选。固定止损保留，实际提醒设置由用户管理。',
                '做 T 的两类提醒作为一组与原仓不动对照；合计包含未接回产生的仓位差异。',
            ],
        },
        'periods': periods,
        'recommendations': recommendations,
        'strong_candidates': [item['key'] for item in recommendations if item['status'] == '验证通过'],
        'excluded_candidates': [item['key'] for item in recommendations if item['status'] == '移出强提醒候选'],
    }
