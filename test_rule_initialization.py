from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import setup_autostart as installer
from strategy_defaults import T_DEFAULTS, initial_v1_rules


class RuleInitializationTests(unittest.TestCase):
    @contextmanager
    def installation(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            app = home / 'app'
            app.mkdir()
            source = home / 'source'
            source.mkdir()
            template = Path(__file__).with_name('config.example.json')
            (source / template.name).write_bytes(template.read_bytes())
            with patch.multiple(installer, APP=app, SOURCE=source, HOME=home):
                yield home, app / 'config.json'

    def test_example_and_fresh_install_use_same_shared_rules(self):
        template = json.loads(Path(__file__).with_name('config.example.json').read_text(encoding='utf-8'))
        self.assertEqual(template['strategy_v1'], initial_v1_rules())
        self.assertEqual(template['t_settings'], T_DEFAULTS)
        with self.installation() as (home, target):
            # Stale example rules cannot override the shared defaults at install time.
            source = home / 'source' / 'config.example.json'
            stale = {**template, 'strategy_v1': {'min_fees': 999}, 't_settings': {'enabled': False}}
            stale.pop('account_profile')
            source.write_text(json.dumps(stale), encoding='utf-8')
            self.assertIn('虚构账户示例', installer.import_previous_config())
            actual = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(actual, template)
            self.assertEqual(actual['account_profile'], 'example')
            self.assertNotIn('started_at', actual['strategy_v1'])
            self.assertEqual(actual['positions'], [])

    def test_preserves_existing_and_imported_ledgers_byte_for_byte(self):
        existing = b'{"cash": 1234, "positions": [], "strategy_v1": {"enabled": false}}\n'
        with self.installation() as (_, target):
            target.write_bytes(existing)
            installer.import_previous_config()
            self.assertEqual(target.read_bytes(), existing)

        old = b'{"cash": 1234, "baseline_equity": 1234, "positions": [], "strategy_v1": {"min_fees": 25}}\n'
        with self.installation() as (home, target):
            legacy = home / 'Downloads' / 'a_share_ab_web_v3_5_9' / 'config.json'
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(old)
            installer.import_previous_config()
            self.assertEqual(target.read_bytes(), old)

        with self.installation() as (home, target):
            (home / 'source' / 'config.json').write_bytes(existing)
            installer.import_previous_config()
            self.assertEqual(target.read_bytes(), existing)


if __name__ == '__main__':
    unittest.main()
