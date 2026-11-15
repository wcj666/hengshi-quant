from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from zoneinfo import ZoneInfo

from alert_journal import capture_rows, load_journal, persist_cycle, validate_fill_link


CN = ZoneInfo('Asia/Shanghai')
AT = datetime(2024, 6, 17, 9, 46, 30, tzinfo=CN)
CODE = '600000'
ALERT_ID = 'a' * 20


class AlertJournalTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.output = Path(folder.name)
        self.signal = {'id': ALERT_ID, 'code': CODE, 'name': '测试股', 'title': 'V1新增',
                       'trigger': 'v1_entry', 'reason': '通过核验', 'price': 10.07, 'level': 10.05,
                       'category': 'add', 'shares': 200}
        self.plans = {CODE: {'signal_date': '2024-06-14'}}

    def rows(self, now=AT, quote_at=None):
        return capture_rows([], [self.signal], {
            CODE: {'source_timestamp': (quote_at or now - timedelta(seconds=5)).isoformat()},
        }, self.plans, now=now, source='测试行情', config_hash='test')

    def read(self, *, trades=(), now=AT):
        return load_journal(self.output, trades, now=now)['rows'][0]

    def test_pending_is_saved_even_without_system_notification(self):
        stored = persist_cycle(self.output, self.rows(), now=AT)
        self.assertEqual(stored['attempted'], 0)
        row = self.read()
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['quote_at'], '2024-06-17T09:46:25+08:00')
        self.assertEqual(row['price'], 10.07)
        self.assertEqual(self.read(now=AT.replace(hour=15))['status'], 'expired')

    def test_retry_is_durable_throttled_and_active_only(self):
        called = []

        def send(row):
            self.assertEqual(row['id'], ALERT_ID)
            self.assertEqual(self.read()['notification']['outcome'], 'sending')
            called.append(row)
            return len(called) == 2

        def cycle(offset):
            now = AT + timedelta(seconds=offset)
            return persist_cycle(self.output, self.rows(now), now=now, notify=True, send=send)

        self.assertEqual(cycle(0), {'attempted': 1, 'submitted': 0})
        self.assertEqual(cycle(29)['attempted'], 0)
        self.assertEqual(cycle(30), {'attempted': 1, 'submitted': 1})
        self.assertEqual(cycle(60)['attempted'], 0)
        self.assertEqual(self.read()['notification']['attempts'], 2)
        self.assertEqual(self.read()['at'], AT.isoformat())
        self.assertEqual(self.rows(AT, AT - timedelta(seconds=91)), [])

    def test_failed_notifications_exhaust_limit_and_expire(self):
        persist_cycle(self.output, self.rows(), now=AT, send=lambda _: False, notify=True)
        for offset in (30, 60, 90, 120, 150):
            now = AT + timedelta(seconds=offset)
            persist_cycle(self.output, self.rows(now), now=now, send=lambda _: False, notify=True)
        self.assertEqual(self.read()['notification']['attempts'], 3)
        self.assertFalse(self.read()['notification']['submitted'])

    def test_unknown_system_submission_is_left_for_review(self):
        persist_cycle(self.output, self.rows(), now=AT, send=lambda _: None, notify=True)
        now = AT + timedelta(seconds=30)
        result = persist_cycle(self.output, self.rows(now), now=now, send=lambda _: True, notify=True)
        self.assertEqual(result['attempted'], 0)
        self.assertEqual(self.read()['notification']['outcome'], 'unknown')

    def test_linked_actual_fills_drive_partial_and_complete_states(self):
        persist_cycle(self.output, self.rows(), now=AT)
        fill = {'fill_id': '券商一', 'alert_id': ALERT_ID, 'code': CODE, 'side': 'buy', 'quantity': 100,
                'price': 10.08, 'executed_at': (AT + timedelta(minutes=1)).isoformat()}
        second = dict(fill, fill_id='券商二')
        self.assertEqual(validate_fill_link(self.output, fill), self.signal['level'])
        self.assertEqual(self.read(trades=[fill, fill])['status'], 'partial')
        self.assertEqual(self.read(trades=[fill, second])['status'], 'filled')
        with self.assertRaisesRegex(ValueError, '累计'):
            validate_fill_link(self.output, dict(second, quantity=101), [fill])
        with self.assertRaisesRegex(ValueError, '匹配'):
            validate_fill_link(self.output, dict(fill, side='sell'))
        with self.assertRaisesRegex(ValueError, '匹配'):
            validate_fill_link(self.output, dict(fill, executed_at=(AT - timedelta(seconds=1)).isoformat()))

    def test_replay_requires_real_data_matching_the_alert_path(self):
        persist_cycle(self.output, self.rows(), now=AT)
        report = self.output / 'v1_backtest' / 'result.json'
        report.parent.mkdir()
        simulated = dict(code=CODE, date='2024-06-17', signal_date='2024-06-14',
                         signal_at='2024-06-17T09:46:00+08:00', reason='breakout', price=10.06,
                         at='2024-06-17T09:46:00+08:00', reference_price=10.05)
        sample = {'meta': {'demo': True, 'start': '2024-06-17', 'end': '2024-06-18'}, 'fills': [simulated]}
        report.write_text(json.dumps(sample))
        self.assertEqual(self.read()['replay']['status'], 'missing')
        sample['meta']['demo'] = False
        report.write_text(json.dumps(sample))
        evidence = self.read()['replay']
        self.assertEqual(evidence['status'], 'match')
        self.assertEqual(evidence['alert_minus_simulated_seconds'], 30)
        sample['fills'].append(simulated.copy())
        report.write_text(json.dumps(sample))
        self.assertEqual(self.read()['replay']['status'], 'ambiguous')


if __name__ == '__main__':
    main()
