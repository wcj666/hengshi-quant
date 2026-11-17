"""Local review journal for verified intraday alerts. It never submits orders."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time
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
MAX_SUCCESSFUL_NOTIFICATIONS = 3
REMINDER_SECONDS = 60
REMINDER_WINDOW_SECONDS = 600
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


def held_observation(signal):
    return signal.get('held') is True and (
        signal.get('category') == 'reduce'
        and signal.get('trigger') in ('structure_support', 'structure_weakness')
        or signal.get('category') == 't' and signal.get('trigger') == 'sell_watch'
    )


def notification_policy(signal):
    """Important held-position observations notify once without becoming orders."""
    side = _alert_side(signal)
    strong = signal.get('priority') == 'high' or bool(side)
    return {'native': bool(strong or held_observation(signal)), 'repeatable': bool(strong)}


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
            'side': side, 'quantity': _quantity(event.get('shares')) if side else None, 'category': event.get('category'),
            'at': stamp, 'day': day, 'quote_at': quote_at, 'price': _number(event.get('price')),
            'level': _number(event.get('level')), 'trigger': trigger, 'reason': event.get('reason'),
            'blockers': event.get('blockers') or [], 'source': source, 'config_hash': config_hash,
            'signal_date': plans.get(code, {}).get('signal_date'),
            'entry_cap': _number(plans.get(code, {}).get('entry_price')) if trigger == 'v1_entry' else None,
            **notification_policy(event),
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
            'repeatable': False,
            'source': source, 'config_hash': config_hash, 'signal_date': None,
        })
    return result


def _can_notify(now):
    return now.weekday() < 5 and (
        time(9, 30) <= now.time() < time(11, 30) or time(13) <= now.time() < time(15)
    )


def _currently_active(row, now):
    last = parse_provider_time(row.get('last_seen_at') or row.get('at'))
    if (row.get('active') is False or not _can_notify(now) or not last
            or last.astimezone(CN).date() != now.date()
            or not 0 <= (now - last).total_seconds() <= 90):
        return False
    return row.get('code') == 'PORT' or bool(_quote_time(
        {'source_timestamp': row.get('latest_quote_at') or row.get('quote_at')}, now))


def set_feedback(output, ident, action, *, day=None, now=None):
    """Acknowledge or decline a reminder. Neither action records a fill."""
    now = _clock(now)
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise ValueError('提醒编号无效')
    if action not in ('seen', 'deferred'):
        raise ValueError('请选择已看到或暂不执行；成交须另行登记真实成交记录')
    day = day or now.date().isoformat()
    if (not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)
            or day > now.date().isoformat()):
        raise ValueError('提醒日期无效')
    try:
        datetime.strptime(day, '%Y-%m-%d')
    except ValueError:
        raise ValueError('提醒日期无效') from None
    folder = journal_dir(output)
    with _locked(folder):
        journal = _day_rows(folder, day)
        row = journal.get(ident)
        if row is None:
            raise ValueError('未找到该日提醒')
        feedback = row.get('feedback') or {}
        if feedback.get('action') == action:
            return feedback
        row['feedback'] = {'action': action, 'at': now.isoformat(timespec='seconds')}
        row.setdefault('feedback_history', []).append(row['feedback'].copy())
        write(folder / (day + '.json'), journal)
        return row['feedback']


def persist_cycle(output, rows, *, now, notify=False, send=None, clock=None, trades=(), inactive_reason=None):
    """Persist current eligibility; repeat fresh unacknowledged actions at most 3 times.

    Each transmission is claimed under the file lock. Failed transmissions get
    at most three attempts per reminder; ambiguous submissions are not retried.
    First-alert retry windows and archived evidence stay tied to the original
    observation, while repeats always use the current provider price/time.
    """
    now = _clock(now)
    day = now.date().isoformat()
    result = {'attempted': 0, 'submitted': 0}
    folder = journal_dir(output)
    path = folder / (day + '.json')
    candidates = []
    current_by_id = {row['id']: row for row in rows}
    with _locked(folder):
        journal = _day_rows(folder, day)
        changed = False
        for ident, saved in journal.items():
            if ident not in current_by_id and saved.get('active') is not False:
                reason = inactive_reason(saved) if inactive_reason else None
                saved.update(active=False, inactive_at=now.isoformat(timespec='seconds'),
                             inactive_reason=reason or '后续轮询未再产生这条提醒')
                changed = True
        for row in rows:
            ident = row['id']
            if ident not in journal:
                journal[ident] = {**row, 'notification': {
                    'attempts': 0, 'round_attempts': 0, 'submitted': False,
                    'successful_count': 0, 'last_success_at': None, 'last_at': None, 'outcome': None,
                }}
            saved = journal[ident]
            saved.update(active=_can_notify(now), last_seen_at=now.isoformat(timespec='seconds'),
                         latest_quote_at=row.get('quote_at'), latest_price=row.get('price'),
                         latest_native=row['native'], latest_repeatable=row.get('repeatable', bool(row.get('side'))))
            if saved['active']:
                saved['inactive_at'] = None
                saved.pop('inactive_reason', None)
            changed = True
            if row['native'] and notify and send is not None:
                candidates.append(ident)
        if changed:
            write(path, journal)
    # An in-flight entry is durably claimed before sending. Release the journal
    # lock around the system call so review/other processes can keep reading.
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
            # Migrate older journals without changing their original evidence.
            successes = state.setdefault('successful_count', int(bool(state['submitted'])))
            rounds = state.setdefault('round_attempts', 0 if successes else state['attempts'])
            last_success = parse_provider_time(state.setdefault('last_success_at', state['last_at'] if successes else None))
            first = parse_provider_time(saved['at'])
            previous = parse_provider_time(state['last_at'])
            window = REMINDER_WINDOW_SECONDS if successes else RETRY_WINDOW_SECONDS
            if ((saved.get('feedback') or {}).get('action') in ('seen', 'deferred')
                    or not _currently_active(saved, attempt_at) or _linked_fills(saved, trades)
                    or successes >= MAX_SUCCESSFUL_NOTIFICATIONS
                    or (successes and (not current.get('repeatable', bool(current.get('side')))
                                       or not last_success or (attempt_at - last_success).total_seconds() < REMINDER_SECONDS))
                    or rounds >= MAX_NOTIFICATION_ATTEMPTS
                    or state['attempts'] >= MAX_NOTIFICATION_ATTEMPTS * MAX_SUCCESSFUL_NOTIFICATIONS
                    or state.get('outcome') in ('sending', 'unknown')
                    or not first or not 0 <= (attempt_at - first).total_seconds() <= window
                    or (previous and (attempt_at - previous).total_seconds() < RETRY_SECONDS)):
                continue
            state['last_at'] = attempt_at.isoformat()
            state['attempts'] += 1
            state['round_attempts'] += 1
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
            if state['attempts'] == attempt and state.get('outcome') == 'sending':
                state['submitted'] = bool(submitted) or state['submitted']
                state['outcome'] = 'submitted' if submitted else 'failed' if submitted is False else 'unknown'
                if submitted:
                    state['successful_count'] += 1
                    state['last_success_at'] = attempt_at.isoformat()
                    state['round_attempts'] = 0
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
            active = not expired and _currently_active(row, clock)
            feedback = row.get('feedback') or {}
            if quantity and row['quantity'] is not None:
                status = 'filled' if quantity >= row['quantity'] else 'partial'
            elif quantity:
                status = 'linked'
            elif row.get('category') == 'diagnostic':
                status = 'blocked'
            elif expired:
                status = 'expired'
            elif feedback.get('action') in ('seen', 'deferred'):
                status = feedback['action']
            else:
                status = 'pending' if active else 'inactive'
            result.append({
                **row, 'filled_quantity': quantity, 'status': status, 'expired': expired,
                'native': row.get('latest_native', row.get('native', False)),
                'repeatable': row.get('latest_repeatable', row.get('repeatable', bool(row.get('side')))),
                'active': active, 'feedback': feedback,
                'fills': [{k: fill.get(k) for k in ('fill_id', 'quantity', 'price', 'executed_at')} for fill in fills],
                'replay': _minutes_comparison(report, row),
            })
            if len(result) >= 300:
                return {'rows': result}
    return {'rows': result}


REVIEW_LABELS = {
    'filled': '已成交', 'partial': '部分成交', 'missed': '错过',
    'skipped': '主动跳过', 'expired': '已失效', 'pending': '待确认',
    'observe': '仅观察／受阻', 'needs_reconcile': '有关联线索，待对账',
}


def close_review_day(value, now):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('复盘日期须为 YYYY-MM-DD')
    try:
        day = date.fromisoformat(value)
    except ValueError:
        raise ValueError('复盘日期无效') from None
    if day > now.date() or (day == now.date() and now.time() < time(15)):
        raise ValueError('请在该交易日收盘后复盘')
    return day.isoformat()


def review_days(output):
    days = []
    for path in journal_dir(output).glob('????-??-??.json'):
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        days.append(day.isoformat())
    return sorted(days, reverse=True)


def _review_fills(row, trades, now):
    relevant = []
    for fill in trades:
        if not isinstance(fill, dict):
            continue
        executed = parse_provider_time(fill.get('executed_at'))
        if executed and executed <= now:
            relevant.append(fill)
    linked = _linked_fills(row, relevant)
    unlinked = [fill for fill in relevant if not fill.get('alert_id')
                and _matches_fill(row, {**fill, 'alert_id': row['id']})]
    return linked, unlinked


def _review_status(row, filled, unlinked):
    quantity = row.get('quantity')
    manual = row.get('manual_review') or {}
    feedback = row.get('feedback') or {}
    user_action = manual.get('outcome')
    user_reason = manual.get('reason')
    if filled:
        if quantity and filled < quantity:
            remaining = f'还有 {quantity - filled} 股/份未关联成交。'
            if user_action in ('missed', 'skipped') and user_reason:
                remaining += f'余量{REVIEW_LABELS[user_action]}：{user_reason}'
            elif feedback.get('action') == 'deferred':
                remaining += '已记录暂不执行反馈。'
            return 'partial', remaining
        reason = '有同日、同向的实际成交记录与提醒明确关联。'
        if not quantity:
            reason += '原提醒未记录计划数量。'
        return 'filled', reason
    if row.get('category') == 'diagnostic' or not row.get('side'):
        blockers = '；'.join(filter(None, row.get('blockers') or []))
        return 'observe', blockers or '结构、账户风险或观察提醒，无可关联的买卖指令。'
    if unlinked:
        return 'needs_reconcile', '提醒之后有同证券、同方向成交尚未关联，需确认是否由这条提醒触发。'
    if user_action in ('missed', 'skipped') and user_reason:
        return user_action, f'本人复盘确认：{user_reason}'
    if feedback.get('action') == 'deferred':
        return 'skipped', '盘中已选择「暂不执行」，具体原因待补。'
    if row.get('inactive_at'):
        return 'expired', row.get('inactive_reason') or '盘中已撤销此提醒，旧记录未留存具体原因。'
    if feedback.get('action') == 'seen':
        return 'pending', '已看到提醒，收盘仍无关联成交。请核对是否错过、跳过或实际成交未登记。'
    last_seen = parse_provider_time(row.get('last_seen_at'))
    if last_seen and last_seen.astimezone(CN).time() >= time(14, 59):
        return 'expired', '最后一轮提醒仍有效，随收盘到期。未见关联成交，实际处理情况未确认。'
    return 'pending', '已到收盘，未见关联成交，也缺少临近收盘的提醒状态。实际处理情况待确认。'


def _notification_label(row):
    result = row.get('notification') or {}
    if result.get('outcome') == 'failed':
        return '桌面通知提交失败'
    if result.get('outcome') in ('sending', 'unknown'):
        return '桌面通知送达待确认'
    if result.get('submitted'):
        return '桌面通知已提交'
    return '桌面通知未提交' if row.get('latest_native', row.get('native')) else '无桌面通知'


def load_close_review(output, day, trades=(), *, now=None):
    now = _clock(now)
    day = close_review_day(day, now)
    summary = dict.fromkeys(REVIEW_LABELS, 0)
    result = []
    for row in sorted(_day_rows(journal_dir(output), day).values(), key=lambda item: item['at']):
        linked, unlinked = _review_fills(row, trades, now)
        filled = sum(fill['quantity'] for fill in linked)
        status, reason = _review_status(row, filled, unlinked)
        summary[status] += 1
        can_link = bool(row.get('side')) and status not in ('filled', 'observe')
        result.append({**{key: row.get(key) for key in ('id', 'day', 'code', 'name', 'kind',
                            'at', 'price', 'level', 'quantity', 'side', 'inactive_at', 'manual_review')},
                       'filled_quantity': filled, 'review_status': status, 'review_label': REVIEW_LABELS[status],
                       'review_reason': reason, 'trigger_reason': row.get('reason'),
                       'notification_label': _notification_label(row),
                       'can_link': can_link, 'can_review': can_link and status != 'needs_reconcile',
                       'fills': [{key: fill.get(key) for key in ('quantity', 'price', 'fee', 'executed_at')}
                                 for fill in sorted(linked, key=lambda item: parse_provider_time(item['executed_at']))]})
    return {'summary': {**summary, 'total': len(result)}, 'rows': result}


def record_close_review(output, ident, day, outcome, reason, trades=(), *, now=None):
    now = _clock(now)
    day = close_review_day(day, now)
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise ValueError('提醒编号无效')
    if outcome not in ('missed', 'skipped', 'clear'):
        raise ValueError('请选择错过、主动跳过或清除复盘标记')
    if outcome != 'clear' and (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 200):
        raise ValueError('请填写 1 至 200 字的具体原因')
    folder = journal_dir(output)
    with _locked(folder):
        journal = _day_rows(folder, day)
        row = journal.get(ident)
        if not row or not row.get('side') or row.get('category') == 'diagnostic':
            raise ValueError('找不到可以复盘标记的交易提醒')
        if outcome == 'clear':
            if row.pop('manual_review', None) is not None:
                write(folder / (day + '.json'), journal)
            return None
        linked, unlinked = _review_fills(row, trades, now)
        filled = sum(fill['quantity'] for fill in linked)
        current, _ = _review_status(row, filled, unlinked)
        if current in ('filled', 'needs_reconcile'):
            raise ValueError('已成交或有同向成交待对账，先核对实际记录')
        row['manual_review'] = {'outcome': outcome, 'reason': reason.strip(), 'at': now.isoformat(timespec='seconds')}
        write(folder / (day + '.json'), journal)
        return row['manual_review']
