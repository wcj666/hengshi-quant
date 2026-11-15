from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from v1_backtest import run_backtest
from v1_backtest_data import load_bundle
from v1_history_import import import_history, write_bundle, write_demo_manifest


class HistoryImportTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.manifest = write_demo_manifest(self.folder / 'example')

    def test_demo_files_import_and_replay_with_original_digests(self):
        bundle = import_history(self.manifest)
        self.assertTrue(bundle['demo'])
        self.assertEqual(bundle['input_digests']['manifest'], sha256(self.manifest.read_bytes()).hexdigest())
        minutes = self.manifest.parent / 'minutes.csv'
        self.assertEqual(bundle['input_digests']['minutes'], sha256(minutes.read_bytes()).hexdigest())
        report = run_backtest(bundle, '2024-06-17', '2024-06-19')
        self.assertEqual([fill['reason'] for fill in report['fills']], [
            'breakout', 'take_profit_1', 'take_profit_2',
        ])
        output = self.folder / 'history.json'
        write_bundle(bundle, output)
        self.assertEqual(load_bundle(output)[0], bundle)
        with self.assertRaises(FileExistsError):
            write_bundle({}, output)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_universe_keeps_empty_days_and_distinguishes_absent_days(self):
        pool = self.manifest.parent / 'universe.csv'
        pool.write_text('date,code\n2024-06-14,\n2024-06-18,600000\n', encoding='utf-8')
        history = import_history(self.manifest)
        self.assertEqual(history['universe'], {'2024-06-14': [], '2024-06-18': ['600000']})
        report = run_backtest(history, '2024-06-17', '2024-06-19')
        self.assertEqual(report['meta']['input_coverage']['universe_missing_days'], 1)

    def test_orphan_minutes_and_escape_paths_rejected(self):
        path = self.manifest.parent / 'minutes.csv'
        original = path.read_text()
        path.write_text(original.replace('2024-06-17,', '2024-06-14,', 1))
        with self.assertRaisesRegex(ValueError, '分钟记录缺少'):
            import_history(self.manifest)
        self.manifest.write_text(self.manifest.read_text().replace('minutes.csv', '../minutes.csv'))
        with self.assertRaisesRegex(ValueError, '位于清单目录下'):
            import_history(self.manifest)


if __name__ == '__main__':
    main()
