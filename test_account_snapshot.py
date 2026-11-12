"""Offline display-snapshot regressions; never read the user's account ledger."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from account_snapshot import save_display_snapshot, load_display_snapshot
from portfolio_test_fixture import account_fixture
from risk_engine import compute


CST = ZoneInfo('Asia/Shanghai')


def fixture(*, historical=False):
    cfg = account_fixture()
    # Use a prior date so the fixture cannot accidentally be a future timestamp.
    day = (datetime.now(CST) - timedelta(days=1)).date()
    source = datetime.combine(day, datetime.min.time(), CST).replace(hour=15)
    start = source + timedelta(minutes=5) if historical else source + timedelta(seconds=2)
    prices = {p['code']: p['baseline_price'] for p in cfg['positions']}
    risk = compute(cfg, prices, fresh=True, previous_closes=prices)
    risk['sizing'] = [{'max_buy_shares': 100}]
    state = {
        'mode': 'live', 'config_hash': hashlib.sha256(json.dumps(cfg, sort_keys=True,
            ensure_ascii=False).encode()).hexdigest(), 'market_time': start.isoformat(),
        'fetch_finished_at': (start + timedelta(seconds=2)).isoformat(),
        'data_source': '腾讯公开行情', 'quotes': [
            dict(code=p['code'], name=p['name'], asset_type=p['type'], held=True,
                 price=prices[p['code']], source_timestamp=source.isoformat())
            for p in cfg['positions']
        ], 'risk': risk,
        'quote_health': {'all_held_usable': not historical,
            'provider_timestamps_verified': not historical, 'block_new_signals': historical,
            'missing': [], 'invalid_jump': []},
        'trade_signals': [{'shares': 100}], 'new_trade_signals': [{'shares': 100}],
    }
    return cfg, state


class DisplaySnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'account_snapshot.json'

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_snapshot_survives_lunch_close_and_new_loader(self):
        cfg, state = fixture()
        original = deepcopy(state)
        self.assertTrue(save_display_snapshot(self.path, cfg, state))
        for future in (datetime.now(timezone.utc) + timedelta(hours=2),
                       datetime.now(timezone.utc) + timedelta(days=3)):
            with patch('account_snapshot.datetime', wraps=datetime) as clock:
                clock.now.return_value = future
                loaded = load_display_snapshot(self.path, deepcopy(cfg))
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded['risk']['equity'], state['risk']['equity'])
            self.assertFalse(loaded['risk']['valid'])
            self.assertTrue(loaded['display_only'])
            self.assertEqual(loaded['risk']['sizing'], [])
            self.assertIsNone(loaded['risk']['available_new_risk'])
            self.assertNotIn('trade_signals', loaded)
        self.assertEqual(state, original)

    def test_post_close_old_provider_time_is_saved_only_as_historical(self):
        cfg, state = fixture(historical=True)
        self.assertFalse(save_display_snapshot(self.path, cfg, state))
        self.assertTrue(save_display_snapshot(self.path, cfg, state, historical=True))
        loaded = load_display_snapshot(self.path, cfg)
        self.assertTrue(loaded['historical'])
        self.assertEqual(loaded['asof'], datetime.fromisoformat(
            state['quotes'][0]['source_timestamp']).astimezone(timezone.utc).isoformat())
        self.assertNotEqual(loaded['asof'], loaded['fetched_at'])
        self.assertFalse(loaded['risk']['valid'])

    def test_only_held_quotes_are_archived(self):
        cfg, state = fixture()
        state['quotes'].append(dict(code='600000', asset_type='stock', held=False,
            price=None, source_timestamp=None))
        self.assertTrue(save_display_snapshot(self.path, cfg, state))
        loaded = load_display_snapshot(self.path, cfg)
        self.assertEqual(len(loaded['quotes']), len(cfg['positions']))

    def test_changed_cash_or_positions_reject_old_snapshot(self):
        cfg, state = fixture()
        self.assertTrue(save_display_snapshot(self.path, cfg, state))
        for changed in (dict(cfg, cash=cfg['cash'] + 1), deepcopy(cfg)):
            if changed['cash'] == cfg['cash']:
                changed['positions'][0]['quantity'] += 100
            self.assertIsNone(load_display_snapshot(self.path, changed))

    def test_failed_refresh_never_overwrites_complete_snapshot(self):
        cfg, state = fixture()
        self.assertTrue(save_display_snapshot(self.path, cfg, state))
        original = self.path.read_bytes()
        bad_states = []
        missing = deepcopy(state); missing['quotes'].pop(); bad_states.append(missing)
        failed = deepcopy(state); failed['quotes'] = []; failed['risk']['valid'] = False; bad_states.append(failed)
        mixed = deepcopy(state); mixed['quotes'][0]['source_timestamp'] = (
            datetime.fromisoformat(mixed['quotes'][0]['source_timestamp']) - timedelta(days=1)).isoformat(); bad_states.append(mixed)
        future = deepcopy(state); future['quotes'][0]['source_timestamp'] = (
            datetime.fromisoformat(future['fetch_finished_at']) + timedelta(seconds=1)).isoformat(); bad_states.append(future)
        unclocked = deepcopy(state); unclocked['quotes'][0]['source_timestamp'] = None; bad_states.append(unclocked)
        badprice = deepcopy(state); badprice['quotes'][0]['price'] = float('nan'); bad_states.append(badprice)
        wrongtype = deepcopy(state); wrongtype['quotes'][0]['asset_type'] = 'etf'; bad_states.append(wrongtype)
        jump = deepcopy(state); jump['quote_health']['invalid_jump'] = ['300274']; bad_states.append(jump)
        wrongvalue = deepcopy(state); wrongvalue['risk']['equity'] += 1; bad_states.append(wrongvalue)
        for bad in bad_states:
            with self.subTest(bad=bad):
                self.assertFalse(save_display_snapshot(self.path, cfg, bad, historical=True))
                self.assertEqual(self.path.read_bytes(), original)

    def test_load_rejects_corrupt_or_incomplete_file(self):
        cfg, state = fixture()
        self.assertIsNone(load_display_snapshot(self.path, cfg))
        self.path.write_text('{')
        self.assertIsNone(load_display_snapshot(self.path, cfg))
        for mutate in (
            lambda d: d['quotes'].pop(),
            lambda d: d['quotes'][0].update(source_timestamp=None),
            lambda d: d['risk']['positions'].__setitem__(0, None),
            lambda d: d['risk'].update(equity=float('inf')),
            lambda d: d.update(asof='invented timestamp'),
        ):
            self.assertTrue(save_display_snapshot(self.path, cfg, state))
            data = json.loads(self.path.read_text())
            mutate(data)
            self.path.write_text(json.dumps(data))
            self.assertIsNone(load_display_snapshot(self.path, cfg))

    def test_load_sanitizes_trading_fields_even_when_file_is_tampered(self):
        cfg, state = fixture()
        self.assertTrue(save_display_snapshot(self.path, cfg, state))
        data = json.loads(self.path.read_text())
        data['risk'].update(valid=True, sizing=[{'max_buy_shares': 100}], available_new_risk=500)
        data['trade_signals'] = [{'shares': 100}]
        self.path.write_text(json.dumps(data))
        loaded = load_display_snapshot(self.path, cfg)
        self.assertFalse(loaded['risk']['valid'])
        self.assertEqual(loaded['risk']['sizing'], [])
        self.assertIsNone(loaded['risk']['available_new_risk'])
        self.assertNotIn('trade_signals', loaded)

    def test_atomic_write_failure_preserves_previous_file(self):
        cfg, state = fixture()
        self.assertTrue(save_display_snapshot(self.path, cfg, state))
        original = self.path.read_bytes()
        with patch('account_snapshot.os.replace', side_effect=OSError('simulated failure')):
            self.assertFalse(save_display_snapshot(self.path, cfg, state))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
