'use strict';
let csrf = document.querySelector('meta[name="csrf-token"]').content;
let sessionRefresh=null;
const $=id=>document.getElementById(id);
let monitorBusy=false;
let STORE=null, ranking='verified', editCode=null, busy=false;
let scanJob=null, scanStarting=false, scanPolling=false;
let EXIT_SUGGESTIONS={},planSuggestionSerial=0;
let DAILY=null, STRATEGY=null;
let T_OBSERVE=null,tObserveBusy=false,tWriteBusy=false;
let popupQueue=[],popupActive=null,signalSeen=new Set();
try{signalSeen=new Set(JSON.parse(localStorage.getItem('henga_signal_seen_v33')||'[]'));}catch{}
const format=(v,n=2)=>v===null||v===undefined||v===''?'—':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:n,maximumFractionDigits:n});
const money=v=>v===null||v===undefined?'—':'¥'+format(v,2);
const escapeHtml=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const val=v=>v===null||v===undefined?'—':escapeHtml(String(v));
const pct=v=>v===null||v===undefined?'—':format(v,2)+'%';
const chg=v=>{let x=Number(v);return v===null||v===undefined?'':(x>0?'up':x<0?'down':'');};
const positive=v=>Number(v)>=0?'up':'down';
const signedClass=v=>v===null||v===undefined||v===''?'':(Number(v)>0?'pnl-gain':Number(v)<0?'pnl-loss':'pct-flat');
const signedPctText=v=>v===null||v===undefined?'—':`<span class="${signedClass(v)}">${Number(v)>0?'+':''}${format(v,2)}%</span>`;
const signedMoneyText=v=>v===null||v===undefined?'—':`<span class="${signedClass(v)}">${Number(v)>0?'+':''}${money(v)}</span>`;
const signedPriceText=(price,signal,digits=3)=>price===null||price===undefined?'—':`<span class="${signedClass(signal)}">${format(price,digits)}</span>`;
function riskTier(r){
 const node=$('risk-tier-banner');if(!node)return;
 const x=r?.alert_level; const key=x?.level||'unavailable';
 node.className='risk-tier-banner tier-'+key;
 node.textContent=x?`三级账户回撤提醒：${x.label} · 回撤已用 ${x.used_pct??'—'}%`:'三级风险预警：等待完整有效持仓行情';
}
async function showTrades(){
 const d=await api('/api/trades');const rows=(d.rows||[]).map(x=>tr([escapeHtml(x.date),escapeHtml(x.code),x.side==='buy'?'<span class="pnl-gain">买入</span>':'<span class="pnl-loss">卖出</span>',format(x.quantity,0),format(x.price,3),money(x.fee),money(x.net_cash_change),escapeHtml(x.note||'—')]));
 $('trades-table').innerHTML=rows.length?table(['日期','代码','方向','数量','成交价','费用','现金变化（仅日志）','备注'],rows):'<p class="tip">暂无人工登记的真实成交；不会自动推测以前的交易。</p>';
}
function curveSvg(rows){
 if(!rows.length)return '<p class="tip">暂无连续净值记录；软件只会从启用本功能后开始积累有效公开行情快照。</p>';
 if(rows.length===1)return `<div class="curve-single">${escapeHtml(rows[0].day_cn)} · 估算净值 ${money(rows[0].equity_estimate)}</div>`;
 const W=880,H=240,L=68,R=20,T=22,B=38;
 const values=rows.map(x=>Number(x.equity_estimate));let lo=Math.min(...values),hi=Math.max(...values);if(lo===hi){lo-=1;hi+=1;}const pad=(hi-lo)*.12;lo-=pad;hi+=pad;
 const x=i=>L+i*(W-L-R)/(rows.length-1),y=v=>H-B-(v-lo)/(hi-lo)*(H-T-B);
 const ticks=[lo,(lo+hi)/2,hi].map(v=>`<line x1="${L}" y1="${y(v).toFixed(1)}" x2="${W-R}" y2="${y(v).toFixed(1)}" stroke="#e2e8ef"/><text x="${L-7}" y="${y(v)+4}" fill="#8395a7" font-size="10" text-anchor="end">${format(v,0)}</text>`).join('');
 // Break visual lines when manual portfolio settings changed: otherwise jump misleads.
 let segments=[],part=[];
 rows.forEach((r,i)=>{if(i&&r.config_hash!==rows[i-1].config_hash){if(part.length)segments.push(part);part=[];}part.push(`${x(i).toFixed(1)},${y(values[i]).toFixed(1)}`)});if(part.length)segments.push(part);
 const paths=segments.map(a=>`<polyline fill="none" stroke="#c44c55" stroke-width="2.5" points="${a.join(' ')}"/>`).join('');
 return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="升级后已记录的估算资产曲线，账户配置发生变化时断开">${ticks}${paths}<text x="${L}" y="${H-9}" font-size="11" fill="#7a8b99">${escapeHtml(rows[0].day_cn)}</text><text x="${W-R}" y="${H-9}" text-anchor="end" font-size="11" fill="#7a8b99">${escapeHtml(rows.at(-1).day_cn)}</text></svg>`;
}
async function refreshCurve(){try{const d=await api('/api/equity-curve');$('equity-curve-chart').innerHTML=curveSvg(d.rows||[]);$('curve-source').textContent=d.source||'';}catch(e){$('curve-source').textContent='历史净值记录加载失败：'+e.message;}}
async function simulateReduction(e){e.preventDefault();const f=e.target;
 $('reduction-result').textContent='正在读取本轮有效持仓行情…';
 try{const d=await api('/api/reduction','POST',{code:f.elements['code'].value,quantity:Number(f.elements['quantity'].value)});
  $('reduction-result').innerHTML=`<strong>仅模拟：${escapeHtml(d.name)} 卖出 ${format(d.quantity,0)}，参考价格 ${format(d.price,3)}</strong><p>假设卖出所得 ${money(d.sale_proceeds)}；计划止损压力 ${money(d.planned_stress_before)} → ${money(d.planned_stress_after)}（减少 ${money(d.stress_reduction)}）</p><p>模拟后可分配新交易风险：${money(d.available_new_risk_after)}；净值仍约 ${money(d.equity_after)}（忽略费用）</p><small>${escapeHtml(d.note)}</small>`;
 }catch(err){$('reduction-result').textContent='当前不能模拟：'+err.message;}}
async function saveManualTrade(e){e.preventDefault();const f=e.target;const data={};for(const k of ['date','code','side','quantity','price','fee','note'])data[k]=f.elements[k].value;
 if(!confirm('确认这是一笔已经真实发生的成交？保存记录不会自动修改持仓和现金。'))return;
 try{const r=await api('/api/trades','POST',data);showToast(r.message);await showTrades();f.elements['note'].value='';}catch(err){showToast('保存失败：'+err.message,true);}}
function renderSignalReview(v){const node=$('signal-review');if(!node)return;
 if(!v){node.textContent='缠论与MACD交叉证据待核验。';return;}
 const checks=(v.checks||[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
 node.innerHTML=`<div class="signal-review-top"><strong>${escapeHtml(v.decision||'继续观察')}</strong><span>${v.verified?'本级别证据一致':'等待进一步确认'}</span></div><ul>${checks}</ul><small>${escapeHtml(v.note||'不是自动买卖指令')}</small>`;}
function showPnl(p, sourceNote){
  if($('floating-label'))$('floating-label').innerHTML='浮动盈亏 <small>按持仓成本估算</small>';
  const f=$('floating-pnl'), d=$('daily-pnl');
  const apply=(element,amount)=>{element.textContent=amount===null||amount===undefined?'—':`${amount>0?'+':''}${money(amount)}`;element.className=amount===null||amount===undefined?'':amount>0?'pnl-gain':amount<0?'pnl-loss':'pct-flat';};
  apply(f,p?.floating_pnl);apply(d,p?.daily_pnl);
  $('floating-sub').textContent=p?.valid?`持仓成本收益率 ${pct(p.floating_pct)} · ${sourceNote}`:'行情或成本缺失，暂不计算';
  $('daily-sub').textContent=p?.daily_valid?`昨收比 ${p.daily_pct>0?'+':''}${pct(p.daily_pct)} · ${sourceNote}`:(p?.note||'缺少完整昨收报价，不计算');
}

const showToast=(message,error=false)=>{const t=$('toast');t.textContent=message;t.className='toast'+(error?' error':'');clearTimeout(t._timer);t._timer=setTimeout(()=>t.classList.add('hidden'),4300);};
async function renewSession(){
 if(!sessionRefresh)sessionRefresh=(async()=>{
  const r=await fetch('/api/session',{credentials:'same-origin',cache:'no-store'});
  const d=await r.json();
  if(!r.ok||typeof d.csrf_token!=='string'||!d.csrf_token)throw Error('安全会话恢复失败，请刷新页面后重试');
  csrf=d.csrf_token;
 })().finally(()=>{sessionRefresh=null;});
 return sessionRefresh;
}
async function api(path,method='GET',obj){
 for(let attempt=0;attempt<2;attempt++){
  const sentToken=csrf,o={method,credentials:'same-origin',headers:{'X-CSRF-Token':sentToken}};
  if(obj!==undefined){o.headers['Content-Type']='application/json';o.body=JSON.stringify(obj);}
  const r=await fetch(path,o);let data;try{data=await r.json()}catch{throw Error('服务器没有返回有效JSON');}
  // This explicit response is issued before the handler: nothing was saved or
  // launched. Retry it once; never retry ambiguous network/HTTP failures.
  if(attempt===0&&r.status===403&&data.code==='csrf_expired'){
   if(csrf===sentToken)await renewSession();
   continue;
  }
  if(!r.ok||data.error)throw Error(data.error||`HTTP ${r.status}`);
  return data;
 }
}
async function refresh(){try{const s=await api('/api/state');STORE=s;render(s);refreshStrategy();refreshMonitor();return true;}catch(e){showToast('读取失败：'+e.message,true);return false;}}
function table(headers, rows){if(!rows.length)return '<div class="empty">暂无数据。请先运行演示扫描或联网扫描。</div>';return `<table><thead><tr>${headers.map(h=>`<th>${escapeHtml(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;}
function tr(cells){return '<tr>'+cells.map(c=>`<td>${c}</td>`).join('')+'</tr>';}
function pill(t,cls='muted'){return `<span class="pill ${cls}">${escapeHtml(t)}</span>`;}

const STRATEGY_DEFAULTS={max_exposure_pct:60,max_position_pct:15,max_sector_pct:30,max_positions:4,fee_reserve:20,per_trade_risk:150,account_risk_budget:1000};
const noteText=x=>typeof x==='string'?x:(x?.reason||x?.message||x?.label||JSON.stringify(x));
function renderStrategy(data=STRATEGY,risk=STORE?.risk){
 if(!$('strategy-panel'))return;
 const d=data||{},v=risk?.strategy||d.risk||{},prefs={...STRATEGY_DEFAULTS,...(d.settings||{})};
 const enabled=d.enabled??v.enabled??false,account=v.account||d.account||{};
 let blockers=[...new Set([...(d.blockers||[]),...(v.blockers||[])].map(noteText))];
 const legacyMissing=d.legacy_positions||[];
 if(legacyMissing.length){
  blockers=blockers.filter(x=>!x.includes('待补：')&&!/已有持仓.*(缺少固定止损|待补固定止损|缺少行业分类|待补行业分类)/.test(x));
  for(const [key,label] of [['固定退出价','固定风控退出计划待补'],['行业分类','行业分类待补']]){
   const names=legacyMissing.filter(p=>(p.missing||[]).includes(key)).map(p=>p.name||p.code);
   if(names.length)blockers.unshift(label+'：'+names.join('、'));
  }
 }
 if(blockers.some(x=>x.includes('现有持仓超过')))blockers=blockers.filter(x=>!x.includes('已达同时持仓数量上限'));
 const labels=[['总权益仓位',pct(prefs.max_exposure_pct)],['单一证券',pct(prefs.max_position_pct)],['同一行业',pct(prefs.max_sector_pct)],['持仓数量',prefs.max_positions+'只'],['单笔总风险',money(prefs.per_trade_risk)],['费用滑点至少',money(prefs.fee_reserve)],['账户预算',money(prefs.account_risk_budget)]];
 $('strategy-rules').innerHTML=labels.map(([name,value])=>`<div><span>${name}</span><strong>${value}</strong></div>`).join('');
 $('strategy-status').textContent=!enabled?'V1未启用':blockers.length?'暂停新增 · 旧仓过渡':'V1已启用 · 待逐笔核验';
 $('strategy-status').className='pill '+(blockers.length?'danger':'muted');
 const initialized=account.initialized===true,verified=account.verified===true;
 $('strategy-resume-controls').hidden=account.budget_exhausted!==true;
 $('strategy-account').textContent=!initialized?'V1账户待初始化：先核对持仓、现金与起点前后资金流水；以有效完整行情建立独立记录。原11月6日历史基准保留。':`V1账户 ${verified&&!account.needs_reconcile?'已核对':'待核对'} · 调流后净值 ${money(account.adjusted_equity)} · 历史高点 ${money(account.high_water??account.high_water_mark)} · 回撤 ${money(account.drawdown)} · 起点后外部净流入 ${money(account.net_external_flow)}${account.pause_until?' · 暂停至 '+account.pause_until:''}。对账、入金及版本升级均不重置高点。`;
 const b=$('strategy-blockers');b.className='signal-note'+(blockers.length?' blocked':'');b.innerHTML=blockers.length?'<strong>当前新增阻断原因</strong><ul>'+blockers.map(x=>`<li>${escapeHtml(x)}</li>`).join('')+'</ul>':'账户规则暂无阻断；仍须通过完整日线形态、有效次日计划、时段及券商报价核验。';
 const fees=(risk?.positions||[]).reduce((sum,p)=>sum+Number(p.future_fee_reserve||0),0);
 $('strategy-exposure').innerHTML=[['当前权益仓位',pct(v.exposure_pct)],['当前持仓',v.position_count==null?'—':v.position_count+'只'],['已有仓未来费用预留',fees?money(fees):'待固定计划'],['可新增风险',money(risk?.available_new_risk)]].map(([k,x])=>`<div><span>${k}</span><strong>${x}</strong></div>`).join('');
 const legacy=d.legacy_positions||[];
 const transition=(v.transition||[]).map(noteText);
 $('strategy-legacy').innerHTML=(transition.length?'<p class="tip">'+transition.map(escapeHtml).join('；')+'</p>':'')+(legacy.length?table(['旧仓','代码','行业','待完成事项'],legacy.map(x=>tr([escapeHtml(x.name||x.code),escapeHtml(x.code),escapeHtml(x.sector||'待补'),escapeHtml((x.reasons||x.blockers||x.missing||[x.reason||'核验固定退出计划及实际持有记录']).map(noteText).join('；'))]))):'<p class="tip">暂无旧仓过渡清单；以当前后端核验为准。</p>');
 const sectors=Object.entries(v.sector_exposure||{});$('strategy-sectors').innerHTML=sectors.length?table(['行业','市值','占净值','上限'],sectors.map(([name,x])=>tr([escapeHtml(name),money(x.value??x.market_value),pct(x.pct),pct(prefs.max_sector_pct)]))):'<p class="tip">行业数据待补；不能按零敞口放行新增。</p>';
 $('strategy-flows').innerHTML=(d.flows||[]).length?table(['日期','外部净流入','备注'],d.flows.map(x=>tr([escapeHtml(x.date),signedMoneyText(x.amount),escapeHtml(x.note||'—')]))):'<p class="tip">尚无起点后外部资金流水。</p>';
 $('strategy-reviews').innerHTML=(d.reviews||[]).length?table(['代码','开仓 / 平仓','净盈亏','净R','规则','改进'],d.reviews.map(x=>tr([escapeHtml(x.code),escapeHtml((x.opened_on||'—')+' / '+(x.closed_on||'—')),signedMoneyText(x.net_pnl),Number(x.planned_risk)>0?format(Number(x.net_pnl)/Number(x.planned_risk),2):'—',x.violation?'存在违规':'遵守',escapeHtml(x.note||'—')]))):'<p class="tip">尚无完整平仓复盘。不会从持仓截图推测成交。</p>';
 const f=$('strategy-settings-form');for(const key of Object.keys(STRATEGY_DEFAULTS))if(document.activeElement!==f.elements[key])f.elements[key].value=prefs[key];
}
async function refreshStrategy(){try{STRATEGY=await api('/api/strategy');renderStrategy(STRATEGY);}catch(e){$('strategy-status').textContent='V1规则读取失败';$('strategy-blockers').textContent='读取失败：'+e.message;showToast('V1规则读取失败：'+e.message,true);}}
async function saveStrategySettings(e){e.preventDefault();const data={};for(const key of Object.keys(STRATEGY_DEFAULTS))data[key]=Number(e.target.elements[key].value);try{const d=await api('/api/strategy/settings','PUT',data);showToast(d.message||'V1风险上限已保存');await refresh();}catch(err){showToast('V1规则保存失败：'+err.message,true);}}
async function saveStrategyFlow(e){e.preventDefault();const f=e.target;try{const d=await api('/api/strategy/cashflows','POST',{date:f.elements.date.value,amount:Number(f.elements.amount.value),note:f.elements.note.value});showToast(d.message||'资金流水已登记，请核对当前台账');f.elements.amount.value='';f.elements.note.value='';await refresh();}catch(err){showToast('资金流水保存失败：'+err.message,true);}}
async function reconcileStrategy(){try{const d=await api('/api/strategy/reconcile','POST',{});showToast(d.message||'已完成台账核对；历史高点保留');await refresh();}catch(e){showToast('台账核对未通过：'+e.message,true);}}
async function resumeStrategy(){const button=$('strategy-resume');button.disabled=true;try{const d=await api('/api/strategy/resume','POST',{acknowledged:true});showToast(d.message||'已解除预算耗尽锁定；回撤高点保留，仍须重新核验新增条件');await refresh();}catch(e){showToast('恢复未通过：'+e.message,true);}finally{button.disabled=false;}}
async function saveStrategyReview(e){e.preventDefault();const f=e.target,data={};for(const k of ['code','opened_on','closed_on','note'])data[k]=f.elements[k].value;for(const k of ['net_pnl','planned_risk'])data[k]=Number(f.elements[k].value);data.violation=f.elements.violation.value==='true';try{const d=await api('/api/strategy/reviews','POST',data);showToast(d.message||'复盘已保存');f.reset();await refresh();}catch(err){showToast('复盘保存失败：'+err.message,true);}}

function render(s){
 if(s.exit_suggestions)EXIT_SUGGESTIONS=s.exit_suggestions;
 const r=s.risk,c=s.config,meta=s.meta||{};
 $('baseline').textContent=money(c.baseline_equity);
 $('equity').textContent=money(r.equity);
 showPnl(r.pnl,s.fresh?'盘后/快照估算':'非有效行情');
 $('cash').textContent=money(c.cash);
 $('remaining').textContent=money(r.remaining);
 $('remaining-sub').textContent='预算上限 '+money(c.max_additional_loss);
 $('equity-sub').textContent=r.valid?'完整行情快照估算（非券商资产）':'行情不完整，暂不计算';
 $('data-badge').textContent=s.fresh?'近期联网快照':meta.mode==='demo'?'演示数据':'行情不可用于风控';
 $('data-badge').className='pill '+(s.fresh?'':'muted');
 const inlineBadge=$('data-badge-inline'); if(inlineBadge){inlineBadge.textContent=s.fresh?'近期联网快照':meta.mode==='demo'?'演示数据':'行情不可用于风控'; inlineBadge.className='inline-tag '+(s.fresh?'':'muted');}
 const note=$('data-note');note.className='data-note'+(s.fresh?'':' warn');note.textContent=s.fresh?
 `当前快照抓取于 ${meta.generated_at||'未知'}；该时间不是交易所确认的报价时间。报告可能有延迟，交易前须核对券商。`:
 `${s.fresh_reason}。网页仍可修改自选池与仓位；必须重新获取真实行情后才能计算新增买入数量。`;
 $('risk-used').textContent=money(r.used);$('risk-stress').textContent=money(r.existing_planned_stress);$('new-risk').textContent=money(r.available_new_risk);
 $('risk-tag').textContent=!r.valid?'需刷新':r.strategy?.blockers?.length?'V1暂停新增':r.over_budget?'风险超预算':'预算内';$('risk-tag').className='pill '+(!r.valid?'muted':r.over_budget?'danger':'');
 let ratio=r.valid&&r.used!=null&&r.existing_planned_stress!=null?Math.max(0,Math.min(100,(r.used+r.existing_planned_stress)/r.budget*100)):0;
 $('risk-fill').style.width=ratio+'%';
 $('scan-mode').textContent=meta.scan_ok===false?'扫描失败':meta.mode==='demo'?'演示合成数据':meta.mode==='live'?'联网快照（非逐笔实时）':'未运行';
 $('scan-time').textContent=meta.generated_at||'—';
 $('scan-count').textContent=meta.universe_count??'—';$('verify-count').textContent=meta.verified_count??'—';
 if(!scanStarting&&!scanPolling)scanJob=s.job;
 updateScanControls();
 const reportNotes=[...(meta.source_notes||[])];
 if(meta.expected_daily_asof)reportNotes.push('应复核日线 '+meta.expected_daily_asof);
 if(meta.mode==='live'&&meta.scan_ok!==false)reportNotes.push(meta.verified_asof?'实际日线截至 '+meta.verified_asof:'本次没有可用的日线复核');
 if(meta.mode==='live'&&s.latest_completed_day&&meta.verified_asof&&s.latest_completed_day!==meta.verified_asof){
  reportNotes.push('历史榜单，最近完成交易日为 '+s.latest_completed_day);
 }
 if(meta.errors_count)reportNotes.push('有 '+meta.errors_count+' 项数据异常，详见完整报告');
 $('scan-source').textContent=reportNotes.join(' · ');
 renderRanking();renderSector();renderWatch();renderPositions();renderSizing();renderStrategy(STRATEGY,r);refreshDailyObserve();
 const choices=(c.positions||[]).filter(p=>Number(p.quantity)>0).map(p=>`<option value="${escapeHtml(p.code)}">${escapeHtml(p.name)}（${escapeHtml(p.code)}，${format(p.quantity,0)}）</option>`).join('');
 const choose=$('reduction-code');if(choose){const prev=choose.value;choose.innerHTML='<option value="">请选择持仓</option>'+choices;if([...choose.options].some(o=>o.value===prev))choose.value=prev;}

 for(const name of ['stop_stock','stop_etf','stop_convertible','tp1_pct','tp2_pct','per_trade_risk','max_position_pct','min_reward_risk','max_breakout_extension_pct'])$('risk-form').elements[name].value=r.settings[name]??s.config.risk_settings?.[name]??s.config[name]??'';
 $('account-form').elements['cash'].value=c.cash;
 if(!s.fresh)applyLatestAccountSnapshot(s);
}
function renderRanking(){if(!STORE)return;let rows=ranking==='verified'?STORE.top:STORE.candidates;if(!STORE.meta||!STORE.meta.mode)rows=[];
 if(scanJob?.running||scanStarting){$('ranking-table').textContent='本次扫描正在运行，完成后更新榜单。';return;}
 if(STORE.meta?.scan_ok===false){$('ranking-table').textContent='本次扫描失败，暂无榜单。';return;}
 if(!rows.length&&STORE.meta?.mode){$('ranking-table').textContent=ranking==='verified'?'本次没有可用的日线复核。':'本次没有符合初筛条件的证券。';return;}
 if(ranking==='verified'&&rows.some(row=>'V1形态通过' in row)){
  const results=rows.map(row=>tr([val(row['股票名称']),val(row['股票代码']),val(row['日期']),val(row['日线来源']),
   signedPriceText(row['收盘'],row['涨跌幅%'],3),signedPctText(row['涨跌幅%']),
   row['V1形态通过']==='True'?pill('通过','') : pill('未通过'),
   val(row['触发下沿']),val(row['建仓上限']),val(row['V1阻断原因'])]));
  $('ranking-table').innerHTML=table(['名称','代码','日线日期','来源','收盘','涨跌幅','V1 形态','触发下沿','建仓上限','原因'],results);
  return;
 }
 let result;if(ranking==='verified'){
 result=rows.map((x,i)=>tr([`<span class="muted">${i+1}.</span>`,val(x['股票名称']),val(x['股票代码']),val(x['日期']),val(x['日线来源']),signedPriceText(x['收盘'],x['涨跌幅%'],3),signedPctText(x['涨跌幅%']),val(x['MA5']),val(x['MA20']),val(x['20日突破']),`<span class="stop-color">${val(x['止损参考'])}</span>`,`<span class="tp-color">${val(x['第一止盈'])}</span>`,`<span class="tp2-color">${val(x['第二止盈'])}</span>`,`<strong>${val(x['观察评分'])}</strong>`]));
 $('ranking-table').innerHTML=table(['#','名称','代码','日线日期','来源','收盘','涨跌幅','MA5','MA20','20日突破','止损参考','止盈一','止盈二','评分'],result);
 }else{
 result=rows.map((x,i)=>tr([i+1,val(x.name),val(x.code),signedPriceText(x.price,x.pct,3),signedPctText(x.pct),x.amount?format(Number(x.amount)/1e8)+'亿':'—',pct(x.turnover),val(x.volume_ratio),val(x['初筛分'])]));
 $('ranking-table').innerHTML=table(['#','名称','代码','快照价','涨跌幅','成交额','换手率','量比','初筛分'],result);
 }}
function renderDailyObserve(){
 const d=DAILY;if(!d)return;
 const rows=d.shortlist||[];
 $('nav-count-daily').textContent=rows.length;
 $('daily-pill').textContent=d.mode==='demo'?'演示数据':d.stale?'历史名单 · 不可当日使用':`${d.asof||'未知'} · ${rows.length}/5`;
 $('daily-pill').className='pill '+(d.mode==='demo'||d.stale?'muted':'');
 $('daily-warning').textContent=(d.status||'')+'；'+(d.disclaimer||'')+'。仅研究观察，不代表当天可以买入。';
 $('daily-warning').className='signal-note '+(d.stale||d.mode==='demo'?'blocked':'');
 $('daily-excludes').textContent='已排除：'+((d.excluded||[]).join('、')||'无')+'；恢复后需下一次盘后扫描才能重新参与完整排名。';
 $('daily-list').innerHTML=rows.length?rows.map(x=>{
  const stats=`<div class="daily-stats"><span>日线涨跌 ${pct(x.pct)}</span><span>量比 ${val(x.volume_ratio)}</span><span>基础分 ${val(x.baseline_score)}</span><span>缠论修正 ${val(x.chan_score)}</span></div>`;
  const prices=`<div class="daily-levels"><div class="stop"><small>止损参考</small><strong>${priceText(x.stop_loss)}</strong></div><div class="tp"><small>第一止盈</small><strong>${priceText(x.take_profit_1)}</strong></div><div class="tp2"><small>第二止盈</small><strong>${priceText(x.take_profit_2)}</strong></div></div>`;
  let risk='无有效实时账户风险核算：仅观察，不据此新增仓位';
  if(!d.stale&&d.mode==='live'&&x.max_buy_shares!==null&&x.max_buy_shares!==undefined){
    risk=x.max_buy_shares>0?`理论最多 ${x.max_buy_shares} 股（非交易建议）；${x.risk_note||''}`:
      '账户现金/风险预算不足，暂不新增。'+(x.risk_note||'');
  }
  const actions=`<div class="daily-actions"><button class="btn small ghost" data-action="analyze" data-code="${escapeHtml(x.code)}">查看缠论</button> `+
   (x.in_watchlist?'<span class="pill muted">已在手动自选</span>':d.mode==='demo'?'':'<button class="btn small primary" data-action="promote-daily" data-code="'+escapeHtml(x.code)+'">加入手动自选</button>')+
   (d.mode==='demo'?'':' <button class="btn small ghost" data-action="exclude-daily" data-code="'+escapeHtml(x.code)+'">不再推荐</button>')+'</div>';
  return `<article class="daily-card"><div class="daily-header"><div><strong>${escapeHtml(x.name)}</strong><small>${escapeHtml(x.code)} · ${escapeHtml(x.asof||'')} 日线</small></div><span class="daily-rank">#${x.rank} · ${val(x.total_score)}分</span></div>`+
  `<div class="daily-price">${signedPriceText(x.close,x.pct,3)} <small class="${signedClass(x.pct)}">${x.pct>0?'+':''}${pct(x.pct)}</small></div>${stats}${prices}`+
  `<div class="daily-condition">${escapeHtml(x.chan_condition||'结构待定')}</div>`+
  `<p class="daily-reason">${(x.reasons||[]).map(escapeHtml).join(' · ')}</p>`+
  `<div class="daily-risk">${escapeHtml(risk)}</div>${actions}</article>`;
 }).join(''):'<div class="panel"><p class="tip">没有满足过滤条件的完整日线候选，或尚未执行盘后扫描。不会为凑够5只而编造股票。</p></div>';
}
async function refreshDailyObserve(){
 try{DAILY=await api('/api/daily-observe');renderDailyObserve();}
 catch(e){$('daily-warning').textContent='读取每日观察区失败：'+e.message;}
}
function chanVisualization(c){
 const bars=c.chart_bars||[],stroke=c.strokes||[];
 if(bars.length<2)return '<p class="tip">K线不足，无法绘制缠论辅助结构。</p>';
 const values=bars.map(x=>Number(x.close));
 const ext=[...values,...stroke.flatMap(s=>[s.start_price,s.end_price])];
 if(c.pivot)ext.push(c.pivot.upper,c.pivot.lower);
 let lo=Math.min(...ext.filter(Number.isFinite)),hi=Math.max(...ext.filter(Number.isFinite)),span=Math.max(.01,hi-lo);lo-=span*.07;hi+=span*.07;
 const W=900,H=260,L=48,R=25,T=15,B=32;
 const ix=bars[0].index,last=bars.at(-1).index;
 const x=p=>L+(p-ix)/Math.max(1,last-ix)*(W-L-R);
 const y=p=>H-B-(p-lo)/(hi-lo)*(H-T-B);
 let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="历史日线收盘线及简化缠论已确认笔和中枢区间示意">`;
 for(let i=0;i<4;i++){const val=lo+(hi-lo)*i/3,yy=y(val);svg+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="#e4eaf0"/><text x="${L-4}" y="${yy+3}" font-size="10" text-anchor="end" fill="#8592a0">${format(val,2)}</text>`;}
 if(c.pivot){for(const [val,label] of [[c.pivot.lower,'中枢下沿'],[c.pivot.upper,'中枢上沿']]){let yy=y(val);svg+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="#7296b3" stroke-dasharray="3 6"/><text x="${W-R-2}" y="${yy-4}" font-size="10" text-anchor="end" fill="#7296b3">${label} ${format(val,2)}</text>`;}}
 svg+=`<polyline fill="none" stroke="#314e68" stroke-width="1.6" points="${bars.map(k=>`${x(k.index).toFixed(1)},${y(k.close).toFixed(1)}`).join(' ')}"/>`;
 for(const s of stroke){if(s.end_index<ix||s.start_index>last)continue;
  let x1=x(Math.max(ix,s.start_index)),x2=x(Math.min(last,s.end_index));
  if(s.start_index<ix||s.end_index>last)continue;
  svg+=`<line x1="${x1}" y1="${y(s.start_price)}" x2="${x2}" y2="${y(s.end_price)}" stroke="${s.direction==='上笔'?'#ca5963':'#159580'}" stroke-width="2.8"/><circle cx="${x2}" cy="${y(s.end_price)}" r="3.2" fill="#fff" stroke="#4e6279"/>`;
 }
 svg+=`<text x="${L}" y="${H-9}" font-size="11" fill="#8895a4">${escapeHtml(bars[0].date)}</text><text x="${W-R}" y="${H-9}" font-size="11" text-anchor="end" fill="#8895a4">${escapeHtml(bars.at(-1).date)}</text></svg>`;
 return svg;
}

// Completed daily OHLCV research graphics. No intraday volume extrapolation.
function technicalPlot(rows,kind){
 if(!Array.isArray(rows)||rows.length<3)return '<p class="tip">有效历史K线不足，不能作图。</p>';
 const W=730,H=195,L=43,R=15,T=19,B=27;
 const usable=rows.slice(-65), bw=(W-L-R)/usable.length;
 const vals=kind==='macd' ? usable.flatMap(r=>[r.dif,r.dea,r.macd_bar,0]) : usable.flatMap(r=>[r.volume,r.volume_ma5,0]);
 const fin=vals.map(Number).filter(Number.isFinite);
 if(!fin.length)return '<p class="tip">指标数据缺失，暂停绘图。</p>';
 let low=kind==='macd'?Math.min(0,...fin):0,high=Math.max(...fin);
 if(kind==='macd'){const margin=Math.max((high-low)*.09,.001);low-=margin;high+=margin;}
 else high=Math.max(high,1)*1.08;
 const range=Math.max(high-low,.001);
 const x=i=>L+(i+.5)*bw;
 const y=v=>T+(high-v)/range*(H-T-B);
 let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${kind==='macd'?'DIF、DEA曲线及MACD红绿柱':'历史日成交量及5日均量曲线'}">`;
 for(let i=0;i<=3;i++){const v=low+(high-low)*i/3,yy=y(v);svg+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="#e5ebf0"/><text x="${L-6}" y="${yy+4}" text-anchor="end" font-size="10" fill="#8794a3">${kind==='macd'?format(v,2):format(v/10000,0)}</text>`;}
 if(kind==='macd'){
   const y0=y(0);
   usable.forEach((r,i)=>{if(!Number.isFinite(Number(r.macd_bar)))return;const top=Math.min(y(r.macd_bar),y0),height=Math.max(1,Math.abs(y(r.macd_bar)-y0));svg+=`<rect x="${(x(i)-bw*.26).toFixed(2)}" y="${top.toFixed(2)}" width="${Math.max(1.4,bw*.52).toFixed(2)}" height="${height.toFixed(2)}" fill="${r.macd_bar>=0?'#c6535d':'#1b987e'}" opacity=".72"/>`;});
   for(const [key,col] of [['dif','#4168a7'],['dea','#e59b34']]){
     const pts=usable.map((r,i)=>Number.isFinite(Number(r[key]))?`${x(i).toFixed(2)},${y(r[key]).toFixed(2)}`:null).filter(Boolean).join(' ');
     svg+=`<polyline points="${pts}" stroke="${col}" stroke-width="2" fill="none" stroke-linejoin="round"/>`;
   }
   svg+=`<text x="${L+4}" y="${T+3}" font-size="10" fill="#4168a7">DIF</text><text x="${L+38}" y="${T+3}" font-size="10" fill="#b47c25">DEA</text>`;
 }else{
   usable.forEach((r,i)=>{const v=Number(r.volume);if(!Number.isFinite(v))return;const yv=y(v),h=Math.max(0,y(0)-yv);const up=i>0?Number(r.close)>=Number(usable[i-1].close):false;svg+=`<rect x="${(x(i)-bw*.38).toFixed(2)}" y="${yv.toFixed(2)}" width="${Math.max(1.2,bw*.76).toFixed(2)}" height="${h.toFixed(2)}" fill="${up?'#c6535d':'#1b987e'}" opacity=".75"/>`;});
   const pts=usable.map((r,i)=>r.volume_ma5===null?null:`${x(i).toFixed(2)},${y(r.volume_ma5).toFixed(2)}`).filter(Boolean).join(' ');
   svg+=`<polyline points="${pts}" stroke="#4d6eb3" stroke-width="2" fill="none"/>`;
   svg+=`<text x="${L+3}" y="${T+4}" font-size="10" fill="#7c90a0">单位：源数据万单位</text>`;
 }
 svg+=`<text x="${L}" y="${H-7}" font-size="10" fill="#8492a0">${escapeHtml(usable[0].date)}</text><text x="${W-R}" y="${H-7}" text-anchor="end" font-size="10" fill="#8492a0">${escapeHtml(usable.at(-1).date)}</text></svg>`;
 return svg;
}
function renderSwing(s){
 const node=$('swing-decision');if(!node)return;
 if(!s){node.textContent='正在核对日线趋势、量能和缠论结构…';return;}
 const level=s.levels||{};
 const lines=(s.checks||[]).map(x=>`<div class="swing-check"><span class="${x.passed?'pass':'pending'}">${x.passed?'✓':'·'} ${escapeHtml(x.name)}</span><span>${escapeHtml(x.detail)}</span></div>`).join('');
 const blockers=(s.blockers||[]).length?`<div class="swing-blockers"><strong>${escapeHtml(s.blockers_title||'目前暂停新增')}</strong><ul>${s.blockers.map(x=>`<li>${escapeHtml(x)}</li>`).join('')}</ul></div>`:'';
 const pr=s.price_review,priceReview=pr?.valid?`<p class="tip">结构观察（日线截至 ${escapeHtml(pr.asof)}）：20日低点 ${priceText(pr.support)} · 20日高点 ${priceText(pr.resistance)} · MA20 ${priceText(pr.ma20)}。这些是观察位，不是已锁定的止损或卖出委托。</p>`:'';
 node.innerHTML=`<article class="swing-card ${escapeHtml(s.tone||'neutral')}">
 <div class="swing-header"><span>操作参考 · ${escapeHtml(s.horizon)}</span><span>日线截至 ${escapeHtml(s.asof||'待核验')}</span></div>
 <h3>${escapeHtml(s.label)}</h3><p class="swing-summary">${escapeHtml(s.summary)}</p>
 <div class="swing-prices"><span>最新参考 <b>${priceText(level.current)}</b></span><span>止损 / 失效参考 <b>${priceText(level.stop)}</b></span><span>第一止盈 <b>${priceText(level.target1)}</b></span><span>第二止盈 <b>${priceText(level.target2)}</b></span></div>
 <p class="tip">${escapeHtml(level.source||'缺少有效价格依据')}${level.quote_asof?' · 报价参考 '+escapeHtml(new Date(level.quote_asof).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}))+'，重新分析可更新':''}${s.chan_confirmation?` · ${escapeHtml(s.chan_confirmation.kind)}，${escapeHtml(s.chan_confirmation.confirmed_on)}确认（${s.chan_confirmation.bars_ago}根日K前）`:''}</p>
 ${priceReview}${blockers}${s.max_buy_shares?`<p class="swing-capacity">预算测算上限 ${format(s.max_buy_shares,0)} 股 · 价差收益/风险比 ${format(s.reward_risk,2)}，不是推荐买入数量。</p>`:''}
 <details class="swing-evidence" open><summary>为什么这样判断</summary>${lines}</details><small>${escapeHtml(s.note)}</small></article>`;
}
function renderTechnical(t,j,opts={}){
 const result=$('integrated-decision'),metrics=$('technical-metrics');
 if(!result||!metrics)return;
 if(!t?.valid){
    result.innerHTML=`<div class="technical-summary neutral"><strong>继续观察 · 指标数据不足</strong><p>${escapeHtml(t?.reason||'没有完整历史OHLCV')}</p></div>`;
    metrics.innerHTML='';$('macd-chart').innerHTML='';$('volume-chart').innerHTML='';$('technical-note').textContent='未满足已完成K线、有效成交量和计算指标的要求，不输出综合结论。';return;
 }
 const holding=Boolean(STORE?.config?.positions?.some(p=>p.code===opts.code));
 const risk=STORE?.risk;
 const cannotAdd=!STORE?.fresh||!risk?.valid||Number(risk.available_new_risk)<=0||!Number.isFinite(Number(risk.available_new_risk));
 const canWarn=(j?.action||'').includes('买入')&&cannotAdd;
 const tone=(j?.action||'').includes('减仓')?'bear':(j?.action||'').includes('买入')?'bull':'neutral';
 const riskWarn=canWarn?'<p class="technical-risk">技术观察可能偏强，但账户快照或新增风险预算未通过核验：暂停新增。</p>':'';
 const heading=canWarn?'技术有买入观察条件，但当前暂停新增':j?.action||'继续观察';
 const due=opts.mode==='demo'?'（合成演示数据，禁止交易）':'';
 const reasons=(j?.reasons||[]).map(s=>`<li>${escapeHtml(s)}</li>`).join('');
 result.innerHTML=`<div class="technical-summary ${tone}">
  <div class="technical-summary-top"><span>综合倾向 · ${escapeHtml(j?.alignment||'待核验')}</span><span>已完成日K ${escapeHtml(t.asof)}${due}</span></div>
  <strong>${escapeHtml(heading)}</strong><p>${escapeHtml(j?.summary||'指标未达一致')}</p>
  <ul>${reasons}</ul>${riskWarn}<small>${escapeHtml(j?.risk_note||'研究参考，不自动交易')}</small>
 </div>`;
 const metricItems=[
  ['MACD(12,26,9)',`${t.macd.status}`,`${t.macd.dif} / ${t.macd.dea} / ${t.macd.bar}`],
  ['成交量 / 前5日均量',t.volume.ratio5==null?'—':`${format(t.volume.ratio5,2)} 倍`,t.volume.status],
  ['均线 MA5 / MA10',`${format(t.ma['5'],3)} / ${format(t.ma['10'],3)}`,t.trend],
  ['均线 MA20 / MA60',`${format(t.ma['20'],3)} / ${format(t.ma['60'],3)}`,'日线已收盘价计算'],
  ['RSI14',format(t.rsi14,1),t.rsi_status],
  ['ATR14 / 波动占比',`${format(t.atr14,3)} / ${format(t.atr_pct,2)}%`,'波动风险，不是止损保障']
 ];
 metrics.innerHTML=metricItems.map(([label,value,note])=>`<div class="technical-kpi"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong><span>${escapeHtml(note)}</span></div>`).join('');
 $('macd-chart').innerHTML=technicalPlot(t.chart,'macd');
 $('volume-chart').innerHTML=technicalPlot(t.chart,'volume');
 $('technical-note').textContent=t.note+'。如果缠论与MACD/成交量冲突，则继续观察；ETF与转债需按自身历史数据口径核验。';
}

function renderChan(c, opts={}){
 const out=$('chan-plain');
 if($('chan-points'))$('chan-points').innerHTML='';
 if(!c||!c.valid){out.innerHTML='<div class="chan-placeholder">结构证据不足：继续观察。'+escapeHtml(c?.reason||'缺少可靠日线')+'</div>';$('chan-status').textContent=c?.reason||'缺少缠论数据';$('chan-chart').innerHTML='';$('chan-metrics').innerHTML='';return;}
 const n=c.interpretation;
 if(n){
  const holding=Boolean(STORE?.config?.positions?.some(p=>p.code===opts.code));
  const risk=STORE?.risk;
  const cannotAdd=!STORE?.fresh||!risk?.valid||!Number.isFinite(Number(risk.available_new_risk))||Number(risk.available_new_risk)<=0;
  let actionLabel=n.action;
  if(n.action==='偏卖出/减仓观察'&&!holding)actionLabel='偏回避买入 / 风险观察';
  if(n.action==='偏买入观察'&&cannotAdd)actionLabel='技术偏强，但当前暂停新增';
  const tone=n.action==='偏买入观察'&&!cannotAdd?'bull':n.action==='偏卖出/减仓观察'?'bear':'neutral';
  const reason=(n.reasons||[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
  const budget=n.action==='偏买入观察' ?
    (cannotAdd?'<div class="chan-budget blocked">账户风险预算不足、行情不完整或已过期：这只是技术观察，不显示允许新增的结论。</div>':
    '<div class="chan-budget">技术结构偏强不等于可以立即买入。实际买入前须用最新券商行情和风险计算复核。</div>') : '';
  const demo=opts.mode==='demo'?' <span class="chan-demo">演示数据，严禁交易</span>':'';
  out.innerHTML=`<div class="chan-decision ${tone}">
   <div class="chan-decision-header"><span>历史K线缠论 · 固定旧笔核验</span><small>数据截至 ${escapeHtml(n.basis_date)}${demo}</small></div>
   <div class="chan-verdict"><strong>${escapeHtml(actionLabel)}</strong><p>${escapeHtml(n.summary||'请结合后续走势确认。')}</p><span>仅为条件式研究结论，不是实时委托信号</span></div>
   <div class="chan-phase-grid">
    <div><small>当前阶段</small><strong>${escapeHtml(n.phase)}</strong></div>
    <div><small>顶分区 / 底分区</small><strong>${escapeHtml(n.zone)}</strong></div>
    <div><small>最近一笔</small><strong>${escapeHtml(n.latest_stroke)}</strong></div>
   </div>
   <div class="chan-detail-grid"><div><small>下一步看什么</small><p>${escapeHtml(n.trigger)}</p></div><div><small>什么情况说明判断失效</small><p>${escapeHtml(n.invalidation)}</p></div></div>
   <div class="chan-why"><strong>为什么这么判断？</strong><ul>${reason}</ul></div>
   ${budget}<p class="chan-caution">${escapeHtml(n.disclaimer)}${opts.mode==='demo'?' 合成价格不可用于交易。':''}</p>
  </div>`;
 }else{out.innerHTML='<div class="chan-placeholder">目前只检测到基础缠论结构，继续观察。</div>';}
 $('chan-status').textContent=c.condition+'。'+c.note;
 const pts=(c.recent_points||[]).slice(-8).reverse();
 if($('chan-points'))$('chan-points').innerHTML=`<div class="chan-point-heading"><strong>一/二/三类买卖点结构候选</strong><small>仅笔级形态候选，不代表严格的已确认买卖点</small></div>`+
 (pts.length?pts.map(x=>`<div class="chan-point-item ${x.kind.includes('买')?'buy':'sell'}"><div><strong>${escapeHtml(x.kind)}</strong><span>${escapeHtml(x.date)} · ${priceText(x.price)}元</span><small>右侧K线确认：${escapeHtml(x.confirmed_on)}</small></div><p>${escapeHtml(x.why)} · ${escapeHtml(x.status)}</p></div>`).join(''):
 '<p class="tip">最近没有符合固定规则的一、二、三类买卖点结构候选。这是正常结果，不应硬凑信号。</p>');
 $('chan-chart').innerHTML=chanVisualization(c);
 const infos=[['有效历史日K',c.raw_count],['合并K线',c.merged_count],['已确认分型',c.fractals_count],['完整旧笔',c.strokes_count],['笔级中枢数量',c.zone_count],['笔级中枢下沿',c.pivot?.lower??'未形成'],['笔级中枢上沿',c.pivot?.upper??'未形成'],['结构支撑',c.support??'未确认'],['结构压力',c.resistance??'未确认']];
 $('chan-metrics').innerHTML=infos.map(x=>`<div class="chan-mini"><small>${escapeHtml(x[0])}</small><strong>${escapeHtml(x[1])}</strong></div>`).join('');
}
function renderSector(){let x=STORE?.meta?.mode?STORE.sectors:[];$('sector-table').innerHTML=table(['#','行业名称','涨跌幅','上涨家数','下跌家数','上涨占比','强度评分'],x.map((r,i)=>tr([i+1,val(r.name),signedPctText(r.pct),val(r.up),val(r.down),pct(r['上涨占比%']),`<strong>${val(r['板块强度分'])}</strong>`])));}
function priceText(v){return v===null||v===undefined?'—':format(v,3)}
function planCell(levels,code=null){
 if(!levels){
  const s=EXIT_SUGGESTIONS[code],t=s?.technical;
  if(t?.valid){const p=s.valid?s.plan:null,wide=s.status==='risk_mismatch';return `<div class="level-stack"><span>支撑 / 压力 ${priceText(t.support)} / ${priceText(t.resistance)}</span><span>MA20 ${priceText(t.ma20)}</span>${p?`<span class="stop-color">${wide?'远端结构':'建议止损'} ${priceText(p.stop_loss)}</span>${wide?'<small>距离超过6%，暂不可采用；优先减仓复核</small>':`<span class="tp-color">分批退出① / ② ${priceText(p.take_profit_1)} / ${priceText(p.take_profit_2)}</span>`}`:''}<small>完整日线 ${escapeHtml(t.asof)} · 结构观察${p&&!wide?' / 退出草案':''}，未锁定</small></div>`;}
  return '<span class="muted">'+escapeHtml(s?.reason||(code?'完整日线读取中，等待技术价位核验':'尚无已核验固定计划'))+'</span>';
 }
 const a=levels,exitOnly=STORE?.config?.trade_plans?.[code]?.legacy_exit_only;
 return `<div class="level-stack"><span class="stop-color">止损 ${priceText(a.stop_loss)}</span><span class="tp-color">${exitOnly?'退出':'止盈'}① ${priceText(a.take_profit_1)}</span><span class="tp2-color">${exitOnly?'退出':'止盈'}② ${priceText(a.take_profit_2)}</span>${a.manual?'<small>手工锁定</small>':'<small>按比例模拟</small>'}</div>`;
}
function renderWatch(){const s=STORE;let levels=new Map(s.config.alert_levels.map(x=>[x.code,x]));let rows=s.watchlist.map(x=>{let lvl=levels.get(x.code)||{};let level=`${lvl.below??'—'} / ${lvl.above??'—'}`;return tr([`<strong>${escapeHtml(x.name)}</strong>`,val(x.code),signedPriceText(x.price,x.pct,3),signedPctText(x.pct),escapeHtml(level),planCell(x.levels,x.code),`<button class="btn small ghost" data-action="analyze" data-code="${x.code}">分析</button> <button class="btn small ghost" data-action="plan" data-code="${x.code}">止盈止损</button> <button class="btn small ghost" data-action="level" data-code="${x.code}">突破位</button> <button class="btn small danger" data-action="remove-watch" data-code="${x.code}">删除</button>`]);});$('watch-table').innerHTML=table(['股票','代码','快照价','当日涨跌','破位/突破','关键止盈止损参考价','操作'],rows);}
function renderPositions(liveRisk=null,liveQuotes=[]){let rows=STORE.config.positions.map(p=>{const now={...((liveRisk?.positions||STORE.risk.positions).find(x=>x.code===p.code)||{})};const live=liveRisk?.pnl?.positions?.[p.code];const tick=liveQuotes.find(x=>x.code===p.code&&x.held&&x.price!==null);if(live&&tick){now.price=tick.price;now.value=Number((Number(tick.price)*Number(p.quantity)).toFixed(2));now.floating_pnl=live.floating_pnl;now.daily_pnl=live.daily_pnl;}let st=now.stop_breached?pill('已触及参考止损','danger'):'';let shown=(v)=>signedMoneyText(v);const priceSignal=now.daily_pnl!==null&&now.daily_pnl!==undefined?now.daily_pnl:(now.price!==null&&now.price!==undefined?Number(now.price)-Number(p.cost):null);return tr([escapeHtml(p.name),val(p.code),p.type==='stock'?'股票':p.type==='etf'?'ETF':'转债',escapeHtml(p.sector||now.sector||'待补'),format(p.quantity,0),format(p.cost,3),signedPriceText(now.price,priceSignal,3),now.value?money(now.value):'—',shown(now.floating_pnl),shown(now.daily_pnl),planCell(now.levels,p.code)+st,`<button class="btn small ghost" data-action="plan" data-code="${p.code}">止盈止损</button> <button class="btn small ghost" data-action="analyze" data-code="${p.code}">缠论</button> <button class="btn small ghost" data-action="edit-position" data-code="${p.code}">编辑</button> <button class="btn small danger" data-action="remove-position" data-code="${p.code}">删除</button>`]);});$('positions-table').innerHTML=table(['证券','代码','类型','行业','数量','成本','快照价','市值','浮动盈亏','当日参考盈亏','关键价格','台账操作'],rows);}
function renderSizing(liveSizing=null){let rows=(liveSizing??STORE.risk.sizing).map(p=>tr([escapeHtml(p.name),val(p.code),p.price===null?'—':format(p.price,3),planCell(p.levels),p.max_buy_shares===null?pill('暂无有效计算'):p.max_buy_shares===0?pill('0股：V1条件未通过','danger'):`<strong>${format(p.max_buy_shares,0)}股</strong>`,`<span class="muted">${escapeHtml(p.reason||'')}</span>`]));$('sizing-table').innerHTML=table(['股票','代码','快照价','参考止盈止损','最多新增股数','计算说明'],rows);}
function plot(data,levels){
 if(!data||data.length<2)return '历史行情不足';
 const values=data.map(x=>Number(x.close));
 const lines=levels?[{v:levels.stop_loss,text:'止损 '+priceText(levels.stop_loss),color:'#bf4c59'},
 {v:levels.take_profit_1,text:'止盈① '+priceText(levels.take_profit_1),color:'#158b70'},
 {v:levels.take_profit_2,text:'止盈② '+priceText(levels.take_profit_2),color:'#467bb8'},
 {v:levels.reference,text:'参考价 '+priceText(levels.reference),color:'#8d99a9'}]:[];
 const bounded=values.concat(lines.map(x=>x.v).filter(Number.isFinite));
 let low=Math.min(...bounded),high=Math.max(...bounded);let range=high-low||1;low-=range*.075;high+=range*.075;range=high-low;
 const W=900,H=300,L=55,R=153,T=18,B=30;
 const y=v=>H-B-(v-low)/range*(H-T-B), x=i=>L+i*(W-L-R)/(values.length-1);
 const pts=values.map((v,i)=>`${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
 let markup=`<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="最近历史日线收盘走势及止损、两级止盈参考线">`;
 for(let k=0;k<5;k++){let v=low+range*k/4;let yy=y(v);markup+=`<line x1="${L}" x2="${W-R}" y1="${yy}" y2="${yy}" stroke="#e4eaf0"/><text x="${L-5}" y="${yy+4}" text-anchor="end" fill="#8290a0" font-size="10">${format(v,2)}</text>`}
 for(const line of lines){let yy=y(line.v);markup+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="${line.color}" stroke-dasharray="6,5" stroke-width="1.8"/><circle cx="${W-R}" cy="${yy}" r="3" fill="${line.color}"/><text x="${W-R+10}" y="${yy+4}" font-size="12" font-weight="700" fill="${line.color}">${escapeHtml(line.text)}</text>`}
 markup+=`<polyline fill="none" stroke="#28475a" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" points="${pts}"/><circle cx="${x(values.length-1)}" cy="${y(values.at(-1))}" r="4" fill="#1b9784"/><text x="${L}" y="${H-9}" font-size="11" fill="#788a9e">起 ${escapeHtml(data[0].date)}</text><text x="${W-R}" y="${H-9}" text-anchor="end" font-size="11" fill="#788a9e">截至 ${escapeHtml(data.at(-1).date)}</text></svg>`;
 return markup;
}
function levelCards(l){if(!l)return '<p class="tip">'+(STORE?.config?.strategy_v1?.enabled?'固定计划待补：新候选请先生成V1形态计划，旧仓请记录实际退出计划。':'尚不能生成关键价位，需要行情或手动输入参考价格。')+'</p>';
 return `<div class="level-cards"><div class="level-card"><small>分析参考价</small><strong>${priceText(l.reference)}</strong><span>${escapeHtml(l.reference_source)}</span></div><div class="level-card stop"><small>止损参考</small><strong>${priceText(l.stop_loss)}</strong><span>${escapeHtml(l.stop_source)}</span></div><div class="level-card tp"><small>第一止盈</small><strong>${priceText(l.take_profit_1)}</strong><span>${escapeHtml(l.tp1_source)}</span></div><div class="level-card tp2"><small>第二止盈</small><strong>${priceText(l.take_profit_2)}</strong><span>${escapeHtml(l.tp2_source)}</span></div></div>`;
}
async function runAnalysis(code,mode='live'){
 const stock=STORE.watchlist.find(p=>p.code===code);$('analysis-title').textContent=(stock?.name||code)+' · 读取历史日线中…';
 $('analysis-badge').textContent='加载中';$('chart-area').textContent='正在获取行情数据…';$('chan-status').textContent='正在计算历史缠论结构…';if($('chan-points'))$('chan-points').innerHTML='';$('chan-plain').innerHTML='<div class="chan-placeholder">正在识别最近分型、笔与中枢结构…</div>';$('chan-chart').innerHTML='';$('chan-metrics').innerHTML='';$('analysis-metrics').innerHTML='';$('analysis-levels').innerHTML='';renderTechnical(null,null);renderSignalReview(null);renderSwing(null);
 if(location.hash!=='#watch')history.pushState(null,'','#watch');
 showSection('watch',false);
 document.getElementById('analyze-panel').scrollIntoView({behavior:'smooth',block:'center'});
 try{
 const d=await api(`/api/analyze/${encodeURIComponent(code)}?mode=${mode}`);
 $('analysis-title').textContent=`${d.name} · ${d.code} | ${d.score['日期']}`;
 $('analysis-badge').textContent=mode==='demo'?'演示数据':`${d.asset_type==='etf'?'ETF':d.asset_type==='convertible'?'转债':'股票'}历史日线 · ${d.chan?.raw_count??0}根 · ${d.analysis_window?.label||'最近1年'}`;
 $('chart-area').innerHTML=plot(d.history,d.levels);
 $('analysis-levels').innerHTML=levelCards(d.levels)+(STORE.watchlist.some(x=>x.code===d.code)||STORE.config.positions.some(x=>x.code===d.code)?`<button class="btn small ghost" data-action="plan" data-code="${d.code}">✎ 设置/锁定关键价位</button>`:'<p class="tip">每日候选需先加入手动自选池，才可锁定分析价位。</p>');
 let fields=[['最近收盘',d.score['收盘']],['涨跌幅',pct(d.score['涨跌幅%'])],['趋势评分',d.score['观察评分']+'/100'],['5日均线',d.score.MA5],['20日均线',d.score.MA20],['量比(相对5日)',d.score['量比5日']],['20日新高',d.score['20日突破']]];
 $('analysis-metrics').innerHTML=fields.map(x=>`<div class="metric-mini"><small>${escapeHtml(x[0])}</small><strong>${escapeHtml(x[1]??'—')}</strong></div>`).join('');
 renderSwing(d.swing);renderTechnical(d.technical,d.integrated,{mode:d.mode,code:d.code});renderSignalReview(d.signal_review);
 renderChan(d.chan,{mode:d.mode,code:d.code});
 $('analysis-note').textContent=(d.analysis_window?`${d.analysis_window.label}：${d.analysis_window.start} 至 ${d.analysis_window.data_end||d.analysis_window.end}。`:'')+d.disclaimer+'。'+d.score['说明']+'。止盈止损都是研究参考线，不是交易委托。';
 }catch(e){renderSwing({label:'数据不足，先观望',summary:e.message,checks:[],levels:{},note:'未形成有效分析。'});$('analysis-badge').textContent='失败';$('chart-area').textContent=e.message;$('chan-plain').innerHTML='<div class="chan-placeholder">无法形成买卖观察结论：'+escapeHtml(e.message)+'</div>';renderTechnical({valid:false,reason:e.message},null);renderSignalReview(null);showToast(e.message,true);}
}
function editTradePlan(code){
 const position=STORE.config.positions.find(p=>p.code===code&&Number(p.quantity)>0),watch=STORE.watchlist.find(p=>p.code===code),prior=STORE.config.trade_plans?.[code];
 const f=$('plan-form');f.reset();planSuggestionSerial++;if(f.elements.exit_suggestion_token)f.elements.exit_suggestion_token.value='';f.elements.code.value=code;f.elements.prepared_id.value=prior?.prepared_id||'';
 for(const key of ['buy_date','actual_entry','initial_quantity','tp1_sold_quantity'])f.elements[key].disabled=false;
 for(const key of ['entry_price','stop_loss','take_profit_1','take_profit_2','signal_date','trigger_price','resistance_price','buy_date','actual_entry','initial_quantity','tp1_sold_quantity'])f.elements[key].value=prior?.[key]??'';
 f.elements.sector.value=prior?.sector||position?.sector||'';
 f.elements.legacy_exit_only.checked=Boolean(position&&(prior?.legacy_exit_only??true));f.elements.legacy_exit_only.disabled=!position;
 for(const key of ['fundamentals_ok','event_clear','corporate_action_clear','resistance_clear'])f.elements[key].checked=prior?.review?.[key]===true;
 $('plan-title').textContent=`${position?.name||watch?.name||code}（${code}）：V1固定计划`;
 $('plan-description').textContent=position?'旧仓可保存人工核验的固定退出计划；真实买入日期、均价与数量未知请留空。':'新开仓必须先生成后端核验的固定计划，再逐项人工复核；形态未通过不能仅手填价位放行。';
 $('plan-setup').textContent=prior?'已有固定计划；保存修改仍须由后端校验止损、版本和准备记录。':'尚未生成V1固定计划。系统不会按成本百分比补造旧仓止损。';
 $('plan-setup').className='signal-note';updatePlanMode();
 if($('plan-suggest'))$('plan-suggest').hidden=!position;
 if($('plan-manual'))$('plan-manual').hidden=!position;
 $('plan-prepare').hidden=!!position;
 if($('plan-reference-label'))$('plan-reference-label').textContent=position?'剩余仓位参考价（非实际买入价）':'允许最高成交价 Ecap（元）';
 if($('plan-save')){$('plan-save').disabled=false;$('plan-save').textContent=prior?'保存原固定计划':'采用系统建议并保存';}if($('plan-suggest'))$('plan-suggest').disabled=false;
 $('plan-dialog').showModal();
 if(position&&!prior)loadExitSuggestion(code);
 
}
function updatePlanMode(){const f=$('plan-form'),legacy=f.elements.legacy_exit_only.checked,held=STORE?.config?.positions?.some(p=>p.code===f.elements.code.value&&Number(p.quantity)>0);for(const key of ['entry_price','stop_loss','take_profit_1','take_profit_2','trigger_price','signal_date'])f.elements[key].readOnly=!legacy;f.elements.stop_loss.readOnly=!legacy&&!held;f.elements.signal_date.required=!legacy;f.elements.trigger_price.required=!legacy;if(f.elements.sector)f.elements.sector.required=!legacy;if(!legacy&&f.elements.exit_suggestion_token)f.elements.exit_suggestion_token.value='';}
async function loadExitSuggestion(code){
 const f=$('plan-form'),serial=++planSuggestionSerial,button=$('plan-suggest'),save=$('plan-save');
 if(!f.elements.legacy_exit_only.checked)return;
 for(const key of ['buy_date','actual_entry','initial_quantity','tp1_sold_quantity'])f.elements[key].disabled=true;
 if(f.elements.exit_suggestion_token)f.elements.exit_suggestion_token.value='';
 if(button)button.disabled=true;if(save)save.disabled=true;
 $('plan-setup').className='signal-note';$('plan-setup').textContent='正在读取已核验的最新完整日线，生成剩余仓位退出草案…';
 try{
  const s=await api('/api/exit-suggestion/'+encodeURIComponent(code));
  if(serial!==planSuggestionSerial||f.elements.code.value!==code||!f.elements.legacy_exit_only.checked)return;
  EXIT_SUGGESTIONS[code]=s;
  for(const key of ['buy_date','actual_entry','initial_quantity','tp1_sold_quantity'])f.elements[key].disabled=!s.preserved;
  if(!s.valid){$('plan-setup').className='signal-note blocked';$('plan-setup').textContent=(s.reason||'完整日线尚未核验')+'。后台正在补取；稍后点击“刷新系统建议”。也可手动录入已核验价位。';return;}
  for(const key of ['entry_price','stop_loss','take_profit_1','take_profit_2'])f.elements[key].value=s.plan[key];
  if(f.elements.exit_suggestion_token)f.elements.exit_suggestion_token.value=s.preserved?'':(s.suggestion_token||'');
  for(const key of ['entry_price','stop_loss','take_profit_1','take_profit_2'])f.elements[key].readOnly=!s.preserved;
  const stress=s.planned_stress==null?'':`；现有全部数量计划压力约 ${money(s.planned_stress)} 元`;
  const width=s.stop_distance_pct==null?'':`；止损距离 ${Number(s.stop_distance_pct).toFixed(2)}%`;
  $('plan-description').textContent=s.preserved?'保留已锁定的原止损与目标；系统不会随行情下移止损。':s.adoptable?'已自动填入系统退出草案，核对后单击“采用系统建议并保存”即可锁定；分批退出目标不代表该笔整体盈利或回本；不补造真实成交日期或价格，不产生新增买入额度。':'暂无适合1～2周周期的可采用近端方案。下方四价仅为远端结构研究草案；优先减仓复核或等待结构，不为凑6%强行抬高止损。';
  $('plan-setup').className='signal-note'+(s.warnings?.length?' blocked':'');
  $('plan-setup').textContent=`${s.preserved?'已有固定计划保持原价':'完整日线 '+s.asof+'；'+s.plan.suggestion_method}${width}${stress}。${(s.warnings||[]).join('；')}`;
  if(save){save.disabled=!s.preserved&&!s.adoptable;save.textContent=s.preserved?'保存原固定计划':s.adoptable?'采用系统建议并保存':'远端结构参考，暂不可采用';}
 }catch(e){if(serial===planSuggestionSerial){$('plan-setup').className='signal-note blocked';$('plan-setup').textContent='建议读取失败：'+e.message+'。请刷新系统建议，或手动录入已核验价位。';}}
 finally{if(serial===planSuggestionSerial&&button)button.disabled=false;}
}
function enableManualExitPlan(){
 const f=$('plan-form');if(!f.elements.legacy_exit_only.checked)return;
 for(const key of ['buy_date','actual_entry','initial_quantity','tp1_sold_quantity'])f.elements[key].disabled=false;
 planSuggestionSerial++;if(f.elements.exit_suggestion_token)f.elements.exit_suggestion_token.value='';
 for(const key of ['entry_price','stop_loss','take_profit_1','take_profit_2'])f.elements[key].readOnly=false;
 if($('plan-save')){$('plan-save').disabled=false;$('plan-save').textContent='保存手动固定计划';}
 if($('plan-suggest'))$('plan-suggest').disabled=false;
 $('plan-setup').textContent='手动调整模式：请按已核验依据填写。已有固定止损不能下移；未保存前仍是草案。';
}
async function prepareTradePlan(){
 const f=$('plan-form'),code=f.elements.code.value,b=$('plan-prepare');b.disabled=true;$('plan-setup').textContent='正在核验已完成日线与市场闸门…';
 try{const d=await api('/api/strategy/prepare/'+encodeURIComponent(code),'POST',{}),setup=d.setup||{},levels=setup.levels||setup;
  $('plan-setup').className='signal-note'+(d.ok?'':' blocked');
  const reasons=d.blockers||setup.blockers||[];$('plan-setup').textContent=d.ok?'形态与固定价格已准备。人工核验通过后方可保存；成交前仍须重新核对预算及报价。':'未通过：'+reasons.map(noteText).join('；');
  if(!d.ok){f.elements.prepared_id.value='';showToast('V1固定计划未通过：'+reasons.map(noteText).join('；'),true);return;}
  const values={entry_price:levels.entry_price??levels.entry_cap??levels.ecap??setup.entry_cap,stop_loss:levels.stop_loss??levels.initial_stop??setup.stop_loss,take_profit_1:levels.take_profit_1??levels.tp1,take_profit_2:levels.take_profit_2??levels.tp2,signal_date:setup.signal_date??levels.signal_date,trigger_price:setup.trigger_price??levels.trigger_price};
  for(const [key,value] of Object.entries(values))f.elements[key].value=value??'';
  f.elements.prepared_id.value=d.prepared_id||'';f.elements.legacy_exit_only.checked=false;updatePlanMode();
 }catch(e){$('plan-setup').textContent='准备失败：'+e.message;showToast('V1计划准备失败：'+e.message,true);}finally{b.disabled=false;}
}
async function saveTradePlan(e){e.preventDefault();const f=e.target;
 if(f.elements.legacy_exit_only.checked&&f.elements.exit_suggestion_token?.value){
  const button=$('plan-save');if(button)button.disabled=true;
  try{const result=await api('/api/exit-suggestion/'+encodeURIComponent(f.elements.code.value)+'/adopt','POST',{suggestion_token:f.elements.exit_suggestion_token.value,sector:f.elements.sector.value.trim()});$('plan-dialog').close();await refresh();showToast(result.message);}
  catch(err){showToast('建议未保存：'+err.message,true);}finally{if(button)button.disabled=false;}
  return;
 }
 const data={code:f.elements.code.value,strategy_version:'1.0',prepared_id:f.elements.prepared_id.value,sector:f.elements.sector.value.trim(),legacy_exit_only:f.elements.legacy_exit_only.checked,review:{}};
 for(const k of ['entry_price','stop_loss','take_profit_1','take_profit_2','signal_date','trigger_price','resistance_price','buy_date','actual_entry','initial_quantity','tp1_sold_quantity'])if(f.elements[k].value!=='')data[k]=f.elements[k].value;
 for(const k of ['fundamentals_ok','event_clear','corporate_action_clear','resistance_clear'])data.review[k]=f.elements[k].checked;
 if(!data.legacy_exit_only&&!data.prepared_id){showToast('请先生成并通过V1固定计划核验',true);return;}
 try{const r=await api('/api/trade-plan','PUT',data);$('plan-dialog').close();await refresh();showToast(r.message||'固定计划已保存');}catch(err){showToast('计划保存失败：'+err.message,true);}}
async function resetTradePlan(){const code=$('plan-form').elements.code.value;if(!confirm(`删除 ${code} 的固定计划？V1将暂停该证券新增；已有持仓风险可能变为待核验。`))return;try{const r=await api('/api/trade-plan/'+code,'DELETE');$('plan-dialog').close();await refresh();showToast(r.message);}catch(e){showToast(e.message,true)}}
function updateScanControls(){
 const pending=scanStarting||Boolean(scanJob?.running);
 $('live-scan').disabled=pending;
 $('demo-scan').disabled=pending;
 $('job-message').textContent=pending?'扫描正在进行，完成后自动刷新…':
   (scanJob?.message||'尚未扫描').trim().slice(0,600);
}
async function scan(mode){
 if(scanStarting||scanJob?.running)return;
 scanStarting=true;
 updateScanControls();
 renderRanking();
 try{
  const result=await api('/api/scan','POST',{mode,top:40});
  scanJob=result.job;
 }catch(e){showToast(e.message,true);}
 finally{scanStarting=false;updateScanControls();renderRanking();}
 if(scanJob?.running)void pollScan();
}
async function pollScan(){
 if(scanStarting||scanPolling||!scanJob?.running)return;
 scanPolling=true;
 try{
  const job=await api('/api/job');
  if(job.running)return;
  // Keep the previous ranking hidden until the new state has loaded.
  if(!(await refresh()))return;
  scanJob=STORE?.job?.running?STORE.job:job;
  updateScanControls();
  renderRanking();
  if(!scanJob.running)showToast(job.returncode===0?'扫描结束，榜单已更新。':'扫描未完成，请查看扫描状态。',job.returncode!==0);
 }catch(e){$('job-message').textContent='暂时无法读取扫描状态，正在重试。';}
 finally{scanPolling=false;}
}
async function updateWatch(event){event.preventDefault();const f=event.target;try{await api('/api/watchlist','POST',{code:f.elements['code'].value.trim(),name:f.elements['name'].value.trim()});f.reset();await refresh();showToast('观察池更新成功');}catch(e){showToast(e.message,true)}}
async function updateRisk(event){event.preventDefault();const f=event.target;let obj={};for(const k of ['stop_stock','stop_etf','stop_convertible','tp1_pct','tp2_pct','per_trade_risk','max_position_pct','min_reward_risk','max_breakout_extension_pct'])obj[k]=Number(f.elements[k].value);try{const d=await api('/api/risk-settings','PUT',obj);showToast(d.message);await refresh();}catch(e){showToast(e.message,true)}}
function clearPosition(){const f=$('position-form');f.reset();editCode=null;f.elements['code'].readOnly=false;$('position-form-title').textContent='增加/更新持仓记录';$('position-save').textContent='保存新持仓';}
async function updatePosition(event){event.preventDefault();const f=event.target;let obj={};for(const k of ['code','name','type','sector','quantity','cost','baseline_price'])obj[k]=f.elements[k].value;try{const d=await api('/api/positions',editCode?'PUT':'POST',obj);clearPosition();showToast(d.message);await refresh();}catch(e){showToast(e.message,true)}}
async function updateAccount(e){e.preventDefault();try{const d=await api('/api/account','PUT',{cash:Number(e.target.elements['cash'].value)});showToast(d.message);await refresh();}catch(e){showToast(e.message,true)}}
async function dynamicButton(e){const b=e.target.closest('[data-action]');if(!b)return;const {action,code}=b.dataset;if(action==='analyze')return runAnalysis(code,'live');
 if(action==='plan')return editTradePlan(code);
 if(action==='promote-daily'){
  const x=DAILY?.shortlist?.find(x=>x.code===code);if(!x||DAILY.mode==='demo')return;
  try{const r=await api('/api/watchlist','POST',{code,name:x.name});showToast(r.message);await refresh();}catch(e){showToast(e.message,true)}return;
 }
 if(action==='exclude-daily'){
  if(!confirm(`排除 ${code} 后，每日候选池将跳过此代码，确定吗？`))return;
  try{const r=await api('/api/daily-excludes','POST',{code});showToast(r.message);await refreshDailyObserve();}catch(e){showToast(e.message,true)}return;
 }
 if(action==='remove-watch'){const name=STORE.watchlist.find(x=>x.code===code)?.name||code;if(!confirm(`从观察池删除 ${name}（${code}）？持仓台账不变。`))return;try{const d=await api(`/api/watchlist/${code}`,'DELETE');await refresh();showToast(d.message);}catch(e){showToast(e.message,true)}}
 if(action==='level'){
 const name=STORE.watchlist.find(x=>x.code===code)?.name||code;const old=STORE.config.alert_levels.find(x=>x.code===code)||{};
 const lower=prompt(`${name}：跌破观察价（留空代表关闭）`,old.below??'');if(lower===null)return;
 const higher=prompt(`${name}：突破观察价（留空代表关闭）`,old.above??'');if(higher===null)return;
 try{const d=await api('/api/levels','PUT',{code,below:lower.trim(),above:higher.trim()});await refresh();showToast(d.message);}catch(e){showToast(e.message,true)}}
 if(action==='edit-position'){const p=STORE.config.positions.find(x=>x.code===code);if(!p)return;const f=$('position-form');$('position-editor').open=true;for(const k of ['code','name','type','sector','quantity','cost','baseline_price'])f.elements[k].value=p[k]??'';f.elements['code'].readOnly=true;editCode=code;$('position-form-title').textContent='编辑持仓 '+p.name;$('position-save').textContent='保存更新';f.scrollIntoView({behavior:'smooth',block:'center'});}
 if(action==='remove-position'){if(!confirm(`将持仓 ${code} 从本地台账移除？必须自己同步现金，该操作不会在券商卖出。`))return;try{const d=await api(`/api/positions/${code}`,'DELETE');await refresh();showToast(d.message);}catch(e){showToast(e.message,true)}}
}
async function restoreDaily(e){
 e.preventDefault();const code=e.target.elements['code'].value.trim();
 try{const r=await api('/api/daily-excludes','DELETE',{code});showToast(r.message);e.target.reset();await refreshDailyObserve();}catch(e){showToast(e.message,true)}
}
function applyLiveAccountSnapshot(d){
 const r=d.risk;
 riskTier(r);
 const budget=Number(r.budget||STORE?.config?.max_additional_loss||0);
 const used=Number(r.used), stress=Number(r.existing_planned_stress);
 const equityValid=r.equity!==null&&r.equity!==undefined&&Number.isFinite(Number(r.equity));
 if(!equityValid)return false;
 const riskComplete=[r.remaining,r.used,r.existing_planned_stress,r.available_new_risk].every(v=>v!==null&&v!==undefined&&Number.isFinite(Number(v)));
 const strategyBlocked=(r.strategy?.blockers||[]).length>0;
 renderStrategy(STRATEGY,r);
 const verified=d.quote_health?.provider_timestamps_verified===true;
 showPnl(r.pnl,verified?'快照估算':'源时间未核验');
 if(STORE){
  renderPositions(r,d.quotes||[]);
  if(verified&&!strategyBlocked&&riskComplete)renderSizing(r.sizing||[]);
  else if(strategyBlocked||!riskComplete)$('sizing-table').innerHTML='<div class="empty">V1账户、固定止损或行业条件待核验，暂停新增股数；最新资产与盈亏继续显示。</div>';
  else $('sizing-table').innerHTML='<div class="empty">源报价时间未全部核验，暂停显示新增可买股数；请核对券商报价。</div>';
 }
 $('equity').textContent=money(r.equity);
 $('equity-sub').textContent='最新完整持仓快照估算 · 非券商资产 / 非逐笔实时';
 $('remaining').textContent=money(r.remaining);
 $('remaining-sub').textContent='账户额外回撤预算上限 '+money(budget);
 $('risk-used').textContent=money(r.used);
 $('risk-stress').textContent=money(r.existing_planned_stress);
 $('new-risk').textContent=verified?money(r.available_new_risk):'—';
 const ratio=riskComplete&&budget>0?Math.max(0,Math.min(100,(used+stress)/budget*100)):0;
 $('risk-fill').style.width=ratio+'%';
 $('risk-tag').textContent=strategyBlocked||!riskComplete?'V1暂停新增':r.over_budget?'风险超预算':verified?'盘中预算估算':'参考估算·暂停新增';
 $('risk-tag').className='pill'+(r.over_budget?' danger':'');
 const localTime=d.market_time?new Date(d.market_time).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未知';
 $('data-badge').textContent='盘中估算快照';
 $('data-badge').className='pill';
 const b=$('data-badge-inline');if(b){b.textContent='盘中估算快照';b.className='inline-tag';}
 const note=$('data-note');note.className='data-note'+(verified?'':' warn');
 note.textContent=`更新 ${localTime} · ${d.quote_health?.held_count??'—'}只持仓 · ${verified?'源时间已核验，仍可能延迟':'源时间未核验，仅供估算，暂停交易提醒'}。${strategyBlocked?'V1新增被规则阻断；资产估值与盈亏仍有效。':''}`;
 return true;
}
function displaySnapshotChoices(data,allowBroker=true){
 const candidates=[];
 if(data.display_snapshot)candidates.push({kind:'quote',snapshot:data.display_snapshot});
 if(allowBroker&&data.broker_snapshot)candidates.push({kind:'broker',snapshot:data.broker_snapshot});
 return candidates.filter(x=>Number.isFinite(Date.parse(x.snapshot.asof||'')))
  .sort((a,b)=>{
   const at=Date.parse(a.snapshot.asof),bt=Date.parse(b.snapshot.asof);
   const acn=new Date(at+8*3600000).toISOString(),bcn=new Date(bt+8*3600000).toISOString();
   // A broker's closing screenshot includes actual fills/fees/realized P&L.
   // Later provider dissemination of the same closing day cannot replace it.
   if(acn.slice(0,10)===bcn.slice(0,10)&&acn.slice(11,16)>='15:00'&&bcn.slice(11,16)>='15:00'&&a.kind!==b.kind)return a.kind==='broker'?-1:1;
   return bt-at||(a.kind==='broker'?-1:1);
  });
}
function applyLatestAccountSnapshot(data,monitor={}){
 for(const item of displaySnapshotChoices(data,monitor.market_session!==true)){
  if(item.kind==='broker'?applyBrokerAccountSnapshot(item.snapshot):applyHistoricalAccountSnapshot(item.snapshot,{...monitor,broker_snapshot:data.broker_snapshot}))return true;
 }
 return false;
}
function applyHistoricalAccountSnapshot(snapshot,monitor={}){
 const r=snapshot?.risk;
 if(!r||!Number.isFinite(Number(r.equity))||r.equity===null)return false;
 const asof=snapshot.asof?new Date(snapshot.asof).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'未知';
 const manual=snapshot.kind==='broker_screenshot';
 const label=manual?'券商截图快照':monitor.market_session===true?'最近完整快照':'休市参考快照';
 const provider=manual?'同花顺 / 国金证券（手动同步）':(snapshot.source||'').startsWith('腾讯')?'腾讯公开行情':'公开行情';
 const timeLabel=manual?'截图时间':'报价时间';
 showPnl(r.pnl,`${label} · ${asof}`);
 if(STORE)renderPositions(r,snapshot.quotes||[]);
 $('equity').textContent=money(r.equity);
 $('equity-sub').textContent=`${label} · ${timeLabel} ${asof}`;
 $('data-badge').textContent=label;$('data-badge').className='pill muted';
 const badge=$('data-badge-inline');if(badge){badge.textContent=label;badge.className='inline-tag muted';}
 $('data-note').className='data-note';
 $('data-note').textContent=`${monitor.market_session===true?'当前完整报价不可用，显示最近成功估值':'休市保留账户估值'} · ${provider} · ${timeLabel} ${asof}。当前不计算新增交易额度。`;
 const broker=monitor.broker_snapshot;
 if(!manual&&broker&&Number.isFinite(Date.parse(broker.asof||''))){
  const brokerTime=new Date(broker.asof).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  $('data-note').textContent+=` 券商截图原值（${brokerTime}）另供对账：总资产 ${money(broker.total_assets)}，${broker.broker_pnl_label||'总盈亏'} ${money(broker.broker_total_pnl??broker.broker_floating_pnl)}，当日参考盈亏 ${money(broker.broker_daily_reference_pnl)}；与公开行情估算口径不同。`;
 }
 riskTier(null);
 $('remaining').textContent='—';$('remaining-sub').textContent='休市或历史快照，交易时重新核验';
 $('risk-used').textContent=money(r.used);$('risk-stress').textContent=money(r.existing_planned_stress);
 $('new-risk').textContent='—';$('risk-fill').style.width='0%';
 $('risk-tag').textContent='历史参考';$('risk-tag').className='pill muted';
 $('sizing-table').innerHTML='<div class="empty">当前显示最近完整估值，暂停计算新增买入股数。</div>';
 return true;
}
function applyBrokerAccountSnapshot(broker){
 if(!broker)return false;
 const rows=(broker.positions||[]).map(p=>({...p,price:p.screenshot_price,value:p.market_value,
  floating_pnl:p.broker_floating_pnl,daily_pnl:p.broker_daily_pnl,
  levels:STORE?.risk?.positions?.find(x=>x.code===p.code)?.levels}));
 const r={equity:broker.total_assets,used:null,existing_planned_stress:null,positions:rows,
  pnl:{valid:true,daily_valid:true,floating_pnl:broker.broker_floating_pnl,floating_pct:null,
       daily_pnl:broker.broker_daily_reference_pnl,daily_pct:broker.broker_daily_reference_pct}};
 if(!applyHistoricalAccountSnapshot({kind:'broker_screenshot',asof:broker.asof,source:broker.source,risk:r,quotes:[]}))return false;
 if($('floating-label')&&broker.broker_pnl_label)$('floating-label').innerHTML=escapeHtml(broker.broker_pnl_label)+' <small>券商截图原值</small>';
 $('floating-sub').textContent='券商截图原始盈亏 · 手动同步';
 $('daily-sub').textContent=`券商截图当日参考收益率 ${pct(broker.broker_daily_reference_pct)} · 手动同步`;
 return true;
}
function markUnavailableIntraday(d){
 const active=(d.mode==='live'&&d.market_session===true)||(!d.mode&&d.stale===true);
 if(!active)return;
 $('data-badge').textContent='盘中数据不可用';$('data-badge').className='pill muted';
 const b=$('data-badge-inline');if(b){b.textContent='盘中数据不可用';b.className='inline-tag muted';}
 $('data-note').className='data-note warn';
 $('data-note').textContent=`盘中没有可用的完整持仓行情：${d.status||'采集失败'}。账户上一次显示的金额只能当历史参考，禁止据此判断新增仓位或风险额度。`;
 riskTier(null);
 $('risk-tag').textContent='预算未核验';$('risk-tag').className='pill muted';
 $('remaining').textContent='—';$('risk-used').textContent='—';$('risk-stress').textContent='—';$('new-risk').textContent='—';
 $('risk-fill').style.width='0%';
 $('sizing-table').innerHTML='<div class="empty">盘中风险预算未核验，暂停计算新增买入股数。</div>';
 $('equity').textContent='—';$('floating-pnl').textContent='—';$('daily-pnl').textContent='—';
 $('floating-pnl').className='';$('daily-pnl').className='';
 $('equity-sub').textContent='盘中价格不完整/过期，当前净值不可用';
 $('floating-sub').textContent='本轮盘中数据不可用，不可依据旧盈亏交易';
 $('daily-sub').textContent='本轮盘中数据不可用，不可依据旧盈亏交易';
}
async function refreshMonitor(){
 if(monitorBusy)return;
 monitorBusy=true;
 try{
  const d=await api('/api/intraday');
  if(d.exit_suggestions)EXIT_SUGGESTIONS=d.exit_suggestions;
  const usable=!d.stale&&d.mode==='live'&&d.market_session===true&&d.calendar_ok===true&&d.risk?.valid===true&&d.quote_health?.all_held_usable===true&&!d.quote_health?.block_new_signals;
  const pillNode=$('monitor-pill');
  pillNode.textContent=d.stale?'数据过期 / 未运行':d.mode==='demo'?'演示模式':d.market_session&&d.calendar_ok?'盘中轮询':'暂停 / 休市';
  $('monitor-status').textContent=(d.status||'尚无状态')+`（目标间隔 ${d.interval_seconds??10}秒`+(d.actual_cycle_gap_seconds!=null?`，实际两轮间隔 ${d.actual_cycle_gap_seconds}秒`:'')+(d.seconds_since_update!==undefined?`，距离上次完成 ${d.seconds_since_update}秒`:'')+(d.fetch_duration_seconds!==undefined?`，本轮耗时 ${d.fetch_duration_seconds}秒`:'')+'；不是交易所逐笔实时报价）';
  $('monitor-market-time').textContent=d.market_time||'—';
  $('monitor-update').textContent=(d.fetch_finished_at||d.updated_at)?new Date(d.fetch_finished_at||d.updated_at).toLocaleString('zh-CN'):'—';
  const provider=(d.data_source||'').startsWith('腾讯')?'腾讯公开行情':'公开报价';
  $('monitor-mode').textContent=d.mode==='demo'?'演示（禁止交易）':d.source_timestamp_verified?`${provider} · 持仓源时间已核验`:`${provider} · 部分源时间未核验`;
  $('monitor-risk').textContent=usable?`累计回撤 ${money(d.risk.used)} / 剩余预算 ${money(d.risk.remaining)}`:'完整持仓风险报价不可用，暂停新增';
  // Every 6s the browser checks the most recent completed background cycle. It does
  // not cause a new quote request. All account and risk values are one snapshot.
  if(STORE){
   if(usable){if(!applyLiveAccountSnapshot(d))markUnavailableIntraday(d);}
   else if(!applyLatestAccountSnapshot(d,d))markUnavailableIntraday(d);
  }
  const quality=d.quote_health||{};
  const bad=[...(quality.missing||[]),...(quality.invalid_jump||[]),...(quality.suspected_freeze||[]),...(quality.stale_source||[])];
  const unverified=quality.unverified_source||[];
  const qualityMsg=`行情质量：持仓${quality.held_count??'—'}只 · 源时间戳已核验${quality.verified_count??0}只 · ${bad.length?'异常/可疑代码 '+[...new Set(bad)].join('、'):'未发现明显跳价或长时间不变'} · ${unverified.length?'尚无可核验源时间：'+unverified.join('、')+'；仅参考估值，暂停新交易提醒':'行情源时间已核验（仍非逐笔实时）'}`;
  $('quote-health-banner').textContent=qualityMsg;
  $('quote-health-banner').className='signal-note'+((bad.length||unverified.length)?' blocked':'');
  const hrows=Object.entries(quality.by_code||{}).map(([code,x])=>tr([escapeHtml(code),x.price==null?'—':format(x.price,3),escapeHtml({ok:'已获取（不一定实时）',missing:'缺失',invalid_jump:'异常跳价',stale_source:'源时间过期',suspected_freeze:'报价长期未变'}[x.status]||'待核查'),x.source_timestamp?escapeHtml(x.source_timestamp):'未提供 / 未核验',format(x.consecutive_same??0,0)]));
  $('quote-health-table').innerHTML=hrows.length?table(['持仓代码','采集价','状态','行情源时间（如可验证）','连续相同次数'],hrows):'';
  const displayedQuotes=usable?(d.quotes||[]):(d.display_snapshot?.quotes||d.quotes||[]);
  let qs=displayedQuotes.map(x=>tr([escapeHtml(x.name),escapeHtml(x.code),!usable?(d.display_snapshot&&x.price!==null?`${format(x.price,3)}（历史参考）`:'—（不可用于风控）'):(x.price===null?'—':format(x.price,3)),
   x.held?'持仓':'自选',x.stop_loss===undefined?'—':priceText(x.stop_loss),x.take_profit_1===undefined?'—':priceText(x.take_profit_1),x.take_profit_2===undefined?'—':priceText(x.take_profit_2)]));
  $('intraday-quotes').innerHTML=table(['名称','代码','参考快照价','类型','止损','止盈①','止盈②'],qs);
  let items=(d.recent_alerts&&d.recent_alerts.length?d.recent_alerts:d.alerts||[]).slice(-20).reverse();
  let es=items.map(x=>tr([escapeHtml(x.at||'本轮'),escapeHtml(x.name),escapeHtml(x.kind),priceText(x.price),priceText(x.level)]));
  $('intraday-alerts').innerHTML=es.length?table(['触发时间','标的','提醒类型','快照值','参考位'],es):'<p class="tip">暂无提醒；同一天同类型的提醒默认仅通知一次。</p>';
  $('monitor-errors').textContent=(d.errors||[]).slice(0,4).join('；')+' '+(d.warning||'');
  renderTradeSignals(d);
 }catch(e){
  $('monitor-status').textContent='获取监控状态失败：'+e.message;
  if(STORE)markUnavailableIntraday({mode:'live',market_session:true,status:e.message});
  renderTradeSignals({stale:true,mode:'live',trade_signals:[],recent_trade_signals:[],risk:null});
 }finally{monitorBusy=false;}
}

const tPhaseLabel=phase=>({sell_watch:'高抛条件观察',sell_ready:'高抛条件观察',sell_first:'先卖后买观察',buyback_ready:'接回条件观察',buyback_wait:'等待企稳',buyback_blocked:'回补受限',buyback_watch:'接回条件观察',buyback:'接回条件观察',carry_cash:'保留现金',risk_first:'风险处理优先',too_small:'底仓不足',inactive:'暂停新开T',wait:'等待条件',blocked:'条件待核验',closed:'本轮结束',expired:'历史轮次'}[phase]||'条件观察');
function discardTPopups(allowed=[]){
 const ids=new Set(allowed.map(x=>x.id));
 popupQueue=popupQueue.filter(x=>x.category!=='t'||ids.has(x.id));
 if(popupActive?.category==='t'&&!ids.has(popupActive.id))nextTradePopup();
}
function tCycleRemaining(cycle){
 const raw=cycle.unbought_quantity??(Number(cycle.quantity)-Number(cycle.buyback_quantity||0));
 const n=Number(raw);return Number.isFinite(n)?Math.max(0,n):null;
}
function setTSelectOptions(formId,field,options,placeholder){
 const select=$(formId)?.elements?.[field];if(!select)return;
 const previous=select.value||'',html='<option value="">'+escapeHtml(placeholder)+'</option>'+options.map(([value,label])=>`<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`).join('');
 if(select.innerHTML!==html){select.innerHTML=html;select.value=options.some(([value])=>String(value)===previous)?previous:'';}
}
function renderTObserve(data){
 if(!$('t-observation-list'))return;
 const d=data||{},active=d.enabled===true&&!d.stale,rows=d.rows||[],cycles=d.cycles||[];
 if(!active)discardTPopups();
 const status=typeof d.status==='string'?d.status:'等待分时行情与成交记录核验';
 $('t-status').textContent=status+(d.asof?' · 数据截至 '+d.asof:'')+(active?'':'；当前不可用于实时做T');
 $('t-count').textContent=!d.enabled?'做T观察未启用':d.stale?'数据待更新':rows.length+'只持仓观察';
 $('t-count').className='pill '+(active?'':'muted');
 $('t-settings').textContent=`先卖后买 · 每只每天最多1轮 · 费用滑点预留至少 ${money(Math.max(20,Number(d.settings?.fee_reserve)||20))}。预期价差需覆盖费用，尾盘不强制接回；收益数字仅估算。`;
 $('t-observation-list').innerHTML=rows.length?rows.map(row=>{
  const blockers=(row.blockers||[]).map(noteText),price=active?row.price:null;
  const quantity=active&&!blockers.length&&row.quantity!==null&&row.quantity!==undefined&&Number.isInteger(Number(row.quantity))&&Number(row.quantity)>=100?Number(row.quantity):null;
  const net=active?row.net_spread_estimate:null,gross=active?row.gross_spread_estimate:null;
  return `<article class="t-observation-card ${active&&blockers.length?'blocked':''}"><div class="signal-card-head"><strong>${escapeHtml(row.name||row.code)}（${escapeHtml(row.code)}）</strong><span class="signal-tag">${escapeHtml(active?tPhaseLabel(row.phase):'历史/待更新')}</span></div>
  <p>${escapeHtml(active?(row.reason||row.status||'等待后端条件核验'):'数据过期或观察未启用，不沿用旧价位作为实时做T条件。')}</p>
  <div class="t-price-grid"><div><span>最新参考</span><b>${priceText(price)}</b></div><div><span>分时均价</span><b>${priceText(active?row.vwap:null)}</b></div><div><span>高抛观察</span><b>${priceText(active?row.target_sell:null)}</b></div><div><span>接回观察</span><b>${priceText(active?row.target_buy:null)}</b></div></div>
  <div class="signal-fields"><span>核验数量 <strong>${quantity===null?'—（待核验）':format(quantity,0)}</strong></span><span>毛价差估算 <strong>${money(gross)}</strong></span><span>费用滑点预留 <strong>${money(row.fee_reserve)}</strong></span><span>扣预留后估算 <strong>${money(net)}</strong></span></div>
  ${blockers.length?'<ul class="t-blockers">'+blockers.map(x=>`<li>${escapeHtml(x)}</li>`).join('')+'</ul>':''}<small>参考区间不是委托；跌了不等于该接回，卖出也不保证能接回。${row.cycle_id?'本轮 '+escapeHtml(row.cycle_id):'尚无本轮实际T卖出记录时，不提供接回任务。'}</small></article>`;
 }).join(''):'<p class="tip">尚无可核验的做T观察。请等待分钟行情与本地持仓匹配；不会用演示价格补齐。</p>';
 const availability=Array.isArray(d.availability)?d.availability:Object.entries(d.availability||{}).map(([code,x])=>({...x,code}));
 $('t-availability-list').innerHTML=availability.length?'<h4>已确认的可卖数量（以记录日期为准）</h4>'+table(['代码','确认日期','确认可卖数量'],availability.map(x=>tr([escapeHtml(x.code),escapeHtml(x.date||'待核验'),format(x.available_quantity,0)]))):'<p class="tip">尚未确认今日可卖数量；只展示观察，不核定可执行股数。</p>';
 $('t-cycle-list').innerHTML=cycles.length?'<h4>本地T轮次 · 不代表券商账户已同步</h4>'+table(['标的 / 日期','实际T卖出','已接回 / 未接回','已配对毛差估算','扣预留后配对估算','状态'],[...cycles].reverse().map(c=>tr([escapeHtml((c.name||c.code||'')+' / '+(c.date||'未知')),format(c.quantity,0)+' × '+priceText(c.sell_price),format(c.buyback_quantity||0,0)+' / '+format(tCycleRemaining(c),0),money(c.paired_gross_estimate),money(c.paired_net_estimate),escapeHtml((c.expired?'历史轮次 · ':'')+(c.status==='closed'?'已结束':c.status==='open'?'未结束':c.status||'待核验')+(c.ledger_sync_required?' · 持仓与现金待同步':''))]))):'<p class="tip">尚无实际T卖出记录；普通风险减仓不会被自动当成做T。</p>';
 const holdings=STORE?.config?.positions?STORE.config.positions.filter(p=>Number(p.quantity)>0):rows;
 const securities=holdings.map(p=>[String(p.code),(p.name||p.code)+'（'+p.code+'）']);
 for(const id of ['t-availability-form','t-sell-form'])setTSelectOptions(id,'code',securities,'选择持仓');
 const open=cycles.filter(c=>c.status==='open'),label=c=>(c.name||c.code)+' · '+c.date+' · 未接回 '+format(tCycleRemaining(c),0);
 setTSelectOptions('t-buyback-form','cycle_id',open.filter(c=>!c.expired&&tCycleRemaining(c)>0).map(c=>[String(c.id),label(c)]),'选择未结束轮次');
 setTSelectOptions('t-close-form','cycle_id',open.map(c=>[String(c.id),label(c)]),'选择未结束轮次');
}
async function refreshTObserve(){
 if(tObserveBusy||!$('t-observation-list'))return;
 tObserveBusy=true;
 try{T_OBSERVE=await api('/api/t-observe');renderTObserve(T_OBSERVE);}
 catch(e){renderTObserve({...T_OBSERVE,enabled:T_OBSERVE?.enabled??true,stale:true,status:'做T观察读取失败：'+e.message});}
 finally{tObserveBusy=false;}
}
function tInput(form,key,{integer=false,zero=false}={}){
 const raw=form.elements[key]?.value,n=Number(raw);
 if(raw===undefined||raw===''||!Number.isFinite(n)||(zero?n<0:n<=0)||(integer&&!Number.isInteger(n)))throw Error(integer?'数量需填写有效整数':'请填写有效成交价格');
 return n;
}
function tCode(form){const code=form.elements.code.value.trim();if(!/^\d{6}$/.test(code))throw Error('请选择6位证券代码');return code;}
async function mutateT(event,build){
 event.preventDefault();if(tWriteBusy)return;
 const form=event.target,button=event.submitter;
 try{
  const {path,method,body}=build(form);
  tWriteBusy=true;if(button)button.disabled=true;
  const d=await api(path,method,body);form.reset();showToast(d.message||'本地做T记录已保存；请同步实际持仓和现金');
  await refreshTObserve();
 }catch(e){showToast('做T记录未保存：'+e.message,true);}
 finally{tWriteBusy=false;if(button)button.disabled=false;}
}
function saveTAvailability(event){return mutateT(event,form=>{
 if(!form.elements.availability_confirmed.checked)throw Error('先核对并确认今日券商可卖数量');
 return {path:'/api/t-availability',method:'PUT',body:{code:tCode(form),available_quantity:tInput(form,'available_quantity',{integer:true,zero:true})}};
});}
function saveTSell(event){return mutateT(event,form=>{
 if(!form.elements.sell_filled.checked)throw Error('仅可登记已经真实成交的T高抛');
 return {path:'/api/t-cycles',method:'POST',body:{code:tCode(form),quantity:tInput(form,'quantity',{integer:true}),sell_price:tInput(form,'sell_price'),sell_filled:true}};
});}
function saveTBuyback(event){return mutateT(event,form=>{
 if(!form.elements.buy_filled.checked)throw Error('仅可登记已经真实成交的接回');
 const id=form.elements.cycle_id.value,cycle=(T_OBSERVE?.cycles||[]).find(x=>String(x.id)===id&&x.status==='open'&&!x.expired),quantity=tInput(form,'quantity',{integer:true});
 if(!cycle)throw Error('先选择已登记实际T卖出的有效轮次');
 if(tCycleRemaining(cycle)===null||quantity>tCycleRemaining(cycle))throw Error('接回数量不能超过本轮尚未接回的实际T卖出量');
 return {path:'/api/t-cycles/'+encodeURIComponent(id)+'/buyback',method:'POST',body:{quantity,price:tInput(form,'price'),buy_filled:true}};
});}
function closeTCycle(event){return mutateT(event,form=>{
 if(!form.elements.leave_cash.checked)throw Error('请确认结束本轮并保留现金');
 const id=form.elements.cycle_id.value,cycle=(T_OBSERVE?.cycles||[]).find(x=>String(x.id)===id&&x.status==='open');
 if(!cycle)throw Error('请选择尚未结束的轮次');
 return {path:'/api/t-cycles/'+encodeURIComponent(id)+'/close',method:'POST',body:{leave_cash:true}};
});}
function initTObserve(){
 for(const [id,handler] of [['t-availability-form',saveTAvailability],['t-sell-form',saveTSell],['t-buyback-form',saveTBuyback],['t-close-form',closeTCycle]])$(id)?.addEventListener('submit',handler);
 $('t-refresh')?.addEventListener('click',refreshTObserve);
 refreshTObserve();setInterval(refreshTObserve,15000);
}

function signalCard(x,active){
 const priority=x.priority==='high'?'需优先复核':'观察条件';
 const label={add:'新增观察',reduce:'减仓/止盈复核',switch:'换仓比较',t:'做T条件观察'}[x.category]||'观察';
 const where=x.code==='PORT'?'全账户':`${x.name}（${x.code}）`;
 return `<article class="signal-card ${escapeHtml(x.category||'')}" aria-label="${escapeHtml(label)}">
 <div class="signal-card-head"><strong>${escapeHtml(where)} · ${escapeHtml(x.title)}</strong><span class="signal-tag">${active?escapeHtml(priority):'历史记录'}</span></div>
 <div class="signal-fields"><span>观察价/回撤 <strong>${x.price===null?'—':priceText(x.price)}</strong></span>
 <span>触发阈值 <strong>${priceText(x.level)}</strong></span>
 ${x.shares?`<span>${x.category==='t'?'本轮核验上限':'理论上限'} <strong>${format(x.shares,0)}股</strong></span>`:''}</div>
 <p>${escapeHtml(x.reason)}</p><small>${escapeHtml(x.at||x.day||'本轮监控')} · ${escapeHtml(x.data_note||'第三方行情，需复核')}</small></article>`;
}
function renderTradeSignals(d){
 const activeAllowed=!d.stale&&d.mode==='live'&&d.market_session===true&&d.calendar_ok===true&&!d.quote_health?.block_trade_signals;
 const live=activeAllowed?(d.trade_signals||[]):[];
 discardTPopups(live.filter(x=>x.category==='t'));
 for(const cat of ['add','reduce','switch','t']){
   const current=live.filter(x=>x.category===cat);
   const recent=(d.recent_trade_signals||[]).filter(x=>x.category===cat).slice(-5).reverse();
   $(`nav-count-${cat}`).textContent=current.length;
   $(`signal-${cat}-badge`).textContent=!activeAllowed?'未在有效盘中监测':current.length?`${current.length}条待复核`:'无当前触发';
   $(`signal-${cat}-badge`).className='pill '+(!activeAllowed?'muted':current.length&&cat==='reduce'?'danger':'');
   const box=$(`signal-${cat}-list`);
   box.innerHTML=current.length?current.map(x=>signalCard(x,true)).join(''):
     `<div class="panel"><p class="tip">${activeAllowed?'本轮未检测到符合该类别的条件。':'当前不在有效实时轮询状态，不能产生交易信号。'}</p></div>`;
   if(recent.length){box.innerHTML+=`<div class="signal-note">历史触发记录（非当前信号，不应据此交易）</div>`+recent.map(x=>signalCard(x,false)).join('');}
 }
 const budget=d.risk;
 const addNote=$('signal-add-note');
 if(!activeAllowed){addNote.textContent='当前不在有效交易时段/后台已过期，或为演示模式：暂停新增信号。';addNote.className='signal-note blocked';}
 else if(!budget?.valid||(budget.strategy?.blockers||[]).length||budget.available_new_risk<=0){addNote.textContent='暂停新增：'+((budget?.strategy?.blockers||[]).map(noteText).join('；')||'持仓行情、固定计划或全账户预算未通过')+'。';addNote.className='signal-note blocked';}
 else{addNote.textContent=`当前理论可分配新交易风险约${money(budget.available_new_risk)}；V1新增观察仍须核实最新报价、固定计划和人工复核。`;addNote.className='signal-note';}
 $('signal-reduce-note').textContent=activeAllowed?'减仓/止盈提示来自第三方快照，须检查券商实时价格、流动性和整账户风险。':'盘中轮询未就绪；历史信号不可用来替代新行情。';
 $('signal-switch-note').textContent='换仓先后两步：核实减仓→成交确认→更新现金与持仓→重新测算目标股可买数量。软件不会自动下单。';
 if(!activeAllowed)return;
 const now=Date.now();
 const fresh=(d.new_trade_signals||[]).filter(x=>{
   const t=Date.parse(x.at||'');
   return Number.isFinite(t)&&now-t>=0&&now-t<12*60*1000&&!signalSeen.has(x.id);
 }).sort((a,b)=>Number(b.priority==='high')-Number(a.priority==='high'));
 for(const x of fresh){signalSeen.add(x.id);popupQueue.push(x);}
 if(fresh.length){
   try{localStorage.setItem('henga_signal_seen_v33',JSON.stringify([...signalSeen].slice(-250)));}catch{}
   if(!popupActive)nextTradePopup();
 }
}
function nextTradePopup(){
 const x=popupQueue.shift();
 popupActive=x||null;
 const el=$('trade-signal-popup');
 if(!x){el.className='trade-signal-popup hidden';return;}
 el.className='trade-signal-popup '+x.category;
 $('trade-popup-tag').textContent=(x.is_demo?'演示 · ':'')+({reduce:'减仓/止盈复核',add:'新增观察',switch:'换仓比较',t:'做T条件观察'}[x.category]||'观察信号');
 $('trade-popup-time').textContent=x.at?new Date(x.at).toLocaleTimeString('zh-CN'):'本轮';
 $('trade-popup-title').textContent=x.title;
 $('trade-popup-reason').textContent=x.reason;
 $('trade-popup-level').textContent=`${x.name} · 参考价/回撤 ${priceText(x.price)} ｜ 观察阈值 ${priceText(x.level)}`;
}
function demoTradePopup(){popupQueue.push({id:'ui-demo',category:'reduce',code:'000000',name:'虚构演示标的',title:'【纯演示】底部减仓提示效果',reason:'这是一条人为生成的界面测试提示，不包含真实行情、持仓或交易建议，也不会进入历史信号记录。',price:20,level:19,at:new Date().toISOString(),is_demo:true});if(!popupActive)nextTradePopup();}
function dismissTradePopup(){nextTradePopup()}
function viewTradePopup(){const cat=popupActive?.category; dismissTradePopup();if(cat){history.pushState(null,'','#signals-'+cat);showSection('signals-'+cat);}}

async function refreshMarketContext(){
 try{
   const d=await api('/api/market-context');
   const demo=d.mode==='demo',old=d.stale;
   $('premarket-pill').textContent=demo?'演示数据':old?'数据过期':({cancellable:'可撤单阶段',locked:'不可撤单阶段',confirmation:'9:25后观察',closed:'非竞价时段'}[d.phase]||'等待更新');
   $('premarket-status').textContent=(d.status||'无数据')+(old?'；缓存已过期，请检查采集进程':'')+(demo?'；模拟数据禁止交易':'');
   const rows=(d.auction?.rows||[]).map(x=>tr([
     escapeHtml(x.name||''),escapeHtml(x.code||''),
     x.price===null||x.price===undefined?'—':format(x.price,3)+(x.usable===false?'（不可用）':''),
     x.indicative_change_pct===null||x.indicative_change_pct===undefined?'—':format(x.indicative_change_pct,2)+'%',
     escapeHtml(x.source_time||'—'),escapeHtml(x.quality||'未核验')]));
   $('auction-table').innerHTML=rows.length?table(['股票','代码','盘前参考价','相对昨收（如可用）','源记录时间','数据状态'],rows):'<p class="tip">当前没有可用的盘前竞价分钟数据。竞价不代表已成交，不提供虚假的委比或资金净流入。</p>';
   $('auction-errors').textContent=(d.auction?.errors||[]).slice(0,3).join('；')+(d.auction?.limited?'；为避免限流仅监测前12只股票':'');
   const g=(d.global?.rows||[]);
   $('global-market-grid').innerHTML=g.map(x=>{
     const unusable=!x.usable||old||demo;
     const n=x.price==null?'—':format(x.price,2);
     const pct=x.pct==null?'—':(x.pct>=0?'+':'')+format(x.pct,2)+'%';
     const color=x.pct>=0?'up':'down';
     return `<article class="global-card ${unusable?'dim':''}"><div class="global-title">${escapeHtml(x.name||'')}</div>`+
       `<div class="global-price">${escapeHtml(n)} <span class="${color}">${escapeHtml(pct)}</span></div>`+
       `<small>${escapeHtml(x.source_time||'没有源时间')} · ${escapeHtml(x.quality||'无数据')}</small></article>`;
   }).join('')||'<p class="tip">尚无海外指数数据</p>';
   $('global-status').textContent=(d.global?.source||'待获取')+(d.global?.error?'；接口错误：'+d.global.error:'')+'。美股日期可能对应上周五，日韩可能因休市、延迟而不可用。';
 }catch(e){$('premarket-status').textContent='读取失败：'+e.message;}
}


function setActiveNav(sectionId){
 let selected=null;
 document.querySelectorAll('.nav-link[href^="#"]').forEach(link=>{
   const active=link.getAttribute('href')===`#${sectionId}`;
   link.classList.toggle('active',active);
   link.classList.toggle('current',active);
   if(active){link.setAttribute('aria-current','location');selected=link;}
   else link.removeAttribute('aria-current');
 });
 // Only scroll the fixed desktop sidebar, never move the main document.
 const sidebar=document.querySelector('.sidebar');
 if(!selected||!sidebar||window.innerWidth<=750)return;
 const sr=sidebar.getBoundingClientRect(),lr=selected.getBoundingClientRect();
 const bottom=sr.bottom-20,top=sr.top+20;
 if(lr.bottom>bottom+8)sidebar.scrollTop+=lr.bottom-bottom;
 else if(lr.top<top-8)sidebar.scrollTop-=top-lr.top;
}
function showSection(id, scroll=true){
 const sections=[...document.querySelectorAll('main > section')];
 const target=document.getElementById(id);
 const page=sections.find(section=>section===target||section.contains(target))||$('overview');
 sections.forEach(section=>{section.hidden=section!==page;});
 const selected=id==='chan-study'?'watch':page.id;
 const active=document.querySelector(`.nav-link[href="#${selected}"]`);
 if(active?.closest('details'))active.closest('details').open=true;
 setActiveNav(selected);
 if(scroll){
  if(target&&target!==page)target.scrollIntoView({block:'start'});
  else window.scrollTo({top:0,behavior:'instant'});
 }
}
function setupNavigation(){
 const update=()=>showSection(location.hash.slice(1)||'overview');
 document.querySelectorAll('a[href^="#"]').forEach(link=>link.addEventListener('click',event=>{
  const id=link.hash.slice(1);
  if(!document.getElementById(id))return;
  event.preventDefault();
  if(location.hash!==link.hash)history.pushState(null,'',link.hash);
  showSection(id);
 }));
 window.addEventListener('hashchange',update);
 window.addEventListener('popstate',update);
 update();
}


async function checkAllHoldingsChan(){
 const box=$('chan-holdings-results'); if(!box||!STORE)return;
 const holdings=STORE.config.positions||[];
 if(!holdings.length){box.innerHTML='<p class="tip">没有持仓台账。</p>';return;}
 box.innerHTML='<p class="tip">正在逐只读取真实历史日线。数据源可能限流，请不要多次重复点击。</p>';
 const parts=[];
 for(const p of holdings){
   try{
     const data=await api('/api/analyze/'+encodeURIComponent(p.code)+'?mode=live');
     const c=data.chan||{};const i=c.interpretation||{};
     const integrated=data.integrated||{};const t=data.technical||{};const swing=data.swing;const wording=swing?swing.label:(c.valid&&t.valid)?`${integrated.action||'继续观察'} · ${integrated.alignment||'待核验'} · ${i.phase||'结构待核验'}`:`继续观察 · ${!c.valid?(c.reason||'缠论历史数据不足'):(t.reason||'技术指标数据不足')}`;
     parts.push(`<article class="chan-holding"><div><strong>${escapeHtml(p.name)}（${escapeHtml(p.code)}）</strong><span class="${(integrated.action||'').includes('减仓')?'pnl-loss':(integrated.action||'').includes('买入')?'pnl-gain':''}">${escapeHtml(wording)}</span></div><p>${escapeHtml(swing?.summary||'等待完整证据')}</p><small>${escapeHtml(data.analysis_window?.label||'最近1年')}日线 ${c.raw_count??0} 根 · MACD ${escapeHtml(t.macd?.status||'未确认')} · 5日量比 ${escapeHtml(t.volume?.ratio5??'—')} · ${escapeHtml(data.score?.['日期']||'—')} · ${escapeHtml(c.confirmation||'未通过严格多级别验证')}</small><button class="btn small ghost" data-action="analyze" data-code="${escapeHtml(p.code)}">查看结构详情</button></article>`);
   }catch(e){parts.push(`<article class="chan-holding"><strong>${escapeHtml(p.name)}（${escapeHtml(p.code)}）</strong><p class="tip">历史数据获取失败：${escapeHtml(e.message)}。暂停判断。</p></article>`);}
   box.innerHTML=parts.join('');
 }
}
function init(){initTObserve();for(const [id,handler] of [['strategy-settings-form',saveStrategySettings],['strategy-flow-form',saveStrategyFlow],['strategy-review-form',saveStrategyReview]])$(id)?.addEventListener('submit',handler);$('strategy-reconcile')?.addEventListener('click',reconcileStrategy);$('strategy-resume')?.addEventListener('click',resumeStrategy);$('plan-prepare')?.addEventListener('click',prepareTradePlan);$('plan-suggest')?.addEventListener('click',()=>loadExitSuggestion($('plan-form').elements.code.value));$('plan-manual')?.addEventListener('click',enableManualExitPlan);$('plan-form').elements.legacy_exit_only.addEventListener('change',updatePlanMode);$('strategy-flow-form').elements.date.value=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());if($('quick-analysis-form'))$('quick-analysis-form').addEventListener('submit',event=>{event.preventDefault();if(!STORE){showToast('账户数据正在加载，请稍候');return;}runAnalysis(event.target.elements['code'].value.trim());});if($('reduction-form'))$('reduction-form').addEventListener('submit',simulateReduction);if($('manual-trade-form')){$('manual-trade-form').addEventListener('submit',saveManualTrade);$('manual-trade-form').elements['date'].value=new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}if($('trades-refresh'))$('trades-refresh').addEventListener('click',()=>showTrades().catch(e=>showToast(e.message,true)));if($('curve-refresh'))$('curve-refresh').addEventListener('click',refreshCurve);refreshCurve();showTrades().catch(console.warn);if($('chan-check-holdings'))$('chan-check-holdings').addEventListener('click',checkAllHoldingsChan); setupNavigation(); $('daily-refresh').addEventListener('click',refreshDailyObserve);$('daily-restore-form').addEventListener('submit',restoreDaily);$('test-trade-popup').addEventListener('click',demoTradePopup);$('trade-popup-close').addEventListener('click',dismissTradePopup);$('trade-popup-dismiss').addEventListener('click',dismissTradePopup);$('trade-popup-detail').addEventListener('click',viewTradePopup);document.querySelectorAll('.table-tab').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.table-tab').forEach(x=>x.classList.remove('selected'));b.classList.add('selected');ranking=b.dataset.tab;renderRanking()}));
 $('refresh').addEventListener('click',refresh);$('premarket-refresh').addEventListener('click',refreshMarketContext);$('monitor-refresh').addEventListener('click',refreshMonitor);$('live-scan').addEventListener('click',()=>scan('live'));$('demo-scan').addEventListener('click',()=>scan('demo'));$('watch-form').addEventListener('submit',updateWatch);$('risk-form').addEventListener('submit',updateRisk);$('account-form').addEventListener('submit',updateAccount);$('position-form').addEventListener('submit',updatePosition);$('position-clear').addEventListener('click',clearPosition);$('plan-form').addEventListener('submit',saveTradePlan);$('plan-close').addEventListener('click',()=>$('plan-dialog').close());$('plan-reset').addEventListener('click',resetTradePlan);document.addEventListener('click',dynamicButton);
 setInterval(pollScan,3000);
 setInterval(()=>{$('clock').textContent=new Date().toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',month:'2-digit',day:'2-digit'})},15000);
 setInterval(refreshDailyObserve,60000);setInterval(refreshStrategy,30000);setInterval(refreshMonitor,6000);setInterval(refreshMarketContext,6000);refreshMarketContext();refreshMonitor();refresh();
}
document.addEventListener('DOMContentLoaded',init);
