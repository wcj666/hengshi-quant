"""Build a concrete, read-only after-close portfolio review.

This module turns the fixed V1 limits into a review plan. It does not submit
orders and it does not change the account ledger. A plan is actionable only
when the broker snapshot and every holding quote are complete and current.
"""
from __future__ import annotations

from datetime import datetime
from math import ceil, floor, isfinite

from strategy_setup import exit_review
from strategy_v1 import strategy_prefs
from trade_levels import config_plan, make_levels


EXIT_LABELS = {"stop": "固定止损", "take_profit_2": "固定第二止盈", "take_profit_1": "固定第一止盈"}

def money(value):
    return round(float(value) + 1e-9, 2)


def _number(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if isfinite(value) else default


def _lot(value, *, up=False):
    if value <= 0:
        return 0
    return int((ceil if up else floor)(value / 100.0) * 100)


def _fee(kind, side, notional):
    commission = max(5.0, notional * 0.0005)
    stamp = notional * 0.0005 if kind == "stock" and side == "sell" else 0.0
    transfer = notional * 0.00001 if kind == "stock" else 0.0
    return money(commission + stamp + transfer)


def _quote_map(quotes):
    result = {}
    for code, quote in (quotes or {}).items():
        code = str(code).zfill(6)
        if isinstance(quote, (list, tuple)):
            quote = {
                "price": quote[0] if len(quote) > 0 else None,
                "name": quote[1] if len(quote) > 1 else "",
                "previous_close": quote[2] if len(quote) > 2 else None,
                "source_timestamp": quote[3] if len(quote) > 3 else None,
            }
        result[code] = quote if isinstance(quote, dict) else {}
    return result


def _label(*values):
    return next((value.strip() for value in values if isinstance(value, str) and value.strip()), None)


def _position_rows(config, snapshot, quotes):
    holdings = {str(position["code"]).zfill(6): position for position in config.get("positions", [])}
    rows = []
    for raw in (snapshot or {}).get("positions", []):
        code = str(raw.get("code", "")).zfill(6)
        quote = quotes.get(code, {})
        price = _number(quote.get("price"))
        previous = _number(quote.get("previous_close"))
        price = price if price is not None and price > 0 else None
        previous = previous if previous is not None and previous > 0 else None
        quantity = int(_number(raw.get("quantity"), 0) or 0)
        available = _number(raw.get("available_quantity"))
        sellable_verified = available is not None and 0 <= available <= quantity and available.is_integer()
        plan = config_plan(config, code)
        holding = holdings.get(code, {})
        value = money(quantity * price) if price is not None else None
        daily_pct = money((price / previous - 1) * 100) if price and previous else None
        rows.append({
            "code": code,
            "name": raw.get("name", quote.get("name", code)),
            "type": raw.get("type", "stock"),
            "quantity": quantity,
            "sellable": int(available) if sellable_verified else 0,
            "sellable_verified": sellable_verified,
            "sector": _label(plan.get("sector"), holding.get("sector")),
            "theme": _label(plan.get("theme"), holding.get("theme")),
            "cost": _number(raw.get("cost")),
            "price": price,
            "previous_close": previous,
            "daily_pct": daily_pct,
            "value": value,
            "source_timestamp": quote.get("source_timestamp"),
        })
    return rows


def _fixed_exit(config, row, can_plan, clock):
    code = row["code"]
    plan = config_plan(config, code)
    levels = make_levels(config, code, price=row["price"], position=row, asset_type=row["type"])
    if not levels or not levels["fixed"] or not levels["valid_order"]:
        return {"status": "固定退出计划待补"}
    result = {"status": "固定退出条件未触发" if can_plan else "收盘行情待核验",
              "stop_loss": levels["stop_loss"], "take_profit_1": levels["take_profit_1"],
              "take_profit_2": levels["take_profit_2"]}
    if can_plan:
        try:
            review = exit_review(config, row, plan, row["price"], now=clock)
        except (ValueError, TypeError, OverflowError):
            return {"status": "固定退出计划需复核"}
        result["stop_loss"] = review["stop_loss"]
        if review["trigger"] in EXIT_LABELS:
            result.update(status=EXIT_LABELS[review["trigger"]] + "已触发", trigger=review["trigger"],
                          quantity=review["quantity"], reason=review["reason"])
    return result


def _sellable(row, requested):
    available = min(requested, row["sellable"])
    return available if available == row["quantity"] else _lot(available)


def _exposure_summary(rows, equity, planned, available, group):
    totals = {}
    for row in rows:
        name = row[group]
        if not name:
            continue
        item = totals.setdefault(name, {"value": 0, "value_after_available": 0,
                                        "value_after_all": 0, "codes": []})
        item["codes"].append(row["code"])
        item["value"] += row["value"]
        item["value_after_available"] += (row["quantity"] - available.get(row["code"], 0)) * row["price"]
        item["value_after_all"] += (row["quantity"] - planned.get(row["code"], 0)) * row["price"]
    for item in totals.values():
        for key in ("value", "value_after_available", "value_after_all"):
            item[key.replace("value", "pct", 1)] = money(item[key] / equity * 100)
            item[key] = money(item[key])
    return dict(sorted(totals.items(), key=lambda item: (-item[1]["value"], item[0])))


def reduction_draft(rows, equity, settings):
    """Size a risk-rule draft before imposing the final sellable-quantity cap."""
    max_position_pct = settings["max_position_pct"]
    max_exposure_pct = settings["max_exposure_pct"]
    max_sector_pct = settings["max_sector_pct"]
    max_positions = settings["max_positions"]
    requested_by_code = {}
    reasons_by_code = {}

    def add_sell(row, quantity, reason):
        requested = min(row["quantity"], quantity)
        requested_by_code[row["code"]] = max(requested_by_code.get(row["code"], 0), requested)
        reasons_by_code.setdefault(row["code"], []).append(reason)

    def planned(row):
        return requested_by_code.get(row["code"], 0)

    def remaining_value(row):
        return (row["quantity"] - planned(row)) * row["price"]

    def exposure_rows(members):
        return sum(remaining_value(row) for row in members)

    def relief(row, quantity, needed=None):
        value = quantity * row["price"]
        applied = value if needed is None else min(value, needed)
        totals = [exposure_rows([item for item in rows if item[key] == row[key]])
                  for key in ("sector", "theme") if row[key]]
        # Reduction in squared concentration at the same account valuation.
        # When sector and theme overlap, the larger effect counts once.
        largest = max(totals, default=remaining_value(row))
        return applied * (2 * largest - applied), applied, -value, row["code"]

    def reduce_exposure(members, limit, reason):
        while (excess := exposure_rows(members) - limit) > 1e-8:
            options = []
            for row in members:
                remaining = row["quantity"] - planned(row)
                if remaining <= 0:
                    continue
                desired = min(remaining, _lot(excess / row["price"], up=True))
                available = max(0, _sellable(row, planned(row) + desired) - planned(row))
                options.append((row, available or desired, bool(available)))
            if not options:
                break
            row, quantity, _ = max(options, key=lambda item: (item[2], *relief(item[0], item[1], excess)))
            add_sell(row, planned(row) + quantity, reason)

    triggered = [row for row in rows if row.get("fixed_exit", {}).get("trigger")]
    priority = tuple(EXIT_LABELS)
    for row in sorted(triggered, key=lambda item: priority.index(item["fixed_exit"]["trigger"])):
        fixed = row["fixed_exit"]
        add_sell(row, fixed["quantity"], fixed["reason"])

    active = [row for row in rows if planned(row) < row["quantity"]]
    for _ in range(max(0, len(active) - max_positions)):
        row = max(active, key=lambda item: (
            _sellable(item, item["quantity"]) == item["quantity"],
            *relief(item, item["quantity"] - planned(item)),
        ))
        active.remove(row)
        add_sell(row, row["quantity"], f"持仓数超过{max_positions:g}只，优先减少集中敞口")

    position_cap = equity * max_position_pct / 100
    for row in sorted(rows, key=remaining_value, reverse=True):
        excess = remaining_value(row) - position_cap
        if excess > 1e-8:
            add_sell(row, _lot(planned(row) + excess / row["price"], up=True),
                     f"单只市值超过{max_position_pct:g}%上限，降至约¥{position_cap:,.2f}以内")

    sectors = sorted({row["sector"] for row in rows if row["sector"]})
    for sector in sectors:
        reduce_exposure([row for row in rows if row["sector"] == sector], equity * max_sector_pct / 100,
                        f"{sector}行业敞口超过{max_sector_pct:g}%上限")
    reduce_exposure(rows, equity * max_exposure_pct / 100,
                    f"组合仓位超过{max_exposure_pct:g}%上限，优先减少集中敞口")

    return requested_by_code, reasons_by_code


def build_close_review(config, snapshot, quotes, candidates=None, *, asof=None,
                       data_verified=False, quote_errors=None):
    """Plan fixed exits first, then reduce concentrated and excess exposure."""
    settings = strategy_prefs(config)
    max_exposure_pct = settings["max_exposure_pct"]
    max_sector_pct = settings["max_sector_pct"]
    per_trade_risk = settings["per_trade_risk"]
    fee_reserve = settings["fee_reserve"]
    snapshot = snapshot or {}
    quotes = _quote_map(quotes)
    rows = _position_rows(config, snapshot, quotes)
    cash = _number(snapshot.get("available_cash"))
    withdrawable_cash = _number(snapshot.get("withdrawable_cash"))
    if cash is None or withdrawable_cash is None or not 0 <= withdrawable_cash <= cash:
        withdrawable_cash = None
    complete = bool(snapshot and cash is not None and cash >= 0 and all(
        row["quantity"] > 0 and row["price"] is not None and row["previous_close"] is not None
        and row["source_timestamp"] for row in rows
    ))
    errors = list(quote_errors or [])
    if not complete:
        errors.append("账户快照或完整持仓行情缺失")

    market_value = money(sum(row["value"] or 0 for row in rows)) if complete else None
    equity = money(cash + market_value) if complete else None
    can_plan = bool(complete and data_verified and equity > 0)
    try:
        clock = datetime.fromisoformat((asof or snapshot.get("asof") or "").replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        clock = None
    target_reserve = money(max(equity * (1 - max_exposure_pct / 100.0),
                               fee_reserve + 2 * per_trade_risk)) if can_plan else None
    current_exposure_pct = money(market_value / equity * 100) if equity else None
    sell_plan = []
    pending_sell_plan = []
    positions = {row["code"]: row for row in rows}
    for row in rows:
        row["fixed_exit"] = _fixed_exit(config, row, can_plan, clock)
    requested_by_code, reasons_by_code = reduction_draft(rows, equity, settings) if can_plan else ({}, {})

    # Build the executable and pending portions after all constraints have been
    # evaluated. This keeps T+1 inventory from causing duplicate exposure cuts.
    available_by_code = {}
    for code, required in requested_by_code.items():
        row = positions[code]
        available = _sellable(row, required)
        available_by_code[code] = available
        reasons = "；".join(dict.fromkeys(reasons_by_code[code]))
        fields = {"code": code, "name": row["name"], "price": row["price"],
                  "sector": row["sector"], "theme": row["theme"],
                  "trigger": row["fixed_exit"].get("trigger"), "source_timestamp": row["source_timestamp"]}
        if available:
            sell_plan.append({**fields, "quantity": available, "value": money(available * row["price"]),
                              "exposure_reduction_pct": money(available * row["price"] / equity * 100),
                              "reason": reasons})
        pending = required - available
        if pending:
            pending_sell_plan.append({
                **fields, "quantity": pending, "value": money(pending * row["price"]),
                "available_now": row["sellable"] if row["sellable_verified"] else None,
                "reason": ("计划数量超过已核对的可卖数量，待券商确认后复核" if row["sellable_verified"]
                           else "券商可卖数量未知，待对账"),
                "detail": reasons,
            })

    def net_proceeds(quantities):
        gross = {code: money(positions[code]["price"] * quantity)
                 for code, quantity in quantities.items() if quantity}
        return money(sum(value - _fee(positions[code]["type"], "sell", value)
                         for code, value in gross.items()))

    proceeds = net_proceeds(available_by_code) if can_plan else None
    pending_quantities = {item["code"]: item["quantity"] for item in pending_sell_plan}
    all_proceeds = money(proceeds + net_proceeds(pending_quantities)) if can_plan else None
    cash_after_sales = money(cash + proceeds) if can_plan else None
    cash_after_all_sales = money(cash + all_proceeds) if can_plan else None
    excess_after_all_sales = money(max(0.0, cash_after_all_sales - target_reserve)) if cash_after_all_sales is not None else None
    buy_plan = []
    for candidate in candidates or []:
        code = str(candidate.get("code", "")).zfill(6)
        entry = _number(candidate.get("entry_price"))
        stop = _number(candidate.get("stop_loss"))
        if not code or entry is None or stop is None or entry <= stop:
            continue
        risk_per_share = entry - stop
        buy_plan.append({
            "code": code,
            "name": candidate.get("name", code),
            "quantity": 0,
            "trigger_price": _number(candidate.get("trigger_price")),
            "entry_price": entry,
            "stop_loss": stop,
            "take_profit_1": _number(candidate.get("take_profit_1")),
            "take_profit_2": _number(candidate.get("take_profit_2")),
            "risk_per_share": money(risk_per_share),
            "reason": "研究候选，买入股数等待盘中V1核验",
            "approved": False,
        })

    execution_status = "可生成次日计划" if can_plan else "仅供复盘，暂不可执行"
    sectors = _exposure_summary(rows, equity, requested_by_code, available_by_code, "sector") if can_plan else {}
    themes = _exposure_summary(rows, equity, requested_by_code, available_by_code, "theme") if can_plan else {}
    return {
        "generated_at": asof,
        "source": "腾讯公开行情与最近已核验账户快照",
        "data_verified": can_plan,
        "execution_status": execution_status,
        "errors": errors,
        "account_asof": snapshot.get("asof"),
        "equity": equity,
        "cash": cash,
        "market_value": market_value,
        "current_exposure_pct": current_exposure_pct,
        "position_count": len(rows),
        "sector_exposure": sectors,
        "theme_exposure": themes,
        "unclassified_codes": [row["code"] for row in rows if not row["sector"]],
        "max_sector_pct": max_sector_pct,
        "cash_policy": {
            "broker_available_cash": cash,
            "broker_withdrawable_cash": withdrawable_cash,
            "estimated_proceeds_sellable": proceeds,
            "estimated_proceeds_all": all_proceeds,
            "minimum_reserve": target_reserve,
            "cash_after_planned_sales": cash_after_sales,
            "available_for_new_buys": 0.0 if can_plan else None,
            "cash_after_all_planned_sales": cash_after_all_sales,
            "available_after_all_planned_sales": excess_after_all_sales,
            "formula": f"至少保留总资产的{100 - max_exposure_pct:g}%，并覆盖两笔单笔风险和费用预留",
        },
        "positions": rows,
        "sell_plan": sell_plan,
        "pending_sell_plan": pending_sell_plan,
        "buy_plan": buy_plan,
        "notes": [
            "卖出数量按100股/份整数手和已确认可卖数量计算。",
            "卖出回款以已核验收盘价扣除预估费用测算，实际委托前核对券商可卖数、停牌和价格限制。",
            "预计仓位以相同的收盘估算总资产为基准。新增买入股数由盘中 V1 核验。",
        ],
    }
