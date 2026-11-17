"""Persist complete account estimates for display outside trading hours.

These snapshots are deliberately separate from live quote/risk state. Loading a
snapshot never grants trade capacity, even when its original quotes were live.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo


CST = ZoneInfo('Asia/Shanghai')


def _config_hash(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('布尔值不是金额')
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError('金额或价格无效')
    return value


def _when(value):
    if not isinstance(value, str) or len(value) < 19:
        raise ValueError('缺少完整报价日期时间')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('报价时间缺少时区')
    return result.astimezone(timezone.utc)


def _holdings(cfg):
    result = {}
    for position in cfg.get('positions', []):
        quantity = _number(position['quantity'])
        if quantity < 0 or quantity != int(quantity):
            raise ValueError('持仓数量无效')
        if not quantity:
            continue
        code = str(position['code']).zfill(6)
        kind = position['type']
        if not re.fullmatch(r'\d{6}', code) or kind not in ('stock', 'etf', 'convertible'):
            raise ValueError('持仓代码或类型无效')
        if code in result:
            raise ValueError('持仓代码重复')
        result[code] = (kind, int(quantity), position)
    if not result:
        raise ValueError('没有可保存报价的持仓')
    return result


def _validated(cfg, *, config_hash, market_time, fetched_at, quotes, risk):
    """Validate values independently of the caller's live/historical flags."""
    if config_hash != _config_hash(cfg):
        raise ValueError('账户配置与快照不一致')
    market = _when(market_time)
    fetched = _when(fetched_at)
    now = datetime.now(timezone.utc)
    if market > fetched or fetched > now:
        raise ValueError('采集时间在未来或顺序无效')
    day = market.astimezone(CST).date()
    if fetched.astimezone(CST).date() != day:
        raise ValueError('采集日期跨日，无法形成统一账户快照')
    expected = _holdings(cfg)
    selected, clocks, prices = [], [], {}
    if not isinstance(quotes, list) or not isinstance(risk, dict):
        raise ValueError('账户快照结构无效')
    for quote in quotes:
        if not isinstance(quote, dict):
            raise ValueError('报价结构无效')
        if quote.get('held') is not True:
            continue
        code = str(quote.get('code', '')).zfill(6)
        if code not in expected or code in prices:
            raise ValueError('报价代码多余或重复')
        kind, _, position = expected[code]
        if quote.get('asset_type') != kind:
            raise ValueError('报价证券类型不一致')
        price = _number(quote.get('price'), positive=True)
        clock = _when(quote.get('source_timestamp'))
        if clock > fetched or clock.astimezone(CST).date() != day:
            raise ValueError('报价时间在未来或报价日期混合')
        clean = deepcopy(quote)
        clean.update(code=code, asset_type=kind, held=True, price=price,
                     source_timestamp=clock.isoformat())
        clean.setdefault('name', position.get('name', code))
        selected.append(clean)
        clocks.append(clock)
        prices[code] = price
    if set(prices) != set(expected):
        raise ValueError('持仓报价不完整')

    cash = _number(cfg.get('cash', 0))
    frozen_cash = _number(cfg.get('frozen_cash', 0))
    if cash < 0 or frozen_cash < 0:
        raise ValueError('可用现金和冻结资金不能为负')
    equity = _number(risk.get('equity'), positive=True)
    expected_equity = round(cash + frozen_cash + sum(prices[c] * p[1] for c, p in expected.items()), 2)
    if abs(equity - expected_equity) > .011:
        raise ValueError('总资产与可用现金、冻结资金及持仓市值不一致')
    if frozen_cash and ('frozen_cash' not in risk or abs(_number(risk['frozen_cash']) - frozen_cash) > .011):
        raise ValueError('快照冻结资金与配置不一致')
    for key in ('used', 'remaining', 'existing_planned_stress', 'available_new_risk',
                'budget', 'baseline', 'cash', 'frozen_cash', 'total_cash', 'exposure', 'change'):
        if risk.get(key) is not None:
            _number(risk[key])
    if risk.get('missing'):
        raise ValueError('风控结果标记持仓缺价')
    risk_positions = risk.get('positions')
    if not isinstance(risk_positions, list):
        raise ValueError('缺少逐只持仓估值')
    seen = set()
    for position in risk_positions:
        if not isinstance(position, dict):
            raise ValueError('逐只持仓估值结构无效')
        code = str(position.get('code', '')).zfill(6)
        if code not in expected or code in seen:
            raise ValueError('逐只持仓估值代码错误')
        kind, quantity, _ = expected[code]
        if position.get('type') != kind or _number(position.get('quantity')) != quantity:
            raise ValueError('逐只持仓估值类型或数量错误')
        if abs(_number(position.get('price'), positive=True) - prices[code]) > .000051:
            raise ValueError('逐只持仓估值价格不一致')
        if abs(_number(position.get('value'), positive=True) - round(prices[code] * quantity, 2)) > .011:
            raise ValueError('逐只持仓市值异常')
        seen.add(code)
    if seen != set(expected):
        raise ValueError('逐只持仓估值不完整')
    pnl = risk.get('pnl')
    if not isinstance(pnl, dict):
        raise ValueError('盈亏快照缺失')
    for key in ('floating_pnl', 'floating_pct', 'daily_pnl', 'daily_pct'):
        if pnl.get(key) is not None:
            _number(pnl[key])
    # Also reject non-finite values in any nested optional provider/risk field.
    json.dumps({'quotes': selected, 'risk': risk}, ensure_ascii=False, allow_nan=False)
    return selected, min(clocks).isoformat(), max(clocks).isoformat()


def _display_risk(risk):
    result = deepcopy(risk)
    result.update(valid=False, sizing=[], available_new_risk=None, display_only=True)
    return result


def save_display_snapshot(path, cfg, state, *, historical=False):
    """Atomically save a valid complete account estimate; failures preserve it."""
    temporary = None
    try:
        if not isinstance(state, dict) or not isinstance(state.get('risk'), dict):
            return False
        if state.get('mode') != 'live' or (state.get('risk') or {}).get('valid') is not True:
            return False
        health = state.get('quote_health') or {}
        if health.get('invalid_jump') or health.get('missing'):
            return False
        if not historical and not (health.get('all_held_usable') is True and
                health.get('provider_timestamps_verified') is True and
                health.get('block_new_signals') is False):
            return False
        fetched_at = state.get('fetch_finished_at')
        quotes, earliest, latest = _validated(cfg,
            config_hash=state.get('config_hash'), market_time=state.get('market_time'),
            fetched_at=fetched_at, quotes=state.get('quotes'), risk=state.get('risk'))
        payload = {
            'source': str(state.get('data_source') or ''), 'asof': earliest,
            'quote_latest_at': latest, 'fetched_at': fetched_at,
            'market_time': state['market_time'], 'historical': bool(historical),
            'config_hash': state['config_hash'], 'quotes': quotes,
            'risk': _display_risk(state['risk']), 'display_only': True,
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                prefix='.' + path.name + '.', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            json.dump(payload, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_display_snapshot(path, cfg):
    """Load an estimate for display only, with no freshness or trading authority."""
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('historical'), bool):
            return None
        quotes, earliest, latest = _validated(cfg,
            config_hash=payload.get('config_hash'), market_time=payload.get('market_time'),
            fetched_at=payload.get('fetched_at'), quotes=payload.get('quotes'), risk=payload.get('risk'))
        if payload.get('asof') != earliest or payload.get('quote_latest_at') != latest:
            return None
        # Return only the display schema; reject any added active-trading wrapper.
        return {
            'source': str(payload.get('source') or ''), 'asof': earliest,
            'quote_latest_at': latest, 'fetched_at': payload['fetched_at'],
            'market_time': payload['market_time'], 'historical': payload['historical'],
            'config_hash': payload['config_hash'], 'quotes': quotes,
            'risk': _display_risk(payload['risk']), 'display_only': True,
        }
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None
