"""Deterministic, offline A-share V1 minute replay. Uses only a supplied bundle."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path

import pandas as pd

from risk_engine import compute
from strategy_setup import CN, build_setup
from strategy_v1 import strategy_prefs
from v1_backtest_data import MINUTE_ENDS, demo_bundle, load_bundle, validate_bundle
from v1_backtest_report import save_report, summarize


ROOT = Path(__file__).resolve().parent
PROFILE = 'v1-minutes-1'
REVIEW_FIELDS = ('fundamentals_ok', 'event_clear', 'corporate_action_clear', 'resistance_clear')
DEFAULT_COSTS = {
    'commission_rate': 0.0005,
    'minimum_commission': 5.0,
    'stock_sell_stamp_rate': 0.0005,
    'stock_transfer_rate': 0.00001,
    'slippage_rate': 0.0005,
    'prior_minute_participation': 0.10,
}


def money(value):
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def execution_time(day, label, *, open_of_bar=False):
    stamp = datetime.fromisoformat(f'{day}T{label}:00').replace(tzinfo=CN)
    return stamp - timedelta(minutes=1) if open_of_bar else stamp


def try_price(reference, kind, side, slippage):
    tick = Decimal('0.001' if kind == 'etf' else '0.01')
    sign = 1 if side == 'buy' else -1
    raw = Decimal(str(reference)) * (1 + sign * Decimal(str(slippage)))
    rounding = ROUND_CEILING if side == 'buy' else ROUND_FLOOR
    return float((raw / tick).to_integral_value(rounding=rounding) * tick)


def costs_for(kind, side, notional, model):
    commission = money(max(model['minimum_commission'], notional * model['commission_rate']))
    stamp = money(notional * model['stock_sell_stamp_rate']) if kind == 'stock' and side == 'sell' else 0.0
    transfer = money(notional * model['stock_transfer_rate']) if kind == 'stock' else 0.0
    return dict(commission=commission, stamp_tax=stamp, transfer=transfer,
                fees=money(commission + stamp + transfer))


def round_lot(quantity):
    return max(0, int(quantity) // 100 * 100)


@dataclass
class Position:
    code: str
    kind: str
    plan: dict
    bought_on: str
    entry_price: float
    entry_fees: float
    initial_risk: float
    initial_quantity: int
    quantity: int
    realized_gross: float = 0.0
    exit_fees: float = 0.0
    tp1_sold: int = 0
    stop_signal_at: str | None = None
    sell_fills: list[int] = field(default_factory=list)

    @property
    def stop(self):
        stop = self.plan['stop_loss']
        return max(stop, self.entry_price) if self.tp1_sold else stop

    @property
    def first_target_quantity(self):
        if self.initial_quantity < 200:
            return self.initial_quantity
        return round_lot(self.initial_quantity / 2)


class Replay:
    def __init__(self, bundle, start, end, initial_cash, costs):
        self.bundle = bundle
        self.symbols = bundle['securities']
        self.calendar = bundle['calendar']
        if start not in self.calendar or end not in self.calendar or start > end:
            raise ValueError('开始和结束必须为数据包内已核验的交易日')
        if not isinstance(initial_cash, (int, float)) or isinstance(initial_cash, bool) or not 0.01 <= initial_cash < 1e12:
            raise ValueError('初始资金须为有限正数')
        self.start_index = self.calendar.index(start)
        self.end_index = self.calendar.index(end)
        self.cost_model = {**DEFAULT_COSTS, **(costs or {})}
        if set(self.cost_model) != set(DEFAULT_COSTS):
            raise ValueError('存在未知费用参数')
        for key, value in self.cost_model.items():
            upper = 1e5 if key == 'minimum_commission' else 1
            if type(value) not in (int, float) or not 0 <= value <= upper:
                raise ValueError(f'费用或参与率参数无效：{key}')
        if self.cost_model['slippage_rate'] >= 1:
            raise ValueError('滑点率须低于100%')
        self.initial_cash = self.cash = self.high_water = money(initial_cash)
        daily_fields = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'complete']
        self.benchmark = self.daily_frame(bundle['benchmark'], daily_fields)
        self.histories = {code: self.daily_frame(item['daily'], daily_fields)
                          for code, item in self.symbols.items()}
        self.positions = {}
        self.last_close = {}
        self.fills = []
        self.closed_trades = []
        self.equity_curve = []
        self.decisions = []
        self.seen_decisions = set()
        self.missing_execution_data = set()
        self.halted = None
        self.pause_through = -1
        self.budget_locked = False
        self.observed_intraday_drawdown = 0.0
        self.technical_signal_count = 0
        self.rules = {'enabled': True, 'exit_mode': 'price_first'}
        self.settings = strategy_prefs({'strategy_v1': self.rules})

    @staticmethod
    def daily_frame(records, fields):
        frame = pd.DataFrame.from_records(records) if records else pd.DataFrame(columns=fields)
        if records and 'complete' in frame:
            frame['complete'] = [row.get('complete', True) for row in records]
        return frame

    def note(self, day, code, reason, *, at=None, detail=None):
        key = (day, code, reason)
        if key not in self.seen_decisions:
            self.seen_decisions.add(key)
            self.decisions.append(dict(date=day, code=code, at=at, reason=reason, detail=detail))

    def setups_for(self, index):
        if index == 0:
            for code in self.symbols:
                self.note(self.calendar[index], code, 'signal_data_missing', detail='交易日历未覆盖前一交易日')
                self.missing_execution_data.add((self.calendar[index], code))
            return {}
        signal_day = self.calendar[index - 1]
        clock = execution_time(signal_day, '15:05')
        benchmark = self.benchmark.loc[self.benchmark['date'] <= signal_day]
        setups = {}
        for code, item in self.symbols.items():
            if code in self.positions:
                continue
            history = self.histories[code]
            prefix = history.loc[history['date'] <= signal_day]
            if len(prefix) < 120 or len(benchmark) < 120 or prefix.iloc[-1]['date'] != signal_day or benchmark.iloc[-1]['date'] != signal_day:
                self.note(self.calendar[index], code, 'signal_data_missing', detail='信号日或基准缺少完整日线')
                self.missing_execution_data.add((self.calendar[index], code))
                continue
            review = item['reviews'].get(signal_day) or {}
            if review and datetime.fromisoformat(review['recorded_at'].replace('Z', '+00:00')) > clock:
                review = {}
            setup = build_setup(prefix, benchmark, asset_type=item['type'],
                                name=review.get('name', code), now=clock)
            if setup['signal_date'] != signal_day:
                self.note(self.calendar[index], code, 'signal_data_missing',
                          detail='信号日或基准日线未确认完整')
                self.missing_execution_data.add((self.calendar[index], code))
            elif setup.get('eligible'):
                setups[code] = setup
        return dict(sorted(setups.items(), key=lambda x: (-x[1]['evidence']['amount20'], x[0])))

    def review_for(self, code, setup, now):
        item = self.symbols[code]
        review = item['reviews'].get(setup['signal_date'])
        if not review or datetime.fromisoformat(review['recorded_at'].replace('Z', '+00:00')) > now:
            return None
        if not all(review.get(key) is True for key in REVIEW_FIELDS):
            return None
        if item['type'] == 'etf' and review.get('equity_etf') is not True:
            return None
        return review

    def mark(self, prices):
        if self.positions.keys() - prices.keys():
            return None
        equity = money(self.cash + sum(p.quantity * prices[p.code] for p in self.positions.values()))
        self.high_water = max(self.high_water, equity)
        drawdown = money(self.high_water - equity)
        self.observed_intraday_drawdown = max(self.observed_intraday_drawdown, drawdown)
        if drawdown >= self.settings['loss_budget']:
            self.budget_locked = True
        return equity

    def record_fill(self, code, kind, side, quantity, reference, price, at, label, reason, *, signal_at=None, signal_date=None):
        notional = money(price * quantity)
        charges = costs_for(kind, side, notional, self.cost_model)
        sign = 1 if side == 'sell' else -1
        self.cash = money(self.cash + sign * notional - charges['fees'])
        fill = dict(sequence=len(self.fills) + 1, date=at.date().isoformat(), at=at.isoformat(),
                    price_bar_end=label, code=code, type=kind, side=side,
                    quantity=quantity, reference_price=reference, price=price, notional=notional,
                    reason=reason, signal_at=signal_at, signal_date=signal_date,
                    slippage=money(abs(price - reference) * quantity), **charges)
        self.fills.append(fill)
        return fill

    def risk_quantity(self, code, kind, plan, prices, price, equity, index):
        cfg = {
            'baseline_equity': self.initial_cash,
            'max_additional_loss': self.settings['loss_budget'],
            'cash': self.cash,
            'positions': [
                dict(code=p.code, type=p.kind, name=p.plan['name'], quantity=p.quantity,
                     cost=p.entry_price, sector=p.plan['sector']) for p in self.positions.values()
            ],
            'trade_plans': {p.code: {**p.plan, 'stop_loss': p.stop} for p in self.positions.values()} | {code: plan},
            'strategy_v1': self.rules,
        }
        account = dict(initialized=True, verified=True, needs_reconcile=False, opens_today=0,
                       paused=self.budget_locked or index <= self.pause_through, high_water=self.high_water,
                       drawdown=money(self.high_water - equity), adjusted_equity=equity, net_external_flow=0.0)
        result = compute(cfg, prices, fresh=True, strategy_account=account,
                         watch_quotes=[dict(code=code, price=price, asset_type=kind, name=plan['name'])])
        return result['sizing'][0], result['available_new_risk']

    def buy(self, index, label, code, setup, session, previous, bar, prices):
        day = self.calendar[index]
        now = execution_time(day, label, open_of_bar=True)
        at = now.isoformat()
        levels = setup['levels']
        trigger = levels['trigger_price']
        cap = levels['entry_price']
        if not trigger <= previous['close'] <= cap:
            return False
        tick_slippage = self.cost_model['slippage_rate']
        kind = self.symbols[code]['type']
        review = self.review_for(code, setup, now)
        if not review:
            self.note(day, code, 'point_in_time_review_missing', at=at)
            self.missing_execution_data.add((day, code))
            return False
        reference = bar['open']
        if not trigger <= reference <= cap:
            self.note(day, code, 'entry_outside_range', at=at)
            return False
        price = try_price(reference, kind, 'buy', tick_slippage)
        if not session['lower_limit'] < reference < session['upper_limit'] or price >= session['upper_limit']:
            self.note(day, code, 'entry_price_limit', at=at)
            return False
        if price > cap:
            self.note(day, code, 'slippage_above_cap', at=at)
            return False
        equity = self.mark(prices)
        if equity is None:
            self.note(day, code, 'held_quotes_missing', at=at)
            return False
        plan = {**levels, 'strategy_version': '1.0', 'setup_verified': True, 'name': review['name'],
                'sector': review['sector'], 'signal_date': setup['signal_date'],
                'review': {key: review[key] for key in REVIEW_FIELDS}, 'equity_etf': review.get('equity_etf')}
        sized, available_risk = self.risk_quantity(code, kind, plan, prices, price, equity, index)
        if not sized['max_buy_shares']:
            self.note(day, code, 'risk_blocked', at=at, detail=sized['reason'])
            return False
        qty = min(sized['max_buy_shares'], round_lot(previous['volume'] * self.cost_model['prior_minute_participation']))
        stop_fill = try_price(levels['stop_loss'], kind, 'sell', tick_slippage)
        target_fill = try_price(levels['take_profit_1'], kind, 'sell', tick_slippage)
        while qty >= 100:
            buy_fee = costs_for(kind, 'buy', qty * price, self.cost_model)['fees']
            stop_fee = costs_for(kind, 'sell', qty * stop_fill, self.cost_model)['fees']
            roundtrip = money(buy_fee + stop_fee)
            risk = money(qty * (price - levels['stop_loss']) +
                         max(self.settings['fee_reserve'], roundtrip + qty * (levels['stop_loss'] - stop_fill)))
            target_fee = costs_for(kind, 'sell', qty * target_fill, self.cost_model)['fees']
            reward = qty * (target_fill - price) - buy_fee - target_fee
            if (qty * price + buy_fee <= self.cash and risk <= min(self.settings['per_trade_risk'], available_risk)
                    and reward >= self.settings['min_reward_risk'] * risk):
                trade = self.record_fill(code, kind, 'buy', qty, reference, price, now, label, 'breakout',
                                         signal_at=now.isoformat(), signal_date=setup['signal_date'])
                trade.update(initial_risk=risk, stop_loss=levels['stop_loss'],
                             take_profit_1=levels['take_profit_1'], take_profit_2=levels['take_profit_2'],
                             entry_cap=cap, trigger_price=trigger, review_recorded_at=review['recorded_at'],
                             review_sources=review['sources'])
                self.positions[code] = Position(code, kind, plan, day, price, buy_fee, risk, qty, qty)
                return True
            qty -= 100
        self.note(day, code, 'round_lot_cost_or_depth', at=at)
        return False

    def sell(self, index, label, position, previous, bar, session):
        day = self.calendar[index]
        now = execution_time(day, label, open_of_bar=True)
        code = position.code
        tp1 = position.plan['take_profit_1']
        tp2 = position.plan['take_profit_2']
        if position.stop_signal_at:
            reason, quantity = 'stop_loss', position.quantity
        elif previous['high'] >= tp2:
            reason, quantity = 'take_profit_2', position.quantity
        elif previous['high'] >= tp1 and position.tp1_sold < position.first_target_quantity:
            reason, quantity = 'take_profit_1', position.first_target_quantity - position.tp1_sold
        else:
            return
        if day <= position.bought_on:
            self.note(day, code, 't_plus_one', at=now.isoformat(), detail=reason)
            return
        reference = bar['open']
        price = try_price(reference, position.kind, 'sell', self.cost_model['slippage_rate'])
        if reference <= session['lower_limit'] or price <= session['lower_limit']:
            self.note(day, code, 'exit_price_limit', at=now.isoformat(), detail=reason)
            return
        if reason != 'stop_loss' and price < (tp2 if reason == 'take_profit_2' else tp1):
            self.note(day, code, 'target_not_executable_at_next_open', at=now.isoformat(), detail=reason)
            return
        quantity = min(quantity, round_lot(previous['volume'] * self.cost_model['prior_minute_participation']))
        if quantity < 100:
            self.note(day, code, 'exit_depth_insufficient', at=now.isoformat(), detail=reason)
            return
        signal_at = position.stop_signal_at or execution_time(day, previous['time']).isoformat()
        fill = self.record_fill(code, position.kind, 'sell', quantity, reference, price, now, label, reason,
                                signal_at=signal_at, signal_date=position.plan['signal_date'])
        position.quantity -= quantity
        position.realized_gross = money(position.realized_gross + (price - position.entry_price) * quantity)
        position.exit_fees = money(position.exit_fees + fill['fees'])
        position.sell_fills.append(fill['sequence'])
        if reason == 'take_profit_1':
            position.tp1_sold += quantity
        if position.quantity:
            return
        net = money(position.realized_gross - position.entry_fees - position.exit_fees)
        self.closed_trades.append(dict(code=code, type=position.kind, signal_date=position.plan['signal_date'],
                                       opened_on=position.bought_on, closed_on=day, entry_price=position.entry_price,
                                       quantity=position.initial_quantity, gross_pnl=position.realized_gross,
                                       net_pnl=net, fees=money(position.entry_fees + position.exit_fees),
                                       initial_risk=position.initial_risk, net_r=round(net / position.initial_risk, 6),
                                       exit_reason=reason, sell_fills=position.sell_fills))
        del self.positions[code]
        if len(self.closed_trades) >= 3 and all(row['net_pnl'] < 0 for row in self.closed_trades[-3:]):
            self.pause_through = index + 2

    def replay_day(self, index):
        day = self.calendar[index]
        setups = self.setups_for(index)
        self.technical_signal_count += len(setups)
        cancelled = {}
        tracked = set(self.positions) | set(setups)
        sessions = {code: self.symbols[code]['sessions'].get(day) for code in tracked}
        for code in sorted(tracked):
            session = sessions[code]
            if not session or session.get('corporate_action_clear') is not True:
                reason = 'missing_execution_session' if not session else 'corporate_action_unverified'
                self.note(day, code, reason)
                self.missing_execution_data.add((day, code))
                if code in self.positions:
                    self.halted = dict(date=day, code=code, reason=reason)
                    return
            elif session['status'] == 'suspended':
                self.note(day, code, 'suspended')
            elif code in setups and session['open'] > setups[code]['levels']['entry_price']:
                self.note(day, code, 'open_above_cap', at=execution_time(day, '09:30').isoformat())
                cancelled[code] = 'open_above_cap'
        minutes = {code: {bar['time']: bar for bar in session['minutes']}
                   for code, session in sessions.items() if session and session['status']=='open'
                   and session['corporate_action_clear']}
        suspended = {code for code, session in sessions.items() if session and session['status']=='suspended'
                     and code in self.last_close}
        previous, broken = {}, set()
        opened_code = None
        for label in MINUTE_ENDS:
            current = {code: series[label] for code, series in minutes.items() if label in series}
            missing = set(minutes) - current.keys()
            for code in sorted(missing - broken):
                self.note(day, code, 'minute_gap', detail=label)
                if code in self.positions or (not opened_code and code not in cancelled):
                    self.missing_execution_data.add((day, code))
            broken.update(missing)
            # The first afternoon bar has no adjacent completed bar.
            if label == '13:01':
                previous = {}
            opening_prices = {code: bar['open'] for code, bar in current.items()}
            opening_prices.update({code: self.last_close[code] for code in suspended})
            self.mark(opening_prices)
            if label <= '14:57':
                for code, position in list(self.positions.items()):
                    if code in current and code in previous:
                        self.sell(index, label, position, previous[code], current[code], sessions[code])
            if not opened_code and not (self.positions.keys() & (broken | suspended)) and (
                '09:46' <= label <= '11:30' or '13:02' <= label <= '14:45'
            ):
                for code, setup in setups.items():
                    if code not in cancelled and code in previous and code in current and code not in broken:
                        if self.buy(index, label, code, setup, sessions[code], previous[code], current[code], opening_prices):
                            opened_code = code
                            break
            # Read high, low, close and volume only once this bar is complete.
            for code, position in self.positions.items():
                if code in current and not position.stop_signal_at and current[code]['low'] <= position.stop:
                    position.stop_signal_at = execution_time(day, label).isoformat()
                    if position.bought_on == day:
                        self.note(day, code, 't_plus_one', at=position.stop_signal_at, detail='止损已触发，等待可卖')
            closing_prices = {code: bar['close'] for code, bar in current.items()}
            closing_prices.update({code: self.last_close[code] for code in suspended})
            self.mark(closing_prices)
            previous = current
        reasons_recorded = {code for date, code, _ in self.seen_decisions if date == day}
        for code in setups:
            if code != opened_code and code not in reasons_recorded:
                reason = 'daily_entry_limit' if opened_code else 'trigger_not_filled'
                self.note(day, code, reason, at=execution_time(day, '14:45').isoformat())
        self.last_close.update(closing_prices)
        missing_marks = sorted(self.positions.keys() - closing_prices.keys())
        equity = self.mark(closing_prices)
        self.equity_curve.append(dict(date=day, equity=equity, cash=self.cash,
                                      positions=len(self.positions), missing_marks=missing_marks))
        if missing_marks:
            for code in missing_marks:
                self.note(day, code, 'closing_mark_missing')
                self.missing_execution_data.add((day, code))

    def run(self):
        for index in range(self.start_index, self.end_index + 1):
            if not self.halted:
                self.replay_day(index)
            if self.halted:
                self.equity_curve.append(dict(date=self.calendar[index], equity=None, cash=self.cash,
                                              positions=len(self.positions), missing_marks=sorted(self.positions)))
        return self


def run_backtest(bundle, start, end, initial_cash=100000.0, *, bundle_sha256=None, costs=None):
    validate_bundle(bundle)
    simulation = Replay(bundle, start, end, initial_cash, costs).run()
    summary = summarize(simulation.initial_cash, simulation.equity_curve, simulation.fills,
                        simulation.closed_trades, bundle['securities'], simulation.decisions)
    summary['execution_coverage_complete'] = not simulation.missing_execution_data and not simulation.halted
    summary['unverified_symbol_sessions'] = len(simulation.missing_execution_data)
    summary['technical_signal_count'] = simulation.technical_signal_count
    summary['observed_intraday_max_drawdown'] = simulation.observed_intraday_drawdown
    summary['coverage_reasons'] = dict(Counter(d['reason'] for d in simulation.decisions
                                               if (d['date'], d['code']) in simulation.missing_execution_data))
    engine_files = ('v1_backtest.py', 'v1_backtest_data.py', 'v1_backtest_report.py',
                    'strategy_setup.py', 'strategy_v1.py', 'risk_engine.py', 'trade_levels.py')
    return {
        'meta': {
            'engine': PROFILE, 'start': start, 'end': end, 'demo': bundle.get('demo') is True,
            'source': bundle['source'], 'calendar_source': bundle['calendar_source'],
            'bundle_sha256': bundle_sha256 or hashlib.sha256(json.dumps(
                bundle, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
            'engine_sha256': hashlib.sha256(b''.join((ROOT / f).read_bytes() for f in engine_files)).hexdigest(),
            'price_basis': bundle['price_basis'], 'initial_cash': simulation.initial_cash,
            'rules': simulation.settings, 'costs': simulation.cost_model, 'universe_size': len(bundle['securities']),
            'halted': simulation.halted,
            'execution_model': '收盘后计算信号。次交易日观察已完成分钟，按下一分钟开盘估算成交，'
                               '可成交量以此前一完整分钟成交量乘参与率估算。T+1，固定止损和两段目标，'
                               '同根分钟先判止损，退出在可卖且盘口价格限制满足时模拟。期末按完整分钟收盘估值。',
            'limitations': [
                '分钟K线无法核实盘口深度或开盘实际排队，分钟参与率为成交量代理。',
                '原始日线与分钟价统一。持仓遇到未核实公司行动或缺失整场行情时暂停结算。',
                '样本由数据包限定，需另外验证历史股票池、停牌退市、公告证据和样本外表现。',
                '净值曲线采用日终估值，策略风险闸门另按分钟开收盘观察。',
            ],
        },
        'summary': summary, 'fills': simulation.fills, 'closed_trades': simulation.closed_trades,
        'equity_curve': simulation.equity_curve, 'decisions': simulation.decisions,
        'open_positions': [
            dict(code=p.code, quantity=p.quantity, entry_price=p.entry_price, stop_loss=p.stop,
                 initial_risk=p.initial_risk, stop_pending=p.stop_signal_at is not None) for p in simulation.positions.values()
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--demo', action='store_true', help='运行明确标注的合成案例')
    source.add_argument('--bundle', type=Path, help='历史分钟、日线、交易日历和复核证据 JSON')
    source.add_argument('--write-demo-bundle', type=Path, help='将合成案例导出为数据包样例')
    parser.add_argument('--start', help='回放开始交易日 YYYY-MM-DD')
    parser.add_argument('--end', help='回放结束交易日 YYYY-MM-DD')
    parser.add_argument('--initial-cash', type=float, default=100000.0)
    parser.add_argument('--output', type=Path, default=ROOT / 'output' / 'v1_backtest')
    args = parser.parse_args()
    try:
        if args.write_demo_bundle:
            with args.write_demo_bundle.open('x', encoding='utf-8') as file:
                json.dump(demo_bundle(), file, ensure_ascii=False, indent=2, allow_nan=False)
                file.write('\n')
            print(args.write_demo_bundle)
            return
        if args.demo:
            bundle, digest = demo_bundle(), None
            start = args.start or bundle['calendar'][-3]
            end = args.end or bundle['calendar'][-1]
        else:
            if not args.start or not args.end:
                parser.error('历史数据包须指定 --start 和 --end')
            bundle, digest = load_bundle(args.bundle)
            start, end = args.start, args.end
        result = run_backtest(bundle, start, end, args.initial_cash, bundle_sha256=digest)
        save_report(result, args.output)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({'report': str(args.output / 'result.json'), 'demo': result['meta']['demo'],
                      'summary': result['summary']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
