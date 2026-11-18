from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import hmac
import json
import unittest

import reminder_policy as policy
from alert_journal import notification_policy
from strategy_runtime import CST

NOW = datetime(2026, 11, 17, 10, 30, tzinfo=CST)
CFG = {'strategy_v1': {'enabled': True}, 't_settings': {'enabled': True}}
SIGNAL = {'id': 'a' * 20, 'code': '159971', 'category': 'add', 'trigger': 'v1_entry',
          'title': '入场复核', 'shares': 100, 'tradable': True, 'held': False}


class ReminderPolicyTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(); self.addCleanup(folder.cleanup)
        self.output = Path(folder.name)

    def evidence(self, *, good=True):
        metric = {'net_return_pct': 3 if good else 0, 'max_drawdown_pct': 1 if good else 3}
        baseline = {'net_return_pct': 1, 'max_drawdown_pct': 2, 'covered': True}
        sample = {'covered': True, 'sample_count': 40, 'session_count': 20,
                  'strategy': metric, 'baseline': baseline,
                  'reminders': {k: {'triggers': 25} for k in set(policy.KINDS.values())},
                  'ablation': {'take_profit_1': baseline, 'take_profit_2': baseline}}
        periods = [{'key': key, 'start': start, 'end': end, 'trade': deepcopy(sample), 't': deepcopy(sample)}
                   for key, start, end in [('development', '2026-08-31', '2026-09-29'),
                       ('validation', '2026-09-30', '2026-10-28'), ('holdout', '2026-10-29', '2026-11-16')]]
        data = {'version': policy._version(), 'parameters': policy._parameters(self.output, CFG),
                'codes': ['159971'], 'published_at': NOW.isoformat(), 'dated_pool': True,
                'bundle_sha256': 'b' * 64,
                'report': {'meta': {'demo': False, 'baseline_code': '159971'}, 'periods': periods,
                           'recommendations': [{'status': '验证通过'}]}}
        directory = self.output / 'reminder_policy'; directory.mkdir()
        key = b'private-test-key' * 2
        (directory / '.key').write_bytes(key)
        signature = hmac.new(key, policy._digest(data).encode(), 'sha256').hexdigest()
        path = directory / 'evidence.json'
        path.write_text(json.dumps({'payload': data, 'signature': signature}))
        return path

    def test_missing_evidence_demotes_entry_without_mutating_signal(self):
        original = deepcopy(SIGNAL)
        result = policy.apply_policy(self.output, CFG, [original], now=NOW)[0]
        self.assertEqual(original, SIGNAL)
        self.assertEqual(result['validation_status'], 'pending')
        self.assertIsNone(result['shares']); self.assertFalse(result['tradable'])
        self.assertFalse(notification_policy(result)['native'])
        again = policy.apply_policy(self.output, CFG, [result], now=NOW)[0]
        self.assertEqual(again['id'], result['id'])
        self.assertEqual(again['title'], result['title'])

    def test_matching_real_evidence_preserves_quantity_and_scope(self):
        self.evidence()
        result = policy.apply_policy(self.output, CFG, [SIGNAL], now=NOW)[0]
        self.assertEqual(result['validation_status'], 'verified'); self.assertEqual(result['shares'], 100)
        other = policy.apply_policy(self.output, CFG, [{**SIGNAL, 'code': '588170'}], now=NOW)[0]
        self.assertIsNone(other['shares'])
        cfg = deepcopy(CFG); cfg['t_settings']['max_fraction'] = .1
        self.assertEqual(policy.load_policy(self.output, cfg, now=NOW)['status'], 'pending')

    def test_forged_verdict_and_tampered_or_demo_evidence_cannot_promote(self):
        path = self.evidence(good=False)
        result = policy.apply_policy(self.output, CFG, [SIGNAL], now=NOW)[0]
        self.assertEqual(result['validation_status'], 'excluded')
        self.assertFalse(notification_policy(result)['native'])
        data = json.loads(path.read_text()); data['payload']['report']['meta']['demo'] = True
        path.write_text(json.dumps(data))
        self.assertEqual(policy.load_policy(self.output, CFG, now=NOW)['status'], 'pending')

    def test_fixed_risk_and_actual_t_handling_remain_available(self):
        stop = {**SIGNAL, 'category': 'reduce', 'trigger': 'stop', 'held': True}
        back = {**SIGNAL, 'category': 't', 'trigger': 'buyback_ready', 'cycle_id': 'actual-cycle'}
        for signal in (stop, back):
            result = policy.apply_policy(self.output, CFG, [signal], now=NOW)[0]
            self.assertEqual(result['shares'], 100)
            self.assertEqual(result['validation_status'], 'risk')
            self.assertEqual(result['id'], signal['id'])

    def test_changed_code_and_stale_evidence_are_rejected(self):
        self.evidence()
        from unittest.mock import patch
        with patch('reminder_policy._version', return_value='new-code'):
            self.assertEqual(policy.load_policy(self.output, CFG, now=NOW)['status'], 'pending')
        later = NOW.replace(year=2027)
        self.assertEqual(policy.load_policy(self.output, CFG, now=later)['status'], 'pending')

    def test_publish_rechecks_the_effective_profile_and_confirmed_fee_terms(self):
        from replay_parameters import load_account_profile
        from v1_backtest_data import demo_bundle
        from v1_validation import PERIOD_KEYS, evaluate

        bundle = demo_bundle()
        bundle.update(demo=False, input_digests={'test_fixture': 'a' * 64})
        cfg = {'strategy_v1': {'enabled': True, 'exit_mode': 'price_first', 'max_position_pct': 12},
               't_settings': {'enabled': True, 'max_fraction': .2}}
        windows = {key: (day, day) for key, day in zip(PERIOD_KEYS, bundle['calendar'][-3:])}
        schedule = self.output / 't_ledger.json'
        fees = {kind: {'commission_per_10k': 4, 'minimum_commission': minimum}
                for kind, minimum in [('stock', 3), ('etf', 2)]}
        schedule.write_text(json.dumps({'fee_model': fees}))
        profile = load_account_profile(self.output, cfg)
        result = evaluate(bundle, windows, profile=profile)
        now = datetime(2024, 6, 20, 11, 30, tzinfo=CST)
        self.assertTrue(policy.publish_evidence(self.output, cfg, bundle, result, now=now))
        self.assertEqual(policy.load_policy(self.output, cfg, now=now)['status'], 'matched')
        fees['stock']['minimum_commission'] = 4
        schedule.write_text(json.dumps({'fee_model': fees}))
        with self.assertRaisesRegex(ValueError, '费用口径未匹配'):
            policy.publish_evidence(self.output, cfg, bundle, result, now=now)
        self.assertEqual(policy.load_policy(self.output, cfg, now=now)['status'], 'pending')


if __name__ == '__main__': unittest.main()
