"""Deterministic T gates; no claim of profitable alpha."""
from copy import deepcopy
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import unittest
from t_engine import build_t_observations
from t_costs import estimate_leg, roundtrip

class TEngineTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,11,10,30,tzinfo=ZoneInfo('Asia/Shanghai'));self.code='600406'
        self.cfg={'cash':90000,'strategy_v1':{'enabled':True},'t_settings':{'enabled':True},
                  'positions':[{'code':self.code,'name':'底仓','type':'stock','quantity':300}],
                  'trade_plans':{self.code:{'stop_loss':19.5}}}
        self.quote={self.code:{'price':21.,'source_timestamp':self.now.isoformat(),'at_limit_up':False,'at_limit_down':False,'limit_status_verified':True}}
        self.risk={'valid':True,'equity':100000,'existing_planned_stress':500,'available_new_risk':1000,
                   'strategy':{'exposure_pct':6.3,'account':{'initialized':True,'verified':True,'needs_reconcile':False,'paused':False,'drawdown':0}}}
        self.ledger={'availability':{self.code:{'date':'2026-11-11','base_quantity':300,'available_quantity':300}},'cycles':[]}
        self.prices=[20]*48+[20.8,21,21.1,21.2,21.3,21.4,21.5,21.4,21.3,21.2,21.1,21.0]
    def minutes(self,prices=None,now=None):
        now=now or self.now;prices=prices or self.prices;rows=[];amount=0.
        for i,p in enumerate(prices):
            amount+=p*10000
            rows.append({'timestamp':(now-timedelta(minutes=len(prices)-i)).isoformat(),'price':p,'cum_volume':(i+1)*100,'cum_amount':amount})
        return {self.code:{'valid':True,'source_timestamp':now.isoformat(),'rows':rows}}
    def call(self,**kw):
        return build_t_observations(kw.get('cfg',self.cfg),kw.get('quotes',self.quote),kw.get('risk',self.risk),kw.get('minutes',self.minutes()),kw.get('ledger',self.ledger),now=kw.get('now',self.now),market_session=kw.get('market_session',True),calendar_ok=kw.get('calendar_ok',True))
    def test_sell_condition_and_fixed_daily_id(self):
        a=self.call();b=self.call();row=a['rows'][0]
        self.assertEqual(row['phase'],'sell_ready');self.assertEqual(row['quantity'],100);self.assertGreaterEqual(row['gross_spread_estimate'],40)
        self.assertEqual(a['signals'][0]['id'],b['signals'][0]['id']);self.assertEqual(a['signals'][0]['category'],'t')
    def test_break_even_uses_product_tick_and_named_fee_basis(self):
        terms={'stock':{'commission_per_10k':5,'minimum_commission':5},
               'etf':{'commission_per_10k':1,'minimum_commission':0}}
        stock=roundtrip('stock',21,100,20,terms,[])
        etf=roundtrip('etf',1.29,100,20,terms,[])
        self.assertEqual(stock['break_even_gap'],.12)
        self.assertEqual(stock['break_even_buy_price'],20.88)
        self.assertEqual(etf['break_even_gap'],.001)
        self.assertEqual(etf['break_even_buy_price'],1.289)
        self.assertEqual(roundtrip('stock',21,100,20,{},[])['fee_basis'],['reserve','reserve'])
        self.assertEqual(estimate_leg('stock','buy',2100,terms,[
            {'kind':'stock','side':'buy','fee':7,'notional':2000},
            {'kind':'etf','side':'buy','fee':20,'notional':2100},
        ]),(7.35,'comparable_fills'))
    def test_fee_assumptions_can_only_tighten_signal_gate(self):
        book=deepcopy(self.ledger)
        book['fee_model']={'stock':{'commission_per_10k':5,'minimum_commission':.01}}
        row=self.call(ledger=book)['rows'][0]
        self.assertEqual(row['phase'],'sell_ready')
        self.assertEqual(row['fee_reserve'],20)
        self.assertLess(row['estimated_roundtrip_fee'],20)
        book['fee_model']['stock']['minimum_commission']=80
        result=self.call(ledger=book)
        self.assertEqual(result['signals'],[])
        self.assertGreater(result['rows'][0]['fee_reserve'],160)
    def test_unknown_sellable_stays_observation_only(self):
        d=self.call(ledger={'availability':{},'cycles':[]});r=d['rows'][0]
        self.assertEqual(r['phase'],'sell_watch');self.assertIsNone(r['quantity']);self.assertFalse(r['tradable'])
    def test_old_availability_is_not_reused(self):
        book=deepcopy(self.ledger);book['availability'][self.code]['date']='2026-11-10'
        self.assertIsNone(self.call(ledger=book)['rows'][0]['quantity'])
    def test_one_and_two_lot_bottoms_preserved(self):
        for qty in (100,200):
            cfg=deepcopy(self.cfg);cfg['positions'][0]['quantity']=qty
            d=self.call(cfg=cfg,ledger={'availability':{},'cycles':[]});self.assertEqual(d['rows'][0]['phase'],'too_small')
    def test_t_fraction_can_only_reduce_amount(self):
        cfg=deepcopy(self.cfg);cfg['t_settings']['max_fraction']=.2
        self.assertEqual(self.call(cfg=cfg)['rows'][0]['phase'],'too_small')
    def test_stale_quotes_or_minutes_and_inactive_never_signal(self):
        q=deepcopy(self.quote);q[self.code]['source_timestamp']=(self.now-timedelta(seconds=91)).isoformat()
        for args in [{'quotes':q},{'market_session':False},{'calendar_ok':False},{'minutes':{}}]:self.assertEqual(self.call(**args)['signals'],[])
    def test_cumulative_units_and_volume_backwards_block(self):
        for change in ('units','reverse','duplicate'):
            data=self.minutes();rows=data[self.code]['rows']
            if change=='units':
                for row in rows:row['cum_amount']/=100
            elif change=='reverse':rows[-1]['cum_volume']=1
            else:rows[-1]['timestamp']=rows[-2]['timestamp']
            self.assertEqual(self.call(minutes=data)['signals'],[])
    def test_gapped_bars_are_not_three_consecutive_minutes(self):
        data=self.minutes();data[self.code]['rows'][-3]['timestamp']=(self.now-timedelta(minutes=4)).isoformat()
        self.assertEqual(self.call(minutes=data)['signals'],[])
    def test_risk_exit_is_not_t_but_resistance_observation_may_be(self):
        q=deepcopy(self.quote);q[self.code]['strategy_exit']={'action':'review','trigger':'structure_weakness'}
        self.assertEqual(self.call(quotes=q)['rows'][0]['phase'],'risk_first')
        q[self.code]['strategy_exit']={'action':'review','trigger':'resistance_test'}
        self.assertEqual(self.call(quotes=q)['rows'][0]['phase'],'sell_ready')
    def test_high_exposure_never_promises_t_buyback(self):
        risk=deepcopy(self.risk);risk['strategy']['exposure_pct']=82.
        row=self.call(risk=risk)['rows'][0];self.assertEqual(row['phase'],'sell_watch');self.assertFalse(row['tradable']);self.assertIsNone(row['quantity'])
        self.assertIn('接回',row['reason'])
    def test_limits_pause_observation(self):
        for flag in ['at_limit_up','at_limit_down']:
            q=deepcopy(self.quote);q[self.code][flag]=True;self.assertEqual(self.call(quotes=q)['signals'],[])
    def buyback_fixture(self):
        cfg=deepcopy(self.cfg);cfg['positions'][0]['quantity']=200
        book=deepcopy(self.ledger);book['cycles']=[{'id':'actual','code':self.code,'date':'2026-11-11','status':'open','quantity':100,'buyback_quantity':0,'sell_price':21.,'base_quantity':300,'fee_reserve':20}]
        prices=[20.2]*48+[20.1,20,19.9,19.8,19.7,19.6,19.55,19.5,19.55,19.6,19.7,19.8]
        q=deepcopy(self.quote);q[self.code]['price']=19.8
        return {'cfg':cfg,'ledger':book,'minutes':self.minutes(prices),'quotes':q}
    def test_buyback_requires_record_sync_caps_and_real_remaining(self):
        args=self.buyback_fixture();d=self.call(**args);self.assertEqual(d['rows'][0]['phase'],'buyback_ready');self.assertEqual(d['rows'][0]['quantity'],100)
        for change in ('sync','risk','overbought','small','cycle_expired'):
            a=deepcopy(args)
            if change=='sync':a['ledger']['cycles'][0]['ledger_sync_required']=True
            elif change=='risk':risk=deepcopy(self.risk);risk['available_new_risk']=None;a['risk']=risk
            elif change=='overbought':a['ledger']['cycles'][0]['buyback_quantity']=200
            elif change=='small':a['ledger']['cycles'][0]['buyback_quantity']=50
            else:a['ledger']['cycles'][0]['date']='2026-11-10'
            self.assertEqual(self.call(**a)['signals'],[],change)
    def test_closed_cycle_and_late_day_do_not_restart(self):
        book=deepcopy(self.ledger);book['cycles']=[{'code':self.code,'date':'2026-11-11','status':'cash'}]
        self.assertEqual(self.call(ledger=book)['rows'][0]['phase'],'closed')
        late=self.now.replace(hour=14,minute=50);q=deepcopy(self.quote);q[self.code]['source_timestamp']=late.isoformat()
        self.assertEqual(self.call(now=late,quotes=q,minutes=self.minutes(now=late))['signals'],[])

if __name__=='__main__':unittest.main()
