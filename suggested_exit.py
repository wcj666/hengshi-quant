"""Deterministic remaining-position exit drafts from completed OHLCV only.

No I/O, saved-plan mutation, fabricated fills, or trading permission. These are
initial, unbacktested research thresholds; a target is not a return forecast.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import time
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math
import pandas as pd

from strategy_setup import completed_history, _now, _calendar

_PRICE_KEYS = ('entry_price', 'stop_loss', 'take_profit_1', 'take_profit_2')
_NOTE = '剩余仓位退出草案，不是实际买入价或收益预测；不保证按价成交，也不保证在1～2周达到目标。'


def _number(value, *, positive=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and (value > 0 if positive else True) else None


def _tick_round(value, tick, rounding):
    value = Decimal(str(value)); step = Decimal(str(tick))
    return float((value / step).to_integral_value(rounding=rounding) * step)


def _existing(plan):
    if not isinstance(plan, dict):
        return None
    parsed = {k: _number(plan.get(k), positive=True) for k in _PRICE_KEYS}
    if any(v is None for v in parsed.values()):
        return None
    # Copy all execution/original-S0 metadata as well as its absolute prices.
    return deepcopy(plan), parsed


def _technical(history, trading_dates, clock):
    """Validate provenance dates separately from indicator computation."""
    frame, error = completed_history(history, clock)
    if error or frame is None:
        return None, {'valid': False, 'reason': error or '完整日线不可用'}
    # completed_history excludes incomplete today; truly future dates are a data
    # defect, not an invitation to silently use an earlier part of the frame.
    try:
        raw_dates = pd.to_datetime(pd.DataFrame(history)['date'], errors='coerce').dt.date
        if any(d > clock.date() for d in raw_dates.dropna()):
            return None, {'valid': False, 'reason': '日线包含未来日期，等待真实数据重新核验'}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, {'valid': False, 'reason': '日线日期格式无效'}
    if len(frame) < 60:
        return None, {'valid': False, 'reason': f'完整日线仅{len(frame)}根，结构与波动建议至少需要60根'}
    days = _calendar(trading_dates, clock.date())
    if not days:
        return None, {'valid': False, 'reason': '交易日历未覆盖当前日期，不按自然工作日推测最近收盘'}
    eligible = [d for d in days if d < clock.date() or
                (d == clock.date() and clock.time() >= time(15, 5))]
    if not eligible:
        return None, {'valid': False, 'reason': '没有已核验的完整交易日'}
    expected = eligible[-1]
    if frame.date.iloc[-1] != expected:
        return None, {'valid': False, 'reason': f'最新完整日线应为{expected.isoformat()}，现有缓存日期不一致',
                      'asof': frame.date.iloc[-1].isoformat(), 'expected_asof': expected.isoformat()}
    close = frame.close
    previous = close.shift(1)
    tr = pd.concat([frame.high-frame.low, (frame.high-previous).abs(),
                    (frame.low-previous).abs()], axis=1).max(axis=1)
    atr = float(tr.iloc[-14:].mean())
    technical = {'valid': True, 'asof': expected.isoformat(),
                 'close': float(close.iloc[-1]),
                 'support': float(frame.low.iloc[-20:].min()),
                 'resistance': float(frame.high.iloc[-20:].max()),
                 'ma5': float(close.iloc[-5:].mean()),
                 'ma10': float(close.iloc[-10:].mean()),
                 'ma20': float(close.iloc[-20:].mean()), 'atr14': atr,
                 'note': '最近20个完整交易日高低点和均线仅为结构观察位，不是已锁定的止损或止盈。'}
    return frame, technical


def build_exit_suggestion(history, position, *, trading_dates, now=None, existing_plan=None):
    """Create a legacy holding's fixed-price draft, or preserve a saved plan.

    Initial S0 = most recent confirmed 2-left/2-right low in the last 20 bars
    minus 0.2 ATR14. With no qualifying confirmed low, the last 10 bars' minimum
    is explicitly labelled a fallback. TP1/TP2 use the latest complete close's
    fixed risk distance, not an inferred execution price. No cost % is used.
    """
    saved = _existing(existing_plan)
    result = {'valid': False, 'reason': '', 'asof': None,
              'technical': {'valid': False, 'reason': '完整日线待核验'},
              'plan': {}, 'warnings': [], 'planned_stress': None,
              'stop_distance_pct': None, 'preserved': bool(saved),
              'adoptable': False, 'status': 'pending',
              'note': _NOTE, 'evidence': {}}
    if saved:
        result['plan'] = saved[0]
        result['asof'] = saved[0].get('source_asof')
    try:
        clock = _now(now)
        frame, technical = _technical(history, trading_dates, clock)
        result['technical'] = technical
        if saved:
            values = saved[1]
            original_stop = _number(saved[0].get('initial_stop_loss'), positive=True)
            sold = _number(saved[0].get('tp1_sold_quantity'), positive=True)
            raised = bool(sold and original_stop is not None and original_stop < values['entry_price']
                          and values['entry_price'] <= values['stop_loss'] < values['take_profit_1'])
            ordered = (values['entry_price'] < values['take_profit_1'] < values['take_profit_2']
                       and (values['stop_loss'] < values['entry_price'] or raised))
            result['valid'] = bool(ordered)
            result['adoptable'] = bool(ordered)
            result['status'] = 'preserved' if ordered else 'pending'
            result['reason'] = ('已有固定计划保持原价；不自动重算止损或目标' if ordered else
                                '已有完整价位顺序异常；保留原值待核对，不自动覆盖')
            if not technical.get('valid'):
                result['warnings'].append('新日线尚未核验：'+technical.get('reason', '数据不足'))
                return result
            result['asof'] = technical['asof']
            close, stop = technical['close'], values['stop_loss']
            if close <= stop:
                result['warnings'].append('最近完整收盘已触及或低于原固定止损；优先退出复核，不以下移止损消除提醒')
        else:
            if not technical.get('valid'):
                result['reason'] = technical.get('reason', '完整日线不足，不生成建议')
                return result
            result['asof'] = technical['asof']
            kind = (position or {}).get('type', 'stock')
            if kind not in ('stock', 'etf', 'convertible'):
                result['reason'] = '证券类型未核验，不能选择最小价位生成建议'
                return result
            tick = .001 if kind in ('etf', 'convertible') else .01
            close = technical['close']; atr = technical['atr14']
            if not math.isfinite(atr) or atr <= 0:
                result['reason'] = 'ATR14无有效正波动，不能补造风险距离'
                return result
            lows = list(frame.low)
            support_index = None
            for i in range(len(frame)-3, max(2, len(frame)-20)-1, -1):
                candidate = float(lows[i])
                if (candidate < close and candidate < min(lows[i-2:i])
                        and candidate <= min(lows[i+1:i+3])):
                    support_index = i
                    break
            if support_index is not None:
                support = float(lows[support_index])
                method = '最近20日内2左2右确认低点－0.2×ATR14'
                evidence = {'support_date': frame.date.iloc[support_index].isoformat(),
                            'support_price': support,
                            'confirmation_dates': [d.isoformat() for d in frame.date.iloc[support_index+1:support_index+3]],
                            'fallback': False}
            else:
                support = float(frame.low.iloc[-10:].min())
                method = '无近端确认低点，回退最近10日低点－0.2×ATR14'
                evidence = {'support_date': None, 'support_price': support,
                            'confirmation_dates': [], 'fallback': True}
                result['warnings'].append('近20日没有可确认的近端摆动低点，采用10日低点回退法；不冒充已确认分型')
            stop = _tick_round(Decimal(str(support))-Decimal('0.2')*Decimal(str(atr)), tick, ROUND_FLOOR)
            if not 0 < stop < close:
                result['reason'] = '结构止损无法形成有效正价差，不挪近或补造止损'
                return result
            risk_distance = Decimal(str(close))-Decimal(str(stop))
            first = _tick_round(Decimal(str(close))+2*risk_distance, tick, ROUND_CEILING)
            second = _tick_round(Decimal(str(close))+3*risk_distance, tick, ROUND_CEILING)
            if not stop < close < first < second:
                result['reason'] = '最小价位取整后无法形成有效固定计划'
                return result
            result['plan'] = {'entry_price': close, 'stop_loss': stop,
                              'take_profit_1': first, 'take_profit_2': second,
                              'initial_stop_loss': stop, 'strategy_version': '1.0',
                              'legacy_exit_only': True, 'setup_verified': False,
                              'source_asof': technical['asof'],
                              'reference_kind': 'remaining_position_snapshot',
                              'suggestion_method': method}
            result['evidence'] = {**evidence, 'atr14': atr, 'tick_size': tick,
                                  'reference_close': close, 'risk_distance': float(risk_distance)}
            result['valid'] = True
            result['reason'] = '已按最新完整日线生成剩余仓位退出草案；采用保存后才成为固定计划'
        quantity = _number((position or {}).get('quantity'))
        if quantity is None or quantity <= 0 or quantity != int(quantity):
            if not saved:
                result['valid'] = False
            result['warnings'].append('持仓数量未核验，不核算计划压力')
            return result
        stop = float(result['plan']['stop_loss'])
        result['planned_stress'] = round(max(close-stop, 0)*quantity+20., 2)
        result['stop_distance_pct'] = round((close-stop)/close*100, 4)
        if result['stop_distance_pct'] > 6:
            result['warnings'].append('结构止损距离超过新开仓6%筛选线；旧仓应复核减仓，不为符合上限强行收紧止损')
        if result['planned_stress'] > 150:
            result['warnings'].append('按现有全部数量及至少20元费用估算的计划压力超过150元；不代表已满足账户风险预算')
        result['warnings'].append('目标为剩余仓位的2R/3R计划参考，不是预测；跳空、跌停及成交费用可能令实际风险更大')
        if not saved and result['valid']:
            if result['stop_distance_pct'] > 6:
                result['status'] = 'risk_mismatch'
                result['adoptable'] = False
                result['reason'] = '暂无适合1～2周周期的可采用近端方案；远端结构仅供参考，优先减仓复核或等待结构'
            else:
                result['status'] = 'ready'
                result['adoptable'] = True
        return result
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, IndexError) as exc:
        result['reason'] = '结构日线或参数未通过核验：'+type(exc).__name__
        if saved:
            result['valid'] = False
            result['warnings'].append('已有固定价位保留，数据错误时不自动改写')
        result['adoptable'] = False
        result['status'] = 'pending'
        return result
