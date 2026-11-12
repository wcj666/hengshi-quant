"""Closing-day display precedence and evidence completion, with no live data."""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import pandas as pd
import strategy_runtime as runtime
import test_strategy_ui as ui_harness


class ClosingDisplayTests(unittest.TestCase):
    run_js=ui_harness.StrategyDashboardTests.run_js

    def test_new_trading_day_quotes_replace_yesterdays_broker_snapshot(self):
        self.run_js(r'''
const data={display_snapshot:{asof:'2026-11-12T07:10:00+00:00'},broker_snapshot:{asof:'2026-11-11T15:40:00+08:00'}};
let selected='';applyHistoricalAccountSnapshot=()=>{selected='today';return true;};applyBrokerAccountSnapshot=()=>{selected='yesterday';return true;};
if(!applyLatestAccountSnapshot(data)||selected!=='today')throw Error('yesterday screenshot hid today close');
''')

    def test_same_closing_day_preserves_broker_actual_pnl(self):
        self.run_js(r'''
const data={display_snapshot:{asof:'2026-11-12T07:20:00+00:00'},broker_snapshot:{asof:'2026-11-12T15:07:00+08:00'}};
if(displaySnapshotChoices(data)[0].kind!=='broker')throw Error('provider dissemination time replaced broker closing accounting');
data.broker_snapshot.asof='2026-11-12T10:00:00+08:00';
if(displaySnapshotChoices(data)[0].kind!=='quote')throw Error('morning screenshot hid newer close');
data.broker_snapshot.asof='invalid';
if(displaySnapshotChoices(data).length!==1)throw Error('invalid timestamp accepted');
''')

    def test_estimate_is_display_only_and_keeps_broker_values_readable(self):
        self.run_js(r'''
STORE=null;riskTier=()=>{};
const snapshot={asof:'2026-11-12T07:00:00+00:00',source:'腾讯公开行情',risk:{equity:60600,pnl:{valid:true,floating_pnl:-100,daily_valid:true,daily_pnl:-800}}};
const broker={asof:'2026-11-11T15:40:00+08:00',total_assets:76000.0,broker_total_pnl:-210.00,broker_daily_reference_pnl:1600.00,broker_pnl_label:'总盈亏'};
if(!applyHistoricalAccountSnapshot(snapshot,{broker_snapshot:broker}))throw Error('valid estimate rejected');
if(!$('data-note').textContent.includes('2026/11/12')||!$('data-note').textContent.includes('76,000.00')||!$('data-note').textContent.includes('总盈亏'))throw Error('source date or actual broker accounting hidden');
if($('new-risk').textContent!=='—'||!$('sizing-table').innerHTML.includes('暂停'))throw Error('historical estimate granted entry authority');
''')


class ClosingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.out=Path(self.tmp.name)
        self.clock=datetime(2026,11,12,15,6,tzinfo=runtime.CST)
        self.days=['2026-11-11','2026-11-12','2026-11-13']
        runtime.write(self.out/'strategy_calendar.json',{'verified':True,'dates':self.days,'fetched_at':self.clock.isoformat()})
        self.cfg={'positions':[{'code':'300274','type':'stock','quantity':100}],
                  'strategy_v1':{'exit_mode':'price_first'}}

    def test_close_cutoff_bypasses_intraday_throttle_but_not_repeated_requests(self):
        attempts={};ran=[]
        class ImmediateThread:
            def __init__(self,target,**kwargs):self.target=target
            def start(self):self.target()
        with patch.object(runtime,'_EVIDENCE_ATTEMPTS',attempts),patch.object(runtime,'refresh_evidence',side_effect=lambda *args:ran.append(1)),patch.object(runtime.threading,'Thread',ImmediateThread),patch.object(runtime,'datetime') as clock:
            clock.now.return_value=self.clock.replace(hour=15,minute=4)
            runtime.refresh_evidence_async(self.out,self.cfg);runtime.refresh_evidence_async(self.out,self.cfg)
            clock.now.return_value=self.clock
            runtime.refresh_evidence_async(self.out,self.cfg);runtime.refresh_evidence_async(self.out,self.cfg)
        self.assertEqual(len(ran),2)

    def test_stale_provider_response_preserves_cache_and_reports_exact_dates(self):
        old={'history':[{'date':'2026-11-11','open':80,'high':81,'low':79,'close':80,'volume':1}]*120,'fetched_at':'2026-11-12T09:30:00+08:00'}
        runtime.write(self.out/'strategy_evidence'/'300274.json',old)
        runtime.write(self.out/'strategy_evidence'/'benchmark.json',{'history':[{'date':'2026-11-12'}]*120,'fetched_at':self.clock.isoformat()})
        stale=pd.DataFrame([{'date':'2026-11-11','open':80,'high':81,'low':79,'close':80,'volume':1}])
        with patch.object(runtime,'datetime') as clock,patch('asset_history.history_by_asset',return_value=stale),patch('asset_history.benchmark_history',create=True):
            clock.now.return_value=self.clock
            errors=runtime.refresh_evidence(self.out,self.cfg)
        self.assertEqual(runtime.evidence(self.out,'300274'),old)
        status=runtime.read(self.out/'strategy_evidence'/'status.json')
        self.assertEqual(status['by_code']['300274']['asof'],'2026-11-11')
        self.assertEqual(status['expected_asof'],'2026-11-12')
        self.assertIn('应更新至2026-11-12',errors[0])


if __name__=='__main__':unittest.main()
