"""Read validated, user-supplied broker screenshots for historical display only."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re


def _number(value):
    if isinstance(value, bool):
        raise ValueError('boolean is not an account amount')
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite account amount')
    return result


def _quantity(value):
    number = _number(value)
    if number < 0 or number != int(number):
        raise ValueError('invalid holding quantity')
    return int(number)


def _finite_fields(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_fields(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite_fields(item) for item in value)
    return True


def _holding(row):
    code = str(row['code'])
    kind = row['type']
    if not re.fullmatch(r'\d{6}', code) or kind not in ('stock', 'etf', 'convertible'):
        raise ValueError('invalid security identity')
    quantity = _quantity(row['quantity'])
    cost = _number(row['cost'])
    if cost <= 0:
        raise ValueError('invalid holding cost')
    return code, kind, quantity, cost


def latest_broker_snapshot(output, cfg):
    """Return the newest internally consistent screenshot matching this ledger.

    Screenshot values remain broker reference data: this function does not turn
    them into a live quotation or reset any risk/transaction validity flags.
    Invalid files are skipped without modifying them or the account ledger.
    """
    try:
        expected = {}
        for row in cfg.get('positions', []):
            if _quantity(row['quantity']) == 0:
                continue
            code, kind, quantity, cost = _holding(row)
            if code in expected:
                return None
            expected[code] = (kind, quantity, cost)
        cash = _number(cfg['cash'])
        frozen = _number(cfg.get('frozen_cash', 0))
        if cash < 0 or frozen < 0:
            return None
        files = sorted((Path(output) / 'broker_snapshots').glob('broker_*.json'),
                       key=lambda path: path.name, reverse=True)
    except (OSError, KeyError, TypeError, ValueError, OverflowError):
        return None

    now = datetime.now(timezone.utc)
    for path in files:
        try:
            snapshot = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(snapshot, dict) or not _finite_fields(snapshot):
                continue
            asof = datetime.fromisoformat(snapshot['asof'].replace('Z', '+00:00'))
            if asof.tzinfo is None or asof.utcoffset() is None or asof > now:
                continue
            actual_cash = _number(snapshot['available_cash'])
            actual_frozen = _number(snapshot.get('frozen_cash', 0))
            market_total = _number(snapshot['total_market_value'])
            assets = _number(snapshot['total_assets'])
            if (actual_cash < 0 or actual_frozen < 0 or market_total < 0 or assets <= 0
                    or abs(actual_cash - cash) > 1e-8 or abs(actual_frozen - frozen) > 1e-8):
                continue
            positions = snapshot['positions']
            if not isinstance(positions, list) or len(positions) != len(expected):
                continue
            seen = set()
            values = []
            for row in positions:
                code, kind, quantity, cost = _holding(row)
                if code in seen or code not in expected or quantity <= 0:
                    raise ValueError('holding mismatch')
                wanted_kind, wanted_quantity, wanted_cost = expected[code]
                if (kind != wanted_kind or quantity != wanted_quantity
                        or abs(cost - wanted_cost) > 1e-8):
                    raise ValueError('holding mismatch')
                price = _number(row['screenshot_price'])
                value = _number(row['market_value'])
                if price <= 0 or value < 0 or abs(price * quantity - value) > .011:
                    raise ValueError('position market value mismatch')
                seen.add(code)
                values.append(value)
            if (seen != set(expected) or abs(math.fsum(values) - market_total) > .011
                    or abs(market_total + actual_cash + actual_frozen - assets) > .011):
                continue
            return snapshot
        except (OSError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
            continue
    return None
