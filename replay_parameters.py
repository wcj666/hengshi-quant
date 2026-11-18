"""Normalize the live account rules and confirmed fee terms for replay."""
from pathlib import Path
import json

from strategy_defaults import initial_v1_rules
from strategy_v1 import enabled, strategy_prefs
from t_costs import nonnegative
from t_engine import _settings as t_parameters


ASSET_KINDS = ('stock', 'etf')


def strategy_rules(cfg):
    raw = cfg.get('strategy_v1')
    execution = cfg.get('execution_preferences')
    return {
        **strategy_prefs(cfg),
        'enabled': enabled(cfg),
        'exit_mode': 'price_first' if isinstance(raw, dict) and raw.get('exit_mode') == 'price_first'
                     else 'time_first',
        'execution_mode': 'exit_only' if isinstance(execution, dict) and execution.get('mode') == 'exit_only'
                          else 'normal',
    }


def t_rules(cfg):
    raw = cfg.get('t_settings')
    return {**t_parameters(cfg), 'enabled': isinstance(raw, dict) and raw.get('enabled') is True}


def broker_terms(raw):
    cleaned = {}
    for kind in ASSET_KINDS:
        fields = raw.get(kind) if isinstance(raw, dict) else None
        if not isinstance(fields, dict):
            continue
        rate = nonnegative(fields.get('commission_per_10k'))
        minimum = nonnegative(fields.get('minimum_commission'))
        if rate is not None and rate <= 30 and minimum is not None and minimum <= 100_000:
            cleaned[kind] = {'commission_per_10k': rate, 'minimum_commission': minimum}
    return cleaned


def read_broker_terms(output):
    try:
        return broker_terms(json.loads((Path(output) / 't_ledger.json').read_text()).get('fee_model'))
    except (OSError, ValueError, AttributeError):
        return {}


def load_account_profile(output, cfg=None):
    output = Path(output)
    if cfg is None:
        try:
            cfg = json.loads((output.parent / 'config.json').read_text())
        except FileNotFoundError:
            return None
    if not isinstance(cfg, dict):
        raise ValueError('本机实盘参数文件结构无效')
    if cfg.get('account_profile') == 'example':
        try:
            account = json.loads((output / 'strategy_account.json').read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(account, dict) or account.get('initialized') is not True:
            return None
    return {
        'rules': strategy_rules(cfg),
        't_settings': t_rules(cfg),
        'costs': {'broker_terms': read_broker_terms(output)},
    }


def normalize_rules(rules):
    raw = initial_v1_rules() if rules is None else rules
    if not isinstance(raw, dict):
        raise ValueError('V1 回放规则必须是键值对象')
    return strategy_rules({'strategy_v1': raw, 'execution_preferences': {'mode': raw.get('execution_mode')}})
