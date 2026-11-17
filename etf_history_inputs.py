"""Cache verified, unadjusted Tencent daily history for local ETF replays."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import requests

from asset_history import _TX_URL, _tx_symbol
from strategy_runtime import read, write


CST = ZoneInfo('Asia/Shanghai')
BENCHMARK = '000300'
SOURCE = '腾讯公开历史日K newfqkline（原始未复权 day）'
CACHE_DIR = 'tencent_history_daily'
DAY = re.compile(r'\d{4}-\d{2}-\d{2}')
NUMERIC = ('open', 'close', 'high', 'low', 'volume')
MAX_ROWS = 641


def _clock(now):
    now = now or datetime.now(CST)
    return now.replace(tzinfo=CST) if now.tzinfo is None else now.astimezone(CST)


def _completed_days(calendar, now):
    days = []
    for label in calendar:
        if not isinstance(label, str) or not DAY.fullmatch(label):
            raise ValueError('日线采集需要已核验的 YYYY-MM-DD 交易日历')
        day = date.fromisoformat(label)
        if day.weekday() >= 5 or (days and day <= days[-1]):
            raise ValueError('日线采集需要升序且不重复的工作日交易日历')
        days.append(day)
    today = now.date()
    include_today = now.time() >= time(15, 5)
    return [day.isoformat() for day in days if day < today or day == today and include_today]


def _codes(codes):
    result = tuple(dict.fromkeys(str(code) for code in codes if str(code) != BENCHMARK))
    if len(result) > 12 or any(not re.fullmatch(r'(?:15|51|56|58)\d{4}', code) for code in result):
        raise ValueError('日线采集仅支持至多 12 只沪深 ETF 和沪深 300')
    return (*result, BENCHMARK)


def _provider_number(raw):
    if type(raw) not in (str, int, float):
        raise ValueError('日线数值字段类型异常')
    try:
        value = Decimal(str(raw))
    except InvalidOperation as error:
        raise ValueError('日线数值字段无效') from error
    if not value.is_finite() or not 0 <= value < Decimal('1e16'):
        raise ValueError('日线数值超出有效范围')
    return value


def _close_quote(section, symbol, row, now):
    quotes = section.get('qt')
    quote = quotes.get(symbol) if isinstance(quotes, dict) else None
    if now.time() < time(15, 5) or not isinstance(quote, list) or len(quote) <= 36 or quote[2] != symbol[2:]:
        return None
    raw_stamp = quote[30]
    if not isinstance(raw_stamp, str) or not re.fullmatch(r'\d{14}', raw_stamp):
        return None
    try:
        stamp = datetime.strptime(raw_stamp, '%Y%m%d%H%M%S').replace(tzinfo=CST)
        if stamp.date() != now.date() or stamp.time() < time(15) or stamp > now:
            return None
        # Embedded quote fields 5/3/33/34/36 are open/last/high/low/volume in lots.
        if any(_provider_number(row[index]) != _provider_number(quote[quote_index])
               for index, quote_index in ((1, 5), (2, 3), (3, 33), (4, 34), (5, 36))):
            return None
    except ValueError:
        return None
    return stamp.isoformat()


def parse_daily(payload, code, completed_days, now):
    """Return usable model rows plus skipped-row reasons from one raw response."""
    now = _clock(now)
    index = code == BENCHMARK
    symbol = _tx_symbol(code, 'index' if index else 'etf')
    if type(payload) is not dict or type(payload.get('code')) is not int or payload['code'] != 0:
        raise ValueError('腾讯日线返回失败状态')
    response = payload.get('data')
    section = response.get(symbol) if isinstance(response, dict) else None
    provider_rows = section.get('day') if isinstance(section, dict) else None
    if not isinstance(provider_rows, list) or not 0 < len(provider_rows) <= MAX_ROWS:
        raise ValueError('腾讯响应缺少该证券的原始 day 日线')

    days = set(completed_days)
    records, skipped = [], {}
    preceding, close_at = '', None
    for raw in provider_rows:
        if not isinstance(raw, list) or len(raw) < 6 or not isinstance(raw[0], str) or not DAY.fullmatch(raw[0]):
            raise ValueError('腾讯日线的日期或 OHLCV 字段无效')
        day = date.fromisoformat(raw[0])
        label = day.isoformat()
        if label <= preceding or day > now.date():
            raise ValueError('腾讯日线时间重复、乱序或来自未来')
        preceding = label
        if label not in days:
            continue
        if day == now.date():
            close_at = _close_quote(section, symbol, raw, now)
            if close_at is None:
                skipped[label] = '当日 OHLCV 尚未与同日期 15:00 后的源报价吻合'
                continue
        try:
            opened, close, high, low, lots = map(_provider_number, raw[1:6])
            volume = lots * 100
            if not (0 < low <= min(opened, close) <= max(opened, close) <= high
                    and volume > 0 and volume == volume.to_integral_value()):
                raise ValueError('原始价格或成交量异常')
            bar = dict(date=label, complete=True, open=float(opened), close=float(close),
                       high=float(high), low=float(low), volume=int(volume))
            if not index:
                if len(raw) <= 8 or raw[8] in (None, ''):
                    raise ValueError('缺少腾讯原始日成交额')
                ten_thousand_yuan = _provider_number(raw[8])
                amount = ten_thousand_yuan * 10_000
                # The provider rounds the amount field in its published units.
                precision = max(-6, min(0, ten_thousand_yuan.as_tuple().exponent))
                rounding = Decimal(10_000).scaleb(precision) / 2
                if not (amount > 0 and low * volume - rounding <= amount <= high * volume + rounding):
                    raise ValueError('日成交量/额换算未通过原始高低价交叉校验')
                bar['amount'] = float(amount)
        except (ValueError, OverflowError, InvalidOperation) as error:
            skipped[label] = str(error)
            continue
        records.append(bar)
    return records, skipped, close_at


def _fetch(code, last):
    symbol = _tx_symbol(code, 'index' if code == BENCHMARK else 'etf')
    through = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
    with requests.get(_TX_URL, params={'param': f'{symbol},day,,{through},640,'},
                      timeout=(3, 5), stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError(f'腾讯日线返回 HTTP {response.status_code}')
        chunks = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            chunks.extend(chunk)
            if len(chunks) > 2_000_000:
                raise ValueError('腾讯日线响应超过大小限制')
    raw = bytes(chunks)
    return json.loads(raw), sha256(raw).hexdigest()


def _cache_path(output, code):
    return Path(output) / CACHE_DIR / (code + '.json')


def _cached_bars(output, code, days):
    cached = read(_cache_path(output, code))
    if (not isinstance(cached, dict) or cached.get('version') != 1 or cached.get('code') != code
            or cached.get('source_url') != _TX_URL or cached.get('price_basis') != 'raw'
            or not isinstance(cached.get('bars'), list)):
        return []
    wanted = set(days)
    bars = {}
    seen = set()
    for bar in cached['bars']:
        if not isinstance(bar, dict):
            continue
        day = bar.get('date')
        if not isinstance(day, str) or day not in wanted or bar.get('complete') is not True:
            continue
        if day in seen:
            bars.pop(day, None)
            continue
        seen.add(day)
        required = NUMERIC if code == BENCHMARK else (*NUMERIC, 'amount')
        if not all(type(bar.get(key)) in (int, float) and 0 < bar[key] < 1e24 and
                   isfinite(bar[key]) for key in required):
            continue
        if not bar['low'] <= min(bar['open'], bar['close']) <= max(bar['open'], bar['close']) <= bar['high']:
            continue
        bars[day] = bar
    return sorted(bars.values(), key=lambda bar: bar['date'])


def load_daily(output, codes, calendar, *, now=None):
    """Read model-ready OHLCV/amount rows. Missing evidence stays absent."""
    days = _completed_days(calendar, _clock(now))
    return {code: _cached_bars(output, code, days) for code in _codes(codes)}


def refresh_daily(output, codes, calendar, *, now=None):
    """Refresh each symbol independently. Keep accepted dates if Tencent fails."""
    clock = _clock(now)
    days = _completed_days(calendar, clock)
    codes = _codes(codes)
    cached = {code: _cached_bars(output, code, days) for code in codes}
    if not days:
        return cached
    last = days[-1]
    wanted = [code for code, bars in cached.items() if not bars or bars[-1]['date'] != last]
    errors = {}

    def download(code):
        try:
            payload, digest = _fetch(code, last)
            downloaded = clock if now is not None else _clock(None)
            bars, skipped, close_at = parse_daily(payload, code, days, downloaded)
            return code, (bars, skipped, close_at, digest, downloaded), None
        except (OSError, ValueError, requests.RequestException) as error:
            return code, None, str(error)

    if wanted:
        with ThreadPoolExecutor(max_workers=min(3, len(wanted)), thread_name_prefix='etf-daily') as executor:
            for code, result, error in executor.map(download, wanted):
                if error is not None:
                    errors[code] = error
                    continue
                bars, skipped, close_at, digest, downloaded = result
                if bars:
                    combined = {bar['date']: bar for bar in cached[code]}
                    combined.update((bar['date'], bar) for bar in bars)
                    cached[code] = sorted(combined.values(), key=lambda bar: bar['date'])
                    write(_cache_path(output, code), {
                        'version': 1, 'code': code, 'source': SOURCE, 'source_url': _TX_URL,
                        'price_basis': 'raw', 'volume_unit': 'shares', 'amount_unit': 'CNY',
                        'latest_response_sha256': digest, 'fetched_at': downloaded.isoformat(),
                        'same_day_close_source_at': close_at, 'bars': cached[code],
                        'skipped_count': len(skipped), 'last_skipped_rows': list(skipped.items())[-12:],
                    })
                if not cached[code] or cached[code][-1]['date'] != last:
                    errors[code] = skipped.get(last, '源数据未返回最近完整交易日')
        write(Path(output) / CACHE_DIR / 'status.json', {
            'expected_date': last, 'attempted_at': clock.isoformat(), 'errors': errors,
            'counts': {code: len(bars) for code, bars in cached.items()},
        })
    return cached
