"""Validate local inputs for the V1 replay. No network or account access."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re


MINUTE_ENDS = tuple(
    f"{minute // 60:02d}:{minute % 60:02d}"
    for start, stop in ((9 * 60 + 31, 11 * 60 + 30), (13 * 60 + 1, 15 * 60))
    for minute in range(start, stop + 1)
)
_MINUTE_ENDS = frozenset(MINUTE_ENDS)
_REVIEW_FLAGS = ("fundamentals_ok", "event_clear", "corporate_action_clear", "resistance_clear")


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是对象")
    return value


def _list(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label}必须是数组")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}必须填写")


def _date(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label}必须使用 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}日期无效") from exc


def _number(value, label, *, zero_allowed=False):
    try:
        valid = type(value) in (float, int) and isfinite(value)
    except OverflowError:
        valid = False
    if not valid or value < 0 or (value == 0 and not zero_allowed):
        raise ValueError(f"{label}数值无效")
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label}必须填写 true 或 false")


def _prices(row, label):
    opened, high, low, close = [_number(row.get(k), f"{label}.{k}") for k in ("open", "high", "low", "close")]
    if not low <= min(opened, close) <= max(opened, close) <= high:
        raise ValueError(f"{label}最高、最低与开收盘价不一致")
    return low, high


def _validate_daily(rows, days, label, *, require_amount):
    preceding = ""
    for index, value in enumerate(_list(rows, label)):
        path = f"{label}[{index}]"
        row = _object(value, path)
        day = row.get("date")
        _date(day, path)
        if day not in days or day <= preceding:
            raise ValueError(f"{path}日期须属于日历、按升序排列且不可重复")
        preceding = day
        _prices(row, path)
        _number(row.get("volume"), f"{path}.volume")
        if require_amount or "amount" in row:
            _number(row.get("amount"), f"{path}.amount")
        if "complete" in row:
            _boolean(row["complete"], f"{path}.complete")


def _validate_session(value, label):
    session = _object(value, label)
    status = session.get("status")
    if status not in ("open", "suspended"):
        raise ValueError(f"{label}.status 必须是 open 或 suspended")
    _boolean(session.get("corporate_action_clear"), f"{label}.corporate_action_clear")
    if status == "suspended":
        if any(k in session for k in ("open", "upper_limit", "lower_limit")) or session.get("minutes", []) != []:
            raise ValueError(f"{label}停牌时不能包含成交行情")
        return
    opened = _number(session.get("open"), f"{label}.open")
    upper = _number(session.get("upper_limit"), f"{label}.upper_limit")
    lower = _number(session.get("lower_limit"), f"{label}.lower_limit")
    if not lower < upper or not lower <= opened <= upper:
        raise ValueError(f"{label}开盘价与涨跌停价不一致")
    preceding = ""
    for index, value in enumerate(_list(session.get("minutes"), f"{label}.minutes")):
        path = f"{label}.minutes[{index}]"
        row = _object(value, path)
        label_time = row.get("time")
        if not isinstance(label_time, str) or label_time not in _MINUTE_ENDS or label_time <= preceding:
            raise ValueError(f"{path}分钟结束时间无效、重复或未按升序排列")
        preceding = label_time
        low, high = _prices(row, path)
        if low < lower or high > upper:
            raise ValueError(f"{path}分钟价格超出涨跌停范围")
        volume = row.get("volume")
        if type(volume) is not int or volume < 0:
            raise ValueError(f"{path}.volume 必须是非负整数，单位为股或份")


def _validate_review(value, label, *, etf):
    review = _object(value, label)
    _text(review.get("name"), f"{label}.name")
    _text(review.get("sector"), f"{label}.sector")
    timestamp = review.get("recorded_at")
    _text(timestamp, f"{label}.recorded_at")
    try:
        recorded = datetime.fromisoformat(timestamp)
        if recorded.utcoffset() is None:
            raise ValueError("timezone missing")
    except ValueError as exc:
        raise ValueError(f"{label}.recorded_at 必须是包含时区的时间") from exc
    sources = _list(review.get("sources"), f"{label}.sources")
    if not sources:
        raise ValueError(f"{label}.sources 缺少出处")
    for source in sources:
        _text(source, f"{label}.sources")
    for flag in _REVIEW_FLAGS:
        _boolean(review.get(flag), f"{label}.{flag}")
    if etf:
        _boolean(review.get("equity_etf"), f"{label}.equity_etf")


def validate_bundle(value):
    """Validate JSON shapes and ordering without filling missing sessions or reviews."""
    bundle = _object(value, "回测数据")
    if type(bundle.get("version")) is not int or bundle["version"] != 1:
        raise ValueError("回测数据的 version 必须为 1")
    if bundle.get("price_basis") != "raw":
        raise ValueError("price_basis 必须为 raw（未复权价格）")
    _text(bundle.get("source"), "source")
    _text(bundle.get("calendar_source"), "calendar_source")
    if "demo" in bundle:
        _boolean(bundle["demo"], "demo")
    calendar = _list(bundle.get("calendar"), "calendar")
    if not calendar:
        raise ValueError("calendar 缺少交易日")
    preceding = ""
    for value in calendar:
        parsed = _date(value, "calendar")
        if parsed.weekday() >= 5 or value <= preceding:
            raise ValueError("calendar 必须为不重复且升序排列的工作日")
        preceding = value
    days = frozenset(calendar)
    _validate_daily(bundle.get("benchmark"), days, "benchmark", require_amount=False)
    securities = _object(bundle.get("securities"), "securities")
    if not securities:
        raise ValueError("securities 缺少回放标的")
    for code, value in securities.items():
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("证券代码必须是六位数字字符串")
        label = f"securities.{code}"
        security = _object(value, label)
        kind = security.get("type")
        if kind not in ("stock", "etf"):
            raise ValueError(f"{label}.type 必须是 stock 或 etf")
        _validate_daily(security.get("daily"), days, f"{label}.daily", require_amount=True)
        for day, session in _object(security.get("sessions"), f"{label}.sessions").items():
            if day not in days:
                raise ValueError(f"{label}.sessions.{day} 不在交易日历")
            _validate_session(session, f"{label}.sessions.{day}")
        for day, review in _object(security.get("reviews"), f"{label}.reviews").items():
            if day not in days:
                raise ValueError(f"{label}.reviews.{day} 不在交易日历")
            _validate_review(review, f"{label}.reviews.{day}", etf=kind == "etf")
    if "universe" in bundle:
        _text(bundle.get("universe_source"), "universe_source")
        for day, codes in _object(bundle["universe"], "universe").items():
            if day not in days:
                raise ValueError(f"universe.{day} 不在交易日历")
            label = f"universe.{day}"
            listed = _list(codes, label)
            if any(not isinstance(code, str) or code not in securities for code in listed):
                raise ValueError(f"{label}包含数据包中不存在的证券")
            if len(listed) != len(set(listed)):
                raise ValueError(f"{label}包含重复证券")
    if "input_digests" in bundle:
        for name, digest in _object(bundle["input_digests"], "input_digests").items():
            _text(name, "input_digests 字段名")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"input_digests.{name} 必须是 SHA-256")
    return bundle


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"JSON 包含非有限数值：{value}")


def load_bundle(path):
    """Return a structured bundle and a digest of the exact file used."""
    raw = Path(path).read_bytes()
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return validate_bundle(value), sha256(raw).hexdigest()


def audit_coverage(bundle, start, end):
    """Measure input coverage across the declared pool, before any signal filters."""
    calendar = bundle['calendar']
    if start not in calendar or end not in calendar or start > end:
        raise ValueError('覆盖核对范围必须位于数据包交易日历内')
    start_index, end_index = calendar.index(start), calendar.index(end)
    securities = bundle['securities']
    universe = bundle.get('universe')

    def completed_days(rows):
        return [row['date'] for row in rows if row.get('complete', True)]

    benchmark_days = completed_days(bundle['benchmark'])
    histories = {code: completed_days(item['daily']) for code, item in securities.items()}

    def ready(dates, prior_day):
        if prior_day is None:
            return False
        count = bisect_right(dates, prior_day)
        return count >= 120 and dates[count - 1] == prior_day

    counts, examples = Counter(), []

    def issue(reason, day, code=None):
        counts[reason] += 1
        if len(examples) < 24:
            examples.append({'date': day, 'code': code, 'reason': reason})

    for index in range(start_index, end_index + 1):
        day = calendar[index]
        prior_day = calendar[index - 1] if index > 0 else None
        if not ready(benchmark_days, prior_day):
            issue('benchmark_unready_days', day)
        if universe is not None and prior_day not in universe:
            issue('universe_missing_days', day)
            continue
        active = securities if universe is None else universe[prior_day]
        for code in active:
            counts['symbol_sessions'] += 1
            if not ready(histories[code], prior_day):
                issue('daily_unready_sessions', day, code)
            session = securities[code]['sessions'].get(day)
            if not session:
                issue('missing_sessions', day, code)
            elif not session['corporate_action_clear']:
                issue('corporate_action_unverified_sessions', day, code)
            elif session['status'] == 'suspended':
                counts['suspended_sessions'] += 1
            elif len(session['minutes']) != len(MINUTE_ENDS):
                issue('partial_minute_sessions', day, code)
            else:
                counts['complete_minutes_sessions'] += 1
    keys = (
        'symbol_sessions', 'complete_minutes_sessions', 'suspended_sessions', 'missing_sessions',
        'partial_minute_sessions', 'corporate_action_unverified_sessions', 'daily_unready_sessions',
        'benchmark_unready_days', 'universe_missing_days',
    )
    return {
        'start': start, 'end': end, 'demo': bundle.get('demo') is True,
        'source': bundle['source'], 'calendar_source': bundle['calendar_source'],
        'trading_days': end_index - start_index + 1,
        'universe_mode': 'dated' if universe is not None else 'fixed',
        **{key: counts[key] for key in keys},
        'examples': examples,
    }


def _demo_session(previous_close, opening, steps):
    minutes = []
    price = opening
    for minute in MINUTE_ENDS:
        opening_price = price
        price = steps.get(minute, price)
        minutes.append({
            "time": minute, "open": opening_price, "high": max(opening_price, price),
            "low": min(opening_price, price), "close": price, "volume": 150_000,
        })
    base = Decimal(str(previous_close))
    lower, upper = [
        float((base * Decimal(factor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        for factor in ("0.90", "1.10")
    ]
    return {
        "status": "open", "open": opening, "upper_limit": upper, "lower_limit": lower,
        "corporate_action_clear": True, "minutes": minutes,
    }


def demo_bundle():
    """Build a synthetic 140-day warm-up and a three-session execution example."""
    signal_date = date(2024, 6, 14)
    prior_days = [signal_date - timedelta(days=offset) for offset in range(220, -1, -1)]
    warmup = [day.isoformat() for day in prior_days if day.weekday() < 5][-140:]
    daily, benchmark = [], []
    for index, day in enumerate(warmup):
        base = 1_000 + index
        pullback = 136 <= index <= 138
        daily.append({
            "date": day, "open": base / 100, "high": (base + 5) / 100,
            "low": (base - (13 if pullback else 5)) / 100, "close": base / 100,
            "volume": 12_000_000 if pullback else 20_000_000, "amount": 200_000_000,
        })
        benchmark_price = 3_500 + 3 * index
        benchmark.append({
            "date": day, "open": benchmark_price, "high": benchmark_price + 11,
            "low": benchmark_price - 11, "close": benchmark_price, "volume": 3_000_000,
        })
    daily[-1].update(close=11.51, high=11.56, volume=40_000_000)
    sessions = {
        "2024-06-17": _demo_session(11.51, 11.53, {"09:45": 11.57}),
        "2024-06-18": _demo_session(11.57, 11.57, {"09:45": 12.40}),
        "2024-06-19": _demo_session(12.40, 12.40, {"09:45": 12.80}),
    }
    for day, session in sessions.items():
        minutes = session["minutes"]
        daily.append({
            "date": day, "open": session["open"], "high": max(bar["high"] for bar in minutes),
            "low": min(bar["low"] for bar in minutes), "close": minutes[-1]["close"],
            "volume": sum(bar["volume"] for bar in minutes), "amount": 200_000_000,
        })
        benchmark_price = benchmark[-1]["close"] + 3
        benchmark.append({
            "date": day, "open": benchmark_price, "high": benchmark_price + 11,
            "low": benchmark_price - 11, "close": benchmark_price, "volume": 3_000_000,
        })
    review = {
        "name": "合成样本", "sector": "演示行业", "recorded_at": "2024-06-14T15:04:00+08:00",
        "sources": ["合成复核条件"], **{flag: True for flag in _REVIEW_FLAGS},
    }
    return {
        "version": 1, "demo": True, "price_basis": "raw",
        "source": "确定性合成样本", "calendar_source": "演示工作日序列",
        "calendar": [*warmup, *sessions], "benchmark": benchmark,
        "securities": {"600000": {
            "type": "stock", "daily": daily, "sessions": sessions,
            "reviews": {signal_date.isoformat(): review},
        }},
    }
