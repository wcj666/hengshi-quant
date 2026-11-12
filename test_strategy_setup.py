"""Offline V1 rule evidence, immutable levels and actual-fill lifecycle tests."""
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
import hashlib
import json
import pandas as pd
from strategy_setup import build_setup, exit_review, evaluate_entry
from swing_analysis import analyze_swing
from trade_signals import build_signals

CN = ZoneInfo('Asia/Shanghai')
now = datetime(2026, 11, 10, 10, tzinfo=CN)
dates = pd.bdate_range(end='2026-11-09', periods=140)
prices = [20+i*.03 for i in range(140)]
x = pd.DataFrame({'date': dates, 'open': prices, 'close': prices,
                  'high': [v+.08 for v in prices], 'low': [v-.08 for v in prices],
                  'volume': [100]*140, 'amount': [200_000_000]*140})
x.loc[136:138, 'low'] = x.loc[136:138, 'close']-.35
x.loc[136:138, 'volume'] = 60
x.loc[139, 'close'] = x.loc[138, 'high']+.2
x.loc[139, 'high'] = x.loc[139, 'close']+.08
x.loc[139, 'volume'] = 200
benchmark = pd.DataFrame({'date': dates, 'open': prices, 'close': prices,
                         'high': [v+.08 for v in prices], 'low': [v-.08 for v in prices],
                         'volume': [100]*140})
setup = build_setup(x, benchmark, now=now)
assert setup['eligible'], setup
levels = setup['levels']
assert abs(levels['take_profit_1']-(levels['entry_price']+2*(levels['entry_price']-levels['stop_loss']))) < 1e-7
assert levels['trigger_price'] <= levels['entry_price']
assert setup['evidence']['pullback_dates'] == [str(d.date()) for d in dates[-4:-1]]
partial = x.copy()
partial.loc[len(partial)] = [pd.Timestamp('2026-11-10'), 30, 30, 31, 29, 1_000_000, 900_000_000]
# A partial current day neither becomes signal T nor moves Ecap/targets.
assert build_setup(partial, benchmark, now=now)['levels'] == levels
assert not build_setup(x.drop(columns='amount'), benchmark, now=now)['eligible']
assert not build_setup(x.iloc[-119:], benchmark, now=now)['eligible']
wrong_benchmark = benchmark.copy()
wrong_benchmark.loc[139, 'date'] = pd.Timestamp('2026-11-06')
assert not build_setup(x, wrong_benchmark, now=now)['eligible']
failed = x.copy()
failed.loc[136:138, 'volume'] = 120
assert not build_setup(failed, benchmark, now=now)['eligible']

plan = {'strategy_version': '1.0', 'sector': '工业', 'signal_date': setup['signal_date'],
        'setup_verified': True, 'review': {k: True for k in ('fundamentals_ok', 'event_clear', 'corporate_action_clear', 'resistance_clear')},
        **levels}
cfg = {'strategy_v1': {'enabled': True}, 'positions': [], 'watchlist': [{'code': '600406', 'name': '测试'}],
       'trade_plans': {'600406': plan}, 'cash': 100000, 'baseline_equity': 100000}
calendar = [str(d.date()) for d in pd.bdate_range('2026-11-09', '2026-11-30')]
quote = {'price': levels['trigger_price'], 'open': levels['trigger_price'], 'asset_type': 'stock',
         'source_timestamp': now.isoformat(), 'strategy_setup': setup}
snapshot = {'mode': 'live', 'calendar_ok': True, 'market_session': True,
            'fetch_finished_at': now.isoformat(), 'trading_dates': calendar,
            'config_hash': hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'risk': {'valid': True, 'strategy': {'new_entries_allowed': True}},
            'strategy_account': {'initialized': True, 'verified': True, 'opens_today': 0}, 'quotes': []}
computed = {'valid': True, 'over_budget': False, 'strategy': {'new_entries_allowed': True},
            'sizing': [{'code': '600406', 'max_buy_shares': 100, 'reward_risk': 1.5}]}
with patch('risk_engine.compute', return_value=computed) as calculate:
    assert evaluate_entry(cfg, '600406', quote, snapshot, now=now)['allowed']
    assert calculate.call_args.kwargs['strategy_account'] == snapshot['strategy_account']
    # Chan/MACD cannot introduce another V1 entry; valid setup ignores their vote.
    swing = analyze_swing(x, {'valid': False}, {'valid': False}, cfg, '600406', quote=quote,
                          snapshot=snapshot, benchmark_hist=benchmark, now=now)
    assert swing['action'] == 'buy', swing
    broken = analyze_swing(failed, {'valid': True}, {'valid': True}, cfg, '600406', quote=quote,
                           snapshot=snapshot, benchmark_hist=benchmark, now=now)
    assert broken['action'] == 'wait'
    risk = {**computed, 'budget': 1000, 'remaining': 1000, 'drawdown': 0}
    with patch('trade_signals.tier_alerts', return_value=[]):
        alerts = build_signals(cfg, {'600406': quote}, risk, trading_day='2026-11-10', market_session=True,
                               calendar_ok=True, snapshot=snapshot, now=now)
        assert len(alerts) == 1 and alerts[0]['category'] == 'add'
    # Buy observation cannot be restored by a cached setup without a full snapshot.
    assert not evaluate_entry(cfg, '600406', quote, {}, now=now)['allowed']
    assert not evaluate_entry(cfg, '600406', {**quote, 'open': levels['entry_price']+.01}, snapshot, now=now)['allowed']
    assert not evaluate_entry(cfg, '600406', {**quote, 'limit_status_verified': False}, snapshot, now=now)['allowed']
    assert not evaluate_entry(cfg, '600406', quote, {**snapshot, 'config_hash': 'old'}, now=now)['allowed']
    assert not evaluate_entry(cfg, '600406', quote, snapshot, now=now.replace(hour=9, minute=44))['allowed']
    assert not evaluate_entry(cfg, '600406', quote, {**snapshot, 'strategy_account': {'opens_today': 1}}, now=now)['allowed']
    # A signal is valid for the immediate next exchange session only.
    later = now.replace(day=11)
    assert not evaluate_entry(cfg, '600406', {**quote, 'source_timestamp': later.isoformat()},
                              {**snapshot, 'fetch_finished_at': later.isoformat()}, now=later)['allowed']

position = {'code': '600406', 'type': 'stock', 'quantity': 100, 'cost': 20}
exit_plan = {'entry_price': 20, 'stop_loss': 19, 'initial_stop_loss': 19,
             'take_profit_1': 22, 'take_profit_2': 23, 'buy_date': '2026-11-10',
             'actual_entry': 20, 'initial_quantity': 100, 'tp1_sold_quantity': 0}
assert exit_review(cfg, position, {}, 10, now=now)['action'] == 'hold'
assert exit_review(cfg, position, exit_plan, 20.5, now=now)['days_held'] is None
day1 = exit_review(cfg, position, exit_plan, 18.5, trading_dates=calendar, now=now)
assert day1['trigger'] == 'stop' and day1['sellable'] == 0 and day1['blockers']
tp1 = exit_review(cfg, position, exit_plan, 22, now=now.replace(day=11))
assert tp1['quantity'] == 100 and tp1['action'] == 'exit'
triple = {**position, 'quantity': 300}
triple_plan = {**exit_plan, 'initial_quantity': 300}
assert exit_review(cfg, triple, triple_plan, 22, now=now.replace(day=11))['quantity'] == 100
after_fill = {**triple_plan, 'tp1_sold_quantity': 100}
review = exit_review(cfg, {**triple, 'quantity': 200}, after_fill, 22.1, now=now.replace(day=11))
assert review['action'] == 'hold' and review['raise_stop_to'] == 20
assert exit_review(cfg, {**triple, 'quantity': 200}, after_fill, 19.9, now=now.replace(day=11))['trigger'] == 'stop'
assert exit_review(cfg, {**triple, 'quantity': 200}, after_fill, 23, now=now.replace(day=11))['quantity'] == 200
# Buy day is day 1: 11/16 is day 5; use its complete close, never current price.
exit_hist = pd.DataFrame({'date': pd.bdate_range('2026-11-10', '2026-11-16'), 'open': 20.4,
                         'close': 20.5, 'high': 20.7, 'low': 20.2, 'volume': 100})
day5 = now.replace(day=16, hour=15, minute=10)
review = exit_review(cfg, position, exit_plan, 20.5, exit_hist, calendar, day5)
assert review['action'] == 'hold' and review['days_held'] == 5 and review['pending']
day6 = now.replace(day=17)
assert exit_review(cfg, position, exit_plan, 21.5, exit_hist, calendar, day6)['trigger'] == 'time_5'
assert exit_review(cfg, position, exit_plan, 21.5, None, calendar, day6)['action'] == 'hold'
assert exit_review(cfg, position, exit_plan, 21.5, exit_hist, None, day6)['days_held'] is None
assert exit_review(cfg, position, exit_plan, 21.5, exit_hist, calendar, now.replace(day=23))['trigger'] == 'time_10'
unknown = {**exit_plan, 'buy_date': None}
assert exit_review(cfg, position, unknown, 21.5, exit_hist, calendar, day6)['days_held'] is None
# Repriced stop cannot change original actual r used by the day-5 test.
raised = {**exit_plan, 'stop_loss': 20, 'tp1_sold_quantity': 100, 'initial_quantity': 300}
assert exit_review(cfg, {**position, 'quantity': 200}, raised, 21.5, exit_hist, calendar, day6)['trigger'] == 'time_5'
with patch('trade_signals.tier_alerts', return_value=[]):
    old = {**cfg, 'positions': [position], 'watchlist': [], 'trade_plans': {}}
    q = {'600406': {'price': 10, 'stop_loss': 19, 'source_timestamp': now.isoformat()}}
    assert build_signals(old, q, risk, trading_day='2026-11-10', market_session=True, calendar_ok=True, now=now) == []

print('PASS: V1 complete bars, fixed pullback/Ecap, shared execution gates, T+1, real-fill targets, trusted-calendar day5/day10 exits')
