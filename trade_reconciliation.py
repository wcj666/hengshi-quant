"""Preview and apply confirmed broker trades and cash flows to the local ledger."""
from __future__ import annotations

import csv
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import io
import json
import re
from zoneinfo import ZoneInfo

CN = ZoneInfo('Asia/Shanghai')
CENT = Decimal('.01')
CODE = re.compile(r'[0-9]{6}')
FIELDS = ('fill_id', 'executed_at', 'code', 'side', 'quantity', 'price', 'fee', 'amount',
          'name', 'type', 'alert_id', 'reference_price', 'planned_risk')


def decimal(value, label, low='0', high='10000000000'):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not Decimal(low) <= number <= Decimal(high):
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError(f'{label}数值无效') from None
    return number


def cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def integer(value, label, low='0'):
    number = decimal(value, label, low, '100000000')
    if number != number.to_integral_value():
        raise ValueError(f'{label}须为整数')
    return int(number)


def timestamp(value, label, now):
    try:
        stamp = datetime.fromisoformat(value.strip())
        stamp = stamp.replace(tzinfo=CN) if stamp.tzinfo is None else stamp.astimezone(CN)
        if len(value.strip()) < 16 or stamp > now:
            raise ValueError
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError(f'{label}须填写北京时间，且不能晚于现在') from None
    return stamp


def digest(value):
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def position_signature(rows):
    return digest(sorted((str(row['code']), row['type'], row['quantity'], row.get('cost')) for row in rows))


def matches_receipt(cfg, receipt):
    return (receipt and cfg['cash'] == receipt.get('available_cash')
            and cfg.get('frozen_cash', 0) == receipt.get('frozen_cash', 0)
            and position_signature(cfg.get('positions', [])) == receipt.get('position_signature'))


def csv_events(value):
    if not isinstance(value, str) or len(value) > 250_000:
        raise ValueError('CSV 过大，请按券商对账批次导入')
    aliases = dict(成交编号='fill_id', 成交时间='executed_at', 证券代码='code', 方向='side',
                   成交数量='quantity', 成交价='price', 费用合计='fee', 转账金额='amount',
                   证券名称='name', 证券类型='type', 提醒编号='alert_id', 计划参考价='reference_price',
                   计划风险='planned_risk')
    try:
        rows = csv.DictReader(io.StringIO(value.lstrip('\ufeff'), newline=''), strict=True)
        names = [aliases.get(x.strip(), x.strip()) for x in (rows.fieldnames or [])]
        if len(names) != len(set(names)) or not {'fill_id', 'executed_at', 'side'} <= set(names):
            raise ValueError('CSV 至少需包含成交编号、成交时间、方向，列名不能重复')
        rows.fieldnames = names
        result = []
        for row in rows:
            if None in row or None in row.values():
                raise ValueError(f'CSV 第 {rows.line_num} 行与表头列数不一致')
            result.append({key: row[key].strip() for key in FIELDS if key in row})
        return result
    except csv.Error as exc:
        raise ValueError(f'CSV 格式错误：{exc}') from exc


def normalize(row, now, *, imported=False):
    if not isinstance(row, dict):
        raise ValueError('成交或资金流水格式无效')
    at = timestamp(row.get('executed_at'), '实际时间', now)
    side = str(row.get('side', '')).strip()
    side = {'买入': 'buy', '卖出': 'sell', '转入': 'deposit', '转出': 'withdraw'}.get(side, side)
    if side not in ('buy', 'sell', 'deposit', 'withdraw'):
        raise ValueError('方向须为买入、卖出、转入或转出')
    fill_id = str(row.get('fill_id') or '').strip()
    if len(fill_id) > 80 or (imported and not fill_id):
        raise ValueError('导入行须有券商成交编号或资金流水编号，最长80字')
    event = dict(executed_at=at.isoformat(), date=at.date().isoformat(), side=side, fill_id=fill_id)
    if side in ('deposit', 'withdraw'):
        amount = cents(decimal(row.get('amount'), '转账金额', '.01'))
        event['amount'] = float(amount if side == 'deposit' else -amount)
    else:
        code = str(row.get('code', '')).strip()
        if not CODE.fullmatch(code):
            raise ValueError('证券代码须为6位数字')
        price = decimal(row.get('price'), '成交价', '.000001', '100000')
        if price != price.quantize(Decimal('.000001')):
            raise ValueError('成交价格最多保留6位小数')
        kind = str(row.get('type') or '').strip()
        kind = {'股票': 'stock', '可转债': 'convertible'}.get(kind, kind.lower())
        name = str(row.get('name') or '').strip()
        alert_id = str(row.get('alert_id') or '').strip()
        if kind not in ('', 'stock', 'etf', 'convertible') or len(name) > 36 or len(alert_id) > 100:
            raise ValueError('证券类型、名称或提醒编号无效')
        event.update(code=code, type=kind, name=name, alert_id=alert_id or None,
                     quantity=integer(row.get('quantity'), '成交数量', '1'), price=float(price),
                     fee=float(cents(decimal(row.get('fee'), '实际费用'))))
        for key, label in (('reference_price', '计划参考价'), ('planned_risk', '本次退出对应的原计划风险')):
            value = row.get(key)
            event[key] = float(decimal(value, label, '.000001')) if value not in (None, '') else None
    event['fingerprint'] = digest(event)
    scope = 'flow' if 'amount' in event else 'trade'
    identity = (scope, event['date'], fill_id) if fill_id else event['fingerprint']
    event['id'] = digest(identity)[:32]
    return event


def _holding(positions, event, cfg):
    code = event['code']
    item = positions.get(code)
    if item is None or item['quantity'] == 0:
        if event['side'] == 'sell':
            raise ValueError(f'{code} 卖出超过台账持仓')
        watch = next((row for row in cfg.get('watchlist', []) if str(row.get('code')) == code), {})
        kind = event['type'] or (item or {}).get('type') or watch.get('type')
        name = event['name'] or (item or {}).get('name') or watch.get('name')
        if kind not in ('stock', 'etf', 'convertible') or not name:
            raise ValueError(f'{code} 新持仓须有证券名称及类型')
        item = positions[code] = {'code': code, 'name': name, 'type': kind, 'quantity': 0,
                                  'cost': event['price'], 'baseline_price': event['price']}
    if event['type'] and event['type'] != item['type']:
        raise ValueError(f'{code} 证券类型与台账不一致')
    return item


def apply_fill(positions, event, cfg):
    item = _holding(positions, event, cfg)
    qty, price, fee = event['quantity'], Decimal(str(event['price'])), Decimal(str(event['fee']))
    before = int(item['quantity'])
    direction = 1 if event['side'] == 'buy' else -1
    after = before + direction * qty
    if after < 0:
        raise ValueError(f"{event['code']} 卖出 {qty} 超过台账持仓 {before}")
    old_cost = (decimal(item['cost'], '台账单位成本', '.00000001')
                if item.get('cost') not in (None, 0) else None)
    notional = cents(price * qty)
    delta = -direction * notional - fee
    if direction > 0 and old_cost is not None:
        cost = (old_cost * before + notional + fee) / after
        item['cost'] = float(cost.quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP))
    pnl = float(cents((price - old_cost) * qty - fee)) if direction < 0 and old_cost is not None else None
    ref = event['reference_price']
    event.update(asset_type=item['type'], security_name=item['name'], quantity_before=before,
                 quantity_after=after, gross=float(notional), net_cash_change=float(delta),
                 net_pnl_estimate=pnl,
                 net_r_estimate=round(pnl / event['planned_risk'], 4) if pnl is not None and event['planned_risk'] else None,
                 adverse_slippage=float(cents(direction * (price - Decimal(str(ref))) * qty)) if ref else None)
    item['quantity'] = after
    item.pop('available_quantity', None)
    return delta


def prepare(cfg, body, *, now=None, previous_snapshot=None, legacy_flows=(), resolve_alert=None):
    """Return a proposed replacement config plus review details. No writes."""
    now = now or datetime.now(CN)
    if not isinstance(body, dict):
        raise ValueError('请填写对账数据')
    imported = 'csv' in body
    rows = csv_events(body['csv']) if imported else body.get('events')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError('每批对账须包含 1 至 500 条实际流水')
    events = sorted((normalize(row, now, imported=imported) for row in rows), key=lambda row: row['executed_at'])
    new_cfg = deepcopy(cfg)
    trades = new_cfg.setdefault('reconciled_trades', [])
    flows = new_cfg.setdefault('reconciled_cashflows', [])
    known = {row['id']: row['fingerprint'] for row in trades + flows}
    new_events, duplicates = [], []
    for row in events:
        if row['id'] in known:
            if row['fingerprint'] != known[row['id']]:
                raise ValueError(f"{row['fill_id']} 编号已存在，但数据不同")
            duplicates.append(row)
        else:
            known[row['id']] = row['fingerprint']
            new_events.append(row)
    result = {'events': new_events, 'duplicates': duplicates, 'cash_before': cfg['cash'],
              'cash_after': cfg['cash'], 'differences': [], 'positions': []}
    if not new_events:
        return new_cfg, result
    previous = cfg.get('trade_reconciliation', {})
    screenshot = previous_snapshot or {}
    anchors = [
        (previous.get('broker_asof'), previous.get('total_assets') if matches_receipt(cfg, previous) else None),
        (screenshot.get('asof'), screenshot.get('total_assets')),
    ]
    anchors = [(timestamp(value, '上次对账时间', now), assets) for value, assets in anchors if value]
    anchor = max(anchors, key=lambda record: record[0]) if anchors else None
    if anchor and timestamp(new_events[0]['executed_at'], '实际时间', now) <= anchor[0]:
        raise ValueError('这批含已对账时间之前的未登记流水，请从下一笔实际流水开始')
    balances = body.get('broker')
    if not isinstance(balances, dict):
        raise ValueError('请填写成交后的券商账户数据')
    asof = timestamp(balances.get('asof'), '券商账户时间', now)
    if asof.isoformat() < new_events[-1]['executed_at']:
        raise ValueError('券商账户时间须覆盖本批最后一条流水')
    available = cents(decimal(balances.get('available_cash'), '券商可用资金'))
    frozen = cents(decimal(balances.get('frozen_cash', 0), '券商冻结资金'))
    withdrawable = balances.get('withdrawable_cash')
    withdrawable = cents(decimal(withdrawable, '券商可取资金')) if withdrawable not in (None, '') else None
    if withdrawable is not None and withdrawable > available:
        raise ValueError('券商可取资金不能超过可用资金')
    total_assets = balances.get('total_assets')
    total_assets = cents(decimal(total_assets, '券商总资产', '.01')) if total_assets not in (None, '') else None
    if total_assets is not None and total_assets < available + frozen:
        raise ValueError('券商总资产不能低于可用资金与冻结资金之和')
    positions = {str(row['code']): row for row in new_cfg.get('positions', [])}
    cash = cents(decimal(cfg['cash'], '台账可用资金')) + cents(decimal(cfg.get('frozen_cash', 0), '台账冻结资金'))
    touched = set()
    for event in new_events:
        if 'amount' in event:
            if any(row.get('date') == event['date'] and row.get('amount') == event['amount'] for row in legacy_flows):
                raise ValueError('这笔资金流水已在原资金流水登记。请核对换账起点，避免重复记账。')
            cash += Decimal(str(event['amount']))
            flows.append(event)
        else:
            if event['alert_id']:
                if resolve_alert is None:
                    raise ValueError('提醒编号暂无法核验')
                ref = resolve_alert(event, trades)
                if event['reference_price'] is None:
                    event['reference_price'] = ref
            cash += apply_fill(positions, event, cfg)
            touched.add(event['code'])
            trades.append(event)
    differences = result['differences']
    if cash != available + frozen:
        differences.append(f'计算总现金 ¥{cash:,.2f}，券商可用＋冻结 ¥{available + frozen:,.2f}')
    declared = balances.get('positions', {})
    if not isinstance(declared, dict):
        raise ValueError('券商持仓核对格式无效')
    checked = {}
    for code in sorted(touched | set(declared)):
        item = positions.get(code)
        qty = int(item['quantity']) if item else 0
        row = declared.get(code)
        if row is None:
            differences.append(f'{code} 请填写券商持仓及可卖数量')
            result['positions'].append({'code': code, 'quantity': qty})
            continue
        if not CODE.fullmatch(code) or not isinstance(row, dict):
            raise ValueError('核对持仓须填写6位证券代码、持仓及可卖数量')
        broker_qty = integer(row.get('quantity'), f'{code} 券商持仓')
        sellable = integer(row.get('available_quantity'), f'{code} 券商可卖量')
        if sellable > broker_qty:
            raise ValueError(f'{code} 券商可卖量不能超过持仓量')
        if broker_qty != qty:
            differences.append(f'{code} 计算持仓 {qty}，券商显示 {broker_qty}')
        checked[code] = {'quantity': qty, 'available_quantity': sellable}
        result['positions'].append({'code': code, 'quantity': qty, 'broker_quantity': broker_qty,
                                    'available_quantity': sellable})
    new_cfg['cash'] = float(available)
    new_cfg['frozen_cash'] = float(frozen)
    new_cfg['positions'] = [item for code, item in positions.items() if item['quantity'] > 0 or code not in touched]
    active = [item for item in new_cfg['positions'] if item['quantity'] > 0]
    if total_assets is not None and active and total_assets == available + frozen:
        differences.append('券商总资产等于全部现金，但本批计算后仍有持仓')
    flow = sum((Decimal(str(event['amount'])) for event in new_events if 'amount' in event), Decimal('0'))
    receipt = {
        'broker_asof': asof.isoformat(), 'available_cash': float(available), 'frozen_cash': float(frozen),
        'total_cash': float(available + frozen),
        'withdrawable_cash': float(withdrawable) if withdrawable is not None else None,
        'positions': checked, 'position_signature': position_signature(new_cfg['positions']),
        'total_assets': float(total_assets) if total_assets is not None else None,
        'period_pnl': None,
    }
    legacy_in_period = anchor and any(
        anchor[0].date().isoformat() <= row.get('date', '') <= asof.date().isoformat()
        for row in legacy_flows
    )
    if anchor and anchor[1] is not None and total_assets is not None and not legacy_in_period:
        beginning = cents(decimal(anchor[1], '上次券商总资产', '.01'))
        receipt['period_pnl'] = {'from': anchor[0].isoformat(), 'net_external_flow': float(flow),
                                 'amount': float(total_assets - beginning - flow)}
    new_cfg['trade_reconciliation'] = receipt
    result.update(cash_after=float(cash - frozen), broker_cash=float(available),
                  frozen_before=cfg.get('frozen_cash', 0), frozen_after=float(frozen),
                  total_cash_after=float(cash), receipt=receipt)
    return new_cfg, result


def close_snapshot(cfg):
    """Expose checked inventory as a snapshot when every holding was verified."""
    receipt = cfg.get('trade_reconciliation', {})
    rows = cfg.get('positions', [])
    if not matches_receipt(cfg, receipt):
        return None
    rows = [row for row in rows if row['quantity'] > 0]
    checked = receipt['positions']
    if any(str(row['code']) not in checked for row in rows):
        return None
    return {'asof': receipt['broker_asof'], 'available_cash': receipt['available_cash'],
            'withdrawable_cash': receipt['withdrawable_cash'], 'frozen_cash': receipt.get('frozen_cash', 0),
            'total_assets': receipt.get('total_assets'), 'source': receipt.get('source') or '已确认成交对账',
            'positions': [dict(row, available_quantity=checked[str(row['code'])]['available_quantity'])
                          for row in rows]}
