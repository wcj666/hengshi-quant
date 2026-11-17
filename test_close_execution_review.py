from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alert_journal import capture_rows, persist_cycle, load_close_review, record_close_review
from action_center import CN
from portfolio_test_fixture import account_fixture
from t_close_marks import capture_marks, load_marks, quote_targets
from web_dashboard import CSRF, create_app


class CloseExecutionReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.output = self.root/'output'
        self.day = '2026-11-16'
        self.during = datetime(2026, 11, 16, 10, 30, tzinfo=CN)
        self.close = self.during.replace(hour=15, minute=30)
        self.ident = 'a' * 20
        self.code = '159971'

    def capture(self, ident=None, *, at=None, diagnostic=False):
        at = at or self.during
        signal = dict(id=ident or self.ident, code=self.code, name='测试ETF', title='减仓提醒',
                      trigger='stop', category='diagnostic' if diagnostic else 'reduce',
                      price=.810, level=.820, shares=None if diagnostic else 300,
                      reason='跌到固定退出参考', blockers=['缺少完整行情'] if diagnostic else [])
        return capture_rows([], [signal], {self.code:{'source_timestamp':at.isoformat()}},
                            {}, now=at, source='synthetic', config_hash='hash')[0]

    def fill(self, ident=None, quantity=200):
        return dict(alert_id=ident, fill_id='actual', code=self.code, side='sell',
                    price=.808, quantity=quantity, fee=5,
                    executed_at=self.during.replace(minute=31).isoformat())

    def first(self, trades=()):
        return load_close_review(self.output, self.day, trades, now=self.close)['rows'][0]

    def test_absence_of_fill_requires_confirmation_then_manual_notes_and_partial_precedence(self):
        persist_cycle(self.output, [self.capture()], now=self.during)
        self.assertEqual(self.first()['review_status'], 'pending')
        record_close_review(self.output, self.ident, self.day, 'missed', '看到时价格已经离开触发区', now=self.close)
        self.assertEqual(self.first()['review_status'], 'missed')
        self.assertIn('已经离开', self.first()['review_reason'])
        self.assertEqual(self.first([self.fill()])['review_status'], 'needs_reconcile')
        partial = self.first([self.fill(self.ident)])
        self.assertEqual(partial['review_status'], 'partial')
        self.assertEqual(partial['filled_quantity'], 200)
        self.assertIn('100', partial['review_reason'])
        self.assertEqual(partial['fills'][0]['fee'], 5)
        self.assertEqual(self.first([self.fill(self.ident, 300)])['review_status'], 'filled')

    def test_inactive_reason_and_user_skip_survive_close(self):
        persist_cycle(self.output, [self.capture()], now=self.during)
        persist_cycle(self.output, [], now=self.during.replace(minute=37),
                      inactive_reason=lambda row: '行情过期，原提醒撤销')
        self.assertEqual(self.first()['review_status'], 'expired')
        self.assertIn('行情过期', self.first()['review_reason'])
        record_close_review(self.output, self.ident, self.day, 'skipped', '未达到个人价格要求', now=self.close)
        self.assertEqual(self.first()['review_status'], 'skipped')
        record_close_review(self.output, self.ident, self.day, 'clear', None, now=self.close)
        self.assertEqual(self.first()['review_status'], 'expired')
        self.assertIsNotNone(self.first()['inactive_at'])

    def test_an_observation_is_not_a_missed_trade(self):
        persist_cycle(self.output, [self.capture(diagnostic=True)], now=self.during)
        row = self.first()
        self.assertEqual(row['review_status'], 'observe')
        self.assertFalse(row['can_review'])
        self.assertEqual(row['review_reason'], '缺少完整行情')

    def test_desktop_review_uses_actual_submission_after_policy_change(self):
        row = self.capture(diagnostic=True)
        row.update(native=False, repeatable=False)
        persist_cycle(self.output, [row], now=self.during)
        self.assertEqual(self.first()['notification_label'], '无桌面通知')
        row.update(native=True)
        persist_cycle(self.output, [row], now=self.during, notify=True, send=lambda _: True)
        self.assertEqual(self.first()['notification_label'], '桌面通知已提交')
        self.assertEqual(self.first()['review_status'], 'observe')

    def test_archive_accepts_individual_actual_close_marks_and_preserves_good_data(self):
        at = self.close
        other = '588170'
        book = {'cycles': [
            {'date':self.day, 'kind':'etf', 'code':self.code, 'unbought_quantity':200},
            {'date':self.day, 'kind':'etf', 'code':other, 'unbought_quantity':100},
            {'date':self.day, 'kind':'stock', 'code':'002463', 'unbought_quantity':0},
        ]}
        targets = quote_targets(book, self.day)
        self.assertEqual(targets, {('etf', self.code), ('etf', other)})
        feed = {('etf', self.code): (.813, 'Test', .852, at.replace(minute=5).isoformat()),
                ('etf', other): (.662, 'Test', .66, at.replace(hour=14).isoformat())}
        capture_marks(self.output, self.day, at, targets, feed, 'Tencent')
        self.assertEqual(load_marks(self.output, self.day, at)[self.code]['price'], .813)
        self.assertNotIn(other, load_marks(self.output, self.day, at))
        capture_marks(self.output, self.day, at, targets, {}, 'Tencent')
        self.assertEqual(load_marks(self.output, self.day, at)[self.code]['price'], .813)
        self.assertEqual(load_marks(self.output, '2026-11-17', at), {})

    def test_page_independent_report_and_manual_marks_do_not_write_account(self):
        cfg = self.root/'config.json'
        cfg.write_text(json.dumps(account_fixture()))
        before = cfg.read_bytes()
        persist_cycle(self.output, [self.capture()], now=self.during)
        app = create_app(self.root).test_client()
        url = '/api/close-execution-review'
        report = app.get(url, query_string={'day':self.day})
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json['alerts']['summary']['pending'], 1)
        self.assertEqual(report.json['t_review']['summary']['net_status'], 'no_samples')
        self.assertEqual(app.get(url, query_string={'day':'../../config'}).status_code, 400)
        body = {'day':self.day, 'outcome':'missed', 'reason':'未及时看到'}
        self.assertEqual(app.post(url+'/'+self.ident, json=body).status_code, 403)
        response = app.post(url+'/'+self.ident, json=body, headers={'X-CSRF-Token':CSRF})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(app.get(url, query_string={'day':self.day}).json['alerts']['summary']['missed'], 1)
        self.assertEqual(cfg.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
