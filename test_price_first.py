from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
import unittest
import pandas as pd
from price_review import review_price
from strategy_setup import exit_review
from swing_analysis import analyze_swing
from trade_signals import build_signals

class PriceFirstTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,11,10,tzinfo=ZoneInfo('Asia/Shanghai'))
        dates=pd.bdate_range(end='2026-11-10',periods=140);closes=[10+i*.1 for i in range(140)]
        self.hist=pd.DataFrame({'date':dates,'open':closes,'close':closes,'high':[p+.2 for p in closes],'low':[p-.2 for p in closes],'volume':[100]*140,'amount':[200000000]*140})
        self.calendar=[str(x.date()) for x in pd.bdate_range(start='2026-01-01',end='2026-12-31')]
        self.cfg={'strategy_v1':{'enabled':True,'exit_mode':'price_first'},'trade_plans':{},'positions':[]}
        self.pos={'code':'600406','type':'stock','quantity':100,'cost':20}
    def test_no_date_still_reviews_price_without_inventing_stop(self):
        r=exit_review(self.cfg,self.pos,{},15,self.hist,self.calendar,self.now)
        self.assertEqual(r['trigger'],'structure_support');self.assertEqual(r['action'],'review')
        self.assertIsNone(r['quantity']);self.assertIsNone(r['stop_loss'])
        self.assertFalse(any('买入日期待补' in x for x in r['pending']))
    def test_no_calendar_exit_on_day_ten_or_later(self):
        plan={'buy_date':'2026-10-01','actual_entry':20,'initial_stop_loss':19}
        r=exit_review(self.cfg,self.pos,plan,23.8,self.hist,self.calendar,self.now)
        self.assertGreater(r['days_held'],10);self.assertEqual(r['action'],'hold');self.assertIsNone(r['trigger'])
    def test_fixed_stop_still_has_priority(self):
        r=exit_review(self.cfg,self.pos,{'stop_loss':19},18,self.hist,self.calendar,self.now)
        self.assertEqual(r['trigger'],'stop');self.assertEqual(r['quantity'],100)
    def test_stale_history_or_unverified_price_never_produces_structure_signal(self):
        self.assertFalse(review_price(self.hist.iloc[:-1],15,now=self.now,trading_dates=self.calendar)['valid'])
        self.assertFalse(review_price(self.hist,None,now=self.now,trading_dates=self.calendar)['valid'])
    def test_resistance_touch_is_review_not_sell(self):
        r=review_price(self.hist,25,now=self.now,trading_dates=self.calendar)
        self.assertEqual(r['trigger'],'resistance_test');self.assertEqual(r['action'],'review')
    def test_fresh_structure_alert_has_no_sell_quantity(self):
        r=exit_review(self.cfg,self.pos,{},15,self.hist,self.calendar,self.now)
        cfg={**self.cfg,'positions':[self.pos]}
        q={'600406':{'price':15,'source_timestamp':self.now.isoformat(),'strategy_exit':r}}
        signals=build_signals(cfg,q,{},trading_day=self.now.date(),market_session=True,calendar_ok=True,now=self.now)
        self.assertEqual(len(signals),1);self.assertEqual(signals[0]['trigger'],'structure_support');self.assertIsNone(signals[0]['shares'])
        q['600406']['source_timestamp']='2026-11-10T15:00:00+08:00'
        self.assertEqual(build_signals(cfg,q,{},trading_day=self.now.date(),market_session=True,calendar_ok=True,now=self.now),[])
    def test_held_analysis_does_not_present_new_setup_as_fixed_exit(self):
        cfg={**self.cfg,'positions':[self.pos]}
        r=analyze_swing(self.hist,{}, {},cfg,'600406',position=self.pos,quote={'price':15,'source_timestamp':self.now.isoformat()},benchmark_hist=self.hist,trading_dates=self.calendar,now=self.now)
        self.assertIsNone(r['levels']['stop']);self.assertEqual(r['label'],'量价结构观察')
        self.assertEqual(r['blockers_title'],'持仓退出管理待核验')

if __name__=='__main__':unittest.main()
