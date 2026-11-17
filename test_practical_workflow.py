from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from action_center import build_actions, CN
from alert_journal import capture_rows, persist_cycle
from portfolio_test_fixture import account_fixture
from risk_engine import compute
import t_runtime


class PracticalWorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory(); self.addCleanup(tmp.cleanup); self.output = Path(tmp.name)
        self.now = datetime(2026, 11, 17, 10, 30, tzinfo=CN)
        self.cfg = account_fixture()
        self.cfg.update(cash=1000, positions=[{'code':'159971','type':'etf','name':'合成ETF',
                       'quantity':1000,'cost':1.,'baseline_price':1.}])
        self.signal = {'id':'a'*20,'code':'159971','name':'合成ETF','title':'固定退出复核',
                       'category':'reduce','trigger':'stop','shares':500,'held':True,
                       'priority':'high','price':1.,'level':1.1,'reason':'合成固定退出'}
        self.book = {'availability':{},'cycles':[], 'fee_model':{
            'etf':{'commission_per_10k':5,'minimum_commission':5}}}

    def state(self, now):
        return {'mode':'live','market_session':True,'calendar_ok':True,'stale':False,
                'fetch_finished_at':now.isoformat(),'risk':{'valid':True,'equity':2000},
                'quote_health':{'all_held_usable':True,'block_trade_signals':False},
                'quotes':[{'code':'159971','price':1.,'source_timestamp':now.isoformat()}],
                'trade_signals':[self.signal]}

    def test_partial_sale_reappears_only_with_newly_confirmed_remaining_inventory(self):
        rows = capture_rows([], [self.signal], {'159971':{'source_timestamp':self.now.isoformat()}},
            {}, now=self.now, source='synthetic', config_hash='test')
        persist_cycle(self.output, rows, now=self.now)
        fill_at = self.now + timedelta(minutes=1)
        self.cfg['reconciled_trades'] = [{'fill_id':'actual-1','alert_id':self.signal['id'],
            'code':'159971','side':'sell','quantity':100,'price':1.,'executed_at':fill_at.isoformat()}]
        self.cfg['positions'][0]['quantity'] = 900
        now = self.now + timedelta(minutes=2)
        data = build_actions(self.cfg, self.state(now), self.book, output=self.output, now=now)
        card = data['cards'][0]
        self.assertTrue(card['followup']); self.assertIsNone(card['quantity'])
        self.assertEqual(card['remaining_target'], 400)
        self.book['availability']['159971'] = {'date':str(now.date()),'base_quantity':900,
            'available_quantity':900,'confirmed_at':now.isoformat()}
        ready = build_actions(self.cfg, self.state(now), self.book, output=self.output, now=now)
        self.assertEqual(ready['cards'][0]['quantity'], 400)
        self.assertNotEqual(card['alert_id'], ready['cards'][0]['alert_id'])
        again = build_actions(self.cfg, {**self.state(now), 'trade_signals':ready['signals']},
                              self.book, output=self.output, now=now)
        self.assertEqual(again['cards'][0]['alert_id'], ready['cards'][0]['alert_id'])
        self.assertEqual(again['cards'][0]['quantity'], 400)

    def test_actual_t_closeout_survives_quote_failure_but_is_not_an_order(self):
        now = self.now.replace(hour=14, minute=50)
        cycle = {'id':'actual-cycle','code':'159971','name':'合成ETF','kind':'etf','status':'open',
                 'date':str(now.date()),'base_quantity':1000,'quantity':400,'sell_price':1.1,
                 'sell_fee':5.,'buyback_quantity':100,'buyback_cost':95.,
                 'fills':[{'quantity':100,'price':.95,'fee':5.}]}
        self.book['cycles'] = [cycle]
        state = self.state(now); state.update(quotes=[],trade_signals=[],risk={'valid':False})
        state['quote_health'] = {'block_trade_signals':True,'all_held_usable':False}
        data = build_actions(self.cfg, state, self.book, output=self.output, now=now)
        self.assertFalse(data['valid']); self.assertEqual(len(data['closeouts']), 1)
        signal = data['closeouts'][0]
        self.assertIsNone(signal['shares']); self.assertIsNone(signal['price'])
        self.assertEqual(signal['closeout']['remaining_quantity'], 300)
        self.assertEqual(signal['closeout']['net_cash_actual'], 335.)
        rows = capture_rows([], [signal], {}, {}, now=now, source='none', config_hash='test')
        self.assertEqual(len(rows), 1)
        sent = []
        persist_cycle(self.output, rows, now=now, notify=True, send=lambda r: sent.append(r) or True)
        later = now + timedelta(minutes=1)
        persist_cycle(self.output, capture_rows([], [signal], {}, {}, now=later, source='none', config_hash='test'),
                      now=later, notify=True, send=lambda r: sent.append(r) or True)
        self.assertEqual(len(sent), 1)
        closed = build_actions(self.cfg, self.state(now.replace(hour=15,minute=0)),
                               self.book, output=self.output, now=now.replace(hour=15,minute=0))
        self.assertEqual(closed['closeouts'], [])

    def test_available_cash_is_separate_from_frozen_assets_and_receipt_feeds_inventory(self):
        self.cfg.setdefault('strategy_v1', {})['enabled'] = False
        original = compute(self.cfg, {'159971':1.}, fresh=True)
        self.cfg.update(cash=800., frozen_cash=200.)
        frozen = compute(self.cfg, {'159971':1.}, fresh=True)
        self.assertEqual(frozen['equity'], original['equity'])
        self.assertEqual(frozen['cash'], 800.)
        self.assertEqual(frozen['frozen_cash'], 200.)
        from trade_reconciliation import position_signature
        self.cfg['trade_reconciliation'] = {'broker_asof':self.now.isoformat(),
            'available_cash':800., 'frozen_cash':200.,
            'position_signature':position_signature(self.cfg['positions']),
            'positions':{'159971':{'quantity':1000,'available_quantity':700}}}
        book = t_runtime.ledger(self.output, self.cfg, now=self.now)
        self.assertEqual(book['availability']['159971']['available_quantity'], 700)
        self.assertEqual(book['availability']['159971']['source'], 'broker_balance')
        self.assertFalse((self.output/'t_ledger.json').exists())

    def test_unvalidated_t_signal_remains_visible_as_observation_on_repeated_reads(self):
        self.book['revision'] = 'same'
        state = self.state(self.now)
        state['trade_signals'] = [{**self.signal, 'id':'b'*20, 'category':'t',
            'trigger':'sell_ready', 'tradable':True, 'priority':'normal'}]
        state['t_observe'] = {'ledger_revision':'same','asof':self.now.isoformat(),
            'rows':[{'code':'159971','minute_source_at':self.now.isoformat(),
                     'minute_asof':(self.now-timedelta(minutes=1)).isoformat()}]}
        data = build_actions(self.cfg, state, self.book, output=self.output, now=self.now)
        self.assertEqual(data['cards'][0]['validation_status'], 'pending')
        self.assertIsNone(data['cards'][0]['quantity'])
        state['trade_signals'] = data['signals']
        again = build_actions(self.cfg, state, self.book, output=self.output, now=self.now)
        self.assertEqual(again['cards'][0]['alert_id'], data['cards'][0]['alert_id'])
        self.assertEqual(again['cards'][0]['label'], '待验证观察')


if __name__ == '__main__': unittest.main()
