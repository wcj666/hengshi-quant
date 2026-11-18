"""Browser-script behavior checks for V1 valuation/entry separation (no network)."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).parent


@unittest.skipUnless(shutil.which('node'), 'Node is required for dashboard behavior checks')
class StrategyDashboardTests(unittest.TestCase):
    def run_js(self, code):
        harness = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes=new Map();
function element(id){if(!nodes.has(id))nodes.set(id,{textContent:'',innerHTML:'',className:'',style:{},classList:{add(){},remove(){}},addEventListener(){},close(){this.closed=true;},showModal(){this.open=true;}});return nodes.get(id);}
const context={console,Intl,Date,JSON,Number,String,Math,Set,Map,Array,Error,Promise,encodeURIComponent,
 document:{querySelector(){return {content:'csrf'};},getElementById:element,addEventListener(){}},
 localStorage:{getItem(){return '[]';},setItem(){}},setTimeout(){return 1;},clearTimeout(){},setInterval(){},
 window:{addEventListener(){}},confirm(){return true;}};
vm.createContext(context);vm.runInContext(fs.readFileSync('static/dashboard.js','utf8'),context);
vm.runInContext(CODE,context);
'''.replace('CODE', json.dumps(code))
        result = subprocess.run(['node', '-e', harness], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_fixed_stop_retains_complete_assets_and_pnl(self):
        self.run_js(r'''
renderStrategy=()=>{};riskTier=()=>{};renderPositions=()=>{};renderSizing=()=>{throw Error('must not show entry sizing');};
STORE={config:{max_additional_loss:1000}};
const ok=applyLiveAccountSnapshot({risk:{equity:75000.0,budget:1000,remaining:null,used:null,existing_planned_stress:null,available_new_risk:0,over_budget:true,strategy:{blockers:['固定止损未知']},pnl:{valid:true,daily_valid:true,floating_pnl:-1800.00,daily_pnl:800.00,floating_pct:-2,daily_pct:1.35}},quote_health:{provider_timestamps_verified:true,held_count:6},quotes:[],market_time:'2026-11-11T10:00:00+08:00'});
if(!ok)throw Error('valuation was rejected because risk metadata is unknown');
if(!$('equity').textContent.includes('75,000.00'))throw Error('complete equity disappeared');
if(!$('floating-pnl').textContent.includes('1,800.00'))throw Error('floating PnL disappeared');
if(!$('daily-pnl').textContent.includes('800.00'))throw Error('daily PnL disappeared');
if(!$('sizing-table').innerHTML.includes('暂停新增'))throw Error('new entry must remain blocked');
''')

    def test_scan_refreshes_results_even_when_started_from_an_idle_page(self):
        self.run_js(r'''
STORE={job:{running:false},meta:{mode:'live'},top:[],candidates:[]};
const calls=[];
api=async(path)=>{
 calls.push(path);
 if(path==='/api/scan')return {job:{running:true}};
 return {running:false,returncode:0,message:'已生成新报告'};
};
refresh=async()=>{calls.push('refresh');return true;};
(async()=>{
 await scan('live');
 while(scanPolling)await Promise.resolve();
 if(calls.join()!=='/api/scan,/api/job,refresh')throw Error('scan did not poll and refresh');
 if($('live-scan').disabled)throw Error('scan button not restored');
 if(!$('job-message').textContent.includes('已生成新报告'))throw Error('completion not shown');
})();
''')

    def test_daily_cards_show_research_without_exposing_execution_levels(self):
        self.run_js(r'''
const entry={code:'600150',name:'中国船舶',asof:'2026-11-18',close:34.71,pct:2.15,volume_ratio:1.41,
 reasons:['收在日内高点','放量转强'],stop_loss:33.69,take_profit_1:36.41,max_buy_shares:100};
DAILY={mode:'live',asof:entry.asof,stale:false,shortlist:[entry],status:'V1账户未通过',disclaimer:'旧文案'};
renderDailyObserve();
const card=$('daily-list').innerHTML;
for(const value of ['中国船舶','600150','34.71','+2.15%','收在日内高点','加入自选','不再推荐']){
 if(!card.includes(value))throw Error('missing research card detail: '+value);
}
for(const value of ['止损','止盈','33.69','36.41','理论最多']){
 if(card.includes(value))throw Error('research card exposed an execution field: '+value);
}
if(!$('daily-warning').hidden)throw Error('obsolete gate status remains visible');
DAILY.stale=true;
renderDailyObserve();
if($('daily-warning').hidden||!$('daily-warning').textContent.includes('历史名单'))throw Error('historical data not labeled');
''')

    def test_budget_resume_is_explicit_and_only_shown_for_latched_accounts(self):
        self.run_js(r'''
const fields={};for(const key of STRATEGY_FIELDS)fields[key]={value:''};
$('strategy-settings-form').elements=fields;
renderStrategy({enabled:true,account:{budget_exhausted:false}},{});
if(!$('strategy-resume-controls').hidden)throw Error('resume shown without budget latch');
renderStrategy({enabled:true,account:{budget_exhausted:true}},{});
if($('strategy-resume-controls').hidden)throw Error('resume hidden for latched account');
let requested=false;api=async(path,method,body)=>{if(path!=='/api/strategy/resume'||method!=='POST'||body.acknowledged!==true)throw Error('incorrect explicit resume request');requested=true;return {message:'review accepted'};};refresh=async()=>{};
resumeStrategy().then(()=>{if(!requested)throw Error('resume request missing');if($('strategy-resume').disabled)throw Error('button left disabled');});
''')

    def test_new_entry_requires_server_prepared_plan(self):
        self.run_js(r'''
const values={code:'600001',prepared_id:'',sector:'制造',legacy_exit_only:false,entry_price:'20',stop_loss:'19',take_profit_1:'22',take_profit_2:'23'};
const elements=new Proxy({}, {get:(target,key)=>({value:values[key]??'',checked:key==='legacy_exit_only'?false:true})});
let calls=0;api=async()=>{calls++;};
saveTradePlan({preventDefault(){},target:{elements}}).then(()=>{if(calls)throw Error('unprepared new plan reached save endpoint');if(!$('toast').className.includes('error'))throw Error('missing explicit error');});
''')

    def test_old_position_plan_does_not_invent_execution_history(self):
        self.run_js(r'''
const fields={};for(const key of ['code','prepared_id','entry_price','stop_loss','take_profit_1','take_profit_2','signal_date','trigger_price','resistance_price','buy_date','actual_entry','initial_quantity','tp1_sold_quantity','sector','legacy_exit_only','fundamentals_ok','event_clear','corporate_action_clear','resistance_clear'])fields[key]={value:'',checked:false};
$('plan-form').elements=fields;$('plan-form').reset=()=>{};
STORE={config:{positions:[{code:'300274',name:'旧仓',quantity:100,cost:97.22}],trade_plans:{}},watchlist:[]};
editTradePlan('300274');
if(!fields.legacy_exit_only.checked)throw Error('legacy position must default to exit-only');
for(const key of ['buy_date','actual_entry','initial_quantity','stop_loss'])if(fields[key].value!=='')throw Error('invented legacy history/stop '+key);
''')

    def test_zero_quantity_position_cannot_be_saved_as_exit_only(self):
        self.run_js(r'''
const fields={};for(const key of ['code','prepared_id','entry_price','stop_loss','take_profit_1','take_profit_2','signal_date','trigger_price','resistance_price','buy_date','actual_entry','initial_quantity','tp1_sold_quantity','sector','legacy_exit_only','fundamentals_ok','event_clear','corporate_action_clear','resistance_clear'])fields[key]={value:'',checked:false};
$('plan-form').elements=fields;$('plan-form').reset=()=>{};
STORE={config:{positions:[{code:'159929',name:'ETF旧记录',quantity:0}],trade_plans:{'159929':{legacy_exit_only:true}}},watchlist:[{code:'159929',name:'医药ETF'}]};
editTradePlan('159929');
if(fields.legacy_exit_only.checked||!fields.legacy_exit_only.disabled)throw Error('zero position incorrectly treated as legacy holding');
if(!fields.signal_date.required)throw Error('zero position bypassed V1 new-entry requirements');
''')

    def test_prepare_populates_all_server_fixed_prices(self):
        self.run_js(r'''
const fields={};for(const key of ['code','prepared_id','entry_price','stop_loss','take_profit_1','take_profit_2','signal_date','trigger_price','legacy_exit_only'])fields[key]={value:key==='code'?'600001':'',checked:false};
$('plan-form').elements=fields;
api=async()=>({ok:true,prepared_id:'server-proof',setup:{signal_date:'2026-11-10',valid_on:'2026-11-11',levels:{trigger_price:20,entry_price:20.2,stop_loss:19,take_profit_1:22.6,take_profit_2:23.8}}});
prepareTradePlan().then(()=>{for(const [key,value] of Object.entries({prepared_id:'server-proof',signal_date:'2026-11-10',trigger_price:20,entry_price:20.2,stop_loss:19,take_profit_1:22.6,take_profit_2:23.8}))if(fields[key].value!==value)throw Error('missing server value '+key);});
''')


if __name__ == '__main__':
    unittest.main()
