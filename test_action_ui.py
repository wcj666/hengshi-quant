from pathlib import Path
import unittest,subprocess,json

class ActionUITests(unittest.TestCase):
 def test_sound_ack_and_stale_controls(self):
  source=Path('static/action_center.js').read_text().replace("document.addEventListener('DOMContentLoaded',init);","globalThis.testAPI={drawAlerts,drawCards,resetAlerts,beep,enable:()=>{sound=true;audio={currentTime:0,destination:{},createOscillator:()=>({connect(){},frequency:{},start(){globalThis.rings++},stop(){}}),createGain:()=>({connect(){},gain:{value:0,exponentialRampToValueAtTime(){}}})}}};")
  harness=r'''
const vm=require('vm');const nodes=new Map();const el=id=>{if(!nodes.has(id))nodes.set(id,{innerHTML:'',textContent:'',replaceChildren(){this.innerHTML=''}});return nodes.get(id)};
const context={window:{},document:{getElementById:el},Date,Map,Set,JSON,Number,String,Array,localStorage:{getItem(){return null},setItem(){}},rings:0};vm.createContext(context);vm.runInContext(SOURCE,context);
vm.runInContext(`const alert={id:'a',day:'2026-11-16',name:'ETF',kind:'T高抛',native:true,side:'sell',quantity:100,level:1,feedback:{},status:'pending'};
testAPI.enable();testAPI.drawAlerts({valid:true,alerts:[alert]});if(rings!==1)throw Error('no sound');testAPI.drawAlerts({valid:true,alerts:[alert]});if(rings!==1)throw Error('too frequent');
if(!document.getElementById('strong-alerts').innerHTML.includes('已看到'))throw Error('no feedback');
testAPI.drawAlerts({valid:true,alerts:[{...alert,feedback:{action:'seen'}}]});if(document.getElementById('strong-alerts').innerHTML)throw Error('ack still flashing');
testAPI.drawAlerts({valid:false,alerts:[alert]});if(document.getElementById('strong-alerts').innerHTML)throw Error('stale alarm');
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
