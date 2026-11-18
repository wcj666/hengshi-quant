"""Metrics and exports for the offline V1 backtest."""
from __future__ import annotations

from collections import Counter
import csv
import json
from math import expm1, fsum, isfinite, log
from pathlib import Path
from tempfile import TemporaryDirectory


CSV_REPORTS = {
    "fills": ("fills.csv", ("date", "time", "code", "type", "side", "quantity", "price", "notional",
                             "commission", "stamp_tax", "transfer", "fees", "slippage")),
    "closed_trades": ("closed_trades.csv", ("code", "type", "gross_pnl", "net_pnl", "initial_risk", "net_r")),
    "equity_curve": ("equity_curve.csv", ("date", "equity", "cash", "positions", "missing_marks")),
    "decisions": ("decisions.csv", ("date", "time", "code", "reason")),
}


def _known_equity(row):
    equity = row.get("equity")
    if equity is None or row.get("missing_marks") or not isfinite(equity) or equity < 0:
        return None
    return equity


def _drawdowns(initial_cash, equities):
    peak = initial_cash
    cash_loss = percentage_loss = 0
    for equity in equities:
        peak = max(peak, equity)
        loss = peak - equity
        cash_loss = max(cash_loss, loss)
        percentage_loss = max(percentage_loss, loss / peak)
    return round(cash_loss, 2), round(percentage_loss * 100, 4)


def _annualized_pct(end_equity, initial_cash, sessions):
    if sessions < 2 or end_equity is None:
        return None
    if end_equity == 0:
        return -100.0
    # Log-space multiplication keeps small changes accurate. A very short
    # extreme run can produce an unrepresentable extrapolation.
    try:
        rate = expm1((log(end_equity) - log(initial_cash)) * 252 / sessions) * 100
    except OverflowError:
        return None
    return round(rate, 4) if isfinite(rate) else None


def _fill_metrics(fills):
    charges = {
        f"{name}_total": round(fsum(fill[name] for fill in fills), 2)
        for name in ("commission", "stamp_tax", "transfer", "slippage")
    }
    fees = round(fsum(fill["fees"] for fill in fills), 2)
    return {
        "fill_count": len(fills),
        "buy_fill_count": sum(fill["side"] == "buy" for fill in fills),
        "sell_fill_count": sum(fill["side"] == "sell" for fill in fills),
        "traded_notional": round(fsum(fill["notional"] for fill in fills), 2),
        "fee_total": fees,
        **charges,
        "estimated_execution_cost": round(fees + charges["slippage_total"], 2),
    }


def _closed_metrics(closed_trades):
    profits = [trade["net_pnl"] for trade in closed_trades]
    wins = [pnl for pnl in profits if pnl > 0]
    losses = [pnl for pnl in profits if pnl < 0]
    net_rs = [trade["net_r"] for trade in closed_trades if trade.get("net_r") is not None]
    gross_pnl = fsum(trade["gross_pnl"] for trade in closed_trades)
    net_pnl = fsum(profits)
    gross_profit = fsum(trade["gross_pnl"] for trade in closed_trades if trade["gross_pnl"] > 0)
    closed_fees = gross_pnl - net_pnl
    losing_streak = current_streak = 0
    for pnl in profits:
        current_streak = current_streak + 1 if pnl < 0 else 0
        losing_streak = max(losing_streak, current_streak)
    return {
        "closed_trade_count": len(profits),
        "winner_count": len(wins),
        "loser_count": len(losses),
        "breakeven_count": len(profits) - len(wins) - len(losses),
        "win_rate_pct": round(len(wins) / len(profits) * 100, 4) if profits else None,
        "win_loss_ratio": round((fsum(wins) / len(wins)) / (-fsum(losses) / len(losses)), 4)
        if wins and losses else None,
        "profit_factor": round(fsum(wins) / -fsum(losses), 4) if losses else None,
        "avg_net_r": round(fsum(net_rs) / len(net_rs), 4) if net_rs else None,
        "max_consecutive_losses": losing_streak,
        "realized_gross_pnl": round(gross_pnl, 2),
        "realized_net_pnl": round(net_pnl, 2),
        "closed_fee_total": round(closed_fees, 2),
        "closed_positive_gross_profit": round(gross_profit, 2),
        "closed_fee_to_gross_profit_pct": round(closed_fees / gross_profit * 100, 4)
        if gross_profit else None,
    }


def summarize(initial_cash, equity_curve, fills, closed_trades, symbols, rejections):
    """Summarize ordered close valuations, fills and completed round trips.

    The curve must contain each modeled session. A missing daily valuation
    prevents computing a complete drawdown or average equity. Closed trades
    are ordered by completion, so a zero-P&L trade resets a losing streak.
    """
    if not isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("初始资金必须为正数")
    equities = [_known_equity(row) for row in equity_curve]
    end_equity = equities[-1] if equities else None
    missing_dates = [row["date"] for row, equity in zip(equity_curve, equities) if equity is None]
    complete = bool(equities) and not missing_dates
    max_drawdown, max_drawdown_pct = _drawdowns(initial_cash, equities) if complete else (None, None)
    net_pnl = end_equity - initial_cash if end_equity is not None else None
    fill_metrics = _fill_metrics(fills)
    mean_equity = fsum(equities) / len(equities) if complete else None
    by_asset_type = {}
    for kind in ("stock", "etf"):
        asset_fills = [fill for fill in fills if fill["type"] == kind]
        asset_trades = [trade for trade in closed_trades if trade["type"] == kind]
        by_asset_type[kind] = {**_fill_metrics(asset_fills), **_closed_metrics(asset_trades)}
    return {
        "initial_cash": round(initial_cash, 2),
        "symbol_count": len(symbols) if symbols is not None else None,
        "session_count": len(equities),
        "valuations_complete": complete,
        "missing_valuation_dates": missing_dates,
        "end_equity": round(end_equity, 2) if end_equity is not None else None,
        "net_pnl": round(net_pnl, 2) if net_pnl is not None else None,
        "return_pct": round(net_pnl / initial_cash * 100, 4) if net_pnl is not None else None,
        "annualized_return_pct": _annualized_pct(end_equity, initial_cash, len(equities)) if complete else None,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "turnover_pct": round(fill_metrics["traded_notional"] / mean_equity * 100, 4) if mean_equity else None,
        **fill_metrics,
        **_closed_metrics(closed_trades),
        "by_asset_type": by_asset_type,
        "rejection_count": len(rejections),
        "rejection_counts": dict(sorted(Counter(item["reason"] for item in rejections).items())),
        "risk_blocker_counts": dict(Counter(blocker for item in rejections for blocker in item.get("blockers", [])).most_common()),
        "metric_notes": {
            "annualized_return_pct": "按每年252个交易日折算，至少两个交易日且估值完整。",
            "max_drawdown": "按日末净值与期初现金计算，无法观察日内回撤。金额和比例各自取最大值。",
            "turnover_pct": "累计买入和卖出成交额之和除以平均日末净值。",
            "fee_total": "所有成交的佣金、印花税与过户费，已从现金扣除。",
            "slippage_total": "所有成交的滑点估算，已反映在成交价和盈亏，不再次扣减。",
            "closed_fee_to_gross_profit_pct": "已结束交易的费用除以其中正毛盈亏之和。毛盈亏已含滑点。",
            "by_asset_type": "股票与ETF分别统计成交和已结束交易，共用组合资金。",
        },
    }


def _csv_cell(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        # Provider names and user notes may begin with spreadsheet formulas.
        return "'" + value
    return value


def save_report(report, output_dir):
    """Stage all exports before replacing them; the full JSON is authoritative."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".backtest-", dir=out) as staging:
        stage = Path(staging)
        (stage / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for key, (filename, base_fields) in CSV_REPORTS.items():
            rows = report.get(key, [])
            fields = [*base_fields, *sorted({field for row in rows for field in row} - set(base_fields))]
            with (stage / filename).open("w", encoding="utf-8-sig", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=fields)
                writer.writeheader()
                writer.writerows({field: _csv_cell(value) for field, value in row.items()} for row in rows)
        for filename, _ in CSV_REPORTS.values():
            (stage / filename).replace(out / filename)
        result = out / "result.json"
        (stage / result.name).replace(result)
    return result
