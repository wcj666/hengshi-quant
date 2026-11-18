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
from strategy_runtime import CST, write
from strategy_v1 import strategy_prefs
from strategy_defaults import BACKTEST_ENGINE_FILES

FILES = ('strategy_setup.py', 'strategy_v1.py', 'risk_engine.py', 'trade_signals.py',
         'strategy_defaults.py', 't_engine.py', 't_runtime.py', 't_costs.py', 'action_center.py', 'alert_journal.py',
         'v1_backtest.py', 'v1_backtest_data.py',
         'reminder_validation.py', 't_history_replay.py', 'reminder_policy.py')
KINDS = {'v1_entry': 'breakout', 'take_profit_1': 'take_profit_1',
         'take_profit_2': 'take_profit_2', 'sell_ready': 'sell_ready', 'sell_watch': 'sell_ready'}


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _version():
    root = Path(__file__).resolve().parent
    return sha256(b''.join((root / name).read_bytes() for name in FILES)).hexdigest()


def _parameters(output, cfg):
    try:
        fees = json.loads((Path(output) / 't_ledger.json').read_text()).get('fee_model', {})
    except (OSError, ValueError, AttributeError):
        fees = {}
    return _digest({'strategy': strategy_prefs(cfg), 't': cfg.get('t_settings', {}), 'fees': fees})


def publish_evidence(output, cfg, bundle, result, *, now=None):
    """Called only after a real replay; legacy or hand-edited verdicts never promote."""
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
    if result['meta'].get('rules') != strategy_prefs(cfg):
        raise ValueError('验证规则参数与当前账户不同，保持待验证')
    from t_engine import _settings as t_parameters
    if t_parameters(cfg) != t_parameters({}):
        raise ValueError('当前做T参数与历史回放默认参数不同，需要重新验证')
    root = Path(__file__).resolve().parent
    current_engine = sha256(b''.join((root / f).read_bytes() for f in BACKTEST_ENGINE_FILES)).hexdigest()
    if result['meta'].get('engine_sha256') != current_engine:
        raise ValueError('回放引擎版本已经变化，保持待验证')
    try:
        model = json.loads((Path(output) / 't_ledger.json').read_text()).get('fee_model', {})
        costs = result['meta']['costs']
        for kind in {s['type'] for s in bundle['securities'].values()}:
            terms = model[kind]
            if (abs(float(terms['commission_per_10k']) / 10000 - costs['commission_rate']) > 1e-10
                    or abs(float(terms['minimum_commission']) - costs['minimum_commission']) > 1e-8):
                raise ValueError('回放费用与当前券商费率不一致')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError('费用口径未匹配，提醒保持待验证') from exc
    baseline = (result.get('reminder_validation') or {}).get('meta', {}).get('baseline_code')
    checked = evaluate_reminders(bundle, result, baseline_code=baseline)
    ends = [p['end'] for p in checked['periods']]
    if not ends or max(ends) >= now.date().isoformat():
        raise ValueError('验证仅能使用已完成的历史区间')
    payload = {'version': _version(), 'parameters': _parameters(output, cfg),
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
