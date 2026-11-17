from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

import account_balance
from trade_reconciliation import CN, close_snapshot


NOW = datetime(2026, 11, 17, 16, 0, tzinfo=CN)
START = {'cash': 1000., 'baseline_equity': 3000., 'max_additional_loss': 1000.,
         'watchlist': [], 'positions': [{'code': '159971', 'name': '合成ETF', 'type': 'etf',
                                       'quantity': 200, 'cost': 1., 'baseline_price': 1.}],
         'trade_plans': {'159971': {'stop_loss': .9, 'confirmed': False}}}


def body(now=NOW):
    return {'mode': 'balance_only', 'no_activity': True, 'broker': {
        'asof': now.isoformat(), 'available_cash': 800, 'withdrawable_cash': 700,
        'frozen_cash': 200, 'total_assets': 1200,
        'positions': {'159971': {'quantity': 200, 'available_quantity': 100}},
    }}


class AccountBalanceTests(TestCase):
    def test_freeze_and_unfreeze_conserve_cash_and_preserve_plans_and_baseline(self):
        cfg = deepcopy(START)
        updated, preview = account_balance.prepare(cfg, body(), now=NOW)
        self.assertEqual(cfg, START)
        self.assertEqual(preview['differences'], [])
        self.assertEqual((updated['cash'], updated['frozen_cash']), (800., 200.))
        self.assertEqual(updated['positions'], START['positions'])
        self.assertEqual(updated['trade_plans'], START['trade_plans'])
        self.assertEqual(updated['baseline_equity'], START['baseline_equity'])
        self.assertNotIn('reconciled_trades', updated)
        self.assertNotIn('reconciled_cashflows', updated)
        self.assertEqual(preview['receipt']['positions']['159971']['available_quantity'], 100)
        snapshot = close_snapshot(updated)
        self.assertEqual(snapshot['frozen_cash'], 200.)
        self.assertEqual(snapshot['source'], '无新增流水余额核对')
        self.assertEqual(updated['account_balance_history'][0]['before']['available_cash'], 1000.)
        later = NOW + timedelta(minutes=1)
        data = body(later)
        data['broker'].update(available_cash=1000, frozen_cash=0)
        unfrozen, preview = account_balance.prepare(updated, data, now=later)
        self.assertEqual(preview['differences'], [])
        self.assertEqual((unfrozen['cash'], unfrozen['frozen_cash']), (1000., 0.))
        self.assertEqual(len(unfrozen['account_balance_history']), 2)

    def test_unexplained_cash_and_position_changes_do_not_propose_updates(self):
        for mutate in (
            lambda b: b['broker'].update(available_cash=801),
            lambda b: b['broker']['positions']['159971'].update(quantity=100),
            lambda b: b['broker']['positions'].clear(),
            lambda b: b['broker']['positions'].update({'588170': {'quantity': 100, 'available_quantity': 100}}),
        ):
            data = body(); mutate(data)
            updated, preview = account_balance.prepare(START, data, now=NOW)
            self.assertTrue(preview['differences'])
            self.assertEqual(updated, START)
            self.assertNotIn('receipt', preview)

    def test_time_confirmation_and_sellability_must_be_current_and_complete(self):
        for mutate in (
            lambda b: b.update(no_activity=False),
            lambda b: b.update(events=[{'side': 'buy'}]),
            lambda b: b['broker'].update(asof=(NOW-timedelta(days=1)).isoformat()),
            lambda b: b['broker'].update(asof=(NOW+timedelta(seconds=1)).isoformat()),
            lambda b: b['broker'].update(withdrawable_cash=900),
            lambda b: b['broker'].update(frozen_cash=-1),
            lambda b: b['broker']['positions']['159971'].update(available_quantity=201),
            lambda b: b['broker']['positions']['159971'].pop('available_quantity'),
        ):
            data = body(); mutate(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                account_balance.prepare(START, data, now=NOW)
        for anchor in ('receipt', 'trade', 'screenshot'):
            cfg = deepcopy(START)
            kwargs = {}
            later = (NOW+timedelta(seconds=1)).isoformat()
            if anchor == 'receipt': cfg['trade_reconciliation'] = {'broker_asof': later}
            if anchor == 'trade': cfg['reconciled_trades'] = [{'executed_at': later}]
            if anchor == 'screenshot': kwargs['previous_snapshot'] = {'asof': later}
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                account_balance.prepare(cfg, body(), now=NOW+timedelta(seconds=2), **kwargs)

    def test_frozen_changes_invalidate_old_receipt(self):
        updated, _ = account_balance.prepare(START, body(), now=NOW)
        updated['frozen_cash'] += 1
        self.assertIsNone(close_snapshot(updated))

    def test_web_preview_requires_security_and_rechecks_concurrent_changes(self):
        from web_dashboard import CSRF, create_app
        with TemporaryDirectory() as folder:
            root = Path(folder); path = root/'config.json'; path.write_text(json.dumps(START))
            client = create_app(root).test_client(); headers = {'X-CSRF-Token': CSRF}
            now = datetime.now(CN).replace(microsecond=0)
            data = body(now)
            endpoint = '/api/account-balance'
            self.assertEqual(client.post(endpoint+'/preview', json=data).status_code, 403)
            preview = client.post(endpoint+'/preview', json=data, headers=headers)
            self.assertEqual(preview.status_code, 200, preview.json)
            self.assertTrue(preview.json['token'])
            self.assertEqual(json.loads(path.read_text()), START)
            confirmation = {'data': data, 'token': preview.json['token']}
            changed = deepcopy(START); changed['cash'] = 999.
            path.write_text(json.dumps(changed))
            self.assertEqual(client.post(endpoint+'/confirm', json=confirmation, headers=headers).status_code, 409)
            path.write_text(json.dumps(START))
            changed_data = deepcopy(confirmation); changed_data['data']['broker'].update(available_cash=700, frozen_cash=300)
            self.assertEqual(client.post(endpoint+'/confirm', json=changed_data, headers=headers).status_code, 409)
            confirmed = client.post(endpoint+'/confirm', json=confirmation, headers=headers)
            self.assertEqual(confirmed.status_code, 200, confirmed.json)
            cfg = json.loads(path.read_text())
            self.assertEqual((cfg['cash'], cfg['frozen_cash']), (800., 200.))
            self.assertEqual(cfg['baseline_equity'], START['baseline_equity'])


if __name__ == '__main__':
    main()
