from pathlib import Path
import unittest,subprocess,json

class ActionUITests(unittest.TestCase):
 def test_sound_ack_and_stale_controls(self):
  source=Path('static/action_center.js').read_text().replace("document.addEventListener('DOMContentLoaded',init);","globalThis.testAPI={drawAlerts,drawCards,resetAlerts,renderCard,expire,beep,enable:()=>{sound=true;audio={currentTime:0,destination:{},createOscillator:()=>({connect(){},frequency:{},start(){globalThis.rings++},stop(){}}),createGain:()=>({connect(){},gain:{value:0,exponentialRampToValueAtTime(){}}})}}};")
  harness=r'''
const vm=require('vm');const nodes=new Map();const el=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',replaceChildren(){this.innerHTML=''}});return nodes.get(id)};
const context={window:{},document:{getElementById:el},Date,Map,Set,JSON,Number,String,Array,localStorage:{getItem(){return null},setItem(){}},rings:0};vm.createContext(context);vm.runInContext(SOURCE,context);
vm.runInContext(`const alert={id:'a',day:'2026-11-16',name:'ETF',kind:'T高抛',native:true,side:'sell',quantity:100,level:1,feedback:{},status:'pending',deadline:Date.now()+30000};
testAPI.enable();testAPI.drawAlerts({valid:true,alerts:[alert]});if(rings!==1)throw Error('no sound');testAPI.drawAlerts({valid:true,alerts:[alert]});if(rings!==1)throw Error('too frequent');
if(!document.getElementById('strong-alerts').innerHTML.includes('已看到'))throw Error('no feedback');
testAPI.drawAlerts({valid:true,alerts:[{...alert,feedback:{action:'seen'}}]});if(document.getElementById('strong-alerts').innerHTML)throw Error('ack still flashing');
testAPI.drawAlerts({valid:false,alerts:[alert]});if(document.getElementById('strong-alerts').innerHTML)throw Error('stale alarm');
const observation={...alert,id:'observe',side:null,quantity:null,repeatable:false,deadline:Date.now()+120000};
testAPI.drawAlerts({valid:true,alerts:[observation]});if(rings!==2)throw Error('observation silent');
const wallNow=Date.now;Date.now=()=>wallNow()+61000;testAPI.drawAlerts({valid:true,alerts:[observation]});Date.now=wallNow;if(rings!==2)throw Error('observation repeated sound');
const card={code:'159971',name:'测试',state:'buy',label:'买入复核',quantity:100,deadline:100,price:.71,thresholds:[{label:'买入触发',price:.700,max_price:.712,direction:'at_or_above',distance:-.01}],t:{cycle:true,sell_price:.75,remaining_quantity:100,buyback_cap:.71,break_even_buy_price:.72,minimum_gross:40,fee_basis:['actual_sell','reserve'],fee_coverage_assumption:20,fee_reserve:20,blockers:['现金不足'],deadline:90}};
const data={cards:[card],alerts:[{...alert,deadline:100}],deadline:200};
const html=testAPI.renderCard(card,false);
if(!html.includes('最高接受价 0.712')||!html.includes('接回最高参考价'))throw Error('price thresholds not visible');
if(!html.includes('本轮保本接回价')||!html.includes('现金不足')||!html.includes('费用与滑点预留')||!html.includes('覆盖试算'))throw Error('T price and cost diagnostics missing');
testAPI.expire(data,91);
if(card.t.buyback_cap!=null||card.t.remaining_quantity!==100||card.quantity!==100)throw Error('T expiry should preserve actual inventory and other actions');
testAPI.expire(data,101);
if(card.quantity!=null||card.thresholds[0].distance!=null||data.alerts.length)throw Error('expired action left executable');
testAPI.resetAlerts('过期');if(!document.getElementById('action-cards').innerHTML.includes('撤销'))throw Error('stale card kept');`,context);
'''.replace('SOURCE',json.dumps(source))
  r=subprocess.run(['node','-e',harness],capture_output=True,text=True)
  self.assertEqual(r.returncode,0,r.stderr)
 def test_standalone_bottom_observation_feedback_and_expiry(self):
  source=Path('static/action_center.js').read_text().replace("document.addEventListener('DOMContentLoaded',init);", "globalThis.testAPI={init,refresh,drawAlerts,checkExpiry,resetAlerts};")
  harness=r'''
const vm=require('vm'),assert=require('assert');
const nodes=new Map(),timers=[];
function element(){return {innerHTML:'',textContent:'',hidden:false,events:{},setAttribute(){},addEventListener(name,fn){this.events[name]=fn},replaceChildren(){this.innerHTML=''}}}
const document={hidden:false,getElementById:id=>nodes.get(id)||null,querySelector:()=>({content:'old-token'}),createElement:element,body:{appendChild(el){nodes.set(el.id,el)}},addEventListener(){}};
const context={window:{},document,Date,Map,Set,JSON,Number,String,Array,Promise,console,process,timers,setImmediate,setInterval(fn,ms){timers.push({fn,ms})},localStorage:{getItem(){return null},setItem(){}}};
vm.createContext(context);vm.runInContext(SOURCE,context);
vm.runInContext(`(async()=>{
 const assert=(value,message)=>{if(!value)throw Error(message)},tick=()=>new Promise(resolve=>setImmediate(resolve));
 const asof=new Date().toISOString(),expires_at=new Date(Date.now()+30000).toISOString();
 const observation={id:'observe-1',code:'159971',day:'2026-11-17',name:'创业100',kind:'结构观察',reason:'等待有效结构确认',native:true,side:null,quantity:null,status:'pending',expires_at};
 let snapshot={valid:true,asof,expires_at,cards:[{code:'159971',state:'wait',thresholds:[],expires_at}],alerts:[observation]};
 let calls=[],postCount=0;
 globalThis.fetch=async(path,options={})=>{
  calls.push({path,options});
  if(path==='/api/session')return {ok:true,status:200,json:async()=>({csrf_token:'renewed-token'})};
  if(options.method==='POST'){
   if(++postCount===1)return {ok:false,status:403,json:async()=>({code:'csrf_expired'})};
   snapshot={...snapshot,alerts:[{...observation,feedback:{action:'seen'}}]};
   return {ok:true,status:200,json:async()=>({ok:true})};
  }
  return {ok:true,status:200,json:async()=>snapshot};
 };
 testAPI.init();await tick();
 const bottom=document.getElementById('action-bottom-alert');
 assert(bottom&&!bottom.hidden&&bottom.innerHTML.includes('创业100'),'fresh held observation missing on standalone page');
 assert(bottom.innerHTML.includes('条件观察')&&!bottom.innerHTML.includes('登记成交')&&!bottom.innerHTML.includes('核验数量'),'observation exposed executable quantity');
 testAPI.drawAlerts({valid:true,alerts:[{...observation,deadline:Date.now()+30000},{...observation,id:'observe-2',name:'科创半导',deadline:Date.now()+30000}]});
 assert(bottom.innerHTML.includes('创业100')&&bottom.innerHTML.includes('科创半导')&&bottom.innerHTML.includes('共 2 条'),'standalone popup hid another active observation');
 assert(calls.every(call=>call.path==='/api/action-center'),'light mode fetched full dashboard data');
 assert(timers.some(timer=>timer.ms===1000)&&timers.some(timer=>timer.ms===10000),'light mode not refreshing and expiring');
 const button={dataset:{id:observation.id,day:observation.day,feedback:'seen'},disabled:false};
 bottom.events.click({target:{closest:selector=>selector==='[data-feedback]'?button:null}});await tick();
 assert(bottom.hidden&&!bottom.innerHTML,'seen observation remained in bottom popup');
 const sent=calls.filter(call=>call.options.method==='POST');
 assert(sent.length===2&&JSON.parse(sent[1].options.body).action==='seen','feedback was not saved through shared endpoint');
 assert(sent[1].options.headers['X-CSRF-Token']==='renewed-token','standalone feedback did not renew security session');
 const active={...observation,deadline:Date.now()+30000};
 testAPI.drawAlerts({valid:true,alerts:[active]});assert(!bottom.hidden,'fresh popup not shown');
 testAPI.drawAlerts({valid:false,alerts:[active]});assert(bottom.hidden&&!bottom.innerHTML,'invalid market left popup visible');
 testAPI.drawAlerts({valid:true,deadline:Date.now()-1,alerts:[active]});assert(bottom.hidden,'global expiry left popup visible');
 testAPI.drawAlerts({valid:true,alerts:[{...active,deadline:Date.now()-1}]});assert(bottom.hidden,'alert expiry left popup visible');
 testAPI.drawAlerts({valid:true,cards:[{code:active.code,state:'paused'}],alerts:[active]});assert(bottom.hidden,'paused card left popup visible');
 testAPI.drawAlerts({valid:true,alerts:[{...active,deadline:undefined}]});assert(bottom.hidden,'unbounded reminder shown');
 testAPI.drawAlerts({valid:true,alerts:[{...active,feedback:{action:'deferred'}}]});assert(bottom.hidden,'deferred popup returned');
 testAPI.drawAlerts({valid:true,alerts:[active]});testAPI.resetAlerts('过期');assert(bottom.hidden&&!bottom.innerHTML,'reset did not remove bottom popup');
})().catch(error=>{console.error(error);process.exitCode=1});`,context);
'''.replace('SOURCE',json.dumps(source))
  result=subprocess.run(['node','-e',harness],capture_output=True,text=True)
  self.assertEqual(result.returncode,0,result.stderr)
 def test_controls_have_unique_ids(self):
  import re
  text=Path('templates/dashboard.html').read_text();ids=re.findall(r'\bid="([^"]+)"',text)
  self.assertEqual(len(ids),len(set(ids)))
  for i in ['strong-alerts','action-cards','execution-mode','native-sound','t-performance-summary']:self.assertIn(i,ids)

if __name__=='__main__':unittest.main()
