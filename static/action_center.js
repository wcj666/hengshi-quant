/* Explicit feedback is not a fill. Strong alerts always come from current server state. */
(()=>{
'use strict';
window.hengshiActionCenter=true;
const node=id=>document.getElementById(id), text=x=>String(x??''), esc=x=>text(x).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const amount=x=>Number.isFinite(x)?'¥'+x.toFixed(2):'—',price=x=>Number.isFinite(x)?x.toFixed(3):'—';
let busy=false,audio=null,sound=false,preferencesDirty=false,loadedPrefs=false,lastSuccess=0;
const notices=new Map();
try{for(const [key,value] of JSON.parse(localStorage.getItem('hengshi_strong_alert_counts')||'[]'))notices.set(key,value);}catch{}
function beep(){if(!sound||!audio)return;const o=audio.createOscillator(),g=audio.createGain();o.connect(g);g.connect(audio.destination);o.frequency.value=760;g.gain.value=.08;o.start();g.gain.exponentialRampToValueAtTime(.001,audio.currentTime+.35);o.stop(audio.currentTime+.4);}
function resetAlerts(message){node('strong-alerts').replaceChildren();node('action-status').textContent=message;node('action-cards').innerHTML='<p class="tip">实时行动卡已撤销，恢复数据后重新核验。</p>';}
function drawCards(d){
 node('action-status').textContent=(d.valid?'盘中条件已核验':'暂停实时执行 · 休市或数据待核验')+(d.mode==='exit_only'?' · 退出休息模式':'');
 node('action-note').textContent=(d.cash_floor!=null?'总仓规则对应现金至少 '+amount(d.cash_floor)+'。':'')+d.note;
 node('action-validation').textContent=d.validation_note;
 if(!loadedPrefs&&!preferencesDirty){node('execution-mode').value=d.preferences.mode;node('native-sound').checked=d.preferences.native_sound;loadedPrefs=true;}
 node('action-cards').innerHTML=d.cards.map(c=>`<article class="action-card ${esc(c.state)}"><div class="action-heading"><strong>${esc(c.name)} ${esc(c.code)}</strong><b>${esc(c.label)}</b></div><p>${esc(c.reason)}</p><dl><div><dt>最新参考</dt><dd>${price(c.price)}</dd></div><div><dt>触发参考</dt><dd>${price(c.level)}</dd></div><div><dt>本次核验上限</dt><dd>${c.quantity==null?'待核验':esc(c.quantity)+'份/股'}</dd></div><div><dt>本笔后现金估算</dt><dd>${amount(c.cash_after)}</dd></div></dl><details><summary>固定计划、失效条件与费用</summary><p>买入触发 ${price(c.plan.trigger_price)} / 最高接受价 ${price(c.plan.entry_price)} / 止损 ${price(c.plan.stop_loss)} / 止盈 ${price(c.plan.take_profit_1)}、${price(c.plan.take_profit_2)}</p><p>以上是已记录的固定计划，不代表当前可以买卖。计划日期 ${esc(c.plan.signal_date||'待补')}；有效日 ${esc(c.plan.valid_on||'须按规则核验')}。</p><p>费用预留 ${amount(c.fee)} · ${esc(c.fee_note||'数量或价格待核验')}</p><ul>${c.cancel.map(x=>'<li>'+esc(x)+'</li>').join('')}</ul><p>券商数量证据：${esc(c.inventory_at||'待核验')}；报价：${esc(c.quote_at||'待核验')}。</p></details>${c.alert_id?'<a href="/trade-reconcile?alert_id='+encodeURIComponent(c.alert_id)+'">登记实际成交</a>':''}</article>`).join('')||'<p>暂无当前持仓。</p>';
}
function drawAlerts(d){
 const active=d.valid?(d.alerts||[]).filter(a=>!['seen','deferred'].includes(a.feedback?.action)&&!['seen','deferred','partial'].includes(a.status)&&a.native):[];
 node('strong-alerts').innerHTML=active.map(a=>`<article class="strong-alert ${a.side==='sell'?'sell':a.side==='buy'?'buy':'risk'}"><div><strong>${esc(a.name)} · ${esc(a.kind)}</strong><p>${esc(a.reason)}</p><span>触发价 ${price(a.level)} · 数量 ${a.quantity==null?'待核验':esc(a.quantity)} · 提醒不是成交</span></div><div class="action-buttons"><button data-id="${esc(a.id)}" data-day="${esc(a.day)}" data-feedback="seen">已看到</button><button data-id="${esc(a.id)}" data-day="${esc(a.day)}" data-feedback="deferred">暂不执行</button>${a.side?'<a href="/trade-reconcile?alert_id='+encodeURIComponent(a.id)+'">登记成交</a>':''}</div></article>`).join('');
 const now=Date.now();let ring=false;
 for(const a of active){const n=notices.get(a.id)||{count:0,last:0};if(n.count<3&&now-n.last>=60000){if(sound){ring=true;n.count++;n.last=now;}notices.set(a.id,n);}}
 if(ring){beep();try{localStorage.setItem('hengshi_strong_alert_counts',JSON.stringify([...notices].slice(-250)));}catch{}}
}
async function refresh(){if(busy)return;busy=true;try{const d=await api('/api/action-center');drawCards(d);drawAlerts(d);lastSuccess=Date.now();}catch(e){resetAlerts('读取失败，暂停提醒：'+e.message);}finally{busy=false;}}
async function review(){try{const d=await api('/api/t-performance'),s=d.summary;node('t-performance-summary').innerHTML=`<div class="action-metrics"><span>实际配对净收益 <b>${amount(s.actual_paired_net)}</b></span><span>费用待补的估算 <b>${amount(s.estimated_paired_net)}</b></span><span>已结束且费用完整样本 <b>${esc(s.closed_actual_samples??0)}</b></span></div><p>${(d.notes||[]).map(esc).join('；')}</p><p>统计窗口 ${esc(d.window_start)} 至 ${esc(d.window_end)}。未接回部分的持有对比：${amount(s.unmatched_mark_comparison)}（独立估算，非已实现T收益）。</p>`;}catch(e){node('t-performance-summary').textContent='复盘暂不可用：'+e.message;}}
async function feedback(e){const b=e.target.closest('[data-feedback]');if(!b)return;b.disabled=true;try{await api('/api/action-feedback/'+encodeURIComponent(b.dataset.id),'POST',{action:b.dataset.feedback,day:b.dataset.day});await refresh();}catch(x){showToast(x.message,true);}finally{b.disabled=false;}}
function init(){
 if(!node('action-cards'))return;
 node('strong-alerts').addEventListener('click',feedback);
 node('action-refresh').addEventListener('click',refresh);
 for(const id of ['execution-mode','native-sound'])node(id).addEventListener('change',()=>{preferencesDirty=true;});
 node('save-execution').addEventListener('click',async()=>{try{const d=await api('/api/execution-preferences','PUT',{mode:node('execution-mode').value,native_sound:node('native-sound').checked});preferencesDirty=false;loadedPrefs=false;showToast(d.message);resetAlerts('设置已改变，等待重新核验');await refresh();}catch(e){showToast(e.message,true);}});
 node('enable-action-sound').addEventListener('click',async()=>{try{audio=audio||new (window.AudioContext||window.webkitAudioContext)();await audio.resume();sound=true;beep();node('action-sound-status').textContent='网页声音已启用：每条持续有效的提醒最多响3次，间隔至少60秒；已看到或失效立即停止。';}catch(e){showToast('浏览器未允许声音，请检查浏览器设置。',true);}});
 node('mute-action-sound').addEventListener('click',()=>{sound=false;node('action-sound-status').textContent='网页声音已关闭；桌面通知音效由上方独立设置。';});
 refresh();review();setInterval(refresh,10000);setInterval(review,60000);
 setInterval(()=>{if(lastSuccess&&Date.now()-lastSuccess>45000)resetAlerts('更新超过45秒，停止沿用旧行动卡。');},5000);
}
document.addEventListener('DOMContentLoaded',init);
})();
