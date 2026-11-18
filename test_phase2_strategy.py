"""Offline V1 daily research path, independent of saved plans and old buy rules."""
from copy import deepcopy
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
import json
import unittest
import pandas as pd
import phase2
from daily_observe import select_five, select_research


def history():
    dates=pd.bdate_range(end='2026-11-09',periods=140)
    prices=[20+i*.03 for i in range(140)]
    hist=pd.DataFrame({'date':dates,'open':prices,'close':prices,'high':[v+.08 for v in prices],
                       'low':[v-.08 for v in prices],'volume':[100]*140,'amount':[200_000_000]*140})
    hist.loc[136:138,'low']=hist.loc[136:138,'close']-.35;hist.loc[136:138,'volume']=60
    hist.loc[139,'close']=hist.loc[138,'high']+.2;hist.loc[139,'high']=hist.loc[139,'close']+.08;hist.loc[139,'volume']=200
    bench=pd.DataFrame({'date':dates,'open':prices,'close':prices,'high':[v+.08 for v in prices],
                        'low':[v-.08 for v in prices],'volume':[100]*140})
    return hist,bench


def cfg():
    return {'strategy_v1':{'enabled':True},'trade_plans':{},'daily_excludes':[],
            'cash':0,'baseline_equity':60000,'max_additional_loss':1000,
            'positions':[{'code':'600010','name':'原仓','type':'stock','quantity':100,'cost':20,'baseline_price':20}],
            'watchlist':[]}


def candidates():
    return pd.DataFrame([{'code':code,'name':'主板研究','price':24.5,'pct':2,'amount':200_000_000,
                          'turnover':.1,'volume_ratio':.1,'初筛分':0} for code in ('600001','600002')])


class Phase2V1Tests(unittest.TestCase):
    def test_strict_setup_without_manual_plan_and_no_old_score(self):
        hist,bench=history();config=cfg();before=deepcopy(config)
        with patch.object(phase2,'stock_history',return_value=hist),patch.object(phase2,'score_stock',side_effect=AssertionError('legacy score called')):
            verified,errors=phase2.verify_candidates(candidates(),'live',2,0,config,benchmark_hist=bench)
        self.assertEqual(errors,[]);self.assertTrue(verified['V1形态通过'].all())
        daily=select_five(verified,config,'live')
        self.assertEqual(daily['count'],2);self.assertEqual(config,before)
        self.assertTrue(all(row['research_only'] and not row['can_buy_now'] for row in daily['shortlist']))
        self.assertTrue(all(row['setup_evidence']['benchmark_date']=='2026-11-09' for row in daily['shortlist']))
        self.assertTrue(all(row['trigger_price']<=row['entry_price'] for row in daily['shortlist']))
        # A high legacy score and percentage targets do not qualify V1.
        legacy=pd.DataFrame([{'股票代码':'600001','股票名称':'旧形态','日期':'2026-11-09','收盘':20,
                              '涨跌幅%':2,'MA5':19,'MA20':18,'观察评分':100,'量比5日':2,
                              '止损参考':19,'第一止盈':22,'第二止盈':23}])
        self.assertEqual(select_five(legacy,config,'live')['count'],0)

    def test_benchmark_loaded_once_failure_no_candidate_and_demo_suppressed(self):
        hist,bench=history()
        with patch.object(phase2,'v1_benchmark_history',return_value=(bench,None)) as benchmark,patch.object(phase2,'stock_history',return_value=hist):
            result,errors=phase2.verify_candidates(candidates(),'live',2,0,cfg())
            self.assertEqual(benchmark.call_count,1);self.assertEqual(len(result),2)
        with patch.object(phase2,'v1_benchmark_history',return_value=(pd.DataFrame(),'沪深300来源失败')), \
                patch.object(phase2,'stock_history',return_value=hist):
            result,errors=phase2.verify_candidates(candidates(),'live',2,0,cfg())
        self.assertIn('来源失败',errors[0])
        self.assertFalse(result['V1形态通过'].any())
        self.assertEqual(len(select_research(result.to_dict('records'),cfg(),expected_day='2026-11-09')),2)
        self.assertEqual(select_five(pd.DataFrame(),cfg(),'demo')['count'],0)

    def test_observation_ranked_without_market_gate_and_rejects_stale_or_limit_up(self):
        hist,bench=history()
        bench['close']=bench['close'].iloc[::-1].to_numpy()
        for field in ('open','high','low'):
            bench[field]=bench[field].iloc[::-1].to_numpy()
        with patch.object(phase2,'stock_history',return_value=hist):
            verified,_=phase2.verify_candidates(candidates(),'live',2,0,cfg(),benchmark_hist=bench)
        self.assertFalse(verified['V1形态通过'].any())
        records=verified.to_dict('records')
        rows=select_research(records,cfg(),expected_day='2026-11-09')
        self.assertEqual([row['code'] for row in rows],['600001','600002'])
        self.assertTrue(all(row['research_only'] and not row['can_buy_now'] and 'entry_price' not in row for row in rows))
        contaminated=[{**records[0],'日期':'2026-11-08'}, {**records[0],'涨跌幅%':10}, records[1]]
        self.assertEqual([row['code'] for row in select_research(contaminated,cfg(),expected_day='2026-11-09')],['600002'])
        self.assertEqual(select_research(records,cfg(),expected_day='2026-11-10'),[])
        config={**cfg(),'daily_excludes':['600001']}
        self.assertEqual([row['code'] for row in select_research(records,config,expected_day='2026-11-09')],['600002'])

    def test_old_daily_bars_are_not_published_as_this_sessions_verification(self):
        hist,bench=history()
        with patch.object(phase2,'stock_history',return_value=hist):
            verified,errors=phase2.verify_candidates(candidates(),'live',2,0,cfg(),bench,expected_day='2026-11-10')
        self.assertTrue(verified.empty)
        self.assertEqual(len(errors),2)
        self.assertIn('日线仅到2026-11-09',errors[0])

    def test_empty_shortlist_can_be_current_after_successful_verification(self):
        from web_dashboard import create_app
        with TemporaryDirectory() as folder:
            root=Path(folder)
            output=root/'output'
            output.mkdir()
            (root/'config.json').write_text(json.dumps(cfg()))
            (output/'run_meta.json').write_text(json.dumps({
                'mode':'live','scan_ok':True,'verified_asof':'2026-11-13',
            }))
            (output/'daily_observe.json').write_text(json.dumps({
                'mode':'live','asof':None,'shortlist':[],'strategy_version':'1.0',
            }))
            client=create_app(root).test_client()
            with patch('web_dashboard.strategy.latest_completed_day',return_value='2026-11-13'):
                latest=client.get('/api/daily-observe').get_json()
            with patch('web_dashboard.strategy.latest_completed_day',return_value='2026-11-16'):
                older=client.get('/api/daily-observe').get_json()
        self.assertEqual(latest['asof'],'2026-11-13')
        self.assertEqual(latest['count'],0)
        self.assertFalse(latest['stale'])
        self.assertTrue(older['stale'])

    def test_scope_and_old_snapshot_filters_do_not_create_second_entry(self):
        universe=candidates();universe.loc[1,'code']='300001'
        chosen=phase2.filter_universe(universe,10,cfg())
        self.assertEqual(list(chosen.code),['600001'])
        self.assertEqual(len(phase2.filter_universe(universe,10)),0)
        hist,bench=history();hist.loc[139,'volume']=10
        with patch.object(phase2,'stock_history',return_value=hist):
            verified,errors=phase2.verify_candidates(candidates(),'live',2,0,cfg(),benchmark_hist=bench)
        self.assertEqual(select_five(verified,cfg(),'live')['count'],0)

    def test_v1_suppresses_historical_baseline_budget_alert(self):
        universe=candidates();prices={('stock','600010'):(20,'原仓',20)}
        alerts,portfolio,missing,equity,loss,remaining=phase2.alert_rules(cfg(),prices,universe,pd.DataFrame(),'live')
        self.assertFalse(any(alerts.type.eq('预算接近/超额')))
        self.assertIsNone(remaining);self.assertIn('历史基准价（非V1额度）',portfolio)
        old=cfg();old.pop('strategy_v1')
        alerts,*_=phase2.alert_rules(old,prices,universe,pd.DataFrame(),'live')
        self.assertTrue(any(alerts.type.eq('预算接近/超额')))

    def test_flat_v1_account_has_no_phantom_quote_missing_or_budget_alert(self):
        config=cfg();config['positions'][0]['quantity']=0;config['cash']=60000
        alerts,portfolio,missing,equity,loss,remaining=phase2.alert_rules(config,{},candidates(),pd.DataFrame(),'live')
        self.assertEqual(missing,[]);self.assertEqual(equity,60000)
        self.assertTrue(portfolio.empty);self.assertIsNone(remaining)

    def test_report_labels_history_and_does_not_write_plans(self):
        hist,bench=history();config=cfg()
        with patch.object(phase2,'stock_history',return_value=hist):
            verified,_=phase2.verify_candidates(candidates(),'live',2,0,config,benchmark_hist=bench)
        daily=select_five(verified,config,'live')
        prices={('stock','600010'):(20,'原仓',20)}
        alerts,portfolio,missing,equity,loss,_=phase2.alert_rules(config,prices,candidates(),verified,'live')
        with TemporaryDirectory() as folder,patch.object(phase2,'OUT',Path(folder)):
            phase2.produce_report('live',candidates(),candidates(),verified,pd.DataFrame(),alerts,portfolio,missing,equity,loss,[],[],2,daily)
            report=(Path(folder)/'report_v2.html').read_text()
            self.assertIn('非V1可用风险额度',report);self.assertIn('V1完整日线形态复核',report)
            self.assertNotIn('缠论只做简化结构辅助',report)
            self.assertEqual(config['trade_plans'],{})


if __name__=='__main__':unittest.main()
