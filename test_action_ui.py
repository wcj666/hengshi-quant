from pathlib import Path
import unittest,subprocess,json

class ActionUITests(unittest.TestCase):
 def test_sound_ack_and_stale_controls(self):
  source=Path('static/action_center.js').read_text().replace("document.addEventListener('DOMContentLoaded',init);","globalThis.testAPI={drawAlerts,drawCards,resetAlerts,renderCard,expire,beep,enable:()=>{sound=true;audio={currentTime:0,destination:{},createOscillator:()=>({connect(){},frequency:{},start(){globalThis.rings++},stop(){}}),createGain:()=>({connect(){},gain:{value:0,exponentialRampToValueAtTime(){}}})}}};")
  harness=r'''
const vm=require('vm');const nodes=new Map();const el=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',replaceChildren(){this.innerHTML=''}});return nodes.get(id)};
const context={window:{},document:{getElementById:el},Date,Map,Set,JSON,Number,String,Array,localStorage:{getItem(){return null},setItem(){}},rings:0};vm.createContext(context);vm.runInContext(SOURCE,context);
vm.runInContext(`const alert={id:'a',day:'2026-11-16',name:'ETF',kind:'T高抛',native:true,side:'sell',quantity:100,level:1,feedback:{},status:'pending'};
testAPI.enable();testAPI.drawAlerts({valid:true,alerts:[alert]});if(rings!==1)throw Error('no sound');testAPI.drawAlerts({valid:true,alerts:[alert]});if(rings!==1)throw Error('too frequent');
if(!document.getElementById('strong-alerts').innerHTML.includes('已看到'))throw Error('no feedback');
testAPI.drawAlerts({valid:true,alerts:[{...alert,feedback:{action:'seen'}}]});if(document.getElementById('strong-alerts').innerHTML)throw Error('ack still flashing');
testAPI.drawAlerts({valid:false,alerts:[alert]});if(document.getElementById('strong-alerts').innerHTML)throw Error('stale alarm');
const card={code:'159971',name:'测试',state:'buy',label:'买入复核',quantity:100,deadline:100,price:.71,thresholds:[{label:'买入触发',price:.700,max_price:.712,direction:'at_or_above',distance:-.01}],t:{cycle:true,sell_price:.75,remaining_quantity:100,buyback_cap:.71,deadline:90}};
const data={cards:[card],alerts:[{...alert,deadline:100}],deadline:200};
const html=testAPI.renderCard(card,false);
if(!html.includes('最高接受价 0.712')||!html.includes('接回最高参考价'))throw Error('price thresholds not visible');
testAPI.expire(data,91);
if(card.t.buyback_cap!=null||card.t.remaining_quantity!==100||card.quantity!==100)throw Error('T expiry should preserve actual inventory and other actions');
testAPI.expire(data,101);
if(card.quantity!=null||card.thresholds[0].distance!=null||data.alerts.length)throw Error('expired action left executable');
testAPI.resetAlerts('过期');if(!document.getElementById('action-cards').innerHTML.includes('撤销'))throw Error('stale card kept');`,context);
'''.replace('SOURCE',json.dumps(source))
  r=subprocess.run(['node','-e',harness],capture_output=True,text=True)
  self.assertEqual(r.returncode,0,r.stderr)
 def test_controls_have_unique_ids(self):
  import re
  text=Path('templates/dashboard.html').read_text();ids=re.findall(r'\bid="([^"]+)"',text)
  self.assertEqual(len(ids),len(set(ids)))
  for i in ['strong-alerts','action-cards','execution-mode','native-sound','t-performance-summary']:self.assertIn(i,ids)

if __name__=='__main__':unittest.main()
