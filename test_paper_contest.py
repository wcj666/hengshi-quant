"""Offline virtual settlement/cost tests, with fictional money and no broker I/O."""
from copy import deepcopy
from datetime import datetime
import unittest
from paper_contest import initialize,execute,observe,fees,sellable,validate_state,preview_trade,CN,fixed_rules_hash
from strategy_defaults import initial_v1_rules
from v1_backtest import DEFAULT_COSTS

DAYS=['2026-11-12','2026-11-13','2026-11-16','2026-11-17','2026-11-18','2026-11-19','2026-11-20']


def account(positions=None):
    positions=positions or []
    value=sum(p['quantity']*p['screenshot_price'] for p in positions)
    opening={'asof':'2026-11-12T15:07:00+08:00','available_cash':100000.-value,
             'total_assets':100000.,'total_market_value':value,'broker_total_pnl':-1234.56,'positions':positions}
    return initialize(opening,DAYS,'2026-11-13','2026-11-19',now=datetime(2026,11,12,18,tzinfo=CN))


def quote(now,price=10.,bid=9.99,ask=10.):
    return {'name':'测试证券','price':price,'bid1':bid,'ask1':ask,'bid1_shares':100000,'ask1_shares':100000,
            'source_timestamp':now.isoformat(),'upper_limit':11.,'lower_limit':9.,
            'open':9.99,'limit_status_verified':True,'at_limit_up':False,'at_limit_down':False}


def plan(state):
    values={'trigger_price':10.,'entry_price':10.1,'stop_loss':9.95,'take_profit_1':10.4,'take_profit_2':10.55}
    state['plans']['600406']={**values,'strategy_version':'1.0','setup_verified':True,
        'legacy_exit_only':False,'signal_date':'2026-11-12','valid_on':'2026-11-13','sector':'测试行业',
        'review':{k:True for k in ['fundamentals_ok','event_clear','corporate_action_clear','resistance_clear']},
        'setup':{'eligible':True,'signal_date':'2026-11-12','levels':values}}


class PaperContestTests(unittest.TestCase):
    def test_opening_snapshot_preserved_and_prior_loss_not_contest_loss(self):
        p={'code':'600406','name':'测试证券','type':'stock','quantity':200,'available_quantity':100,
           'cost':15.,'screenshot_price':10.,'market_value':2000.,'broker_floating_pnl':-1005.}
        state=account([p]);self.assertEqual(state['opening_snapshot']['positions'],[p])
        self.assertEqual(state['last_equity']-state['initial_equity'],0)
        self.assertEqual(state['positions']['600406']['cost'],15.)
        self.assertEqual(state['positions']['600406']['contest_basis'],10.)

    def test_frozen_rules_and_cash_cannot_be_silently_rewritten(self):
        state=account();state['fees']['commission_rate']=0
        with self.assertRaises(ValueError):validate_state(state)
        state=account();state['cash']+=100
        with self.assertRaises(ValueError):validate_state(state)

    def test_new_and_previous_ledgers_share_defaults_without_rewriting_previous_rules(self):
        state=account();previous=deepcopy(state)
        self.assertEqual(state['policy'],initial_v1_rules())
        for key,cost in DEFAULT_COSTS.items():
            paper_key='book_fraction' if key=='prior_minute_participation' else key
            self.assertEqual(state['fees'][paper_key],cost)
        for key in ('account_risk_budget','max_daily_opens','enabled','version'):
            previous['policy'].pop(key)
        previous['fixed_rules_hash']=fixed_rules_hash(previous)
        before=deepcopy(previous)
        validate_state(previous)
        self.assertEqual(previous,before)

    def test_opening_snapshot_requires_consistent_cash_value_and_asof(self):
        snapshot=account()['opening_snapshot']
        now=datetime(2026,11,12,18,tzinfo=CN)
        for invalid in (
            dict(snapshot, available_cash=-1),
            dict(snapshot, total_assets=0.),
            dict(snapshot, total_market_value=1.),
            dict(snapshot, asof='2026-11-12T15:07:00'),
            dict(snapshot, asof='2026-11-13T09:30:00+08:00'),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                initialize(invalid,DAYS,'2026-11-13','2026-11-19',now=now)

    def test_provider_book_prices_and_lot_units_are_not_last_price(self):
        from portfolio_quotes import _parse_tencent_quotes
        fields=['']*50;fields[1]='测试证券';fields[2]='600406';fields[3]='10';fields[4]='9.9'
        fields[9]='9.99';fields[10]='123';fields[19]='10.01';fields[20]='456'
        fields[30]='20261113100000';fields[47]='10.89';fields[48]='8.91'
        feed,_=_parse_tencent_quotes('v_sh600406="'+'~'.join(fields)+'";',{('stock','600406')})
        book=feed[('stock','600406')][4]
        self.assertEqual((book['bid1'],book['bid1_shares']),(9.99,12300))
        self.assertEqual((book['ask1'],book['ask1_shares']),(10.01,45600))

    def test_before_start_after_end_lunch_and_weekend_cannot_fill(self):
        state=account();plan(state)
        for now in [datetime(2026,11,12,14,tzinfo=CN),datetime(2026,11,13,12,tzinfo=CN),
                    datetime(2026,11,14,10,tzinfo=CN),datetime(2026,11,20,10,tzinfo=CN)]:
            before=deepcopy(state)
            with self.assertRaises(ValueError):execute(state,'600406','buy',800,{'600406':quote(now)},now,'test')
            self.assertEqual(state,before)

    def test_round_trip_fee_slippage_cash_and_net_r(self):
        state=account();plan(state);buy=datetime(2026,11,13,10,tzinfo=CN)
        t=execute(state,'600406','buy',800,{'600406':quote(buy)},buy,'synthetic valid setup')
        self.assertEqual(t['price'],10.01);self.assertEqual(t['fee']['total'],5.08)
        self.assertEqual(state['cash'],91986.92)
        self.assertEqual(observe(state,{'600406':quote(buy)},buy)['net_pnl'],-13.08)
        sell=datetime(2026,11,16,10,tzinfo=CN)
        t=execute(state,'600406','sell',800,{'600406':quote(sell,10.2,10.19,10.2)},sell,'virtual exit')
        self.assertEqual(t['price'],10.18);self.assertEqual(t['fee']['total'],9.15)
        self.assertEqual(state['cash'],100121.77);self.assertEqual(state['closed_trades'][0]['net_pnl'],121.77)
        self.assertAlmostEqual(state['closed_trades'][0]['net_r'],121.77/68,places=4)

    def test_new_virtual_inventory_cannot_sell_same_day(self):
        state=account();plan(state);now=datetime(2026,11,13,10,tzinfo=CN);qs={'600406':quote(now)}
        execute(state,'600406','buy',800,qs,now,'synthetic setup');before=deepcopy(state)
        with self.assertRaises(ValueError):execute(state,'600406','sell',800,qs,now,'same day')
        self.assertEqual(state,before)

    def test_unknown_opening_frozen_inventory_does_not_self_unlock(self):
        p={'code':'600406','name':'测试证券','type':'stock','quantity':200,'available_quantity':100,
           'cost':15.,'screenshot_price':10.,'market_value':2000.}
        state=account([p]);now=datetime(2026,11,13,10,tzinfo=CN)
        with self.assertRaises(ValueError):execute(state,'600406','sell',200,{'600406':quote(now)},now,'unknown availability')
        execute(state,'600406','sell',100,{'600406':quote(now)},now,'known inventory only')
        self.assertEqual(sellable(state['positions']['600406'],datetime(2026,11,16,10,tzinfo=CN)),0)

    def test_stale_quotes_missing_limits_and_thin_books_never_fill(self):
        now=datetime(2026,11,13,10,tzinfo=CN)
        for changes in [{'source_timestamp':'2026-11-13T09:50:00+08:00'},
                        {'limit_status_verified':False},{'ask1_shares':100},{'ask1':None},
                        {'price':11.,'at_limit_up':True}]:
            state=account();plan(state);before=deepcopy(state);q={**quote(now),**changes}
            with self.assertRaises((ValueError,TypeError)):execute(state,'600406','buy',800,{'600406':q},now,'test')
            self.assertEqual(state,before)

    def test_moved_above_cap_not_filled_at_a_historical_price(self):
        state=account();plan(state);now=datetime(2026,11,13,10,tzinfo=CN)
        with self.assertRaises(ValueError):execute(state,'600406','buy',800,{'600406':quote(now,10.1,10.09,10.1)},now,'gap over cap after slip')
        self.assertFalse(state['trades'])

    def test_buy_without_verified_setup_remains_blocked(self):
        state=account();now=datetime(2026,11,13,10,tzinfo=CN)
        with self.assertRaises(ValueError):execute(state,'600406','buy',800,{'600406':quote(now)},now,'no invented entry')
        self.assertFalse(state['positions'])

    def test_etf_fee_model_has_no_stock_stamp_or_transfer(self):
        state=account();self.assertEqual(fees(state,'etf','sell',10000),{'commission':5.,'stamp_tax':0.,'transfer':0.,'total':5.})

    def test_preview_sell_uses_same_fill_cost_and_inventory_without_writing_trade(self):
        holding = {'code':'600406','name':'测试证券','type':'stock','quantity':200,'available_quantity':100,
                   'cost':15.,'screenshot_price':10.,'market_value':2000.}
        state = account([holding])
        now = datetime(2026, 11, 13, 10, tzinfo=CN)
        quotes = {'600406': quote(now)}
        original = deepcopy(state)
        preview = preview_trade(state, '600406', 'sell', 100, quotes, now, '模拟减仓预览')
        self.assertEqual(state, original)
        self.assertTrue(preview['allowed'])
        self.assertEqual(preview['fillable_quantity'], 100)
        self.assertEqual((preview['position_before'], preview['position_after']), (200, 100))
        self.assertEqual((preview['sellable_before'], preview['sellable_after']), (100, 0))
        actual = execute(state, '600406', 'sell', 100, quotes, now, '模拟减仓')
        self.assertEqual((preview['price'], preview['fee']), (actual['price'], actual['fee']))
        self.assertEqual(preview['cash_after'], state['cash'])
        self.assertEqual(preview['quote_asof'], actual['source_timestamp'])
        self.assertEqual(preview['realized_contest_pnl'], actual['net_realized_contest_pnl'])

    def test_preview_buy_shows_t_plus_one_and_stale_or_incomplete_quotes_are_rejected(self):
        state = account()
        plan(state)
        now = datetime(2026, 11, 13, 10, tzinfo=CN)
        original = deepcopy(state)
        preview = preview_trade(state, '600406', 'buy', 800, {'600406':quote(now)}, now, '模拟买入预览')
        self.assertTrue(preview['allowed'])
        self.assertEqual(preview['position_after'], 800)
        self.assertEqual(preview['sellable_after'], 0)
        for changes, error in [({'source_timestamp':'2026-11-13T09:50:00+08:00'}, '源报价超过90秒'),
                               ({'ask1':None}, '最优档盘口价格'), ({'ask1_shares':100}, '最优档10%')]:
            blocked = preview_trade(state, '600406', 'buy', 800, {'600406':{**quote(now), **changes}}, now, '模拟预览')
            self.assertFalse(blocked['allowed'])
            self.assertEqual(blocked['fillable_quantity'], 0)
            self.assertIn(error, blocked['error'])
            self.assertNotIn('cash_after', blocked)
        self.assertEqual(state, original)

    def test_closing_mark_requires_observed_today_close(self):
        p={'code':'600406','name':'测试证券','type':'stock','quantity':100,'available_quantity':100,
           'cost':15.,'screenshot_price':10.,'market_value':1000.}
        state=account([p]);now=datetime(2026,11,13,15,20,tzinfo=CN)
        with self.assertRaises(ValueError):observe(state,{'600406':quote(now.replace(hour=14))},now,closing=True)
        q=quote(now.replace(minute=0),10.1,10.09,10.1)
        self.assertEqual(observe(state,{'600406':q},now,closing=True)['net_pnl'],10.)


if __name__=='__main__':unittest.main()
