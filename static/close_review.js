const $=id=>document.getElementById(id);
const money=x=>x==null?'—':'¥'+Number(x).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const price=x=>x==null?'—':Number(x).toFixed(3);
const time=x=>x?new Date(x).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
const td=(value,small)=>{const node=document.createElement('td');node.textContent=value;if(small){const note=document.createElement('small');note.textContent=small;node.append(note)}return node};
function rows(id,list,empty,build){const body=$(id);body.replaceChildren();if(!list.length){const tr=document.createElement('tr');const cell=td(empty);cell.colSpan=5;cell.className='empty';tr.append(cell);body.append(tr);return}body.append(...list.map(build));}
async function refresh(){
  $('refresh').disabled=true;
  try{
    const response=await fetch('/api/close-review',{cache:'no-store'});const data=await response.json();if(!response.ok)throw Error(data.error||'读取失败');
    $('status').className='status'+(data.data_verified?'':' warn');$('status').textContent=data.execution_status+(data.errors.length?' · '+data.errors.join('；'):'');
    $('source').textContent='账户快照 '+time(data.account_asof)+' · 行情来源时间 '+time(data.generated_at);
    $('equity').textContent=money(data.equity);$('cash').textContent=money(data.cash);$('after-sales').textContent=money(data.cash_policy.cash_after_planned_sales);$('count').textContent=(data.position_count||0)+' 只';
    $('exposure').textContent=data.current_exposure_pct==null?'仓位待核验':'估算仓位 '+data.current_exposure_pct.toFixed(2)+'%';
    $('reserve').textContent='建议至少保留 '+money(data.cash_policy.minimum_reserve);
    $('buy-budget').textContent='全部减仓后的情景现金 '+money(data.cash_policy.cash_after_all_planned_sales);
    $('errors').textContent=data.errors.length?'数据待补齐':'行情和持仓完整';
    rows('sells',data.sell_plan,'当前没有符合规则的卖出计划',item=>{const tr=document.createElement('tr');tr.append(td(item.name,item.code),td(item.quantity.toLocaleString()),td(price(item.price)),td(money(item.value)),td(item.reason));return tr});
    rows('pending-sells',data.pending_sell_plan||[],'当前没有待解锁卖出计划',item=>{const tr=document.createElement('tr');tr.append(td(item.name,item.code),td(item.quantity.toLocaleString()),td(item.available_now.toLocaleString()),td(price(item.price)),td(item.reason));return tr});
    rows('buys',data.buy_plan,'暂无次日研究候选',item=>{const tr=document.createElement('tr');tr.append(td(item.name,item.code),td('待盘中核验'),td(price(item.trigger_price)+' / '+price(item.entry_price)),td([item.stop_loss,item.take_profit_1,item.take_profit_2].map(price).join(' / ')),td(item.reason));return tr});
    rows('positions',data.positions,'暂无持仓',item=>{const tr=document.createElement('tr');const pct=item.daily_pct==null?null:item.daily_pct;tr.append(td(item.name,item.code),td(item.quantity.toLocaleString()),td(price(item.price)),td(pct==null?'—':(pct>0?'+':'')+pct.toFixed(2)+'%'),td(money(item.value)));tr.children[3].className=pct>0?'gain':pct<0?'loss':'';return tr});
    $('notes').replaceChildren(...data.notes.map(note=>{const li=document.createElement('li');li.textContent=note;return li}));
  }catch(error){$('status').className='status warn';$('status').textContent='复盘读取失败：'+error.message}
  finally{$('refresh').disabled=false}
}
$('refresh').addEventListener('click',refresh);refresh();
