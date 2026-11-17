"""Replay actual T reminder rules against a predeclared, unchanged reference basket."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pandas as pd

from price_review import review_price
from quote_health import parse_provider_time
from t_engine import _buyback_blockers, _restore_blockers, _risk_conflict, build_t_observations
from v1_backtest import DEFAULT_COSTS, costs_for, execution_time, money, round_lot, try_price
from v1_backtest_data import MINUTE_ENDS


PHASES = ('sell_ready', 'buyback_ready')
POLICY = (
    '两组从同一估值底仓和剩余现金开始，期初底仓按区间前一交易日收盘价计值。',
    '底仓、固定止损和可选固定止盈须事前注明。只比较做 T 干预，其他买卖及实际账户的临盘判断不模拟。',
    '价格结构风险复用当时可见的已完成日线，失效或走弱时暂停做 T。价格采用数据包原始口径。',
    '以已完成分钟收盘价代替盘中观察价，按真实成交额计算 VWAP；信号只能在相邻下一根开盘模拟，扣费并加不利滑点。',
    '当日新买不得当日卖。未完成 T 不跨日接回，同证券待人工复核期间不重开。',
    '成交占比按每只证券、日期、阶段的唯一提醒计算，任意部分成交记为有成交；净值与最大回撤用日末估值。',
)


def _positive(value):
    return type(value) in (int, float) and 0 < value < 1e12


def _seed(bundle, start):
    scenario = bundle.get('t_replay')
    if not isinstance(scenario, dict) or not isinstance(scenario.get('positions'), list) or not scenario['positions']:
        raise ValueError('缺少事前锁定的做 T 底仓场景，不能核验原仓不动对照。')
    stamp = parse_provider_time(scenario.get('recorded_at'))
    source = scenario.get('source')
    if not stamp or stamp >= execution_time(start, '09:30') or not isinstance(source, str) or not source.strip():
        raise ValueError('做 T 底仓须记录来源和带时区的事前时间，且须早于回放首日开盘。')
    seeds = {}
    for row in scenario['positions']:
        if not isinstance(row, dict):
            raise ValueError('做 T 底仓记录无效。')
        code, qty, stop = (row.get(key) for key in ('code', 'quantity', 'stop_loss'))
        if (not isinstance(code, str) or code not in bundle['securities'] or code in seeds
                or type(qty) is not int or qty < 100 or qty % 100 or qty >= 10**10 or not _positive(stop)):
            raise ValueError('做 T 底仓须是数据包内不重复证券、整手持仓及正数固定止损。')
        plan = {'stop_loss': stop}
        previous = stop
        for field in ('take_profit_1', 'take_profit_2'):
            if field in row:
                value = row[field]
                if not _positive(value) or value <= previous:
                    raise ValueError('做 T 场景固定止盈须依次高于止损。')
                plan[field] = previous = value
        seeds[code] = {'quantity': qty, 'plan': plan, 'type': bundle['securities'][code]['type']}
    return seeds, source.strip(), stamp.isoformat()


def _depth_and_amount(bar):
    volume, amount = bar.get('volume'), bar.get('amount')
    return (
        type(volume) is int and volume >= 0
        and type(amount) in (int, float) and 0 <= amount < 1e16
        and (amount == 0 if volume == 0 else
             amount > 0 and bar['low'] * volume - .011 <= amount <= bar['high'] * volume + .011)
    )


def _record_issue(errors, examples, reason, day, code):
    errors[reason] += 1
    if len(examples) < 12:
        examples.append({'date': day, 'code': code, 'reason': reason})


def _coverage(bundle, seeds, days):
    errors = Counter()
    examples = []
    structures = {day: {} for day in days}
    for code in sorted(seeds):
        sessions = bundle['securities'][code]['sessions']
        for day in days:
            session = sessions.get(day)
            if not session:
                issue = '缺少底仓回放场次'
            elif not session['corporate_action_clear']:
                issue = '停复牌、除权或公司行动待核对'
            elif session['status'] == 'suspended':
                continue
            elif tuple(bar['time'] for bar in session['minutes']) != MINUTE_ENDS:
                issue = '缺少完整分钟时间序列'
            elif not all(_depth_and_amount(bar) for bar in session['minutes']):
                issue = '缺少原始分钟成交额或金额与量价单位不一致'
            elif not any(bar['volume'] for bar in session['minutes']):
                issue = '开市记录无成交，无法核验分钟观察价'
            else:
                structures[day][code] = _prior_structure(bundle, code, day)
                if structures[day][code] is not None:
                    continue
                issue = '缺少截至前一交易日的完整结构风险证据'
            _record_issue(errors, examples, issue, day, code)
    return errors, examples, structures


def _prior_structure(bundle, code, day):
    prior = [bar for bar in bundle['securities'][code]['daily']
             if bar['date'] < day and bar.get('complete', True)]
    if len(prior) < 60:
        return None
    history = pd.DataFrame.from_records(prior[-60:])
    now = execution_time(day, '09:30')
    calendar = bundle['calendar']
    observed = review_price(history, prior[-1]['close'], now=now, trading_dates=calendar)
    if not observed['valid']:
        return None
    support = observed['support']
    # At exactly support, the support-break branch cannot hide the weakening branch.
    weak = review_price(history, support, now=now, trading_dates=calendar)
    return support, weak['level'] if weak['trigger'] == 'structure_weakness' else None


def _metrics(initial, curve, fee, fill_count):
    if not curve:
        return dict(net_pnl=None, net_return_pct=None, max_drawdown_pct=None, fees=None,
                    final_equity=None, fill_count=None)
    peak = initial
    drawdown = 0
    for row in curve:
        peak = max(peak, row['equity'])
        drawdown = max(drawdown, (peak - row['equity']) / peak * 100)
    pnl = money(curve[-1]['equity'] - initial)
    return dict(net_pnl=pnl, net_return_pct=round(pnl / initial * 100, 4),
                max_drawdown_pct=round(drawdown, 4), fees=money(fee),
                final_equity=curve[-1]['equity'], fill_count=fill_count)


def _summary_reminders(events, covered=True):
    result = {}
    for phase in PHASES:
        subset = [event for event in events if event['kind'] == phase]
        count = sum(event['filled'] for event in subset)
        result[phase] = dict(triggers=len(subset) if covered else None,
                             filled=count if covered else None,
                             fill_rate_pct=round(count / len(subset) * 100, 2) if subset else None)
    return result


def _blocked(message, errors=None, examples=None):
    return dict(covered=False, sample_count=0, warnings=[message], policy=list(POLICY),
                strategy=_metrics(1, [], 0, 0), baseline=_metrics(1, [], 0, 0),
                reminders=_summary_reminders([], False), coverage=dict(errors or {}), coverage_examples=examples or [],
                events=[], fills=[], equity_curve=[], baseline_curve=[], unmatched_cycles=0, unmatched_by_code={},
                relative_net_pnl=None)


class TReplay:
    def __init__(self, bundle, seeds, initial, old_close, costs, structures):
        self.bundle = bundle
        self.seeds = seeds
        self.initial = self.high_water = initial
        self.costs = costs
        self.structures = structures
        self.original = {code: row['quantity'] for code, row in seeds.items()}
        self.holdings = self.original.copy()
        self.last_close = old_close.copy()
        self.cash = self.initial_cash = money(initial - sum(qty * old_close[code] for code, qty in self.original.items()))
        self.events, self.fills, self.cycles = {}, [], {}
        self.equity_curve, self.baseline_curve = [], []
        self.fee_model = {kind: {'commission_per_10k': costs['commission_rate'] * 10000,
                                 'minimum_commission': costs['minimum_commission']} for kind in ('stock', 'etf')}

    def _value(self, qty, cash, prices):
        return money(cash + sum(q * prices[code] for code, q in qty.items()))

    def _snapshot(self, prices):
        equity = self._value(self.holdings, self.cash, prices)
        if equity <= 0:
            raise ValueError('回放净资产不足。')
        self.high_water = max(equity, self.high_water)
        drawdown = money(self.high_water - equity)
        positions = [{'code': code, 'name': code, 'type': self.seeds[code]['type'], 'quantity': qty}
                     for code, qty in self.holdings.items()]
        plans = {code: row['plan'] for code, row in self.seeds.items()}
        cfg = dict(cash=self.cash, positions=positions, trade_plans=plans,
                   strategy_v1={'enabled': True}, t_settings={'enabled': True})
        breached = [code for code, qty in self.holdings.items()
                    if qty and prices[code] <= plans[code]['stop_loss']]
        stress = money(sum(max(0, prices[code] - plans[code]['stop_loss']) * qty + 20
                           for code, qty in self.holdings.items() if qty))
        account = dict(initialized=True, verified=True, needs_reconcile=False, paused=drawdown >= 500,
                       drawdown=drawdown, budget_exhausted=drawdown + stress >= 1000)
        risk = dict(valid=True, equity=equity, stop_breaches=breached, existing_planned_stress=stress,
                    available_new_risk=max(0, money(1000 - drawdown - stress)) if not breached else 0,
                    strategy={'account': account, 'exposure_pct': (equity - self.cash) / equity * 100})
        return cfg, risk

    def _cycle_list(self):
        return list(self.cycles.values())

    def _quote(self, code, price, session, stamp):
        plan = self.seeds[code]['plan']
        review = {}
        for key in ('take_profit_2', 'take_profit_1'):
            if key in plan and price >= plan[key]:
                review = {'action': 'reduce', 'trigger': key}
                break
        if not review:
            support, weakness = self.structures[stamp.date().isoformat()][code]
            if price < support:
                review = {'action': 'review', 'trigger': 'structure_support'}
            elif weakness is not None and price < weakness:
                review = {'action': 'review', 'trigger': 'structure_weakness'}
        return dict(price=price, source_timestamp=stamp.isoformat(), strategy_exit=review,
                    at_limit_up=price >= session['upper_limit'], at_limit_down=price <= session['lower_limit'],
                    limit_status_verified=True)

    def _execute(self, day, label, opens, sessions, pending, available, minute_books):
        at = execution_time(day, label, open_of_bar=True)
        for code, signal, previous in pending:
            event = self.events[(day, code, signal['trigger'])]
            event['attempts'] += 1
            kind = self.seeds[code]['type']
            side = 'sell' if signal['trigger'] == 'sell_ready' else 'buy'
            reference = opens[code]
            price = try_price(reference, kind, side, self.costs['slippage_rate'])
            context = self._quote(code, reference, sessions[code], at)
            cfg, risk = self._snapshot(opens)
            position = next(row for row in cfg['positions'] if row['code'] == code)
            qty = min(signal['shares'], round_lot(previous['volume'] * self.costs['prior_minute_participation']))
            cycle = self.cycles.get(code)
            if not sessions[code]['lower_limit'] < min(reference, price) <= max(reference, price) < sessions[code]['upper_limit']:
                reason = 'price_limit'
            elif _risk_conflict(cfg, position, context, risk, price):
                reason = 'exit_priority'
            elif side == 'sell':
                qty = min(qty, available[code]['available_quantity'])
                reason = 'risk' if _restore_blockers(cfg, risk, position['quantity'], price, code) else None
                # Re-evaluate the live fee threshold at the modeled sell price,
                # using the signal prefix VWAP and the shares that could fill.
                if qty:
                    notional = price * qty
                    modeled_fees = sum(costs_for(kind, order, notional, self.costs)['fees']
                                       for order in ('sell', 'buy'))
                    reserve = max(signal['fee_reserve'], modeled_fees)
                    if (price - signal['vwap']) * qty < 2 * reserve:
                        reason = reason or 'sale_spread_below_cost'
            else:
                qty = min(qty, cycle['quantity'] - cycle['buyback_quantity'])
                reason = 'buyback_cap' if price > signal['level'] else None
                if qty >= 100 and not reason and _buyback_blockers(
                        cfg, position, context, risk, cycle, minute_books, qty, price, signal['fee_reserve']):
                    reason = 'risk'
            fee = costs_for(kind, side, price * qty, self.costs)['fees']
            if qty < 100:
                reason = reason or 'prior_minute_depth_or_inventory'
            elif side == 'buy' and money(price * qty + fee) > self.cash:
                reason = reason or 'cash'
            if reason:
                event['last_blocker'] = reason
                continue
            fill = dict(code=code, date=day, time=label, at=at.isoformat(), quantity=qty, side=side,
                        price=price, reference_price=reference, fee=fee, signal_at=signal['signal_at'])
            self.fills.append(fill)
            event['filled'] = True
            event['filled_quantity'] += qty
            if side == 'sell':
                self.cash = money(self.cash + price * qty - fee)
                self.holdings[code] -= qty
                available[code]['available_quantity'] -= qty
                available[code]['base_quantity'] = self.holdings[code]
                self.cycles[code] = dict(id=signal['id'], code=code, date=day, status='open',
                                         base_quantity=position['quantity'], quantity=qty, buyback_quantity=0,
                                         sell_price=price, sell_fee=fee, fee_reserve=signal['fee_reserve'])
            else:
                self.cash = money(self.cash - price * qty - fee)
                self.holdings[code] += qty
                available[code]['base_quantity'] = self.holdings[code]
                cycle['buyback_quantity'] += qty
                if cycle['buyback_quantity'] == cycle['quantity']:
                    cycle['status'] = 'closed'
                    cycle['completed'] = True
            self._snapshot(opens)

    def run_day(self, day):
        sessions = {code: self.bundle['securities'][code]['sessions'][day] for code in self.holdings}
        opened = {code: session['minutes'] for code, session in sessions.items() if session['status'] == 'open'}
        available = {code: dict(date=day, base_quantity=qty, available_quantity=qty)
                     for code, qty in self.holdings.items()}
        minutes = {code: [] for code in opened}
        totals = {code: [0, 0.0] for code in opened}
        prices = self.last_close.copy()
        pending = []
        last_at = None
        for index, label in enumerate(MINUTE_ENDS):
            current = {code: rows[index] for code, rows in opened.items()}
            at = execution_time(day, label)
            opens = {**prices, **{code: bar['open'] for code, bar in current.items()}}
            books = dict(availability=available, cycles=self._cycle_list(), fee_model=self.fee_model)
            if pending and at - timedelta(minutes=1) == last_at:
                self._execute(day, label, opens, sessions, pending, available, books)
            self._snapshot(opens)
            prices.update({code: bar['close'] for code, bar in current.items()})
            cfg, risk = self._snapshot(prices)
            for code, bar in current.items():
                totals[code][0] += bar['volume']
                totals[code][1] += bar['amount']
                # Live adapter stores the minute start and the source is the
                # now-complete aggregate. Volume is in lots in that API.
                minutes[code].append(dict(timestamp=(at - timedelta(minutes=1)).isoformat(), price=bar['close'],
                                          cum_volume=totals[code][0] / 100, cum_amount=totals[code][1]))
            pending = []
            last_at = at
            if index < 29 or label >= '14:45' or not current:
                continue
            quote = {code: self._quote(code, bar['close'], sessions[code], at) for code, bar in current.items()}
            data = {code: {'valid': True, 'source_timestamp': at.isoformat(), 'rows': bars}
                    for code, bars in minutes.items()}
            books['cycles'] = self._cycle_list()
            observation = build_t_observations(cfg, quote, risk, data, books, now=at)
            by_code = {row['code']: row for row in observation['rows']}
            for signal in sorted(observation['signals'], key=lambda entry: entry['code']):
                code, phase = signal['code'], signal['trigger']
                if phase not in PHASES or signal.get('tradable') is not True:
                    continue
                key = (day, code, phase)
                self.events.setdefault(key, dict(kind=phase, code=code, date=day, at=at.isoformat(),
                                                  filled=False, attempts=0, filled_quantity=0, last_blocker=None))
                row = by_code[code]
                volume, amount = totals[code]
                details = {**signal, 'vwap': amount / volume, 'fee_reserve': row['fee_reserve'],
                           'signal_at': at.isoformat()}
                pending.append((code, details, current[code]))
        for event in self.events.values():
            if event['date'] == day and event['attempts'] == 0:
                event['last_blocker'] = 'no_adjacent_open'
        self.last_close = prices
        self.equity_curve.append(dict(date=day, equity=self._value(self.holdings, self.cash, prices)))
        self.baseline_curve.append(dict(date=day, equity=self._value(self.original, self.initial_cash, prices)))


def evaluate_t_window(bundle, start, end, initial_cash, costs=None):
    """Return one comparable T/hold trajectory, or a reason the evidence is insufficient."""
    calendar = bundle['calendar']
    if start not in calendar or end not in calendar or start > end:
        raise ValueError('做 T 回放区间须位于数据包交易日历内。')
    if not _positive(initial_cash) or initial_cash < .01:
        raise ValueError('做 T 回放初始资金须为有限正数。')
    try:
        seeds, source, seed_at = _seed(bundle, start)
    except ValueError as error:
        return _blocked(str(error))
    index = calendar.index(start)
    if index == 0:
        return _blocked('缺少区间前一交易日的底仓计值行情。')
    prior_day = calendar[index - 1]
    previous = {}
    for code in seeds:
        bar = next((r for r in reversed(bundle['securities'][code]['daily']) if r['date'] == prior_day), None)
        if not bar or bar.get('complete') is False:
            return _blocked(f'{code} 缺少前一交易日的完整原始收盘价。')
        previous[code] = bar['close']
    if money(sum(previous[code] * row['quantity'] for code, row in seeds.items())) > money(initial_cash):
        return _blocked('预设底仓本身的市值超过回放初始净资产。')
    window = calendar[index:calendar.index(end) + 1]
    errors, examples, structures = _coverage(bundle, seeds, window)
    if errors:
        return _blocked('做 T 回放证据不完整，暂不计算区间可成交比例与收益。', errors, examples)
    model = dict(DEFAULT_COSTS, **(costs or {}))
    simulation = TReplay(bundle, seeds, money(initial_cash), previous, model, structures)
    completed = 0
    for day in window:
        simulation.run_day(day)
        completed += sum(cycle.get('completed') is True and cycle['date'] == day
                         for cycle in simulation.cycles.values())
    events = list(simulation.events.values())
    fees = sum(fill['fee'] for fill in simulation.fills)
    strategy = _metrics(simulation.initial, simulation.equity_curve, fees, len(simulation.fills))
    baseline = _metrics(simulation.initial, simulation.baseline_curve, 0, 0)
    unmatched = {code: quantity - simulation.holdings[code] for code, quantity in simulation.original.items()
                 if quantity > simulation.holdings[code]}
    return dict(covered=True, sample_count=completed, strategy=strategy, baseline=baseline,
                reminders=_summary_reminders(events), warnings=[], policy=list(POLICY), source=source,
                seed_at=seed_at, initial_equity=simulation.initial, initial_cash_balance=simulation.initial_cash,
                seed_positions=[dict(code=code, quantity=value['quantity'], **value['plan'])
                                for code, value in sorted(seeds.items())], events=events, fills=simulation.fills, coverage={}, coverage_examples=[],
                equity_curve=simulation.equity_curve, baseline_curve=simulation.baseline_curve,
                unmatched_cycles=sum(cycle['status'] == 'open' for cycle in simulation.cycles.values()),
                unmatched_by_code=unmatched,
                relative_net_pnl=money(strategy['net_pnl'] - baseline['net_pnl']))
