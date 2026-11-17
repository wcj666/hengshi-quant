from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from unittest import TestCase, main

import trade_reconciliation as reconciliation
from alert_journal import capture_rows, persist_cycle
from web_dashboard import CSRF, create_app


START = {'cash': 1000.0, 'baseline_equity': 3000.0, 'max_additional_loss': 1000.0, 'watchlist': [],
         'positions': [{'code': '600000', 'type': 'stock', 'name': '合成标的', 'quantity': 200,
                        'baseline_price': 10.0, 'cost': 10.0}]}
TIME = datetime.fromisoformat('2024-06-17T16:00:00+08:00')


def batch():
    return {'events': [
        {'executed_at': '2024-06-17T10:15:00', 'fill_id': 'x1', 'code': '600000', 'side': 'sell',
         'quantity': 100, 'price': 11, 'fee': 5, 'reference_price': 11.05, 'planned_risk': 50},
        {'executed_at': '2024-06-17T11:00:00', 'fill_id': 'flow1', 'side': 'deposit', 'amount': 1000},
    ], 'broker': {'asof': '2024-06-17T15:05:04', 'available_cash': 3095, 'total_assets': 4245,
                 'withdrawable_cash': 2000,
                 'positions': {'600000': {'quantity': 100, 'available_quantity': 100}}}}


class ReconciliationTests(TestCase):
    def test_atomic_preparation_and_receipt_values(self):
        cfg = deepcopy(START)
        previous = {'asof': '2024-06-14T15:05:01+08:00', 'total_assets': 3200}
        updated, view = reconciliation.prepare(cfg, batch(), now=TIME, previous_snapshot=previous)
        self.assertEqual(cfg, START)
        self.assertFalse(view['differences'])
        self.assertEqual(updated['cash'], 3095.0)
        self.assertEqual(updated['positions'][0]['quantity'], 100)
        fill = updated['reconciled_trades'][0]
        self.assertEqual((fill['net_pnl_estimate'], fill['net_r_estimate'], fill['adverse_slippage']),
                         (95.0, 1.9, 5.0))
        self.assertEqual(view['receipt']['period_pnl']['amount'], 45)
        snapshot = reconciliation.close_snapshot(updated)
        self.assertEqual(snapshot['withdrawable_cash'], 2000)
        self.assertEqual(snapshot['positions'][0]['available_quantity'], 100)
        updated['cash'] += 1
        self.assertIsNone(reconciliation.close_snapshot(updated))

    def test_csv_dedup_no_reapply_and_conflict_for_existing_ids(self):
        data = batch()
        data.pop('events')
        data['csv'] = ('\ufeff成交编号,成交时间,方向,证券代码,成交数量,成交价,费用合计,转账金额\r\n'
                       'x1,2024-06-17 10:15:00,卖出,600000,100,11,5,\r\n'
                       'x1,2024-06-17 10:15:00,卖出,600000,100,11,5,\r\n'
                       'flow1,2024-06-17 11:00:00,转入,,,,,1000\r\n')
        updated, view = reconciliation.prepare(START, data, now=TIME)
        self.assertEqual(len(view['duplicates']), 1)
        _, duplicate_view = reconciliation.prepare(updated, data, now=TIME)
        self.assertEqual(len(duplicate_view['events']), 0)
        data['csv'] = data['csv'].replace(',11,5,', ',11,6,')
        with self.assertRaisesRegex(ValueError, '编号已存在'):
            reconciliation.prepare(updated, data, now=TIME)

    def test_broker_sellability_and_cash_discrepancies_are_explicit(self):
        data = batch()
        data['broker']['available_cash'] = 3094.99
        data['broker']['positions'] = {}
        _, view = reconciliation.prepare(START, data, now=TIME)
        self.assertEqual(len(view['differences']), 2)
        data['broker']['positions'] = {'600000': {'quantity': 100, 'available_quantity': 200}}
        with self.assertRaisesRegex(ValueError, '可卖量不能超过'):
            reconciliation.prepare(START, data, now=TIME)

    def test_trade_cash_reconciliation_includes_frozen_balance_changes(self):
        cfg = deepcopy(START)
        cfg.update(cash=800, frozen_cash=200)
        data = batch()
        data['broker'].update(available_cash=2995, frozen_cash=100)
        updated, preview = reconciliation.prepare(cfg, data, now=TIME)
        self.assertEqual(preview['differences'], [])
        self.assertEqual((updated['cash'], updated['frozen_cash']), (2995., 100.))
        self.assertEqual(preview['cash_after'], 2995.)
        self.assertEqual(reconciliation.close_snapshot(updated)['frozen_cash'], 100.)
        data['broker']['frozen_cash'] = 99
        _, preview = reconciliation.prepare(cfg, data, now=TIME)
        self.assertTrue(preview['differences'])

    def test_web_preview_stale_tokens_and_confirm_are_safe(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / 'config.json'
            file.write_text(json.dumps(START))
            client = create_app(Path(folder)).test_client()
            headers = {'X-CSRF-Token': CSRF}
            data = batch()
            self.assertEqual(client.get('/trade-reconcile').status_code, 200)
            self.assertEqual(client.get('/trade-reconcile/template.csv').status_code, 200)
            self.assertEqual(client.post('/api/trade-reconcile/preview', json=data).status_code, 403)
            preview = client.post('/api/trade-reconcile/preview', json=data, headers=headers)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(json.loads(file.read_text()), START)
            confirmation = {'token': preview.json['token'], 'data': data}
            stale = {'token': 'wrong', 'data': data}
            url = '/api/trade-reconcile/confirm'
            self.assertEqual(client.post(url, json=stale, headers=headers).status_code, 409)
            self.assertEqual(client.post(url, json=confirmation, headers=headers).status_code, 200)
            self.assertEqual(client.post(url, json=confirmation, headers=headers).status_code, 200)
            applied = json.loads(file.read_text())
            self.assertEqual((applied['cash'], len(applied['reconciled_trades'])), (3095.0, 1))
            self.assertEqual(len(client.get('/api/trades').json['rows']), 1)

    def test_confirmed_fill_advances_alert_with_plan_price(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'config.json').write_text(json.dumps(START))
            at = datetime.fromisoformat('2024-06-17T09:46:30+08:00')
            alert_id = 'c' * 20
            signal = {'id': alert_id, 'code': '600000', 'name': '合成标的', 'title': '试验提醒',
                      'trigger': 'v1_entry', 'reason': '测试通过', 'price': 10.07, 'level': 10.05,
                      'category': 'add', 'shares': 100}
            rows = capture_rows([], [signal], {'600000': {'source_timestamp': at.isoformat()}},
                                {}, now=at, source='合成测试', config_hash='example')
            persist_cycle(root / 'output', rows, now=at)
            data = {'events': [
                {'executed_at': '2024-06-17T10:15:00', 'fill_id': 'partial', 'code': '600000',
                 'side': 'buy', 'quantity': 50, 'price': 10.08, 'fee': 5, 'alert_id': alert_id},
            ], 'broker': {'asof': '2024-06-17T15:05:01', 'available_cash': 491,
                          'positions': {'600000': {'quantity': 250, 'available_quantity': 200}}}}
            client = create_app(root).test_client()
            headers = {'X-CSRF-Token': CSRF}
            response = client.post('/api/trade-reconcile/preview', json=data, headers=headers)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json['events'][0]['adverse_slippage'], 1.5)
            confirmation = {'token': response.json['token'], 'data': data}
            self.assertEqual(client.post('/api/trade-reconcile/confirm', json=confirmation, headers=headers).status_code, 200)
            result = client.get('/api/alert-journal').json['rows'][0]
            self.assertEqual((result['status'], result['filled_quantity']), ('partial', 50))


if __name__ == '__main__':
    main()
