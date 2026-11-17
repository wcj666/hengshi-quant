from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from zoneinfo import ZoneInfo

from alert_journal import capture_rows, load_journal, persist_cycle, validate_fill_link, set_feedback


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

    def test_successful_strong_notifications_repeat_at_most_three_times(self):
        sent = []
        def send(row):
            sent.append(row.copy())
            return True
        for offset in (0, 30, 59, 60, 90, 119, 120, 180, 240):
            now = AT + timedelta(seconds=offset)
            self.signal['price'] = 10.07 + offset / 1000
            persist_cycle(self.output, self.rows(now), now=now, notify=True, send=send)
        saved = self.read(now=AT + timedelta(seconds=240))
        self.assertEqual(len(sent), 3)
        self.assertEqual([round(row['price'], 3) for row in sent], [10.07, 10.13, 10.19])
        self.assertEqual(saved['notification']['successful_count'], 3)
        self.assertEqual(saved['notification']['attempts'], 3)
        self.assertEqual(saved['price'], 10.07)  # Original evidence retained.
        self.assertEqual(saved['at'], AT.isoformat())
        self.assertEqual(saved['last_seen_at'], (AT + timedelta(seconds=240)).isoformat())

    def test_seen_and_deferred_stop_reminders_without_recording_fills(self):
        for action in ('seen', 'deferred'):
            with self.subTest(action=action):
                persist_cycle(self.output, self.rows(), now=AT, notify=True, send=lambda _: True)
                feedback = set_feedback(self.output, ALERT_ID, action, day=str(AT.date()), now=AT + timedelta(seconds=5))
                self.assertEqual(feedback['action'], action)
                self.assertEqual(set_feedback(self.output, ALERT_ID, action, now=AT + timedelta(seconds=6)), feedback)
                now = AT + timedelta(seconds=60)
                result = persist_cycle(self.output, self.rows(now), now=now, notify=True,
                                       send=lambda _: self.fail('acknowledged alert notified again'))
                self.assertEqual(result['attempted'], 0)
                row = self.read(now=now)
                self.assertEqual(row['status'], action)
                self.assertEqual(row['filled_quantity'], 0)
                self.assertEqual(row['fills'], [])

    def test_disappeared_or_stale_conditions_invalidate_previous_alert(self):
        persist_cycle(self.output, self.rows(), now=AT, notify=True, send=lambda _: True)
        later = AT + timedelta(seconds=60)
        persist_cycle(self.output, [], now=later, notify=True,
                      send=lambda _: self.fail('empty cycle cannot re-notify'))
        saved = self.read(now=later)
        self.assertFalse(saved['active'])
        self.assertEqual(saved['status'], 'inactive')
        self.assertEqual(saved['inactive_at'], later.isoformat())
        # A paused/stopped worker also expires the live view without rewriting history.
        persist_cycle(self.output, self.rows(later), now=later)
        self.assertEqual(self.read(now=later + timedelta(seconds=91))['status'], 'inactive')

    def test_actual_partial_fill_stops_reminders_and_feedback_cannot_mark_filled(self):
        persist_cycle(self.output, self.rows(), now=AT, notify=True, send=lambda _: True)
        fill = {'fill_id': 'actual-1', 'alert_id': ALERT_ID, 'code': CODE, 'side': 'buy',
                'quantity': 100, 'price': 10.08, 'executed_at': (AT + timedelta(seconds=5)).isoformat()}
        later = AT + timedelta(seconds=60)
        persist_cycle(self.output, self.rows(later), now=later, trades=[fill], notify=True,
                      send=lambda _: self.fail('do not repeat the original full quantity after partial fill'))
        self.assertEqual(self.read(trades=[fill], now=later)['status'], 'partial')
        with self.assertRaises(ValueError):
            set_feedback(self.output, ALERT_ID, 'filled', now=later)
        self.assertEqual(self.read(now=later)['filled_quantity'], 0)

    def test_informational_basic_notifications_do_not_repeat(self):
        self.signal.update(category='diagnostic', shares=None)
        self.signal['priority'] = 'normal'
        rows = self.rows()
        rows[0].update(native=True, repeatable=False, side=None)
        persist_cycle(self.output, rows, now=AT, notify=True, send=lambda _: True)
        later = AT + timedelta(seconds=60)
        again = [{**row, 'at': later.isoformat(), 'quote_at': later.isoformat()} for row in rows]
        result = persist_cycle(self.output, again, now=later, notify=True,
                               send=lambda _: self.fail('informational alert repeated'))
        self.assertEqual(result['attempted'], 0)

    def test_held_structure_and_t_watch_notify_once_without_order_fields(self):
        for category, trigger in [('reduce', 'structure_support'), ('reduce', 'structure_weakness'),
                                  ('t', 'sell_watch')]:
            with self.subTest(trigger=trigger), TemporaryDirectory() as folder:
                self.signal.update(category=category, trigger=trigger, held=True,
                                   blockers=['固定退出计划待核'], tradable=False, shares=200)
                rows = self.rows()
                self.assertTrue(rows[0]['native'])
                self.assertFalse(rows[0]['repeatable'])
                self.assertIsNone(rows[0]['side'])
                self.assertIsNone(rows[0]['quantity'])
                first = persist_cycle(folder, rows, now=AT, notify=True, send=lambda _: True)
                self.assertEqual(first['submitted'], 1)
                later = AT + timedelta(seconds=60)
                repeat = persist_cycle(folder, self.rows(later), now=later, notify=True,
                                       send=lambda _: self.fail('observation repeated'))
                self.assertEqual(repeat['attempted'], 0)
                self.signal['held'] = False
                self.assertFalse(self.rows()[0]['native'])

    def test_current_eligibility_updates_without_rewriting_initial_evidence(self):
        self.signal.update(category='t', trigger='sell_watch', held=False, shares=None)
        persist_cycle(self.output, self.rows(), now=AT)
        self.signal['held'] = True
        later = AT + timedelta(seconds=10)
        persist_cycle(self.output, self.rows(later), now=later)
        current = self.read(now=later)
        self.assertTrue(current['native'])
        saved = json.loads((self.output / 'alert_journal' / '2024-06-17.json').read_text())[ALERT_ID]
        self.assertFalse(saved['native'])
        self.assertTrue(saved['latest_native'])
        self.assertEqual(saved['at'], AT.isoformat())
        self.assertEqual(saved['notification']['attempts'], 0)

    def test_unknown_repeat_submission_cannot_retry_or_overwrite_prior_success(self):
        persist_cycle(self.output, self.rows(), now=AT, notify=True, send=lambda _: True)
        later = AT + timedelta(seconds=60)
        persist_cycle(self.output, self.rows(later), now=later, notify=True, send=lambda _: None)
        now = AT + timedelta(seconds=120)
        result = persist_cycle(self.output, self.rows(now), now=now, notify=True,
                               send=lambda _: self.fail('ambiguous submission duplicated'))
        self.assertEqual(result['attempted'], 0)
        notification = self.read(now=now)['notification']
        self.assertTrue(notification['submitted'])
        self.assertEqual(notification['successful_count'], 1)
        self.assertEqual(notification['outcome'], 'unknown')

    def test_feedback_validates_identity_date_and_known_row(self):
        persist_cycle(self.output, self.rows(), now=AT)
        for ident, day, action in [(ALERT_ID, '../escape', 'seen'), (ALERT_ID, '2024-02-30', 'seen'),
                                   (ALERT_ID, '2024-06-18', 'seen'), ('b'*20, '2024-06-17', 'seen'),
                                   ('bad', '2024-06-17', 'seen'), (ALERT_ID, '2024-06-17', 'executed')]:
            with self.subTest(ident=ident,day=day,action=action), self.assertRaises(ValueError):
                set_feedback(self.output, ident, action, day=day, now=AT)

    def test_no_notification_after_session_or_initial_retry_window(self):
        persist_cycle(self.output, self.rows(), now=AT)
        later = AT + timedelta(seconds=121)
        result = persist_cycle(self.output, self.rows(later), now=later, notify=True,
                               send=lambda _: self.fail('old unnotified alert replayed'))
        self.assertEqual(result['attempted'], 0)
        close = AT.replace(hour=15, minute=0)
        persist_cycle(self.output, self.rows(close), now=close, notify=True,
                      send=lambda _: self.fail('notification after close'))
        self.assertEqual(self.read(now=close)['status'], 'expired')


if __name__ == '__main__':
    main()
