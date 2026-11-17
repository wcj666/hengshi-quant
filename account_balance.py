"""Read-only preparation of a broker balance check with no new activity."""
from copy import deepcopy
from datetime import datetime

from trade_reconciliation import CN, CODE, cents, decimal, integer, position_signature, timestamp


def prepare(cfg, body, *, now=None, previous_snapshot=None):
    """Return a proposed config and a preview; writes belong to the caller.

    Available cash may move to/from frozen cash, but total cash and every
    holding must match the existing ledger. Unexplained changes require actual
    trade/cash-flow reconciliation and never create synthetic fills or flows.
    """
    now = (now or datetime.now(CN)).astimezone(CN)
    if not isinstance(body, dict) or body.get('mode') != 'balance_only' or body.get('no_activity') is not True:
        raise ValueError('请确认本次核对期间没有未登记的成交或资金转账')
    if body.get('events') or body.get('csv'):
        raise ValueError('有成交或转账时请使用流水对账')
    balances = body.get('broker')
    if not isinstance(balances, dict):
        raise ValueError('请填写券商账户余额及完整持仓')
    asof = timestamp(balances.get('asof'), '券商账户时间', now)
    if asof.date() != now.date():
        raise ValueError('余额核对须使用今天的券商账户时间')
    previous = cfg.get('trade_reconciliation') or {}
    anchors = [previous.get('broker_asof'), (previous_snapshot or {}).get('asof')]
    anchors += [row.get('executed_at') for row in
                cfg.get('reconciled_trades', []) + cfg.get('reconciled_cashflows', []) if isinstance(row, dict)]
    for value in filter(None, anchors):
        if asof < timestamp(value, '已有台账核对或流水时间', now):
            raise ValueError('账户时间早于已有对账或流水，请读取最新券商账户')

    available = cents(decimal(balances.get('available_cash'), '券商可用现金'))
    frozen = cents(decimal(balances.get('frozen_cash', 0), '券商冻结资金'))
    withdrawable = balances.get('withdrawable_cash')
    withdrawable = cents(decimal(withdrawable, '券商可取资金')) if withdrawable not in (None, '') else None
    if withdrawable is not None and withdrawable > available:
        raise ValueError('券商可取资金不能超过可用现金')
    total_assets = balances.get('total_assets')
    total_assets = cents(decimal(total_assets, '券商总资产', '.01')) if total_assets not in (None, '') else None
    total = available + frozen
    if total_assets is not None and total_assets < total:
        raise ValueError('券商总资产不能低于可用现金与冻结资金之和')
    before_available = cents(decimal(cfg.get('cash'), '台账可用现金'))
    before_frozen = cents(decimal(cfg.get('frozen_cash', 0), '台账冻结资金'))
    before_total = before_available + before_frozen
    declared = balances.get('positions')
    if not isinstance(declared, dict) or any(not isinstance(code, str) or not CODE.fullmatch(code) for code in declared):
        raise ValueError('请按6位证券代码填写券商完整持仓和可卖数量')
    positions = {str(row['code']): row for row in cfg.get('positions', []) if row.get('quantity', 0) > 0}
    differences = []
    if total != before_total:
        differences.append(f'总现金不一致：台账可用＋冻结 ¥{before_total:,.2f}，券商 ¥{total:,.2f}。请登记实际成交或资金流水后对账。')
    if set(declared) != set(positions):
        missing = sorted(set(positions) - set(declared))
        extra = sorted(set(declared) - set(positions))
        differences.append('完整持仓不一致：' + ('待补 ' + '、'.join(missing) if missing else '')
                           + ('；台账外证券 ' + '、'.join(extra) if extra else '') + '。请核对实际成交。')
    checked, rows = {}, []
    for code in sorted(declared):
        row = declared[code]
        if not isinstance(row, dict):
            raise ValueError(f'{code} 券商持仓格式无效')
        quantity = integer(row.get('quantity'), f'{code} 券商持仓')
        sellable = integer(row.get('available_quantity'), f'{code} 券商可卖量')
        if sellable > quantity:
            raise ValueError(f'{code} 券商可卖量不能超过持仓量')
        expected = positions.get(code, {}).get('quantity', 0)
        if quantity != expected:
            differences.append(f'{code} 持仓不一致：台账 {expected}，券商 {quantity}。请登记实际成交后对账。')
        checked[code] = dict(quantity=quantity, available_quantity=sellable)
        rows.append(dict(code=code, quantity=expected, broker_quantity=quantity, available_quantity=sellable))
    if total_assets == total and positions:
        differences.append('券商总资产等于全部现金，但台账仍有持仓，请核对总资产。')

    preview = dict(mode='balance_only', events=[], duplicates=[], differences=differences, positions=rows,
                   cash_before=float(before_available), cash_after=float(available),
                   frozen_before=float(before_frozen), frozen_after=float(frozen),
                   total_cash_before=float(before_total), total_cash_after=float(total),
                   withdrawable_cash=float(withdrawable) if withdrawable is not None else None)
    updated = deepcopy(cfg)
    if differences:
        return updated, preview
    receipt = dict(broker_asof=asof.isoformat(), available_cash=float(available), frozen_cash=float(frozen),
                   withdrawable_cash=float(withdrawable) if withdrawable is not None else None,
                   total_cash=float(total), total_assets=float(total_assets) if total_assets is not None else None,
                   positions=checked, position_signature=position_signature(cfg.get('positions', [])),
                   source='无新增流水余额核对', mode='balance_only', period_pnl=None)
    updated.update(cash=float(available), frozen_cash=float(frozen), trade_reconciliation=receipt)
    audit = dict(kind='balance_only', confirmed_at=now.isoformat(), broker_asof=asof.isoformat(),
                 before=dict(available_cash=float(before_available), frozen_cash=float(before_frozen),
                             withdrawable_cash=previous.get('withdrawable_cash')),
                 after=dict(available_cash=float(available), frozen_cash=float(frozen),
                            withdrawable_cash=receipt['withdrawable_cash']),
                 position_signature=receipt['position_signature'], no_activity=True)
    updated['account_balance_history'] = (list(cfg.get('account_balance_history') or []) + [audit])[-100:]
    preview['receipt'] = receipt
    preview['audit'] = audit
    return updated, preview
