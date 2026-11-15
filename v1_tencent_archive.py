"""Capture a recent, explicitly dated Tencent minute session for local research."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time
import json
from math import isfinite
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

import requests

from v1_backtest_data import MINUTE_ENDS


SOURCE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
SHANGHAI = ZoneInfo("Asia/Shanghai")
ROWS_PER_REQUEST = 320
MAX_SYMBOLS = 12
MAX_RESPONSE_BYTES = 256 * 1024
REGULAR_LABELS = tuple(label.replace(":", "") for label in MINUTE_ENDS)


def fetch_minutes(symbol):
    """Use the recent m1 window. Tencent does not accept historical dates here."""
    with requests.get(
        SOURCE_URL, params={"param": f"{symbol},m1,,{ROWS_PER_REQUEST}"},
        timeout=(3, 5), stream=True, allow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise ValueError(f"腾讯请求返回 HTTP {response.status_code}")
        payload = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("腾讯响应超过大小限制")
            payload.extend(chunk)
    return json.loads(payload)


def validate_session(payload, symbol, day):
    """Check minute-ending labels and OHLCV while preserving original rows."""
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
        raise ValueError("腾讯分钟 K 线返回失败状态")
    data = payload.get("data")
    entry = data.get(symbol) if isinstance(data, dict) else None
    rows = entry.get("m1") if isinstance(entry, dict) else None
    if not isinstance(rows, list) or not 0 < len(rows) <= ROWS_PER_REQUEST:
        raise ValueError("腾讯响应缺少指定证券的 1 分钟 K 线")

    date_label = day.strftime("%Y%m%d")
    labels = []
    preceding = ""
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError("腾讯分钟 K 线缺少开高低收或成交量")
        stamp = row[0]
        if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{12}", stamp) or stamp <= preceding:
            raise ValueError("腾讯分钟时间无效、重复或未按升序排列")
        preceding = stamp
        if stamp[:8] != date_label:
            continue
        if any(type(value) not in (str, int, float) for value in row[1:6]):
            raise ValueError("腾讯分钟 K 线数值类型无效")
        opened, close, high, low, volume = map(float, row[1:6])
        if not (all(map(isfinite, (opened, close, high, low, volume)))
                and 0 < low <= min(opened, close) <= max(opened, close) <= high and volume >= 0):
            raise ValueError("腾讯分钟 K 线价格或成交量异常")
        labels.append(stamp[8:])

    if not labels:
        raise ValueError(f"腾讯近期窗口未包含指定日期 {day}，最近行情日期为 {preceding[:8]}")
    auction = labels[:1] == ["0930"]
    regular_labels = labels[1:] if auction else labels
    if tuple(regular_labels) != REGULAR_LABELS:
        raise ValueError("指定交易日缺少完整的 240 个常规分钟或包含时段外记录")
    return {"regular_rows": len(regular_labels), "auction_0930": auction}


def write_archive(path, archive):
    """Publish a private file atomically. A concurrent writer keeps its file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            json.dump(archive, target, ensure_ascii=False, allow_nan=False, indent=2)
            target.write("\n")
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def capture_session(day, symbols, path, *, fetch=fetch_minutes, now=None):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
        raise ValueError("日期须使用 YYYY-MM-DD")
    session_day = date.fromisoformat(day)
    started_at = now.astimezone(SHANGHAI) if now else datetime.now(SHANGHAI)
    if (session_day.weekday() > 4 or session_day > started_at.date()
            or (session_day == started_at.date() and started_at.time() < time(15, 5))):
        raise ValueError("请选择交易日，并在北京时间 15:05 后采集当日行情")
    if (not 0 < len(symbols) <= MAX_SYMBOLS
            or any(not re.fullmatch(r"(?:sh|sz|bj)[0-9]{6}", symbol) for symbol in symbols)
            or len(symbols) != len(set(symbols))):
        raise ValueError(f"请提供 1 至 {MAX_SYMBOLS} 个不重复的市场代码，例如 sh600000")
    path = Path(path)
    if path.suffix != ".json":
        raise ValueError("输出路径必须以 .json 结尾")
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"档案已存在：{path}")

    session_summary, payloads = {}, {}
    for symbol in symbols:
        try:
            payloads[symbol] = fetch(symbol)
            session_summary[symbol] = validate_session(payloads[symbol], symbol, session_day)
        except (ValueError, OverflowError, requests.RequestException) as error:
            raise ValueError(f"{symbol} 采集失败：{error}") from error

    archive = {
        "format": "tencent_m1_archive",
        "version": 1,
        "source_url": SOURCE_URL,
        "source_frequency": "m1",
        "source_window_rows": ROWS_PER_REQUEST,
        "timezone": SHANGHAI.key,
        "captured_at": (started_at if now else datetime.now(SHANGHAI)).isoformat(timespec="seconds"),
        "session_date": day,
        "price_basis": "provider_unverified",
        "volume_unit": "provider_unverified",
        "session_summary": session_summary,
        "raw_responses": payloads,
    }
    write_archive(path, archive)
    return archive


def main():
    parser = argparse.ArgumentParser(description="将腾讯最近完整的 1 分钟 K 线原样保存到本地研究档案")
    parser.add_argument("--date", required=True, help="预期交易日 YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="+", required=True, help="明确指定的市场代码，例如 sh600000 sz159919")
    parser.add_argument("--output", type=Path, help="输出 JSON，默认写入当前目录 output/v1_tencent_archive/")
    args = parser.parse_args()
    path = args.output or Path("output/v1_tencent_archive") / f"{args.date}.json"
    try:
        archive = capture_session(args.date, args.symbols, path)
    except (ValueError, OSError) as error:
        parser.exit(1, f"保存失败：{error}\n")
    print(f"已保存 {archive['session_date']} 的 {len(archive['session_summary'])} 个标的原始行情至 {path}")


if __name__ == "__main__":
    main()
