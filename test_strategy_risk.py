"""Offline V1 risk invariants, including legacy-mode compatibility."""
from copy import deepcopy
import unittest
from risk_engine import compute
from risk_extensions import risk_level, tier_alerts, simulate_reduction
from trade_levels import make_levels, validate_plan
from strategy_v1 import strategy_prefs
from v1_execution import normalize_execution_costs


def fixed(entry=20,stop=19,sector='工业',**extra):
    plan={'strategy_version':'1.0','sector':sector,'entry_price':entry,'stop_loss':stop,
          'take_profit_1':entry+2*(entry-stop),'take_profit_2':entry+3*(entry-stop),
          'trigger_price':entry-.01,'signal_date':'2026-11-10','setup_verified':True,
          'review':{'fundamentals_ok':True,'event_clear':True,'corporate_action_clear':True,'resistance_clear':True}}
    plan.update(extra);return plan


def account(**extra):
    context={'initialized':True,'verified':True,'drawdown':0.,'high_water':60000.,
             'net_external_flow':8000.,'adjusted_equity':60000.,'opens_today':0,
             'paused':False,'pause_until':None,'needs_reconcile':False}
    context.update(extra);return context


def configuration():
    return {'baseline_equity':60000.0,'max_additional_loss':1000,'cash':60000.,
            'positions':[],'trade_plans':{'600001':fixed()},'strategy_v1':{'enabled':True}}


def run(cfg=None,context=None,quotes=None,code='600001',price=20,*,execution_costs=None):
    return compute(cfg or configuration(),quotes or {},fresh=True,
                   watch_quotes=[{'code':code,'name':'测试普通股','price':price}],
                   strategy_account=account() if context is None else context,execution_costs=execution_costs)


def add_position(cfg,code='600010',quantity=100,price=20,sector='其他'):
    cfg['positions'].append({'code':code,'name':'原仓','type':'stock','quantity':quantity,'cost':price})
    cfg['trade_plans'][code]=fixed(price,price-1,sector)
    cfg['cash']-=price*quantity
    return {code:price}


class V1RiskTests(unittest.TestCase):
    def test_total_risk_contains_fee_and_lot(self):
        risk=run();size=risk['sizing'][0]
        self.assertEqual(size['max_buy_shares'],100)
        self.assertEqual(size['fee_reserve'],20)
        self.assertEqual(size['total_planned_risk'],120)
        self.assertEqual(size['reward_risk'],1.5)
        self.assertEqual(risk['baseline'],60000.0)
        self.assertEqual(risk['used'],0)
        cfg=configuration();cfg['strategy_v1']['min_fees']=60
        self.assertEqual(run(cfg)['sizing'][0]['max_buy_shares'],0)
        cfg['strategy_v1']['min_fees']=0
        self.assertEqual(strategy_prefs(cfg)['fee_reserve'],20)

    def test_net_tp1_filter_not_gross(self):
        cfg=configuration();cfg['trade_plans']['600001']=fixed(20,19.8)
        # Gross 2r passes, but the 15% capital cap allows only 400 shares:
        # net TP1 is 140/100=1.4, so this proposed trade is rejected.
        size=run(cfg)['sizing'][0]
        self.assertEqual(size['max_buy_shares'],0)
        self.assertIn('净价差筛选值不足1.5',size['reason'])

    def test_confirmed_fees_and_protected_risk_are_separate(self):
        cfg=configuration()
        cfg['trade_plans']['159001']=fixed(1,.95,equity_etf=True)
        terms=normalize_execution_costs({'etf':{'commission_per_10k':5,'minimum_commission':5}})
        row=run(cfg,code='159001',price=1,execution_costs=terms)['sizing'][0]
        self.assertEqual(row['max_buy_shares'],2600)
        self.assertEqual(row['expected_stop_fees'],10)
        self.assertEqual(row['stop_slippage'],2.6)
        self.assertEqual(row['fee_reserve'],20)
        self.assertEqual(row['risk_cost_allowance'],20)
        self.assertEqual(row['total_planned_risk'],150)
        self.assertEqual(row['stop_net_risk'],142.6)
        self.assertGreater(row['reward_risk'],1.5)
        self.assertIsNone(run(cfg,code='159001',price=1)['sizing'][0]['expected_stop_fees'])

    def test_actual_thresholds_in_failed_sizing(self):
        cfg=configuration()
        cfg['strategy_v1'].update(max_position_pct=3,min_reward_risk=1.8)
        row=run(cfg)['sizing'][0]
        self.assertIn('单只仓位3%额度不足一手',row['reason'])
        cfg['strategy_v1']['max_position_pct']=15
        self.assertIn('筛选值不足1.8',run(cfg)['sizing'][0]['reason'])

    def test_missing_stop_retains_complete_quote_assets_and_pnl(self):
        cfg=configuration();prices=add_position(cfg);cfg['trade_plans'].pop('600010')
        risk=run(cfg,quotes=prices)
        self.assertTrue(risk['valid']);self.assertEqual(risk['equity'],60000)
        self.assertIsNotNone(risk['pnl'])
        self.assertIsNone(risk['existing_planned_stress'])
        self.assertEqual(risk['available_new_risk'],0);self.assertTrue(risk['over_budget'])
        self.assertEqual(risk['strategy']['unknown_stops'],['600010'])
        self.assertIsNone(make_levels(cfg,'600010',position=cfg['positions'][0]))
        self.assertEqual(risk_level(risk)['level'],'unavailable');self.assertEqual(tier_alerts(risk),[])
        simulation=simulate_reduction(cfg,prices,'600010',100,strategy_account=account())
        self.assertIsNone(simulation['stress_reduction'])

    def test_touch_stop_is_not_zero_risk(self):
        cfg=configuration();prices=add_position(cfg);prices['600010']=19
        risk=run(cfg,quotes=prices)
        self.assertEqual(risk['stop_breaches'],['600010'])
        self.assertEqual(risk['existing_planned_stress'],20)
        self.assertEqual(risk['available_new_risk'],0)

    def test_unknown_or_unreconciled_account_blocks_only_risk(self):
        for context in ({},account(verified=False),account(needs_reconcile=True),account(drawdown=float('nan'))):
            risk=run(context=context)
            self.assertTrue(risk['valid']);self.assertEqual(risk['equity'],60000)
            self.assertEqual(risk['sizing'][0]['max_buy_shares'],0)
            self.assertFalse(risk['strategy']['new_entries_allowed'])
        self.assertEqual(run(context=account(drawdown=500))['available_new_risk'],0)
        self.assertEqual(run(context=account(paused=True))['available_new_risk'],0)

    def test_context_drawdown_not_old_baseline_or_deposit(self):
        cfg=configuration();cfg['cash']=70000
        risk=run(cfg,context=account(drawdown=80,net_external_flow=18000,adjusted_equity=52000,high_water=52080))
        self.assertEqual(risk['used'],80);self.assertEqual(risk['remaining'],920)
        self.assertEqual(risk['baseline'],60000.0)
        self.assertEqual(risk['change'],10000.0)

    def test_security_and_total_caps(self):
        cfg=configuration();prices=add_position(cfg,quantity=500)
        risk=run(cfg,quotes=prices)
        self.assertIn('600010',risk['strategy']['single_excess'])
        self.assertEqual(risk['sizing'][0]['max_buy_shares'],0)
        cfg=configuration();prices={}
        for index in range(4):prices.update(add_position(cfg,code=str(600010+index),quantity=450,sector=str(index)))
        risk=run(cfg,quotes=prices)
        self.assertEqual(risk['strategy']['exposure_pct'],60)
        self.assertEqual(risk['sizing'][0]['max_buy_shares'],0)
        cfg['positions'][0]['quantity']=451
        risk=run(cfg,quotes=prices)
        self.assertTrue(any('权益总仓位' in x for x in risk['strategy']['blockers']))

    def test_sector_cap_limits_new_notional(self):
        cfg=configuration();prices={}
        for index in range(2):prices.update(add_position(cfg,code=str(600010+index),quantity=450,sector='工业'))
        for code in prices:cfg['trade_plans'][code]=fixed(20,19.9,'工业')
        # Sector is exactly at 30%, so the candidate has zero industry room.
        risk=run(cfg,quotes=prices)
        self.assertEqual(risk['strategy']['sector_exposure']['工业']['pct'],30)
        self.assertEqual(risk['sizing'][0]['max_buy_shares'],0)
        cfg['trade_plans']['600010'].pop('sector')
        self.assertEqual(run(cfg,quotes=prices)['strategy']['unknown_sectors'],['600010'])

    def test_no_add_and_daily_open_count_and_max_names(self):
        cfg=configuration();prices=add_position(cfg)
        self.assertEqual(run(cfg,quotes=prices,code='600010')['sizing'][0]['max_buy_shares'],0)
        self.assertEqual(run(context=account(opens_today=1))['sizing'][0]['max_buy_shares'],0)
        cfg=configuration();prices={}
        for index in range(4):prices.update(add_position(cfg,code=str(600010+index)))
        self.assertEqual(run(cfg,quotes=prices)['sizing'][0]['max_buy_shares'],0)

    def test_scope_and_plan_review(self):
        cfg=configuration();cfg['trade_plans']['300001']=fixed()
        self.assertEqual(run(cfg,code='300001')['sizing'][0]['max_buy_shares'],0)
        cfg['trade_plans']['600001']['review']['event_clear']=False
        self.assertEqual(run(cfg)['sizing'][0]['max_buy_shares'],0)
        cfg['trade_plans']['600001']=fixed(resistance_price=21.99)
        self.assertEqual(run(cfg)['sizing'][0]['max_buy_shares'],0)
        cfg['trade_plans']['600001']=fixed(resistance_price=22)
        self.assertEqual(run(cfg)['sizing'][0]['max_buy_shares'],100)

    def test_remaining_fees_and_whole_account_budget(self):
        cfg=configuration();prices=add_position(cfg,quantity=400)
        risk=run(cfg,context=account(drawdown=490),quotes=prices)
        self.assertEqual(risk['existing_planned_stress'],420)
        self.assertEqual(risk['strategy']['budget_occupancy'],910)
        self.assertEqual(risk['available_new_risk'],90)
        self.assertEqual(risk['sizing'][0]['max_buy_shares'],0)
        cfg['trade_plans']['600010']['remaining_fee_reserve']=8
        risk=run(cfg,context=account(drawdown=490),quotes=prices)
        self.assertEqual(risk['existing_planned_stress'],408)
        self.assertEqual(risk['available_new_risk'],102)
        cfg['trade_plans']['600010']['remaining_fee_reserve']=float('nan')
        self.assertEqual(run(cfg,quotes=prices)['existing_planned_stress'],420)

    def test_raised_stop_keeps_original_r_and_position_sector(self):
        cfg=configuration();prices=add_position(cfg)
        cfg['positions'][0]['sector']='原行业'
        plan=cfg['trade_plans']['600010'];plan.pop('sector')
        plan.update(stop_loss=20,initial_stop_loss=19,tp1_sold_quantity=100)
        prices['600010']=21
        risk=run(cfg,quotes=prices)
        levels=risk['positions'][0]['levels']
        self.assertTrue(levels['raised_stop']);self.assertTrue(levels['valid_order'])
        self.assertEqual(levels['initial_stop_loss'],19)
        self.assertEqual(risk['positions'][0]['sector'],'原行业')
        self.assertEqual(risk['existing_planned_stress'],120)
        self.assertEqual(risk['strategy']['unknown_stops'],[])
        self.assertEqual(validate_plan(plan,allow_raised_stop=True)['stop_loss'],20)
        with self.assertRaises(ValueError):validate_plan(plan)
        plan.pop('initial_stop_loss')
        self.assertEqual(run(cfg,quotes=prices)['strategy']['unknown_stops'],['600010'])

    def test_fees_reduce_cash_and_fee_floor_cannot_be_disabled(self):
        cfg=configuration();cfg['cash']=2010;cfg['baseline_equity']=2010
        cfg['strategy_v1']['max_position_pct']=100  # Cannot relax V1's 15% ceiling.
        self.assertEqual(strategy_prefs(cfg)['max_position_pct'],15)
        self.assertEqual(run(cfg)['sizing'][0]['max_buy_shares'],0)
        cfg['trade_plans']['600001']=fixed();cfg['cash']=2010
        self.assertEqual(run(cfg)['sizing'][0]['max_buy_shares'],0)

    def test_legacy_mode_keeps_cost_percent_levels_and_sizing(self):
        cfg=configuration();cfg.pop('strategy_v1');cfg['baseline_equity']=60000
        cfg['trade_plans']={}
        prices=add_position(cfg)
        cfg['trade_plans'].pop('600010')
        risk=run(cfg,quotes=prices)
        self.assertTrue(risk['valid']);self.assertNotIn('strategy',risk)
        self.assertEqual(risk['positions'][0]['levels']['stop_loss'],19)
        self.assertEqual(risk['sizing'][0]['max_buy_shares']%100,0)


if __name__=='__main__':unittest.main()
