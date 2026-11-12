"""Doing-T UI guards: no inferred fills, no stale executable hints or over-buyback."""
from pathlib import Path
import re
import unittest
import test_strategy_ui

ROOT=Path(__file__).parent

class TDashboardTests(unittest.TestCase):
    run_js=test_strategy_ui.StrategyDashboardTests.run_js

    def test_markup_has_t_navigation_and_explicit_confirmation_forms(self):
        markup=(ROOT/'templates/dashboard.html').read_text()
        ids=re.findall(r'\bid="([^"]+)"',markup)
        self.assertEqual(len(ids),len(set(ids)))
        for id_ in ['signals-t','nav-count-t','t-count','signal-t-list','signal-t-badge','t-observation-list','t-availability-form','t-sell-form','t-buyback-form','t-close-form']:
            self.assertIn(id_,ids)
        self.assertIn('黄金股ETF也属于股票ETF',markup)
        for name in ['availability_confirmed','sell_filled','buy_filled','leave_cash']:
            checkbox=re.search(r'<input[^>]*name="'+name+r'"[^>]*>',markup).group(0)
            self.assertIn('required',checkbox)
            self.assertNotIn('checked',checkbox)

    def test_stale_t_prices_cannot_look_executable(self):
        self.run_js(r'''
renderTObserve({enabled:true,stale:true,asof:'2026-11-11T10:00:00+08:00',rows:[{code:'600641',name:'旧行情',price:123.456,vwap:120,target_sell:125,target_buy:117,quantity:300,fee_reserve:20,net_spread_estimate:78.91}],cycles:[]});
const html=$('t-observation-list').innerHTML;
if(html.includes('123.456')||html.includes('78.91')||html.includes('>300<'))throw Error('stale T price or quantity remained executable-looking');
if(!html.includes('不沿用旧价位')||!html.includes('待核验'))throw Error('stale reason not visible');
''')

    def test_missing_sellable_quantity_does_not_invent_t_size_or_cycle(self):
        self.run_js(r'''
renderTObserve({enabled:true,stale:false,rows:[{code:'600641',name:'条件待核验',phase:'sell_watch',price:34,quantity:100,blockers:['今日券商可卖数量未确认']}],availability:{},cycles:[]});
const html=$('t-observation-list').innerHTML;
if(!html.includes('今日券商可卖数量未确认')||!html.includes('—（待核验）'))throw Error('unknown sellable quantity was ignored');
if(!$('t-cycle-list').innerHTML.includes('尚无实际T卖出记录'))throw Error('ordinary reduction inferred as T cycle');
''')

    def test_actual_sell_requires_confirmation_and_accepts_partial_fill_integer(self):
        self.run_js(r'''
(async()=>{
let calls=[];api=async(path,method,body)=>{calls.push({path,method,body});return {message:'saved'};};refreshTObserve=async()=>{};
const fields={code:{value:'600641'},quantity:{value:'50'},sell_price:{value:'34.2'},sell_filled:{checked:false}},form={elements:fields,reset(){this.resetCalled=true;}},event={preventDefault(){},target:form};
await saveTSell(event);if(calls.length)throw Error('unconfirmed sell recorded');
fields.sell_filled.checked=true;await saveTSell(event);
if(calls.length!==1||calls[0].path!=='/api/t-cycles'||calls[0].body.quantity!==50||calls[0].body.sell_filled!==true)throw Error('actual partial fill not preserved');
if(!form.resetCalled)throw Error('confirmation not reset after successful record');
})()
''')

    def test_availability_is_today_confirmed_and_zero_is_allowed(self):
        self.run_js(r'''
(async()=>{
let calls=[];api=async(path,method,body)=>{calls.push({path,method,body});return {};};refreshTObserve=async()=>{};
const fields={code:{value:'600641'},available_quantity:{value:'0'},availability_confirmed:{checked:false}},form={elements:fields,reset(){}},event={preventDefault(){},target:form};
await saveTAvailability(event);if(calls.length)throw Error('availability not explicitly checked');
fields.availability_confirmed.checked=true;await saveTAvailability(event);
if(calls.length!==1||calls[0].method!=='PUT'||calls[0].body.available_quantity!==0)throw Error('zero available quantity was not preserved');
})()
''')

    def test_buyback_requires_real_sell_and_never_exceeds_unbought_quantity(self):
        self.run_js(r'''
(async()=>{
let calls=[];api=async(path,method,body)=>{calls.push({path,method,body});return {};};refreshTObserve=async()=>{};
const fields={cycle_id:{value:'cycle-1'},quantity:{value:'101'},price:{value:'33.5'},buy_filled:{checked:true}},form={elements:fields,reset(){}},event={preventDefault(){},target:form};
T_OBSERVE={cycles:[]};await saveTBuyback(event);if(calls.length)throw Error('buyback without actual T sell');
T_OBSERVE={cycles:[{id:'cycle-1',status:'open',quantity:200,buyback_quantity:100,unbought_quantity:100}]};await saveTBuyback(event);if(calls.length)throw Error('over-buyback posted');
fields.quantity.value='50';await saveTBuyback(event);
if(calls.length!==1||calls[0].path!=='/api/t-cycles/cycle-1/buyback'||calls[0].body.quantity!==50||calls[0].body.buy_filled!==true)throw Error('actual partial buyback malformed');
})()
''')

    def test_expired_cycle_not_offered_for_buyback_but_may_close_to_cash(self):
        self.run_js(r'''
(async()=>{
let calls=[];api=async(path,method,body)=>{calls.push({path,method,body});return {};};refreshTObserve=async()=>{};
T_OBSERVE={cycles:[{id:'old',code:'600641',date:'2026-11-10',status:'open',quantity:100,buyback_quantity:0,expired:true}]};
const fields={cycle_id:{value:'old'},quantity:{value:'100'},price:{value:'33.5'},buy_filled:{checked:true},leave_cash:{checked:false}},form={elements:fields,reset(){}},event={preventDefault(){},target:form};
await saveTBuyback(event);await closeTCycle(event);if(calls.length)throw Error('expired buyback or unconfirmed close posted');
fields.leave_cash.checked=true;await closeTCycle(event);
if(calls.length!==1||calls[0].path!=='/api/t-cycles/old/close'||calls[0].body.leave_cash!==true)throw Error('cash close not explicit');
})()
''')

    def test_t_signals_reach_cards_popup_and_clear_when_stale(self):
        self.run_js(r'''
const event={id:'t-1',category:'t',code:'600641',name:'T测试',title:'做T高抛条件观察',price:34,level:34.1,shares:100,reason:'条件观察，不下单',at:new Date().toISOString()};
renderTradeSignals({mode:'live',stale:false,market_session:true,calendar_ok:true,quote_health:{block_trade_signals:false},risk:{valid:true,available_new_risk:0},trade_signals:[event],new_trade_signals:[event]});
if($('nav-count-t').textContent!==1||!$('signal-t-list').innerHTML.includes('做T高抛条件观察'))throw Error('t signal filtered out');
if(popupActive?.category!=='t'||!$('trade-popup-tag').textContent.includes('做T条件观察'))throw Error('T popup filtered or wrong label');
renderTradeSignals({mode:'live',stale:true,market_session:true,calendar_ok:true,trade_signals:[event],new_trade_signals:[event]});
if(popupActive?.category==='t'||!$('trade-signal-popup').className.includes('hidden'))throw Error('stale T popup remained active');
if($('nav-count-t').textContent!==0)throw Error('stale T badge remained current');
''')

if __name__=='__main__':unittest.main()
