"""Bind live reminder levels to authentic, version-matched replay evidence."""
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import secrets

from quote_health import parse_provider_time
from replay_parameters import load_account_profile
from strategy_runtime import CST, write
from strategy_defaults import BACKTEST_ENGINE_FILES

FILES = tuple(dict.fromkeys((*BACKTEST_ENGINE_FILES, 'trade_signals.py', 't_engine.py',
                            't_runtime.py', 'action_center.py', 'alert_journal.py', 'replay_parameters.py',
                            'v1_validation.py', 'reminder_validation.py', 't_history_replay.py', 'reminder_policy.py')))
KINDS = {'v1_entry': 'breakout', 'take_profit_1': 'take_profit_1',
         'take_profit_2': 'take_profit_2', 'sell_ready': 'sell_ready', 'sell_watch': 'sell_ready'}


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _version():
    root = Path(__file__).resolve().parent
    return sha256(b''.join((root / name).read_bytes() for name in FILES)).hexdigest()


def _parameters(output, cfg):
    return _digest(load_account_profile(output, cfg))


def _engine_version():
    root = Path(__file__).resolve().parent
    return sha256(b''.join((root / file).read_bytes() for file in BACKTEST_ENGINE_FILES)).hexdigest()


def compare_live_parameters(output, meta):
    """Annotate a saved report without modifying its historical results."""
    from v1_backtest import simulation_costs

    if not isinstance(meta, dict) or meta.get('demo') is True:
        return None
    try:
        profile = load_account_profile(output)
    except (OSError, ValueError):
        profile = None
    if profile is None:
        return '当前实盘参数待核对。下方保留历史研究结果，实时提醒以当前核验状态为准。'
    if meta.get('engine_sha256') != _engine_version() or meta.get('rule_source') != 'account':
        return '此报告使用旧版引擎或独立研究参数。请按当前规则重新回放，历史结果保留供查看。'
    if (meta.get('rules') != profile['rules']
            or ('t_settings' in meta and meta['t_settings'] != profile['t_settings'])
            or meta.get('costs') != simulation_costs(profile['costs'])):
        return '实盘参数或券商费率已变化。此报告保留历史结果，需重跑才能影响当前提醒。'
    kinds = meta.get('asset_kinds')
    if not isinstance(kinds, list) or any(not isinstance(kind, str) for kind in kinds):
        return '此报告缺少按品种记录的费用口径，请按当前规则重新回放。'
    if set(kinds) - profile['costs']['broker_terms'].keys():
        return '有品种采用模拟费用。需补齐对应券商费率并重跑，才能用于当前强提醒验证。'
    return None


def publish_evidence(output, cfg, bundle, result, *, now=None):
    """Called only after a real replay; legacy or hand-edited verdicts never promote."""
    from v1_backtest import simulation_costs
    from v1_backtest_data import validate_bundle
    from reminder_validation import evaluate_reminders
    now = now or datetime.now(CST)
    validate_bundle(bundle)
    if bundle.get('demo') is not False or result.get('meta', {}).get('demo') is not False:
        return False
    if not bundle.get('input_digests'):
        raise ValueError('历史输入缺少逐文件证据指纹，保持待验证')
    if result['meta'].get('bundle_sha256') != _digest(bundle):
        raise ValueError('验证数据指纹不匹配，不能影响实时提醒')
    profile = load_account_profile(output, cfg)
    meta = result['meta']
    if (profile is None or meta.get('rule_source') != 'account'
            or meta.get('rules') != profile['rules'] or meta.get('t_settings') != profile['t_settings']):
        raise ValueError('验证规则参数与当前账户不同，保持待验证')
    kinds = {s['type'] for s in bundle['securities'].values()}
    if kinds - profile['costs']['broker_terms'].keys() or meta.get('costs') != simulation_costs(profile['costs']):
        raise ValueError('费用口径未匹配，提醒保持待验证')
    if meta.get('engine_sha256') != _engine_version():
        raise ValueError('回放引擎版本已经变化，保持待验证')
    for original in result['reports'].values():
        if any(original['meta'].get(field) != meta[field] for field in ('rules', 'costs', 'engine_sha256', 'bundle_sha256')):
            raise ValueError('分段回放的参数或证据不一致，提醒保持待验证')
    baseline = (result.get('reminder_validation') or {}).get('meta', {}).get('baseline_code')
    checked = evaluate_reminders(bundle, result, baseline_code=baseline)
    ends = [p['end'] for p in checked['periods']]
    if not ends or max(ends) >= now.date().isoformat():
        raise ValueError('验证仅能使用已完成的历史区间')
    payload = {'version': _version(), 'parameters': _digest(profile),
               'codes': sorted(bundle['securities']), 'published_at': now.isoformat(),
               'bundle_sha256': _digest(bundle), 'report': checked,
               'dated_pool': bool(bundle.get('universe') and bundle.get('universe_source'))}
    folder = Path(output) / 'reminder_policy'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = folder / '.key'
    try:
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(secrets.token_bytes(32))
    signed = hmac.new(key.read_bytes(), _digest(payload).encode(), 'sha256').hexdigest()
    write(folder / 'evidence.json', {'payload': payload, 'signature': signed})
    return True


def load_policy(output, cfg, *, now=None):
    from reminder_validation import decision
    now = now or datetime.now(CST)
    absent = {'status': 'pending', 'reason': '缺少同版本、同参数的真实分段验证；仅显示观察', 'rules': {}, 'codes': []}
    try:
        folder = Path(output) / 'reminder_policy'
        envelope = json.loads((folder / 'evidence.json').read_text())
        p = envelope['payload']
        expected = hmac.new((folder / '.key').read_bytes(), _digest(p).encode(), 'sha256').hexdigest()
        stamp = parse_provider_time(p['published_at'])
        if (not hmac.compare_digest(expected, envelope['signature']) or p['version'] != _version()
                or p['parameters'] != _parameters(output, cfg) or not stamp
                or not timedelta(0) <= now - stamp <= timedelta(days=90)
                or p['report']['meta']['demo'] is not False):
            return absent
        report = p['report']
        end = max(period['end'] for period in report['periods'])
        end_at = datetime.fromisoformat(end).replace(tzinfo=CST)
        if not timedelta(0) <= now - end_at <= timedelta(days=90):
            return absent
        rules = {}
        for kind in KINDS.values():
            status, reason = decision(report['periods'], kind,
                't' if kind in ('sell_ready', 'buyback_ready') else 'trade', False,
                report['meta'].get('baseline_code'), p['dated_pool'])
            rules[kind] = {'status': {'验证通过': 'verified', '移出强提醒候选': 'excluded'}.get(status, 'pending'),
                           'reason': reason}
        return {'status': 'matched', 'reason': '已核对真实分段证据与当前规则版本',
                'rules': rules, 'codes': p['codes'], 'evidence_id': _digest(p)}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return absent


def summary(output, cfg, *, now=None):
    return load_policy(output, cfg, now=now)


def apply_policy(output, cfg, signals, *, now=None):
    policy = load_policy(output, cfg, now=now)
    result = []
    for original in signals:
        s = deepcopy(original)
        kind = KINDS.get(s.get('trigger'))
        # Exits and handling already-recorded T sales remain risk controls.
        if not kind or s.get('ledger_notice'):
            s.update(validation_status='risk', validation_reason='保留已有仓位风险及实际成交收尾提醒')
            result.append(s)
            continue
        rule = policy['rules'].get(kind, {}) if s.get('code') in policy['codes'] else {}
        status = rule.get('status', 'pending')
        reason = rule.get('reason', policy['reason'])
        original_title = s.get('validation_base_title') or s.get('title', s.get('name', '条件'))
        s['validation_base_title'] = original_title
        s['blockers'] = [b for b in s.get('blockers') or [] if b != s.get('validation_reason')]
        base_id = s.get('validation_base_id') or s['id']
        if not s.get('followup'):
            s['id'] = sha256(f'{base_id}|{status}|{policy.get("evidence_id", "pending")}'.encode()).hexdigest()[:20]
        s.update(validation_base_id=base_id, validation_status=status, validation_reason=reason)
        if status != 'verified':
            s.update(shares=None, tradable=False, priority='normal', side=None)
            s['blockers'] = list(dict.fromkeys((s.get('blockers') or []) + [reason]))
            s['title'] = ('已停用强提醒：' if status == 'excluded' else '待验证观察：') + original_title
        result.append(s)
    return result
