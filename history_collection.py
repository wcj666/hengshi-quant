"""Collect and audit Tencent history for an explicitly registered ETF research basket."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, time
from math import isfinite
import fcntl
import hashlib
import json
from pathlib import Path
import re

from etf_history_inputs import load_daily, refresh_daily
from quote_health import parse_provider_time
from reminder_validation import MIN_DAYS
from strategy_runtime import CST, calendar_dates, read, write
from v1_backtest import ROOT, run_backtest
from v1_backtest_data import MINUTE_ENDS, _validate_review, _validate_session, audit_coverage, validate_bundle

SUBDIR = 'etf_history_collection'
TRIAL_URL = '/api/backtest/history-trial'
BENCHMARK = '000300'


def absent_status():
    return {'status': '等待本机历史数据采集', 'positions': [], 'warnings': [],
            'trial': {'status': '等待真实数据'}, 'validation': {'status': '待验证', 'message': '等待可回放样本'}}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _read_dict(path):
    return _mapping(read(path))


def completed_dates(calendar, now):
    today = now.date().isoformat()
    return [day for day in calendar if day < today or day == today and now.time() >= time(15, 5)]


def remember_basket(work, entries, baseline, now):
    """Record research symbols prospectively without reading or copying account sizing."""
    if not 1 <= len(entries) <= 6 or len({code for code, _ in entries}) != len(entries):
        raise ValueError('观察篮子须含 1 至 6 个不重复的 ETF')
    for code, name in entries:
        if not re.fullmatch(r'(?:15|51|56|58)\d{4}', code) or not name.strip() or len(name) > 30:
            raise ValueError('ETF 代码或名称无效')
    if baseline and baseline not in {code for code, _ in entries}:
        raise ValueError('持有基准须来自已登记 ETF')
    path = work/'watch.json'
    items = [{'code': code, 'name': name} for code, name in entries]
    old = _read_dict(path)
    if old.get('positions') == items and old.get('baseline_code') == baseline:
        return old
    settings = {'positions': items, 'baseline_code': baseline, 'registered_at': now.isoformat(),
                'initial_equity': 100000}
    write(path, settings)
    return settings


def _archive(work, codes, calendar):
    result = {code: {} for code in codes}
    known_days = set(calendar)
    for folder in sorted((work/'minutes').glob('????-??-??')):
        day = folder.name
        if day not in known_days or not folder.is_dir():
            continue
        for code in codes:
            data = _read_dict(folder/f'{code}.json')
            if data.get('code') == code and data.get('day') == day:
                result[code][day] = data
    return result


def _confirmed_actions(evidence, code, day, now=None):
    records = _mapping(_mapping(evidence.get('corporate_actions')).get(code))
    row = _mapping(records.get(day))
    stamp = parse_provider_time(row.get('recorded_at'))
    opened = datetime.combine(date.fromisoformat(day), time(9, 30), CST)
    if row.get('clear') is not True or not isinstance(row.get('source'), str) or not row['source'].strip():
        return False
    if stamp and stamp <= opened:
        return True
    # The date of a mechanical market adjustment can be established later from
    # published records. Keep the real check time separate from a trading review.
    now = now or datetime.now(CST)
    checked = parse_provider_time(row.get('checked_at'))
    close = opened.replace(hour=15, minute=0)
    reference, prior = row.get('same_date_reference'), row.get('previous_raw_close')
    provenance = row.get('provenance')
    return bool(row.get('basis') == 'reference_price_and_published_action_crosscheck'
                and checked and close <= checked <= now and row.get('reference_price_unchanged') is True
                and type(reference) in (int, float) and type(prior) in (int, float)
                and isfinite(reference) and 0 < reference == prior
                and isinstance(row.get('reference_sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', row['reference_sha256'])
                and isinstance(provenance, list) and len(provenance) >= 2
                and all(isinstance(item, dict) and isinstance(item.get('url'), str)
                        and isinstance(item.get('sha256'), str) and re.fullmatch(r'[a-f0-9]{64}', item['sha256'])
                        for item in provenance))


def _true_reviews(evidence, code, days):
    valid = {}
    rows = _mapping(_mapping(evidence.get('reviews')).get(code))
    for day, row in rows.items():
        if day not in days:
            continue
        try:
            _validate_review(row, f'{code} {day}', etf=True)
        except ValueError:
            continue
        valid[day] = row
    return valid


def _eligible_sessions(archive, evidence, code):
    rows = {}
    from tencent_history_capture import session_from_evidence

    quotes = _mapping(_mapping(evidence.get('historical_quotes')).get(code))
    for day, item in archive.items():
        session = item.get('session')
        if not isinstance(session, dict) and day in quotes:
            session = session_from_evidence(item, quotes[day], datetime.now(CST))
        if not isinstance(session, dict):
            continue
        normalized = {**session, 'corporate_action_clear': _confirmed_actions(evidence, code, day)}
        try:
            _validate_session(normalized, f'{code} {day}')
        except ValueError:
            continue
        rows[day] = normalized
    return rows


def build_bundle(daily, archives, calendar, settings, evidence):
    codes = [item['code'] for item in settings['positions']]
    securities = {}
    for code in codes:
        securities[code] = {
            'type': 'etf', 'daily': daily.get(code, []),
            'sessions': _eligible_sessions(archives[code], evidence, code),
            'reviews': _true_reviews(evidence, code, set(calendar)),
        }
    bundle = {'version': 1, 'demo': False, 'source': '腾讯原始日线及已核验逐日分钟档案',
              'calendar_source': '本机已核验交易日历（新浪 / AKShare）', 'price_basis': 'raw',
              'calendar': calendar, 'benchmark': daily.get(BENCHMARK, []), 'securities': securities}
    if isinstance(evidence.get('t_replay'), dict):
        bundle['t_replay'] = evidence['t_replay']
    return validate_bundle(bundle)


def describe_positions(settings, daily, archives, bundle):
    result = []
    for item in settings['positions']:
        code = item['code']
        days = archives[code]
        sessions = (bundle or {}).get('securities', {}).get(code, {}).get('sessions', {})
        details = Counter()
        for day, archive in days.items():
            session = sessions.get(day)
            for message in archive.get('blockers', []):
                if not isinstance(message, str):
                    continue
                if session and (message.startswith('腾讯原始涨跌停价') or message.startswith('缺少可核验的当日涨跌停')):
                    continue
                if session and session.get('corporate_action_clear') and message.startswith('公司行动或除权状态'):
                    continue
                details[message] += 1
        supplemented = sum(session.get('corporate_action_clear') is True for session in sessions.values())
        if supplemented:
            details[f'已补 {supplemented} 日涨跌停及当日除权参考价证据（含回顾性核验）'] += 1
        if not any(s.get('corporate_action_clear') for s in sessions.values()):
            details['待补有日期与出处的公司行动/除权核验证据'] += 1
        if not (bundle or {}).get('securities', {}).get(code, {}).get('reviews'):
            details['待补当时已知的 V1 人工复核记录'] += 1
        result.append({**item, 'daily_count': len(daily.get(code, [])),
                       'amount_daily_count': sum(row.get('amount', 0) > 0 for row in daily.get(code, [])),
                       'complete_minute_days': sum(row.get('minutes_count') == len(MINUTE_ENDS) for row in days.values()),
                       'turnover_days': sum(row.get('turnover_count') == len(MINUTE_ENDS) for row in days.values()),
                       'matched_amount_days': sum(row.get('amount_count') == len(MINUTE_ENDS) for row in days.values()),
                       'replay_days': sum(s.get('corporate_action_clear') is True for s in sessions.values()),
                       'latest_day': max(days, default=None),
                       'warnings': [f'{message}（{count}）' if count > 1 else message for message, count in details.most_common(6)]})
    return result


def run_trial(bundle, settings, completed, work, *, start=None, end=None):
    from t_history_replay import evaluate_t_window

    if not completed or not bundle['benchmark'] or any(not item['daily'] for item in bundle['securities'].values()):
        return {'status': '真实日线不足，等待数据'}
    start = start or completed[-min(4, len(completed))]
    end = end or completed[-1]
    result = run_backtest(bundle, start, end, settings['initial_equity'])
    coverage = audit_coverage(bundle, start, end)
    t_result = evaluate_t_window(bundle, start, end, settings['initial_equity'], result['meta']['costs'])
    scenario = bundle.get('t_replay') or {}
    if scenario.get('start_on', '') > end:
        t_result['warnings'] = [f'该历史区间早于已登记的实验起点 {scenario["start_on"]}，后续从起点开始积累。']
    summary = result['summary']
    ready = (summary['execution_coverage_complete'] and summary['valuations_complete']
             and not any(coverage.get(key) for key in ('missing_sessions', 'partial_minute_sessions',
                 'corporate_action_unverified_sessions', 'benchmark_unready_days', 'daily_unready_sessions')))
    compact = {key: summary[key] for key in ('net_pnl', 'return_pct', 'max_drawdown_pct', 'fill_count')} if ready else {}
    write(work/'trial.json', {
        'label': '关注篮子的真实行情试跑。回顾性选样，不用于正式筛选。',
        'start': start, 'end': end, 'source': bundle['source'], 'coverage': coverage,
        'execution_verified': ready, 'summary': compact, 'decisions': result['decisions'],
        'technical_signal_count': summary['technical_signal_count'],
        'reminders': result['reminders'],
        'mechanical_evidence_note': '机械行情事实允许事后查证并保留真实核验时间；交易审核及实验底仓仍保留其实际设定时间。',
        't': t_result,
    })
    return {'status': '已用真实行情试跑' if ready else '已试跑，执行证据不足',
            'day_or_range': f'{start} 至 {end}', 'report_url': TRIAL_URL,
            'summary': ('执行证据不全，净收益、回撤和成交比例暂不采用。' if not ready else
                        f'模拟成交 {summary["fill_count"]} 笔。仍需独立分段验证。') +
                       ((' 做 T：' + t_result['warnings'][0]) if not t_result['covered'] else '')}


def contiguous_windows(bundle, settings):
    from v1_validation import PERIOD_KEYS

    registered = parse_provider_time(settings.get('registered_at'))
    calendar = bundle['calendar']
    chain, best = [], []
    histories = {code: {row['date'] for row in item['daily']}
                 for code, item in bundle['securities'].items()}
    benchmark = {bar['date'] for bar in bundle['benchmark']}

    def complete(day, prior):
        if prior not in benchmark:
            return False
        for code, item in bundle['securities'].items():
            session = item['sessions'].get(day, {})
            if prior not in histories[code] or session.get('corporate_action_clear') is not True:
                return False
            if session.get('status') == 'open' and len(session['minutes']) != len(MINUTE_ENDS):
                return False
        return True

    for prior, day in zip(calendar, calendar[1:]):
        if not registered or registered > datetime.combine(date.fromisoformat(prior), time(15, 5), CST):
            continue
        if not complete(day, prior):
            chain = []
            continue
        chain.append(day)
        if len(chain) >= len(best):
            best = chain.copy()
    if len(best) < MIN_DAYS * len(PERIOD_KEYS):
        return {}, len(best)
    step = len(best)//len(PERIOD_KEYS)
    return {key: (best[index*step], best[(index+1)*step-1] if index+1 < len(PERIOD_KEYS) else best[-1])
            for index, key in enumerate(PERIOD_KEYS)}, len(best)


def auto_validate(bundle, settings, work, runtime_output):
    from reminder_policy import _parameters, _version, publish_evidence
    from v1_validation import evaluate, save_comparison

    windows, sessions = contiguous_windows(bundle, settings)
    if not windows:
        return {'status': '待验证', 'message': f'已连续核验 {sessions} 个可回放交易日，每段至少 {MIN_DAYS} 日；'
                                              '之后还要核验真实触发和完成交易样本。'}
    clock = parse_provider_time(settings['registered_at'])
    selected = [day for day in bundle['calendar']
                if clock <= datetime.combine(date.fromisoformat(day), time(15, 5), CST)]
    bundle = {**bundle, 'universe': {day: list(bundle['securities']) for day in selected},
              'universe_source': f'事前登记的本地 ETF 研究篮子，登记 {settings["registered_at"]}'}
    cfg_path = runtime_output.parent / 'config.json'
    cfg = _read_dict(cfg_path) if cfg_path.is_file() else None
    active_parameters = _parameters(runtime_output, cfg) if cfg is not None else None
    digest = hashlib.sha256(json.dumps([bundle, windows, settings, _version(), active_parameters],
                                       sort_keys=True, allow_nan=False).encode()).hexdigest()
    state = _read_dict(work/'validation.json')
    if digest == state.get('digest'):
        return state['status']
    result = evaluate(bundle, windows, settings['initial_equity'], baseline_code=settings.get('baseline_code'))
    decisions = result['reminder_validation']['recommendations']
    qualified = any(item['status'] in ('验证通过', '移出强提醒候选') for item in decisions)
    status = {'status': '已完成自动分段' if qualified else '已分段，部分或全部仍待验证',
              'message': '自动按时间分段，样本不足仍保留待验证。固定止损纳入风控。'}
    save_comparison(result, runtime_output/'v1_validation')
    if cfg is not None:
        try:
            publish_evidence(runtime_output, cfg, bundle, result)
        except ValueError as exc:
            status['message'] += ' 提醒等级保持待验证：' + str(exc)
    write(work/'validation.json', {'digest': digest, 'status': status})
    return status


def collect(runtime_output, settings, work, now, *, lookback=4, fetch=True):
    calendar = calendar_dates(runtime_output, now)
    completed = completed_dates(calendar, now)
    if not completed:
        return {**absent_status(), 'status': '交易日历尚未核验，采集暂缓'}
    codes = [item['code'] for item in settings['positions']]
    daily = load_daily(runtime_output, codes, calendar, now=now)
    warnings = []
    if fetch and any(not daily.get(code) or daily[code][-1]['date'] != completed[-1] for code in [*codes, BENCHMARK]):
        daily = refresh_daily(runtime_output, codes, calendar, now=now)
    turnover_cache = {}
    for day in completed[-lookback:]:
        eligible = codes if day == now.date().isoformat() else [
            code for code in codes if not (work/'minutes'/day/f'{code}.json').is_file()
        ]
        if fetch and eligible:
            try:
                from tencent_history_capture import capture_day
                captured = capture_day(day, eligible, output=work/'minutes', now=now, turnover_cache=turnover_cache)
                for code, item in captured['results'].items():
                    if item.get('archived') is False:
                        detail = '；'.join(item.get('blockers', [])[:2])
                        warnings.append(f'{day} {code} 未取得可保存的原始响应。{detail}')
            except (OSError, ValueError) as exc:
                warnings.append(f'{day} 分钟采集待核验：{exc}')
    for code in [*codes, BENCHMARK]:
        if not daily.get(code) or daily[code][-1]['date'] != completed[-1]:
            warnings.append(f'{code} 尚无 {completed[-1]} 已核验日线，已有日线保留。')
    archives = _archive(work, codes, completed)
    evidence = _read_dict(work/'evidence.json')
    scenario = _mapping(evidence.get('t_replay'))
    if not scenario:
        warnings.append('做 T 实验缺事前留存的底仓与固定退出证据。本次只锁定观察证券，不从当前持仓反推历史。')
    elif scenario.get('start_on'):
        detail = '；'.join(f'{row["code"]} {row["quantity"]:,}份，固定退出 {row["stop_loss"]:.3f}'
                          for row in scenario.get('positions', []))
        warnings.append(f'做 T 研究场景已登记，起点 {scenario["start_on"]}。{detail}。'
                        f'虚拟期初净资产 ¥{settings["initial_equity"]:,.2f}。')
    bundle = build_bundle(daily, archives, completed, settings, evidence)
    trial = run_trial(bundle, settings, completed, work)
    validation = auto_validate(bundle, settings, work, runtime_output)
    return {'status': '已更新腾讯历史行情归档与证据检查', 'updated_at': now.isoformat(),
            'latest_completed_day': completed[-1], 'positions': describe_positions(settings, daily, archives, bundle),
            'warnings': warnings, 'evidence_url': '/api/backtest/history-evidence',
            'trial': trial, 'validation': validation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'output')
    parser.add_argument('--watch', action='append', nargs=2, metavar=('CODE', 'NAME'))
    parser.add_argument('--baseline-code')
    parser.add_argument('--lookback', type=int, default=2, choices=range(1, 6))
    parser.add_argument('--cached-only', action='store_true')
    args = parser.parse_args()
    work = args.output/SUBDIR
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (work/'.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        now = datetime.now(CST)
        try:
            settings = (remember_basket(work, args.watch, args.baseline_code, now) if args.watch
                        else _read_dict(work/'watch.json'))
            result = (collect(args.output, settings, work, now, lookback=args.lookback,
                              fetch=not args.cached_only) if settings.get('positions') else absent_status())
        except (ValueError, OSError) as exc:
            prior = _read_dict(work/'status.json') or absent_status()
            result = {**prior, 'status': '本轮采集待核验，保留已保存档案',
                      'warnings': [str(exc)], 'last_attempt': now.isoformat()}
        write(work/'status.json', result)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
