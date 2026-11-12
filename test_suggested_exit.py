"""Deterministic drafts must never change saved prices or use cost percentages."""
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest
import pandas as pd
from suggested_exit import build_exit_suggestion

CN=ZoneInfo('Asia/Shanghai')

class SuggestedExitTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,11,16,tzinfo=CN)
        ds=pd.bdate_range(end='2026-11-11',periods=100)
        prices=[20+i*.1 for i in range(100)]
        self.history=pd.DataFrame({'date':ds,'open':prices,'close':prices,
                                   'high':[p+.2 for p in prices],'low':[p-.2 for p in prices],
                                   'volume':[10000]*100})
        self.days=[str(d.date()) for d in pd.bdate_range(start='2025-01-01',end='2026-12-31')]
        self.position={'code':'600406','type':'stock','quantity':300,'cost':50.}
    def calc(self,history=None,position=None,**kwargs):
        return build_exit_suggestion(self.history if history is None else history,
                                     self.position if position is None else position,
                                     trading_dates=kwargs.pop('trading_dates',self.days),
                                     now=kwargs.pop('now',self.now),**kwargs)
    def test_fallback_is_explicit_not_cost_percent(self):
        r=self.calc();p=r['plan']
        self.assertTrue(r['valid'],r);self.assertTrue(r['evidence']['fallback'])
        self.assertTrue(r['adoptable']);self.assertEqual(r['status'],'ready')
        self.assertIn('10日低点',p['suggestion_method'])
        self.assertEqual(p['entry_price'],29.9)
        self.assertEqual(self.calc(position={**self.position,'cost':1000})['plan'],p)
        self.assertTrue(p['legacy_exit_only']);self.assertFalse(p['setup_verified'])
        for forbidden in ('buy_date','actual_entry','initial_quantity'):
            self.assertNotIn(forbidden,p)
        self.assertEqual(p['reference_kind'],'remaining_position_snapshot')
    def test_recent_confirmed_low_used_with_two_right_bars(self):
        x=self.history.copy();x.loc[95,'low']=27.5
        r=self.calc(x)
        self.assertTrue(r['valid'],r);self.assertFalse(r['evidence']['fallback'])
        self.assertEqual(r['evidence']['support_date'],str(x.date.iloc[95].date()))
        self.assertEqual(len(r['evidence']['confirmation_dates']),2)
        self.assertLessEqual(r['plan']['stop_loss'],27.5-.2*r['technical']['atr14']+1e-9)
    def test_unconfirmed_last_or_penultimate_low_not_selected(self):
        x=self.history.copy();x.loc[95,'low']=27.5;x.loc[98,'low']=26.0
        r=self.calc(x)
        self.assertEqual(r['evidence']['support_date'],str(x.date.iloc[95].date()))
    def test_most_recent_confirmed_low_beats_older_lower_low(self):
        x=self.history.copy();x.loc[85,'low']=24.5;x.loc[95,'low']=27.5
        r=self.calc(x);self.assertEqual(r['evidence']['support_price'],27.5)
    def test_only_recent_twenty_bars_search_and_higher_than_close_rejected(self):
        x=self.history.copy();x.loc[70,'low']=10.
        self.assertTrue(self.calc(x)['evidence']['fallback'])
        # A confirmed low above the reference must not invent a stop above it.
        x.loc[97,'low']=29.;x.loc[99,['open','close','low']]=[25.,25.,24.8]
        r=self.calc(x);self.assertTrue(r['valid']);self.assertLess(r['plan']['stop_loss'],25)
    def test_stock_and_etf_round_to_real_ticks_and_fixed_targets(self):
        for kind,tick in [('stock',.01),('etf',.001)]:
            r=self.calc(position={**self.position,'type':kind});p=r['plan'];d=p['entry_price']-p['stop_loss']
            for key in ('stop_loss','take_profit_1','take_profit_2'):
                self.assertAlmostEqual(p[key]/tick,round(p[key]/tick),places=7)
            self.assertGreaterEqual(p['take_profit_1']+1e-9,p['entry_price']+2*d)
            self.assertGreaterEqual(p['take_profit_2']+1e-9,p['entry_price']+3*d)
    def test_wide_risk_warns_without_tightening_structure(self):
        x=self.history.copy();x.loc[95,'low']=20.
        r=self.calc(x)
        self.assertTrue(r['valid']);self.assertGreater(r['stop_distance_pct'],6)
        self.assertFalse(r['adoptable']);self.assertEqual(r['status'],'risk_mismatch')
        self.assertIn('暂无适合1～2周',r['reason'])
        self.assertGreater(r['planned_stress'],150)
        self.assertLess(r['plan']['stop_loss'],20.)
        self.assertTrue(any('6%' in w for w in r['warnings']))
    def test_saved_prices_execution_and_initial_stop_never_mutated(self):
        p={'entry_price':26.,'stop_loss':25.,'take_profit_1':28.,'take_profit_2':29.,'initial_stop_loss':24.,'buy_date':'2026-11-01','actual_entry':25.9,'tp1_sold_quantity':100,'custom':'keep'}
        original=deepcopy(p);r=self.calc(existing_plan=p)
        self.assertTrue(r['valid']);self.assertTrue(r['preserved']);self.assertEqual(r['plan'],original);self.assertEqual(p,original)
        self.assertGreater(r['stop_distance_pct'],6)
        self.assertTrue(r['adoptable']);self.assertEqual(r['status'],'preserved')
    def test_raised_saved_stop_not_replaced_and_breach_explicit(self):
        p={'entry_price':20.,'stop_loss':30.,'take_profit_1':31.,'take_profit_2':33.,'initial_stop_loss':19.,'tp1_sold_quantity':100}
        r=self.calc(existing_plan=p)
        self.assertEqual(r['plan']['stop_loss'],30.);self.assertTrue(r['preserved'])
        self.assertTrue(any('已触及' in w for w in r['warnings']))
        self.assertEqual(r['planned_stress'],20.)
    def test_missing_history_keeps_existing_plan_and_marks_technical_pending(self):
        p={'entry_price':20.,'stop_loss':19.,'take_profit_1':22.,'take_profit_2':23.}
        r=self.calc(pd.DataFrame(),existing_plan=p)
        self.assertTrue(r['preserved']);self.assertTrue(r['valid']);self.assertEqual(r['plan'],p);self.assertFalse(r['technical']['valid'])
        self.assertFalse(self.calc(pd.DataFrame())['valid'])
    def test_current_partial_daily_cannot_change_suggestion(self):
        morning=self.now.replace(hour=10)
        r=self.calc(now=morning)
        before=self.history.iloc[:-1].copy()
        self.assertEqual(r['plan'],self.calc(before,now=morning)['plan'])
        self.assertEqual(r['asof'],'2026-11-10')
    def test_no_future_stale_missing_calendar_or_insufficient_history(self):
        future=self.history.copy();future.loc[len(future)]=[pd.Timestamp('2026-11-12'),30,30,31,29,1000]
        for history,days in [(future,self.days),(self.history.iloc[:-1],self.days),(self.history,[]),(self.history,self.days[:-300]),(self.history.iloc[-59:],self.days)]:
            r=self.calc(history,trading_dates=days)
            self.assertFalse(r['valid'],r);self.assertEqual(r['plan'],{})
            self.assertFalse(r['adoptable']);self.assertEqual(r['status'],'pending')
    def test_bad_numeric_geometry_duplicates_and_flat_atr_fail_closed(self):
        cases=[]
        for col,val in [('close',float('nan')),('low',-1),('high',1)]:
            x=self.history.copy();x.loc[50,col]=val;cases.append(x)
        x=self.history.copy();x.loc[50,'date']=x.loc[49,'date'];cases.append(x)
        for x in cases:self.assertFalse(self.calc(x)['valid'])
        flat=self.history.copy();flat[['open','close','low','high']]=20.
        self.assertFalse(self.calc(flat)['valid'])
    def test_invalid_quantity_not_granted_plan_pressure(self):
        for qty in (0,True,float('nan'),1.5):
            r=self.calc(position={**self.position,'quantity':qty});self.assertFalse(r['valid']);self.assertIsNone(r['planned_stress'])
    def test_saved_bad_order_not_silently_repaired(self):
        p={'entry_price':20.,'stop_loss':25.,'take_profit_1':22.,'take_profit_2':23.}
        r=self.calc(existing_plan=p);self.assertFalse(r['valid']);self.assertTrue(r['preserved']);self.assertEqual(r['plan'],p)

if __name__=='__main__':unittest.main()
