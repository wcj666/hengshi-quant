"""Private, date-specific Tencent minute archives for offline replay research."""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

import requests

from portfolio_quotes import _tencent_symbol
from v1_backtest_data import MINUTE_ENDS

CN = ZoneInfo('Asia/Shanghai')
KLINE_URL = 'https://ifzq.gtimg.cn/appstock/app/kline/mkline'
TURNOVER_URL = 'https://web.ifzq.gtimg.cn/appstock/app/day/query'
HEADERS = {'Referer': 'https://gu.qq.com/', 'User-Agent': 'Mozilla/5.0'}
FORMAT = 'tencent_replay_market_day'
REGULAR = tuple(label.replace(':', '') for label in MINUTE_ENDS)
REGULAR_SET = frozenset(REGULAR)
TURNOVER_PREVIOUS = dict(zip(REGULAR, ('0930', *REGULAR[:-1])))
MAX_BYTES = 512 * 1024


def _number(value):
    if type(value) not in (str, int, float):
        raise ValueError('腾讯数值格式无效')
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('腾讯数值格式无效') from error
    if not result.is_finite() or not 0 <= result < 10**18:
        raise ValueError('腾讯数值超范围')
    return result


def _integer(value):
    value = value if isinstance(value, Decimal) else _number(value)
    if value != value.to_integral_value():
        raise ValueError('腾讯成交量无法按完整单位核对')
    return int(value)


def _reject_constant(value):
    raise ValueError(f'腾讯响应包含非有限数值：{value}')


def _fetch(url, params):
    with requests.get(url, params=params, headers=HEADERS, timeout=(3, 5),
                      stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError(f'腾讯接口返回 HTTP {response.status_code}')
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise ValueError('腾讯响应超过大小限制')
    return json.loads(raw, parse_constant=_reject_constant)


def fetch_klines(symbol, day):
    # The provider's end minute is exclusive; 15:01 includes the 15:00 bar.
    return _fetch(KLINE_URL, {'param': f"{symbol},m1,{day.replace('-', '')}1501,320"})


def fetch_turnover(symbol):
    return _fetch(TURNOVER_URL, {'code': symbol})


def _section(payload, symbol):
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
        raise ValueError('腾讯接口未返回成功状态')
    data = payload.get('data')
    section = data.get(symbol) if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise ValueError('腾讯响应缺少指定证券')
    return section


def _klines(payload, symbol, day):
    rows = _section(payload, symbol).get('m1')
    if not isinstance(rows, list) or not 0 < len(rows) <= 320:
        raise ValueError('腾讯响应没有可核验的分钟 K 线')
    prefix = day.replace('-', '')
    results = {}
    preceding = ''
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError('腾讯分钟 K 线字段不完整')
        stamp = row[0]
        if not isinstance(stamp, str) or not re.fullmatch(r'\d{12}', stamp) or stamp <= preceding:
            raise ValueError('腾讯分钟 K 线标签无效或重复')
        preceding = stamp
        if stamp[:8] != prefix or stamp[8:] not in REGULAR_SET | {'0930'}:
            continue
        opened, close, high, low = map(_number, row[1:5])
        if not 0 < low <= min(opened, close) <= max(opened, close) <= high:
            raise ValueError('腾讯 K 线价格关系异常')
        results[stamp[8:]] = {
            'time': stamp[8:10] + ':' + stamp[10:12],
            'open': opened, 'high': high, 'low': low, 'close': close,
            'shares': _integer(_number(row[5]) * 100),
        }
    return results


def _turnover(payload, symbol, day):
    rows = _section(payload, symbol).get('data')
    if not isinstance(rows, list) or not 0 < len(rows) <= 6:
        raise ValueError('腾讯响应缺少近期交易日累计成交额')
    exact = [record for record in rows if isinstance(record, dict) and record.get('date') == day.replace('-', '')]
    if len(exact) != 1:
        raise ValueError('腾讯近期成交额窗口没有唯一匹配的交易日')
    raw = exact[0].get('data')
    if not isinstance(raw, list) or not 0 < len(raw) <= 300:
        raise ValueError('腾讯累计成交量额分时行缺失')
    result = {}
    previous_lots = 0
    previous_amount = Decimal(0)
    previous_stamp = ''
    for item in raw:
        fields = item.split() if isinstance(item, str) else []
        if len(fields) != 4 or not re.fullmatch(r'\d{4}', fields[0]) or fields[0] <= previous_stamp:
            raise ValueError('腾讯累计成交量额时间标签无效或重复')
        stamp = fields[0]
        if stamp not in REGULAR_SET | {'0930', '1300'} and not '1501' <= stamp <= '1530':
            raise ValueError('腾讯累计量额包含无法识别的时段')
        if not previous_stamp and stamp != '0930':
            raise ValueError('累计成交量额缺少集合竞价基准，无法核验首分钟差分')
        price, lots, amount = _number(fields[1]), _integer(fields[2]), _number(fields[3])
        delta_lots, delta_amount = lots - previous_lots, amount - previous_amount
        if not price or delta_lots < 0 or delta_amount < 0:
            raise ValueError('累计成交量额回退或价格无效')
        if stamp == '1300' and (delta_lots or delta_amount):
            raise ValueError('午间参考行出现成交，无法分配到常规分钟')
        if stamp in REGULAR_SET:
            adjacent = previous_stamp == TURNOVER_PREVIOUS[stamp] or (stamp == '1301' and previous_stamp == '1300'
                        and '1130' in result)
            result[stamp] = {'close': price, 'shares': 100 * delta_lots, 'amount': delta_amount,
                             'single_minute': adjacent, 'cum_volume_lots': lots, 'cum_amount': amount}
        previous_stamp = stamp
        previous_lots, previous_amount = lots, amount
    return result


def _quote(payload, symbol, code, day, now):
    section = _section(payload, symbol)
    quotes = section.get('qt')
    row = quotes.get(symbol) if isinstance(quotes, dict) else None
    if not isinstance(row, list) or len(row) <= 48 or str(row[2]) != code:
        raise ValueError('缺少精确日期的腾讯行情与实际涨跌停价')
    stamp = str(row[30])
    if not re.fullmatch(r'\d{14}', stamp):
        raise ValueError('腾讯收盘报价时间字段无效')
    at = datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=CN)
    if at.date().isoformat() != day or at.time() < time(15) or at > now:
        raise ValueError('腾讯原始涨跌停价只有其他日期或未收盘报价，不能回填')
    opened, close, prior, upper, lower = map(_number, (row[5], row[3], row[4], row[47], row[48]))
    if not 0 < lower <= min(opened, close) <= max(opened, close) <= upper or lower >= upper or prior <= 0:
        raise ValueError('腾讯实际涨跌停价与收盘报价不一致')
    return {'open': float(opened), 'close': float(close), 'previous_close': float(prior),
            'upper_limit': float(upper), 'lower_limit': float(lower), 'source_timestamp': at.isoformat()}


def _atomic(path, record):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as target:
            temporary = Path(target.name)
            json.dump(record, target, ensure_ascii=False, allow_nan=False, indent=2)
            target.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _fingerprint(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return sha256(canonical.encode()).hexdigest()


def normalize_market_day(code, day, now, *, kline=None, turnover=None, errors=()):
    """Join only mutually supported bars and keep the unmodified observations."""
    symbol = _tencent_symbol(code)
    blockers = list(errors)
    k_rows, t_rows = {}, {}
    raw = {key: value for key, value in (('m1', kline), ('day', turnover)) if value is not None}
    for key, operation in (('m1', _klines), ('day', _turnover)):
        if key not in raw:
            continue
        try:
            parsed = operation(raw[key], symbol, day)
        except (ValueError, TypeError, OverflowError) as error:
            blockers.append(str(error))
        else:
            if key == 'm1':
                k_rows = parsed
            else:
                t_rows = parsed
    k_labels = set(k_rows) & REGULAR_SET
    common = sorted(k_labels & t_rows.keys())
    matching_volumes = sum(t_rows[stamp]['single_minute']
                           and abs(k_rows[stamp]['shares'] - t_rows[stamp]['shares']) < 100
                           and k_rows[stamp]['close'] == t_rows[stamp]['close'] for stamp in common)
    # Cross-check the inferred unit on multiple ordinary bars, not the field
    # name or the order of magnitude alone. No use of price × volume as a fill.
    unit_confirmed = len(common) >= 30 and matching_volumes * 5 >= len(common) * 4
    if not unit_confirmed:
        blockers.append('分钟 K 线成交量的手/份单位缺少足够一致的交叉核验')
    if len(k_labels) != len(REGULAR):
        blockers.append(f'历史原始分钟 K 线仅 {len(k_labels)}/{len(REGULAR)} 根')
    if len(t_rows) != len(REGULAR):
        blockers.append(f'历史分时累计成交额仅覆盖 {len(t_rows)}/{len(REGULAR)} 个常规分钟')
    minute_rows = []
    amount_count = 0
    discrepancies = []
    for stamp in sorted(k_labels):
        bar = k_rows[stamp]
        amount_row = t_rows.get(stamp)
        shares = bar['shares']
        normalized = {key: float(bar[key]) for key in ('open', 'high', 'low', 'close')}
        normalized.update(time=bar['time'], volume=shares)
        if amount_row and unit_confirmed:
            turnover_shares, amount = amount_row['shares'], amount_row['amount']
            if not amount_row['single_minute']:
                reason = '累计值差分跨越缺口，不能分配到单分钟'
            elif amount_row['close'] != bar['close']:
                reason = '同标签收盘价不一致'
            # K-line volume can contain fractional lots; the cumulative feed
            # rounds to full lots. A unit-precision check never changes either
            # source value. Amount must additionally fit the precise K-line
            # share count and observed high/low.
            elif abs(turnover_shares - shares) >= 100:
                reason = '累计成交量差分与分钟 K 线成交量超出单位精度'
            elif (shares == 0) != (amount == 0):
                reason = '成交量与成交额的零值不一致'
            elif not bar['low'] * shares - Decimal('.011') <= amount <= bar['high'] * shares + Decimal('.011'):
                reason = '累计成交额差分不在同根原始 K 线的量价范围'
            else:
                reason = None
            if reason:
                discrepancies.append({'time': bar['time'], 'reason': reason, 'amount': float(amount),
                                      'turnover_volume': turnover_shares, 'kline_volume': shares})
            else:
                normalized['amount'] = float(amount)
                amount_count += 1
        minute_rows.append(normalized)
    if discrepancies:
        blockers.append(f'腾讯两源有 {len(discrepancies)} 个分钟不完全一致，未合并这些成交额')
    quote = None
    quote_errors = []
    for key in ('day', 'm1'):
        if key not in raw:
            continue
        try:
            quote = _quote(raw[key], symbol, code, day, now)
            break
        except (TypeError, ValueError, OverflowError) as error:
            quote_errors.append(str(error))
    if quote is None:
        blockers.extend(dict.fromkeys(quote_errors or ['缺少可核验的当日涨跌停档案']))
    session = None
    if quote and len(k_labels) == len(REGULAR) and unit_confirmed:
        if (minute_rows[-1]['close'] == quote['close']
                and all(quote['lower_limit'] <= row['low'] <= row['high'] <= quote['upper_limit']
                        for row in minute_rows)):
            session = {'status': 'open', 'corporate_action_clear': False,
                       'open': quote['open'], 'upper_limit': quote['upper_limit'],
                       'lower_limit': quote['lower_limit'], 'minutes': minute_rows}
        else:
            blockers.append('分钟收盘价或高低价与当日腾讯行情不一致')
    blockers.append('公司行动或除权状态需要独立、对应日期的证据')
    return {'format': FORMAT, 'version': 1, 'code': code, 'symbol': symbol, 'day': day,
            'captured_at': now.isoformat(timespec='seconds'), 'quote': quote, 'session': session,
            'minutes_count': len(k_labels), 'turnover_count': len(t_rows), 'amount_count': amount_count,
            'volume_unit_verified': unit_confirmed, 'matching_volume_count': matching_volumes,
            'discrepancy_count': len(discrepancies), 'discrepancies': discrepancies,
            'blockers': blockers, 'sources': {'m1': KLINE_URL, 'day': TURNOVER_URL},
            'raw_sha256': {kind: _fingerprint(payload) for kind, payload in raw.items()},
            'raw_responses': raw}


def _load(path, day, code):
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (not isinstance(stored, dict) or stored.get('format') != FORMAT or stored.get('day') != day
            or stored.get('code') != code or stored.get('version') != 1):
        return None
    return stored


def _quality(record):
    return (record.get('session') is not None, record['minutes_count'], record['turnover_count'], record['amount_count'])


SUMMARY_FIELDS = (
    'code', 'day', 'captured_at', 'minutes_count', 'turnover_count', 'amount_count',
    'discrepancy_count', 'volume_unit_verified', 'blockers',
)


def summarize_archive(entry, path):
    return {**{key: entry[key] for key in SUMMARY_FIELDS}, 'has_session': entry['session'] is not None,
            'archive_path': str(path), 'archived': path.is_file()}


def capture_day(day, codes, *, output, now=None, turnover_cache=None,
                kline_fetch=fetch_klines, turnover_fetch=fetch_turnover):
    """Save one independent raw archive per symbol and return its coverage summary."""
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('日期须为 YYYY-MM-DD')
    session_day = date.fromisoformat(day)
    fixed_clock = now is not None
    now = now.astimezone(CN) if fixed_clock else datetime.now(CN)
    if (session_day > now.date() or session_day.weekday() > 4
            or (session_day == now.date() and now.time() < time(15, 5))):
        raise ValueError('只归档工作日，且当日收盘档案须在北京时间 15:05 后采集')
    codes = tuple(codes)
    if not 1 <= len(codes) <= 12 or len(set(codes)) != len(codes):
        raise ValueError('须提供 1 至 12 个不重复的证券代码')
    if any(not isinstance(code, str) or not re.fullmatch(r'\d{6}', code) for code in codes):
        raise ValueError('证券代码必须为 6 位字符串')
    output = Path(output)
    cache = {} if turnover_cache is None else turnover_cache
    result = {}
    for code in codes:
        symbol = _tencent_symbol(code)
        path = output / day / f'{code}.json'
        saved = _load(path, day, code)
        if saved and (saved.get('session') is not None and saved.get('amount_count') == len(REGULAR)
                      or session_day < now.date() and saved.get('minutes_count') == len(REGULAR)
                      and saved.get('turnover_count') == len(REGULAR)):
            result[code] = summarize_archive(saved, path)
            continue
        kline = turnover = None
        errors = []
        try:
            kline = kline_fetch(symbol, day)
        except (ValueError, OSError, requests.RequestException) as error:
            errors.append(f'腾讯分钟 K 线采集失败：{error}')
        try:
            if symbol not in cache:
                cache[symbol] = turnover_fetch(symbol)
            turnover = cache[symbol]
        except (ValueError, OSError, requests.RequestException) as error:
            errors.append(f'腾讯分时成交额采集失败：{error}')
        reference_clock = now if fixed_clock else datetime.now(CN)
        entry = normalize_market_day(code, day, reference_clock, kline=kline, turnover=turnover, errors=errors)
        if entry['raw_responses'] and (not saved or _quality(entry) >= _quality(saved)):
            _atomic(path, entry)
        elif saved:
            entry = saved
        result[code] = summarize_archive(entry, path)
    return {'day': day, 'results': result}
