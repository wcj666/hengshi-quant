from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from zoneinfo import ZoneInfo

from alert_journal import capture_rows, load_journal, persist_cycle, validate_fill_link, set_feedback, followup_signals


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


class RemainingSellFollowupTests(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.output = Path(folder.name)
        self.signal = dict(id=ALERT_ID, code=CODE, name='测试ETF', title='退出复核', category='reduce',
                           trigger='stop', held=True, shares=500, price=1., level=1.,
                           priority='high', blockers=[], reason='固定退出条件仍触发')
        self.positions = [dict(code=CODE, quantity=900)]
        self.now = AT + timedelta(seconds=10)
        self.inventory = {CODE: dict(date=str(AT.date()), base_quantity=900, available_quantity=900,
                                    confirmed_at=self.now.isoformat())}
        self.trades = [self.fill('one', ALERT_ID, 100, AT + timedelta(seconds=5))]
        persist_cycle(self.output, self.capture(self.signal, AT), now=AT)

    def fill(self, ident, alert_id, quantity, at):
        return dict(fill_id=ident, alert_id=alert_id, code=CODE, side='sell', quantity=quantity,
                    executed_at=at.isoformat(), price=1.)

    def capture(self, signal, now):
        return capture_rows([], [signal], {CODE: {'source_timestamp': now.isoformat()}}, {},
                            now=now, source='synthetic', config_hash='fixture')

    def follow(self, signals=None):
        return followup_signals(self.output, signals or [self.signal], self.positions,
                                self.inventory, self.trades, now=self.now)

    def test_partial_fills_form_stable_residual_chain_until_target_complete(self):
        first = self.follow()[0]
        self.assertEqual(first['shares'], 400)
        self.assertEqual(first['remaining_target'], 400)
        self.assertEqual(first['filled_before'], 100)
        self.assertNotEqual(first['id'], ALERT_ID)
        self.assertEqual(self.follow()[0]['id'], first['id'])
        self.assertEqual(self.follow([first])[0], first)
        persist_cycle(self.output, self.capture(first, self.now), now=self.now)
        self.trades.append(self.fill('two', first['id'], 200, AT + timedelta(seconds=15)))
        self.now = AT + timedelta(seconds=20)
        self.positions[0]['quantity'] = 700
        self.inventory[CODE].update(base_quantity=700, available_quantity=700,
                                    confirmed_at=self.now.isoformat())
        second = self.follow()[0]
        self.assertEqual(second['shares'], 200)
        self.assertEqual(second['root_alert_id'], ALERT_ID)
        self.assertEqual(second['parent_alert_id'], first['id'])
        self.assertNotEqual(second['id'], first['id'])
        persist_cycle(self.output, self.capture(second, self.now), now=self.now)
        self.trades.append(self.fill('three', second['id'], 200, AT + timedelta(seconds=21)))
        self.now += timedelta(seconds=5)
        self.assertEqual(self.follow(), [])
        original = json.loads((self.output/'alert_journal'/f'{AT.date()}.json').read_text())[ALERT_ID]
        self.assertEqual(original['quantity'], 500)
        self.assertEqual(original['at'], AT.isoformat())

    def test_missing_or_old_inventory_is_observation_then_confirmation_promotes_once(self):
        for changes in ({'date': '2024-06-14'}, {'base_quantity': 1000},
                        {'confirmed_at': AT.isoformat()}, {'confirmed_at': (self.now+timedelta(seconds=1)).isoformat()}):
            original = self.inventory[CODE].copy()
            with self.subTest(changes=changes):
                self.inventory[CODE].update(changes)
                pending = self.follow()[0]
                self.assertIsNone(pending['shares'])
                self.assertEqual(pending['remaining_target'], 400)
                row = self.capture(pending, self.now)[0]
                self.assertTrue(row['native'])
                self.assertFalse(row['repeatable'])
                self.assertIsNone(row['side'])
            self.inventory[CODE] = original
        pending = self.follow()[0]
        self.inventory[CODE]['available_quantity'] = 0
        blocked = self.follow()[0]
        self.assertIsNone(blocked['shares'])
        self.assertNotEqual(pending['id'], blocked['id'])
        self.inventory[CODE]['available_quantity'] = 300
        self.assertEqual(self.follow()[0]['shares'], 300)

    def test_duplicates_wrong_direction_dates_and_future_fills_do_not_change_target(self):
        self.trades += [self.trades[0].copy(),
                        dict(self.trades[0], fill_id='wrong-side', side='buy'),
                        dict(self.trades[0], fill_id='wrong-code', code='600001'),
                        dict(self.trades[0], fill_id='old', executed_at=(AT-timedelta(days=1)).isoformat()),
                        dict(self.trades[0], fill_id='future', executed_at=(self.now+timedelta(seconds=1)).isoformat()),
                        dict(self.trades[0], fill_id='bad', quantity=True)]
        self.assertEqual(self.follow()[0]['remaining_target'], 400)

    def test_parent_feedback_does_not_silence_new_remaining_task_but_child_feedback_does(self):
        set_feedback(self.output, ALERT_ID, 'seen', now=AT)
        child = self.follow()[0]
        sent = persist_cycle(self.output, self.capture(child, self.now), now=self.now,
                             notify=True, send=lambda _: True, trades=self.trades)
        self.assertEqual(sent['submitted'], 1)
        set_feedback(self.output, child['id'], 'deferred', now=self.now)
        self.now += timedelta(seconds=60)
        sent = persist_cycle(self.output, self.capture(self.follow()[0], self.now), now=self.now,
                             notify=True, send=lambda _: self.fail('child feedback ignored'), trades=self.trades)
        self.assertEqual(sent['attempted'], 0)

    def test_child_fill_cannot_exceed_total_original_goal(self):
        child = self.follow()[0]
        persist_cycle(self.output, self.capture(child, self.now), now=self.now)
        valid = self.fill('two', child['id'], 400, self.now+timedelta(seconds=1))
        self.assertEqual(validate_fill_link(self.output, valid, self.trades), 1.)
        # Another fill linked to the root is also part of the same total goal.
        self.trades.append(self.fill('other', ALERT_ID, 200, self.now))
        with self.assertRaisesRegex(ValueError, '原减仓目标'):
            validate_fill_link(self.output, valid, self.trades)


if __name__ == '__main__':
    main()
