"""What-if portfolio sales from a single verified valuation and a local account ledger."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from action_center import CN, fresh, market_status, number, positive
from close_review import EXIT_LABELS, reduction_draft
from quote_health import parse_provider_time
from strategy_v1 import strategy_prefs
from t_costs import cash_impact, decimal, money
from trade_reconciliation import matches_receipt

ZERO = Decimal(0)


def quote_context(state, holdings, now, completed_day):
    pause = market_status(state, now)
    if not pause:
        quotes = {str(q['code']): q for q in state.get('quotes', []) if q.get('held') is True}
        if all(positive(quotes.get(code, {}).get('price')) is not None
               and fresh(quotes[code].get('source_timestamp'), now) for code in holdings):
            stamps = [parse_provider_time(quotes[code]['source_timestamp']) for code in holdings]
            fetch_at = parse_provider_time(state['fetch_finished_at'])
            end = time(11, 30) if now.hour < 12 else time(15)
            session_end = datetime.combine(now.date(), end, CN)
            expires = min(fetch_at + timedelta(seconds=45), session_end,
                          *(stamp + timedelta(seconds=90) for stamp in stamps))
            return 'live', quotes, min(stamps, default=fetch_at), expires, state.get('data_source'), None
        pause = '部分持仓缺少新鲜报价，暂停组合测算'
    clock = now.time()
    outside_session = now.weekday() >= 5 or not (time(9, 30) <= clock < time(11, 30) or time(13) <= clock < time(15))
    snapshot = state.get('display_snapshot') or {}
    if outside_session and completed_day and snapshot.get('historical') is True:
        quotes = {str(q['code']): q for q in snapshot.get('quotes', [])}
        stamps = [parse_provider_time(quotes.get(code, {}).get('source_timestamp')) for code in holdings]
        if stamps and all(stamp and stamp <= now and stamp.astimezone(CN).date().isoformat() == completed_day
                          and stamp.astimezone(CN).time() >= time(15) for stamp in stamps) and all(
                              positive(quotes[code].get('price')) is not None for code in holdings):
            return 'close', quotes, min(stamps), None, snapshot.get('source'), None
    return 'unavailable', {}, None, None, None, pause


def inventory_evidence(holdings, cfg, book, receipt, now):
    """Use the newest same-day evidence, never promote a prior-day count."""
    latest_fills = {}
    for fill in cfg.get('reconciled_trades', []):
        stamp = parse_provider_time(fill.get('executed_at'))
        code = str(fill.get('code'))
        if stamp and (code not in latest_fills or stamp > latest_fills[code]):
            latest_fills[code] = stamp
    receipt_at = parse_provider_time(receipt.get('broker_asof'))
    evidence = {}
    for code, position in holdings.items():
        held = position['quantity']
        manual = (book.get('availability') or {}).get(code, {})
        checked = (receipt.get('positions') or {}).get(code, {})
        options = [
            (parse_provider_time(manual.get('confirmed_at')), manual.get('date'),
             manual.get('base_quantity'), manual.get('available_quantity')),
            (receipt_at, receipt_at.astimezone(CN).date().isoformat() if receipt_at else None,
             checked.get('quantity'), checked.get('available_quantity')),
        ]
        verified = [(stamp, available) for stamp, day, quantity, available in options
                    if stamp and stamp <= now and day == now.date().isoformat()
                    and stamp.astimezone(CN).date() == now.date() and quantity == held
                    and type(available) is int and 0 <= available <= held
                    and (code not in latest_fills or stamp >= latest_fills[code])]
        if verified:
            evidence[code] = max(verified, key=lambda item: item[0])
    return evidence


def preview_calculation(rows, quantities, cash, settings, book, pause, mode, *, frozen_cash=0):
    metrics = {key: None for key in ('cash_before', 'cash_after', 'cash_target', 'cash_shortfall',
              'exposure_before_pct', 'exposure_after_pct', 'market_value_after', 'fees',
              'net_proceeds', 'remaining_count', 'equity_after', 'estimated_fees',
              'safety_reserve', 'reserved_cost', 'estimated_net_proceeds',
              'estimated_cash_after', 'estimated_equity_after', 'conservative_equity_after',
              'frozen_cash', 'total_cash_before', 'estimated_total_cash_after')}
    preview = dict(valid=False, errors=[], unverified=[], over_limits=[], rows=[], execution_status=None, **metrics)
    if pause:
        preview['errors'].append(pause)
        return preview
    if number(frozen_cash) is None or frozen_cash < 0:
        preview['errors'].append('冻结资金待核验，暂停组合测算')
        return preview
    indexed = {row['code']: row for row in rows}
    for code, amount in quantities.items():
        row = indexed.get(code)
        if row is None:
            preview['errors'].append(f'{code} 不在当前持仓中')
        elif type(amount) is not int or not 0 <= amount <= row['quantity']:
            preview['errors'].append(f'{row["name"]}卖出数量须为非负整数且不超过持仓')
        elif amount % row['lot'] and amount != row['quantity']:
            preview['errors'].append(f'{row["name"]}部分卖出按 {row["lot"]} 单位取整，清仓可输入全部持有量')
    if preview['errors']:
        return preview

    cash = decimal(cash)
    frozen = decimal(frozen_cash)
    market_before, market_after, proceeds, total_fee, safety_reserve, estimated_fees = (ZERO,) * 6
    fees_known = True
    sector_values = {}
    for row in rows:
        code, held, price = row['code'], row['quantity'], decimal(row['price'])
        quantity = quantities.get(code, 0)
        remaining = held - quantity
        before, gross, after = (price * count for count in (held, quantity, remaining))
        impact = cash_impact(row['kind'], 'sell', float(gross), settings['fee_reserve'],
                             book.get('fee_model') or {}, book.get('fee_samples') or [])
        fee = decimal(impact['reserved_cost'])
        if quantity:
            if gross < fee:
                preview['errors'].append(f'{row["name"]}预计毛回款不足以覆盖费用预留')
            if row['eligibility'] != '可卖数量已核对':
                preview['unverified'].append(f'{row["name"]}：{row["eligibility"]}')
            elif quantity > row['available_quantity']:
                preview['unverified'].append(f'{row["name"]}：超过已核对可卖 {row["available_quantity"]} 股/份')
        net = gross - fee
        market_before += before
        market_after += after
        total_fee += fee
        safety_reserve += decimal(impact['safety_reserve'])
        fees_known &= impact['estimated_fee'] is not None
        estimated_fees += decimal(impact['estimated_fee'] or 0)
        proceeds += net
        sector = row['sector']
        if sector:
            sector_values[sector] = sector_values.get(sector, ZERO) + after
        preview['rows'].append(dict(code=code, sell_quantity=quantity, remaining_quantity=remaining,
                                    remaining_value=after, fee=impact['estimated_fee'], net_proceeds=money(net),
                                    estimated_fee=impact['estimated_fee'], fee_basis=impact['fee_basis'],
                                    safety_reserve=impact['safety_reserve'], reserved_cost=impact['reserved_cost'],
                                    estimated_net_proceeds=impact['estimated_cash_delta']))
    equity_before = market_before + cash + frozen
    cash_after = cash + proceeds
    conservative_equity = market_after + cash_after + frozen
    estimated_equity = conservative_equity + safety_reserve if fees_known else None
    # Earmarked cash is still an account asset. It affects spending capacity,
    # never the estimated net asset value when brokerage costs are known.
    limit_equity = estimated_equity if estimated_equity is not None else conservative_equity
    if equity_before <= 0 or limit_equity <= 0:
        preview['errors'].append('扣费后的账户总资产不足，无法计算仓位比例')
    if preview['errors']:
        preview['rows'] = []
        return preview

    exposure = market_after * 100 / limit_equity
    target = max(limit_equity * (1 - decimal(settings['max_exposure_pct']) / 100),
                 decimal(settings['fee_reserve']) + 2 * decimal(settings['per_trade_risk']))
    remaining_count = sum(row['remaining_quantity'] > 0 for row in preview['rows'])
    limits = preview['over_limits']
    if exposure > decimal(settings['max_exposure_pct']):
        limits.append(f'总仓位仍超过 {settings["max_exposure_pct"]:g}% 上限')
    if remaining_count > settings['max_positions']:
        limits.append(f'剩余持仓仍超过 {settings["max_positions"]} 只上限')
    for item in preview['rows']:
        value = item['remaining_value']
        weight = 100 * value / limit_equity
        item.update(remaining_value=money(value), remaining_weight_pct=money(weight))
        if weight > decimal(settings['max_position_pct']):
            limits.append(f'{indexed[item["code"]]["name"]}仍超过单只 {settings["max_position_pct"]:g}% 上限')
    for sector, value in sector_values.items():
        if 100 * value / limit_equity > decimal(settings['max_sector_pct']):
            limits.append(f'{sector}仍超过行业 {settings["max_sector_pct"]:g}% 上限')
    execution_status = ('收盘情景，按假设全部成交测算' if mode == 'close' else
                        '按假设全部成交测算' if preview['unverified'] else
                        '可卖数量已核对，按参考价估算' if any(quantities.values()) else '当前组合基线 · 未选卖出')
    preview.update(valid=True, execution_status=execution_status, cash_before=money(cash),
                   frozen_cash=money(frozen), total_cash_before=money(cash+frozen),
                   estimated_total_cash_after=money(cash_after+safety_reserve+frozen) if fees_known else None,
                   cash_after=money(cash_after), cash_target=money(target), cash_shortfall=money(max(ZERO, target - cash_after)),
                   exposure_before_pct=money(market_before * 100 / equity_before), exposure_after_pct=money(exposure),
                   market_value_after=money(market_after),
                   equity_after=money(estimated_equity) if estimated_equity is not None else None,
                   conservative_equity_after=money(conservative_equity),
                   fees=money(estimated_fees) if fees_known else None, reserved_cost=money(total_fee),
                   estimated_fees=money(estimated_fees) if fees_known else None,
                   safety_reserve=money(safety_reserve),
                   estimated_net_proceeds=money(proceeds + safety_reserve) if fees_known else None,
                   estimated_cash_after=money(cash_after + safety_reserve) if fees_known else None,
                   estimated_equity_after=money(estimated_equity) if estimated_equity is not None else None,
                   equity_basis='仅扣预计手续费；安全预留仍计入资产' if fees_known else '费率待补，仓位上限按费用预算保守测算；净值待核验',
                   net_proceeds=money(proceeds), remaining_count=remaining_count)
    return preview


def build_reduction_preview(cfg, state, book, quantities, *, now=None, completed_day=None):
    now = (now or datetime.now(CN)).astimezone(CN)
    settings = strategy_prefs(cfg)
    holdings = {str(p['code']): p for p in cfg.get('positions', []) if p.get('quantity', 0) > 0}
    mode, quotes, asof, expires, source, pause = quote_context(state, holdings, now, completed_day)
    receipt = cfg.get('trade_reconciliation') or {}
    if not matches_receipt(cfg, receipt):
        receipt = {}
    broker = state.get('broker_snapshot') or {}
    receipt_at = parse_provider_time(receipt.get('broker_asof'))
    broker_at = parse_provider_time(broker.get('asof'))
    if receipt_at and receipt_at <= now and (broker_at is None or receipt_at >= broker_at):
        account_source, account_at = '本地已确认成交对账', receipt_at
    else:
        account_source, account_at = ('最近与台账匹配的券商快照', broker_at) if broker_at else ('本地持仓台账', None)
    inventory = inventory_evidence(holdings, cfg, book, receipt, now) if mode == 'live' else {}
    synced = not any(c.get('date') == now.date().isoformat() and c.get('status') == 'open'
                     and c.get('ledger_sync_required') for c in book.get('cycles', []))
    cash = number(cfg.get('cash'))
    frozen_cash = number(cfg.get('frozen_cash', 0))
    if cash is None or cash < 0 or frozen_cash is None or frozen_cash < 0 or not synced:
        pause = '持仓或现金待对账，请先同步券商余额和做 T 实际成交'
        mode, quotes, asof, expires, source = 'unavailable', {}, None, None, None
        inventory = {}
    rows = []
    plans = cfg.get('trade_plans') or {}
    for code, position in holdings.items():
        q = quotes.get(code, {})
        price = q.get('price')
        held, kind = position['quantity'], position['type']
        checked_at, available = inventory.get(code, (None, None))
        if mode == 'close':
            eligibility = '收盘参考，可卖数量待下一交易时段核对'
        elif mode == 'unavailable':
            eligibility = '等待完整行情和账户核验'
        elif q.get('at_limit_down') is True:
            eligibility = '触及跌停，成交条件待券商核实'
        elif q.get('limit_status_verified') is not True:
            eligibility = '涨跌停状态待核验'
        elif available is None:
            eligibility = '今日券商可卖数量未核对'
        elif available == 0:
            eligibility = '券商记录可卖数量为 0'
        else:
            eligibility = '可卖数量已核对'
        plan = plans.get(code) or {}
        review = q.get('strategy_exit') or {}
        fixed = review if review.get('trigger') in EXIT_LABELS and type(review.get('quantity')) is int else {}
        rows.append(dict(code=code, name=position.get('name', code), kind=kind, quantity=held,
                         lot=10 if kind == 'convertible' else 100, max_quantity=held if not pause else None,
                         available_quantity=available, available_asof=checked_at.isoformat() if checked_at else None,
                         sellable=available if eligibility == '可卖数量已核对' else 0,
                         eligibility=eligibility, price=price, value=money(decimal(price) * held) if price else None,
                         sector=plan.get('sector') or position.get('sector'),
                         theme=plan.get('theme') or position.get('theme'), fixed_exit=fixed))

    equity = (cash + frozen_cash + sum(row['value'] for row in rows)) if not pause else None
    for row in rows:
        row['weight_pct'] = money(row['value'] * 100 / equity) if equity else None
    draft_quantities, reasons = reduction_draft(rows, equity, settings) if equity and rows else ({}, {})
    draft = dict(quantities=draft_quantities, reasons={code: '；'.join(dict.fromkeys(items)) for code, items in reasons.items()},
                 status='按既有仓位上限生成，可自行调整' if draft_quantities else '暂无仓位规则草案')
    preview = preview_calculation(rows, quantities, cash, settings, book, pause, mode,
                                  frozen_cash=frozen_cash)
    return dict(asof=asof.isoformat() if asof else None, server_time=now.isoformat(),
                expires_at=expires.isoformat() if expires else None, mode=mode, source=source,
                status=pause or ('盘中参考价 · 组合情景测算' if mode == 'live' else '最近完整收盘 · 组合情景测算'),
                cash_target_pct=100 - settings['max_exposure_pct'], account_source=account_source,
                frozen_cash=frozen_cash, total_cash=money(cash+frozen_cash) if cash is not None and frozen_cash is not None else None,
                account_asof=account_at.isoformat() if account_at else None, positions=rows, suggested=draft,
                fee_note=f'逐只计一次卖出费用；手续费与额外安全预留分列，合计每只至少预留 ¥{settings["fee_reserve"]:,.2f}。保守回款按全部成交测算，预留不是实际费用或损失。冻结资金计入资产但不计可用现金，与券商可取资金分开。',
                preview=preview)
