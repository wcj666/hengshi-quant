"""Behavior checks for automatic old-position drafts; no browser or network."""
import shutil
import unittest
import test_strategy_ui as dashboard_tests

FIELDS=r'''
const fields={};for(const key of ['code','prepared_id','exit_suggestion_token','entry_price','stop_loss','take_profit_1','take_profit_2','signal_date','trigger_price','resistance_price','buy_date','actual_entry','initial_quantity','tp1_sold_quantity','sector','legacy_exit_only','fundamentals_ok','event_clear','corporate_action_clear','resistance_clear'])fields[key]={value:'',checked:false};
$('plan-form').elements=fields;$('plan-form').reset=()=>{};
STORE={config:{positions:[{code:'300274',name:'旧仓',quantity:100,cost:97.22}],trade_plans:{}},watchlist:[]};
const draft={valid:true,adoptable:true,status:'ready',asof:'2026-11-11',suggestion_token:'server-proof',plan:{entry_price:80.09,stop_loss:76.,take_profit_1:88.27,take_profit_2:92.36,suggestion_method:'已确认低点－0.2ATR'},warnings:['整体盈利待核对'],planned_stress:429,stop_distance_pct:5.1};
'''


@unittest.skipUnless(shutil.which('node'),'Node is required')
class SuggestedUiTests(unittest.TestCase):
    run_js=dashboard_tests.StrategyDashboardTests.run_js

    def test_auto_draft_fills_four_prices_and_adopts_signed_server_plan(self):
        self.run_js(FIELDS+r'''
let adopted=false;api=async(path,method,body)=>{if(method==='POST'){if(path!=='/api/exit-suggestion/300274/adopt'||body.suggestion_token!=='server-proof'||body.sector!=='')throw Error('wrong adoption request');adopted=true;return {message:'saved'};}return draft;};refresh=async()=>{};
fields.code.value='300274';fields.legacy_exit_only.checked=true;
(async()=>{await loadExitSuggestion('300274');
 for(const key of ['entry_price','stop_loss','take_profit_1','take_profit_2'])if(fields[key].value!==draft.plan[key])throw Error('not filled '+key);
 for(const key of ['buy_date','actual_entry','initial_quantity']){if(fields[key].value!=='')throw Error('invented fill '+key);if(!fields[key].disabled)throw Error('draft allows execution data that adoption would ignore '+key);}
 if($('plan-save').disabled)throw Error('ready draft cannot be adopted');
 if(!$('plan-description').textContent.includes('不代表该笔整体盈利或回本'))throw Error('exit target portrayed as overall profit');
 await saveTradePlan({preventDefault(){},target:$('plan-form')});if(!adopted)throw Error('not adopted');
})().catch(e=>{throw e;});
''')

    def test_wide_draft_visible_but_adoption_disabled(self):
        self.run_js(FIELDS+r'''
api=async()=>({...draft,adoptable:false,status:'risk_mismatch',suggestion_token:undefined,stop_distance_pct:25,plan:{...draft.plan,stop_loss:60}});
fields.code.value='300274';fields.legacy_exit_only.checked=true;
loadExitSuggestion('300274').then(()=>{if(!$('plan-save').disabled)throw Error('wide research draft is adoptable');if(fields.exit_suggestion_token.value)throw Error('unadoptable token retained');if(!$('plan-description').textContent.includes('暂无适合1～2周'))throw Error('missing risk mismatch');if(fields.stop_loss.value!==60)throw Error('remote reference was shifted closer');});
''')

    def test_existing_plan_open_never_fetches_replacement_or_resets_history(self):
        self.run_js(FIELDS+r'''
const saved={entry_price:70,stop_loss:65,take_profit_1:80,take_profit_2:85,initial_stop_loss:60,legacy_exit_only:true,buy_date:'2026-10-01',actual_entry:69};STORE.config.trade_plans['300274']=saved;
api=async()=>{throw Error('must not automatically replace existing plan');};editTradePlan('300274');
for(const key of ['entry_price','stop_loss','take_profit_1','take_profit_2','buy_date','actual_entry'])if(fields[key].value!==saved[key])throw Error('saved plan rewritten '+key);
if(!$('plan-reference-label').textContent.includes('剩余仓位'))throw Error('legacy reference labelled as entry');
''')

    def test_manual_adjustment_cancels_late_auto_response_and_clears_proof(self):
        self.run_js(FIELDS+r'''
let resolve;api=()=>new Promise(r=>resolve=r);fields.code.value='300274';fields.legacy_exit_only.checked=true;
const loading=loadExitSuggestion('300274');enableManualExitPlan();fields.stop_loss.value=77;resolve(draft);
loading.then(()=>{if(fields.stop_loss.value!==77)throw Error('late suggestion overwrote manual work');if(fields.exit_suggestion_token.value)throw Error('manual draft kept server proof');if($('plan-save').disabled)throw Error('manual form disabled');if(fields.buy_date.disabled)throw Error('manual execution remains disabled');});
''')

    def test_key_prices_show_technical_date_without_claiming_fixed_plan(self):
        self.run_js(FIELDS+r'''
EXIT_SUGGESTIONS['300274']={...draft,technical:{valid:true,asof:'2026-11-11',support:75,resistance:83,ma20:79}};
let cell=planCell(null,'300274');for(const text of ['支撑 / 压力','MA20','2026-11-11','未锁定','分批退出'])if(!cell.includes(text))throw Error('missing technical context '+text);
EXIT_SUGGESTIONS['300274'].status='risk_mismatch';cell=planCell(null,'300274');if(!cell.includes('暂不可采用')||cell.includes('建议止损'))throw Error('wide research falsely recommended');
EXIT_SUGGESTIONS['300274']={valid:false,reason:'交易日历待核验'};if(!planCell(null,'300274').includes('交易日历待核验'))throw Error('missing actual data failure reason');
''')


if __name__=='__main__':unittest.main()
