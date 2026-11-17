from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from history_collection import (build_bundle, completed_dates, contiguous_windows,
                                describe_positions, remember_basket, run_trial)
from install_history_collection_mac import job_spec
from strategy_runtime import CST
from v1_backtest_data import demo_bundle
from web_dashboard import create_app


class HistoryCollectionTests(unittest.TestCase):
    def test_today_only_enters_at_verified_close_and_weekend_is_not_assumed(self):
        dates = ['2026-11-16', '2026-11-17', '2026-11-18']
        now = datetime(2026, 11, 17, 15, 4, tzinfo=CST)
        self.assertEqual(completed_dates(dates, now), dates[:1])
        self.assertEqual(completed_dates(dates, now.replace(minute=5)), dates[:2])

    def test_actual_turnover_archive_is_not_claimed_as_replay_ready(self):
        fixture = demo_bundle()
        dates = fixture['calendar']
        day = dates[-1]
        daily = {'159971': fixture['securities']['600000']['daily'], '000300': fixture['benchmark']}
        settings = {'positions': [{'code': '159971', 'name': '测试ETF'}], 'initial_equity': 100000}
        normalized = fixture['securities']['600000']['sessions'][day]
        archives = {'159971': {day: {'minutes_count': 240, 'turnover_count': 240,
                                     'amount_count': 226, 'session': normalized, 'blockers': ['量额不一致']}}}
        bundle = build_bundle(daily, archives, dates, settings, {})
        security = bundle['securities']['159971']
        self.assertFalse(security['sessions'][day]['corporate_action_clear'])
        self.assertEqual(security['reviews'], {})
        item = describe_positions(settings, daily, archives, bundle)[0]
        self.assertEqual(item['turnover_days'], 1)
        self.assertEqual(item['matched_amount_days'], 0)
        self.assertEqual(item['replay_days'], 0)
        with TemporaryDirectory() as root:
            result = run_trial(bundle, settings, dates, Path(root))
            self.assertEqual(result['status'], '已试跑，执行证据不足')

    def test_dated_point_in_time_evidence_does_not_backfill_a_late_review(self):
        fixture = demo_bundle()
        dates = fixture['calendar']
        day = dates[-1]
        daily = {'159971': fixture['securities']['600000']['daily'], '000300': fixture['benchmark']}
        settings = {'positions': [{'code': '159971', 'name': '测试ETF'}]}
        archives = {'159971': {day: {'session': fixture['securities']['600000']['sessions'][day]}}}
        evidence = {'corporate_actions': {'159971': {day: {'clear': True, 'source': '离线测试',
                                                          'recorded_at': day + 'T15:06:00+08:00'}}}}
        self.assertFalse(build_bundle(daily, archives, dates, settings, evidence)['securities']['159971']
                         ['sessions'][day]['corporate_action_clear'])
        evidence['corporate_actions']['159971'][day]['recorded_at'] = day + 'T09:00:00+08:00'
        self.assertTrue(build_bundle(daily, archives, dates, settings, evidence)['securities']['159971']
                        ['sessions'][day]['corporate_action_clear'])

    def test_retrospective_adjustment_facts_keep_actual_check_time_and_require_corroboration(self):
        from history_collection import _confirmed_actions

        day = '2026-11-16'
        now = datetime(2026, 11, 17, 15, tzinfo=CST)
        record = {'clear': True, 'source': '官方基金产品和分红拆分档案核验',
                  'basis': 'reference_price_and_published_action_crosscheck',
                  'checked_at': now.isoformat(), 'reference_price_unchanged': True,
                  'same_date_reference': .852, 'previous_raw_close': .852,
                  'reference_sha256': 'a' * 64,
                  'provenance': [{'url': 'https://example.com/fund', 'sha256': 'b' * 64},
                                 {'url': 'https://example.com/actions', 'sha256': 'c' * 64}]}
        evidence = {'corporate_actions': {'159971': {day: record}}}
        self.assertTrue(_confirmed_actions(evidence, '159971', day, now))
        self.assertNotIn('recorded_at', record)
        record['previous_raw_close'] = .85
        self.assertFalse(_confirmed_actions(evidence, '159971', day, now))
        record['previous_raw_close'] = .852
        record['checked_at'] = '2026-11-18T10:00:00+08:00'
        self.assertFalse(_confirmed_actions(evidence, '159971', day, now))

    def test_repeat_basket_registration_preserves_earliest_proof(self):
        now = datetime(2026, 11, 17, 14, tzinfo=CST)
        with TemporaryDirectory() as root:
            work = Path(root)
            first = remember_basket(work, [('159971', '测试ETF')], '159971', now)
            again = remember_basket(work, [('159971', '测试ETF')], '159971', now.replace(hour=16))
            self.assertEqual(first['registered_at'], again['registered_at'])
        spec = job_spec(Path('/app'))
        self.assertTrue(spec['RunAtLoad'])
        self.assertEqual(len(spec['StartCalendarInterval']), 10)
        self.assertNotIn(0, {x['Weekday'] for x in spec['StartCalendarInterval']})

    def test_development_validation_holdout_are_contiguous_and_only_use_prospective_basket(self):
        fixture = demo_bundle()
        calendar = fixture['calendar']
        item = {'daily': [{'date': day} for day in calendar],
                'sessions': {day: {'status': 'suspended', 'corporate_action_clear': True} for day in calendar[-65:]}}
        bundle = {'calendar': calendar, 'benchmark': item['daily'], 'securities': {'159971': item}}
        windows, count = contiguous_windows(bundle, {'registered_at': calendar[-50] + 'T10:00:00+08:00'})
        self.assertFalse(windows)
        self.assertEqual(count, 49)
        windows, count = contiguous_windows(bundle, {'registered_at': calendar[-66] + 'T10:00:00+08:00'})
        self.assertEqual(count, 65)
        self.assertLess(windows['development'][1], windows['validation'][0])
        self.assertLess(windows['validation'][1], windows['holdout'][0])

    def test_read_only_routes_report_waiting_when_worker_has_not_run(self):
        with TemporaryDirectory() as folder:
            client = create_app(Path(folder)).test_client()
            self.assertEqual(client.get('/api/backtest/history-collection').json['positions'], [])
            self.assertEqual(client.get('/api/backtest/history-trial').status_code, 404)


if __name__ == '__main__':
    unittest.main()
