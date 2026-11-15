"""Build a concrete, read-only after-close portfolio review.

This module turns the fixed V1 limits into a review plan. It does not submit
orders and it does not change the account ledger. A plan is actionable only
when the broker snapshot and every holding quote are complete and current.
"""
from __future__ import annotations

from math import ceil, floor, isfinite


def money(value):
    return round(float(value) + 1e-9, 2)


def _number(value, default=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
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


def _position_rows(snapshot, quotes):
    rows = []
    for raw in (snapshot or {}).get("positions", []):
        code = str(raw.get("code", "")).zfill(6)
        quote = quotes.get(code, {})
        price = _number(quote.get("price"))
        previous = _number(quote.get("previous_close"))
        price = price if price is not None and price > 0 else None
        previous = previous if previous is not None and previous > 0 else None
        quantity = int(_number(raw.get("quantity"), 0) or 0)
        value = money(quantity * price) if price is not None else None
        daily_pct = money((price / previous - 1) * 100) if price and previous else None
        rows.append({
            "code": code,
            "name": raw.get("name", quote.get("name", code)),
            "type": raw.get("type", "stock"),
            "quantity": quantity,
            "sellable": max(0, min(quantity, int(_number(raw.get("available_quantity"), 0)))),
            "cost": _number(raw.get("cost")),
            "price": price,
            "previous_close": previous,
            "daily_pct": daily_pct,
            "value": value,
            "source_timestamp": quote.get("source_timestamp"),
        })
    return rows


def build_close_review(config, snapshot, quotes, candidates=None, *, asof=None,
                       data_verified=False, quote_errors=None):
    """Return an explainable review with sell quantities, buy triggers and cash.

    The sell plan applies max positions first, then per-position caps, then the
    total exposure cap. The weakest daily performers are chosen for the count
    reduction. Buy candidates stay in research until the intraday checks run.
    """
    settings = config.get("strategy_v1", {})
    max_position_pct = float(settings.get("max_position_pct", 15.0))
    max_exposure_pct = float(settings.get("max_exposure_pct", 60.0))
    max_positions = int(settings.get("max_positions", 4))
    per_trade_risk = float(settings.get("per_trade_risk", 150.0))
    fee_reserve = float(settings.get("fee_reserve", settings.get("min_fees", 20.0)))
    snapshot = snapshot or {}
    quotes = _quote_map(quotes)
    rows = _position_rows(snapshot, quotes)
    cash = _number(snapshot.get("available_cash"))
    complete = bool(snapshot and cash is not None and cash >= 0 and all(
        row["quantity"] > 0 and row["price"] is not None and row["previous_close"] is not None
        and row["source_timestamp"] for row in rows
    ))
    errors = list(quote_errors or [])
    if not complete:
        errors.append("账户快照或完整持仓行情缺失")

    market_value = money(sum(row["value"] or 0 for row in rows)) if complete else None
    equity = money(cash + market_value) if complete else None
    can_plan = complete and data_verified
    target_reserve = money(max(equity * (1 - max_exposure_pct / 100.0),
                               fee_reserve + 2 * per_trade_risk)) if can_plan else None
    current_exposure_pct = money(market_value / equity * 100) if equity else None
    sell_plan = []
    pending_sell_plan = []
    requested_by_code = {}
    reasons_by_code = {}
    active_codes = {row["code"] for row in rows}

    def add_sell(row, quantity, reason):
        requested = min(row["quantity"], _lot(quantity, up=True))
        requested_by_code[row["code"]] = max(requested_by_code.get(row["code"], 0), requested)
        reasons_by_code.setdefault(row["code"], []).append(reason)

    if can_plan and len(active_codes) > max_positions:
        ordered = sorted(rows, key=lambda row: row["daily_pct"] if row["daily_pct"] is not None else 0)
        for row in ordered[:len(active_codes) - max_positions]:
            add_sell(row, row["quantity"],
                     f"持仓数超过{max_positions}只上限，且日跌幅{row['daily_pct']:+.2f}%居前，先退出弱势仓位")

    position_cap = equity * max_position_pct / 100 if equity is not None else None
    for row in rows:
        if not can_plan or requested_by_code.get(row["code"], 0) >= row["quantity"]:
            continue
        excess = row["value"] - position_cap
        if excess > 0:
            add_sell(row, excess / row["price"],
                     f"单只市值超过{max_position_pct:g}%上限，减至约¥{position_cap:,.2f}")

    planned_value = sum(
        (row["quantity"] - requested_by_code.get(row["code"], 0)) * row["price"]
        for row in rows if row["price"] is not None
    ) if complete else None
    exposure_cap = equity * max_exposure_pct / 100 if equity is not None else None
    if can_plan and planned_value > exposure_cap:
        remaining = planned_value - exposure_cap
        for row in sorted(rows, key=lambda item: item["daily_pct"] if item["daily_pct"] is not None else 0):
            available = row["quantity"] - requested_by_code.get(row["code"], 0)
            if available <= 0:
                continue
            quantity = min(available, _lot(remaining / row["price"], up=True))
            if quantity <= 0:
                continue
            add_sell(row, requested_by_code.get(row["code"], 0) + quantity,
                     f"组合仓位超过{max_exposure_pct:g}%上限，继续降低总敞口")
            remaining -= quantity * row["price"]
            if remaining <= 0:
                break

    # Build the executable and pending portions after all constraints have been
    # evaluated. This keeps T+1 inventory from causing duplicate exposure cuts.
    for row in rows:
        required = min(row["quantity"], requested_by_code.get(row["code"], 0))
        available = min(required, row["sellable"])
        if available < row["quantity"]:
            available = _lot(available)
        reasons = "；".join(dict.fromkeys(reasons_by_code.get(row["code"], [])))
        if available:
            sell_plan.append({"code": row["code"], "name": row["name"], "quantity": available,
                              "price": row["price"], "value": money(available * row["price"]),
                              "reason": reasons, "source_timestamp": row["source_timestamp"]})
        pending = required - available
        if pending:
            pending_sell_plan.append({
                "code": row["code"], "name": row["name"], "quantity": pending,
                "price": row["price"], "value": money(pending * row["price"]),
                "available_now": row["sellable"],
                "reason": "计划数量超过当前可卖数量，待T+1或下一次券商对账后复核",
                "detail": reasons,
                "source_timestamp": row["source_timestamp"],
            })

    proceeds = money(sum(item["value"] - _fee(next(row["type"] for row in rows if row["code"] == item["code"]), "sell", item["value"]) for item in sell_plan)) if complete else 0.0
    cash_after_sales = money(cash + proceeds) if can_plan else None
    all_planned = sell_plan + pending_sell_plan
    all_proceeds = money(sum(item["value"] - _fee(next(row["type"] for row in rows if row["code"] == item["code"]), "sell", item["value"]) for item in all_planned)) if complete else 0.0
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

    execution_status = "可生成次日计划" if complete and data_verified else "仅供复盘，暂不可执行"
    return {
        "generated_at": asof,
        "source": "腾讯公开行情与最近已核验账户快照",
        "data_verified": bool(complete and data_verified),
        "execution_status": execution_status,
        "errors": errors,
        "account_asof": snapshot.get("asof"),
        "equity": equity,
        "cash": cash,
        "market_value": market_value,
        "current_exposure_pct": current_exposure_pct,
        "position_count": len(rows),
        "cash_policy": {
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
            "卖出后的现金是情景测算。新增买入股数由盘中V1核验。",
            "收盘复盘只生成计划，不写入真实账户，不提交券商订单。",
        ],
    }
