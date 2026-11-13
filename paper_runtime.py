"""Local paper valuation worker. Refreshes quotes; never submits any orders.

Runs with the local web server, independently of whether a browser is open.
The contest's verified exchange calendar, not just weekdays, gates all polling.
"""
from copy import deepcopy
from datetime import time
import fcntl
import json
from pathlib import Path
import threading

from paper_contest import clock_now, validate_state, observe, fetch_quotes, _write, report

INTERVAL = 30


def refresh_phase(state, now):
    day = str(now.date())
    if (now.weekday() >= 5 or day not in state['calendar'] or
            not state['start'] <= day <= state['end']):
        return 'idle', '非比赛交易日，休市'
    moment = now.time()
    if time(9, 30) <= moment < time(11, 30) or time(13) <= moment < time(15):
        return 'intraday', '交易时段每30秒获取腾讯行情'
    if time(15, 5) <= moment < time(16):
        stamp = state.get('last_valuation_asof', '')
        if state.get('last_mark_closing') and stamp[:10] == day:
            return 'idle', '当日收盘估值已完成'
        return 'closing', '核验当日完整收盘报价'
    if time(11, 30) <= moment < time(13):
        return 'idle', '午间休市，13:00恢复取价'
    if time(15) <= moment < time(15, 5):
        return 'idle', '等待15:05后核验收盘报价'
    return 'idle', '非取价时段，保留最近来源时间'


def read_status(store):
    try:
        data = json.loads((Path(store) / 'refresh_status.json').read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def refresh_store(store, *, now=None, force=False, fetcher=None):
    """Serialize valuation with CLI trades. A failed poll preserves last good NAV."""
    store = Path(store)
    if not (store / 'state.json').is_file():
        return {'status': 'missing', 'message': '尚无模拟账本'}
    with (store / 'paper.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'busy', 'message': '账本更新中，稍后读取'}
        started = now or clock_now()
        previous = read_status(store)
        status = {'status': 'updating', 'attempted_at': started.isoformat(),
                  'last_success_at': previous.get('last_success_at'),
                  'interval_seconds': INTERVAL, 'message': '正在核验腾讯报价'}
        try:
            state = json.loads((store / 'state.json').read_text())
            validate_state(state)
            phase, message = refresh_phase(state, started)
            status.update(phase=phase, message=message)
            if phase == 'idle':
                status['status'] = 'idle'
            else:
                # Rate-limit both browser refreshes and background polls. Force
                # refresh still obeys the calendar and source/closing checks.
                prior = previous.get('attempted_at')
                if not force and prior:
                    from datetime import datetime
                    age = (started - datetime.fromisoformat(prior)).total_seconds()
                    if 0 <= age < INTERVAL:
                        return previous
                _write(store / 'refresh_status.json', status)
                quotes, errors = (fetcher or fetch_quotes)(state)
                finished = now or clock_now()
                working = deepcopy(state)
                record = observe(working, quotes, finished, closing=phase == 'closing')
                validate_state(working)
                _write(store / 'state.json', working)
                (store / 'REPORT.md').write_text(report(working), encoding='utf-8')
                status.update(status='ok', last_success_at=finished.isoformat(),
                              source_asof=record['source_asof'], quote_errors=errors,
                              message='腾讯行情已更新；模拟买卖另按策略巡检')
        except Exception as exc:
            # No copied state is saved until the entire holding set passes.
            status.update(status='error', message=str(exc)[:500])
        status['finished_at'] = (now or clock_now()).isoformat()
        _write(store / 'refresh_status.json', status)
        return status


def start_quote_worker(store):
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                refresh_store(store)
            except Exception:
                # OS failures must not stop the WSGI server. No broker actions.
                import logging
                logging.exception('Paper quote refresh failed')
            stop.wait(INTERVAL)

    thread = threading.Thread(target=run, name='paper-quotes', daemon=True)
    thread.start()
    return stop
