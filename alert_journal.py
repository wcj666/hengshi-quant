"""Local review journal for verified intraday alerts. It never submits orders."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time
import fcntl
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from quote_health import parse_provider_time
from strategy_runtime import write

CN = ZoneInfo('Asia/Shanghai')
ID = re.compile(r'[a-f0-9]{20}')
MAX_NOTIFICATION_ATTEMPTS = 3
RETRY_SECONDS = 30
RETRY_WINDOW_SECONDS = 120
REPLAY_REASONS = {
    'v1_entry': 'breakout', 'stop': 'stop_loss',
    'take_profit_1': 'take_profit_1', 'take_profit_2': 'take_profit_2',
}
LEGACY_TRIGGERS = {'触及止损参考': 'stop', '触及第一止盈': 'take_profit_1', '触及第二止盈': 'take_profit_2'}


def journal_dir(output):
    return Path(output) / 'alert_journal'


def _read_json(path, *, optional=False):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        if optional:
            return None
        raise ValueError('提醒账本读取失败，请核验本地记录') from error


def _day_rows(folder, day):
    path = folder / (day + '.json')
    data = _read_json(path)
    if data is None and not path.exists():
        return {}
    if not isinstance(data, dict) or any(
        not isinstance(row, dict) or not ID.fullmatch(key) or row.get('id') != key for key, row in data.items()
    ):
        raise ValueError('提醒账本格式无效，请核验本地记录')
    return data


@contextmanager
def _locked(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.lock').open('a') as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield


def _clock(now):
    return (now or datetime.now(CN)).astimezone(CN)


def _number(value):
    if type(value) in (int, float) and isfinite(value):
        return value
    return None


def _quantity(value):
    return value if type(value) is int and value > 0 else None


def _quote_time(quote, now):
    at = parse_provider_time(quote.get('source_timestamp'))
    if at and at.astimezone(CN).date() == now.date() and 0 <= (now - at).total_seconds() <= 90:
        return at.astimezone(CN).isoformat(timespec='seconds')
    return None


def _alert_side(signal):
    trigger = signal.get('trigger')
    category = signal.get('category')
    if signal.get('code') == 'PORT' or signal.get('blockers'):
        return None
    if category == 'add':
        return 'buy'
    if category == 'reduce' and trigger in (
        'stop', 'take_profit_1', 'take_profit_2', 'breakdown', 'time_5', 'time_10',
    ):
        return 'sell'
    if category == 't' and signal.get('tradable') is True:
        return {'sell_ready': 'sell', 'buyback_ready': 'buy'}.get(trigger)
    return None


def capture_rows(events, signals, quotes, plans, *, now, source, config_hash):
    """Build stable observation rows; overlap between basic and V1 alerts is merged."""
    now = _clock(now)
    day = now.date().isoformat()
    stamp = now.isoformat(timespec='seconds')
    result = []
    seen_triggers = set()
    for event in signals:
        code, trigger = event['code'], event['trigger']
        quote_at = _quote_time(quotes.get(code, {}), now)
        if code != 'PORT' and not quote_at:
            continue
        side = _alert_side(event)
        row = {
            'id': event['id'], 'code': code, 'name': event['name'], 'kind': event['title'],
            'side': side, 'quantity': _quantity(event.get('shares')), 'category': event.get('category'),
            'at': stamp, 'day': day, 'quote_at': quote_at, 'price': _number(event.get('price')),
            'level': _number(event.get('level')), 'trigger': trigger, 'reason': event.get('reason'),
            'blockers': event.get('blockers') or [], 'source': source, 'config_hash': config_hash,
            'signal_date': plans.get(code, {}).get('signal_date'),
            'native': bool(event.get('priority') == 'high' or side),
        }
        result.append(row)
        seen_triggers.add((code, row['kind'] if code == 'PORT' else trigger))
    for event in events:
        code, kind = event['code'], event['kind']
        trigger = LEGACY_TRIGGERS.get(kind, kind)
        price, level = _number(event['price']), _number(event['level'])
        quote_at = _quote_time(quotes.get(code, {}), now)
        if (code, kind if code == 'PORT' else trigger) in seen_triggers or (code != 'PORT' and not quote_at):
            continue
        identity = f"{day}|basic|{code}|{kind}|{event.get('tier', level)}"
        result.append({
            'id': sha256(identity.encode()).hexdigest()[:20], 'code': code, 'name': event['name'],
            'kind': kind, 'side': None, 'quantity': None, 'category': 'basic', 'at': stamp, 'day': day,
            'quote_at': quote_at, 'price': price, 'level': level, 'trigger': trigger,
            'reason': '已触发观察条件，请核对券商报价。', 'blockers': [], 'native': True,
            'source': source, 'config_hash': config_hash, 'signal_date': None,
        })
    return result


def _can_notify(now):
    return now.weekday() < 5 and (
        time(9, 30) <= now.time() < time(11, 30) or time(13) <= now.time() < time(15)
    )


def persist_cycle(output, rows, *, now, notify=False, send=None, clock=None):
    """Save alerts before sending. Retry only when a fresh prompt still exists."""
    day = now.astimezone(CN).date().isoformat()
    result = {'attempted': 0, 'submitted': 0}
    if not rows:
        return result
    folder = journal_dir(output)
    path = folder / (day + '.json')
    candidates = []
    with _locked(folder):
        journal = _day_rows(folder, day)
        changed = False
        for row in rows:
            ident = row['id']
            if ident not in journal:
                journal[ident] = {**row, 'notification': {
                    'attempts': 0, 'submitted': False, 'last_at': None, 'outcome': None,
                }}
                changed = True
            if row['native'] and notify and send is not None:
                candidates.append(ident)
        if changed:
            write(path, journal)
    # An in-flight entry is durably claimed before sending. Release the journal
    # lock around the system call so review/other processes can keep reading.
    current_by_id = {row['id']: row for row in rows}
    for ident in dict.fromkeys(candidates):
        attempt_at = _clock(clock() if clock else now)
        if attempt_at.date().isoformat() != day or not _can_notify(attempt_at):
            break
        current = current_by_id[ident]
        if current['code'] != 'PORT' and not _quote_time({'source_timestamp': current['quote_at']}, attempt_at):
            continue
        with _locked(folder):
            journal = _day_rows(folder, day)
            saved = journal[ident]
            state = saved['notification']
            first = parse_provider_time(saved['at'])
            previous = parse_provider_time(state['last_at'])
            if (state['submitted'] or state['attempts'] >= MAX_NOTIFICATION_ATTEMPTS
                    or state.get('outcome') in ('sending', 'unknown')
                    or not 0 <= (attempt_at - first).total_seconds() <= RETRY_WINDOW_SECONDS
                    or (previous and (attempt_at - previous).total_seconds() < RETRY_SECONDS)):
                continue
            state['last_at'] = attempt_at.isoformat()
            state['attempts'] += 1
            state['outcome'] = 'sending'
            attempt = state['attempts']
            write(path, journal)
        try:
            submitted = send(current)
        except (OSError, ValueError):
            submitted = False
        with _locked(folder):
            journal = _day_rows(folder, day)
            state = journal[ident]['notification']
            if submitted or (state['attempts'] == attempt and not state['submitted']):
                state['submitted'] = bool(submitted)
                state['outcome'] = 'submitted' if submitted else 'failed' if submitted is False else 'unknown'
                write(path, journal)
        result['attempted'] += 1
        result['submitted'] += int(bool(submitted))
        if result['attempted'] >= 2:
            break
    return result


def _matches_fill(row, fill):
    filled_at = parse_provider_time(fill.get('executed_at'))
    observed_at = parse_provider_time(row.get('at'))
    return bool(row['side'] and fill.get('alert_id') == row['id'] and fill.get('code') == row['code']
                and fill.get('side') == row['side'] and _quantity(fill.get('quantity'))
                and filled_at and observed_at and filled_at >= observed_at
                and filled_at.astimezone(CN).date().isoformat() == row['day'])


def _linked_fills(row, trades):
    fills = {}
    for fill in trades:
        if isinstance(fill, dict) and _matches_fill(row, fill):
            ident = fill.get('fill_id') or fill.get('id')
            if isinstance(ident, str) and ident:
                fills.setdefault(ident, fill)
    return list(fills.values())


def validate_fill_link(output, fill, trades=()):
    """Check one matching same-day historical alert and return its reference level."""
    ident = fill.get('alert_id')
    if not ident:
        return
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise ValueError('提醒编号无效')
    stamp = parse_provider_time(fill.get('executed_at'))
    if not stamp:
        raise ValueError('关联提醒须记录带时区的实际成交时间')
    day = stamp.astimezone(CN).date().isoformat()
    row = _day_rows(journal_dir(output), day).get(ident)
    if not row:
        raise ValueError('成交日没有这条提醒')
    if not _matches_fill(row, fill):
        raise ValueError('成交证券、买卖方向及时间须与这条提醒匹配')
    total = fill['quantity'] + sum(item['quantity'] for item in _linked_fills(row, trades))
    if row['quantity'] is not None and total > row['quantity']:
        raise ValueError('累计关联数量超过提醒数量，可另行登记余下成交')
    level = _number(row['level'])
    return level if level and level > 0 else _number(row['price'])


def _minutes_comparison(report, row):
    reason = REPLAY_REASONS.get(row['trigger'])
    if not reason:
        return {'status': 'unsupported'}
    meta = (report or {}).get('meta', {})
    if not isinstance(meta, dict) or meta.get('demo') is not False or not all(
        isinstance(meta.get(key), str) for key in ('start', 'end')
    ) or not meta['start'] <= row['day'] <= meta['end']:
        return {'status': 'missing'}
    matched = [
        fill for fill in report.get('fills', [])
        if isinstance(fill, dict) and fill.get('date') == row['day'] and fill.get('code') == row['code']
        and fill.get('reason') == reason
        and (not row.get('signal_date') or fill.get('signal_date') == row['signal_date'])
    ]
    if len(matched) > 1:
        return {'status': 'ambiguous', 'matches': len(matched)}
    if not matched:
        decisions = sorted({
            item['reason'] for item in report.get('decisions', [])
            if isinstance(item, dict) and item.get('date') == row['day'] and item.get('code') == row['code']
            and isinstance(item.get('reason'), str)
        })
        complete = (report.get('summary') or {}).get('execution_coverage_complete') is True
        return {'status': 'no_match' if complete else 'incomplete', 'decisions': decisions}
    fill = matched[0]
    simulated_time = parse_provider_time(fill.get('signal_at'))
    live_time = parse_provider_time(row['at'])
    return {
        'status': 'match', 'signal_at': fill.get('signal_at'), 'fill_at': fill.get('at'),
        'price': fill.get('price'), 'reference_price': fill.get('reference_price'),
        'alert_minus_simulated_seconds': int((live_time - simulated_time).total_seconds())
        if live_time and simulated_time else None,
    }


def load_journal(output, trades=(), *, now=None):
    clock = _clock(now)
    folder = journal_dir(output)
    report = _read_json(Path(output) / 'v1_backtest' / 'result.json', optional=True)
    report = report if isinstance(report, dict) else None
    days = sorted((path.stem for path in folder.glob('????-??-??.json')
                   if re.fullmatch(r'\d{4}-\d{2}-\d{2}', path.stem)), reverse=True)[:20]
    result = []
    for day in days:
        rows = sorted(_day_rows(folder, day).values(), key=lambda row: row['at'], reverse=True)
        for row in rows:
            fills = _linked_fills(row, trades)
            quantity = sum(fill['quantity'] for fill in fills)
            expired = day < clock.date().isoformat() or (day == clock.date().isoformat() and clock.time() >= time(15))
            if quantity and row['quantity'] is not None:
                status = 'filled' if quantity >= row['quantity'] else 'partial'
            elif quantity:
                status = 'linked'
            elif row.get('category') == 'diagnostic':
                status = 'blocked'
            else:
                status = 'expired' if expired else 'pending'
            result.append({
                **row, 'filled_quantity': quantity, 'status': status, 'expired': expired,
                'fills': [{k: fill.get(k) for k in ('fill_id', 'quantity', 'price', 'executed_at')} for fill in fills],
                'replay': _minutes_comparison(report, row),
            })
            if len(result) >= 300:
                return {'rows': result}
    return {'rows': result}
