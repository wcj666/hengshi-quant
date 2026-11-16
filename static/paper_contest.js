const $=id=>document.getElementById(id);
const cash=x=>x==null?'—':'¥'+Number(x).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const price=x=>x==null?'—':Number(x).toFixed(3);
const time=x=>x?new Date(x).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';
const tone=x=>x>0?'gain':x<0?'loss':'neutral';
const profit=x=>x==null?'—':(x>0?'+':x<0?'−':'')+cash(Math.abs(x));
function cell(text,small){const td=document.createElement('td');td.textContent=text;if(small){const note=document.createElement('small');note.textContent=small;td.append(note);}return td;}
function table(id,rows,empty,columns=6){const body=$(id);body.replaceChildren();if(!rows.length){const tr=document.createElement('tr'),td=cell(empty);td.colSpan=columns;td.className='empty';tr.append(td);body.append(tr);}else body.append(...rows);}
let refreshing=false;
async function refresh(pullQuotes=false){
 if(refreshing)return;
 refreshing=true;
 $('refresh').disabled=true;
 try{
  let options={cache:'no-store'},url='/api/paper-contest';
  if(pullQuotes){
   const session=await fetch('/api/session',{cache:'no-store'});if(!session.ok)throw Error('会话读取失败');
   const token=await session.json();options={...options,method:'POST',headers:{'X-CSRF-Token':token.csrf_token}};url+='/refresh';
  }
  const r=await fetch(url,options);const body=await r.json();if(!r.ok)throw Error(body.error||'读取失败');const d=body.snapshot||body;
  $('period').textContent=d.start+' — '+d.end+' · 北京时间';
  $('equity').textContent=cash(d.equity);$('source').textContent=d.source+' · '+time(d.asof);
  const daily=d.daily||{};
  $('daily-label').textContent=(daily.date?daily.date.slice(5)+' ':'')+'当日盈亏';
  $('daily-pnl').textContent=profit(daily.pnl);$('daily-pnl').className=tone(daily.pnl);
  $('daily-basis').textContent=daily.pnl==null?(daily.message||'等待读取收盘基准'):
    (daily.return_pct>0?'+':'')+daily.return_pct.toFixed(2)+'% · 基准 '+daily.basis_asof.slice(5,10)+' '+daily.basis_label;
  $('pnl').textContent=profit(d.net_pnl);$('pnl').className=tone(d.net_pnl);$('return').textContent='比赛收益率 '+Number(d.return_pct).toFixed(2)+'% · 起点 '+cash(d.initial_equity);
  $('cash').textContent=cash(d.cash);$('exposure').textContent=d.exposure_pct==null?'持仓估值待核验':'持仓仓位 '+d.exposure_pct.toFixed(2)+'%';
  $('trade-count').textContent=d.trade_count+' 笔';$('drawdown').textContent='最大观察回撤 '+cash(d.maximum_drawdown);$('holding-count').textContent=d.positions.length+' 只持仓';
  $('status').className='status'+(d.stale||d.paused?' warn':'');$('status').textContent=d.label+' · 来源时间 '+time(d.asof)+(d.paused?'。已暂停新增，继续管理旧仓风险。':'。')+(d.trade_count?'':' 尚未发生虚拟买卖，当前为承接开赛持仓的估值。');
  table('holdings',d.positions.map(p=>{const tr=document.createElement('tr');tr.append(cell(p.name,p.code+' · '+(p.opening_inventory?'承接开赛持仓':'虚拟新买')),
   cell(p.quantity.toLocaleString()+' / '+p.sellable.toLocaleString()),cell(price(p.cost)),cell(price(p.price)+' / '+cash(p.value),time(p.source_timestamp)),
   cell(profit(p.daily_pnl),p.previous_close!=null?'昨收 '+price(p.previous_close):null),
   cell(profit(p.contest_pnl)),cell(price(p.stop_loss),p.stop_loss==null?'固定退出条件待核验':null));
   tr.children[4].className=tone(p.daily_pnl);tr.children[5].className=tone(p.contest_pnl);return tr;}),'暂无模拟持仓',7);
  table('trades',[...d.trades].reverse().map(t=>{const tr=document.createElement('tr');tr.append(cell(time(t.at)),cell(t.name,t.code),cell((t.side==='buy'?'模拟买入':'模拟卖出')+' / '+t.quantity),cell(price(t.price)),cell(cash(t.fee.total)+' / '+cash(t.slippage_estimate)),cell(t.reason));tr.children[5].className='reason';return tr;}),'尚未发生虚拟成交；不会将复制的初始持仓记成买入。');
  $('costs').textContent=d.fees.note;$('opening').textContent='开赛前券商总盈亏 '+cash(d.opening_broker_total_pnl)+' 单独保留，不计入本次比赛收益。';
  const feed=d.refresh||{};
  $('refresh-status').textContent=(feed.status==='error'?'最近取价失败：'+feed.message+'；保留上次有效估值。':d.phase_message)+
   ' · 最近取价成功 '+time(feed.last_success_at)+' · 页面读取 '+time(new Date().toISOString());
 }catch(e){$('status').className='status warn';$('status').textContent='模拟数据读取失败：'+e.message+'。页面上一次显示的数字仅供历史参考。';}
 finally{$('refresh').disabled=false;refreshing=false;}
}
$('refresh').addEventListener('click',()=>refresh(true));refresh();setInterval(()=>refresh(),15000);
