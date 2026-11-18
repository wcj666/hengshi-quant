const $ = id => document.getElementById(id);
const cash = value => value == null ? '—' : '¥' + Number(value).toLocaleString('zh-CN', {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const price = value => value == null ? '—' : Number(value).toFixed(3);
const time = value => value ? new Date(value).toLocaleString('zh-CN', {
  timeZone: 'Asia/Shanghai', hour12: false,
}) : '—';
const quantity = value => value == null ? '—' : Number(value).toLocaleString('zh-CN');
const tone = value => value > 0 ? 'gain' : value < 0 ? 'loss' : 'neutral';
const profit = value => value == null ? '—' : (value > 0 ? '+' : value < 0 ? '−' : '') + cash(Math.abs(value));
const sideName = side => side === 'buy' ? '模拟买入' : '模拟卖出';
const form = $('paper-order-form');
const fields = form.elements;
let csrf = '';
let snapshot = null;
let pending = null;
let formVersion = 0;
let snapshotVersion = 0;
let refreshing = null;
let previewing = false;
let confirming = false;

async function api(path, body) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const options = {cache: 'no-store', credentials: 'same-origin'};
    if (body !== undefined) {
      if (!csrf) {
        const session = await fetch('/api/session', options);
        if (!session.ok) throw Error('会话读取失败');
        csrf = (await session.json()).csrf_token;
        if (!csrf) throw Error('会话读取失败');
      }
      options.method = 'POST';
      options.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf};
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const data = await response.json();
    if (attempt === 0 && response.status === 403 && data.code === 'csrf_expired') {
      csrf = '';
      continue;
    }
    if (!response.ok || data.ok === false) {
      const error = Error(data.error || data.message || '操作失败，请重试');
      error.details = data.preview;
      throw error;
    }
    return data;
  }
}

function cell(text, small) {
  const td = document.createElement('td');
  td.textContent = text;
  if (small) {
    const note = document.createElement('small');
    note.textContent = small;
    td.append(note);
  }
  return td;
}

function table(id, rows, empty, columns = 6) {
  const body = $(id);
  if (rows.length) {
    body.replaceChildren(...rows);
    return;
  }
  const tr = document.createElement('tr');
  const td = cell(empty);
  td.colSpan = columns;
  td.className = 'empty';
  tr.append(td);
  body.replaceChildren(tr);
}

function selectedPosition() {
  return snapshot?.positions.find(position => position.code === fields.code.value.trim());
}

function orderBlocker() {
  if (!snapshot) return '等待模拟账本加载。';
  const trading = snapshot.trading;
  if (trading?.session_open === false) return trading.session_reason || '当前时段暂停模拟交易。';
  if (snapshot.before_start || snapshot.ended) return '当前日期不在比赛交易期间。';
  if (fields.side.value === 'buy') {
    if (trading?.buy_allowed === false || snapshot.paused) {
      return trading?.buy_reason || '当前暂停新增模拟持仓。';
    }
    if (selectedPosition()) return '已持有该证券，比赛规则暂停加仓。';
  } else if (trading?.sell_allowed === false) {
    return trading.sell_reason || '当前暂停模拟卖出。';
  } else if (fields.code.value.trim() && !(selectedPosition()?.sellable > 0)) {
    return '该证券当前没有已核对的可卖数量。';
  }
  return '';
}

function fillableSellQuantity(position) {
  if (!position || !position.sellable) return 0;
  if (position.sellable === position.quantity) return position.sellable;
  return Math.floor(position.sellable / 100) * 100;
}

function updateOrderState() {
  const blocker = orderBlocker();
  const selected = selectedPosition();
  const selling = fields.side.value === 'sell';
  const fillable = selling ? fillableSellQuantity(selected) : 0;
  $('paper-session').textContent = blocker || '预览时使用新盘口核对交易条件。';
  $('paper-inventory').textContent = selected ? `${selected.name} · 持有 ${quantity(selected.quantity)}，当前可卖 ${quantity(selected.sellable)} 股／份。` :
    (selling ? '点击持仓表中的“模拟卖出”可填入证券。' : '买入须通过当日计划、账户风险和盘口核验。');
  $('paper-fill-sellable').hidden = !fillable;
  $('paper-fill-sellable').disabled = confirming;
  $('paper-preview-button').disabled = Boolean(blocker) || previewing || confirming;
  $('paper-preview-button').textContent = previewing ? '正在核对盘口…' : '预览模拟成交';
  $('paper-confirm-button').disabled = !pending || Boolean(blocker) || confirming;
  $('paper-confirm-button').textContent = confirming ? '正在确认…' : '确认模拟成交';
  const options = selling ? snapshot?.positions || [] : snapshot?.universe || [];
  const datalist = $('paper-securities');
  const key = options.map(row => `${row.code}|${row.name}`).join(',');
  if (datalist.dataset.options !== key) {
    datalist.replaceChildren(...options.map(row => {
      const option = document.createElement('option');
      option.value = row.code;
      option.label = row.name;
      return option;
    }));
    datalist.dataset.options = key;
  }
}

function orderStatus(message, type = '') {
  const node = $('paper-order-status');
  node.textContent = message;
  node.className = 'operation-status' + (type ? ' ' + type : '');
  node.hidden = !message;
}

function clearPreview() {
  pending = null;
  $('paper-preview').hidden = true;
  $('paper-confirm-button').disabled = true;
}

function editOrder() {
  formVersion++;
  clearPreview();
  if (!confirming) orderStatus('');
  updateOrderState();
}

function prepareSale(position) {
  if (confirming) return;
  fields.side.value = 'sell';
  if (fields.code.value.trim() !== position.code) fields.reason.value = '';
  fields.code.value = position.code;
  fields.quantity.value = '';
  editOrder();
  $('paper-operation').scrollIntoView({block: 'start'});
  fields.quantity.focus({preventScroll: true});
}

function render(d) {
  const prior = snapshot;
  snapshot = d;
  if (pending && !confirming) {
    const oldPosition = prior?.positions.find(position => position.code === pending.trade.code);
    const newPosition = selectedPosition();
    const blocker = orderBlocker();
    if (blocker || prior?.trade_count !== d.trade_count || prior?.cash !== d.cash ||
        oldPosition?.quantity !== newPosition?.quantity || oldPosition?.sellable !== newPosition?.sellable) {
      clearPreview();
      orderStatus(blocker || '模拟账本已更新，请重新预览。', 'error');
    }
  }
  const daily = d.daily || {};
  $('period').textContent = d.start + ' — ' + d.end + ' · 北京时间';
  $('equity').textContent = cash(d.equity);
  $('source').textContent = d.source + ' · ' + time(d.asof);
  $('daily-label').textContent = (daily.date ? daily.date.slice(5) + ' ' : '') + '当日盈亏';
  $('daily-pnl').textContent = profit(daily.pnl);
  $('daily-pnl').className = tone(daily.pnl);
  $('daily-basis').textContent = daily.pnl == null ? (daily.message || '等待读取收盘基准') :
    (daily.return_pct > 0 ? '+' : '') + daily.return_pct.toFixed(2) + '% · 基准 ' + daily.basis_asof.slice(5,10) + ' ' + daily.basis_label;
  $('pnl').textContent = profit(d.net_pnl);
  $('pnl').className = tone(d.net_pnl);
  $('return').textContent = '比赛收益率 ' + Number(d.return_pct).toFixed(2) + '% · 起点 ' + cash(d.initial_equity);
  $('cash').textContent = cash(d.cash);
  $('exposure').textContent = d.exposure_pct == null ? '持仓估值待核验' : '持仓仓位 ' + d.exposure_pct.toFixed(2) + '%';
  $('trade-count').textContent = d.trade_count + ' 笔';
  $('drawdown').textContent = '最大观察回撤 ' + cash(d.maximum_drawdown);
  $('holding-count').textContent = d.positions.length + ' 只持仓';
  $('status').className = 'status' + (d.stale || d.paused ? ' warn' : '');
  $('status').textContent = d.label + ' · 来源时间 ' + time(d.asof) +
    (d.paused ? '。已暂停新增，继续管理旧仓风险。' : '。') +
    (d.trade_count ? '' : ' 尚未发生虚拟买卖，当前为承接开赛持仓的估值。');
  const focusedCode = document.activeElement?.dataset.paperSell;
  let focusTarget;
  const holdings = d.positions.map(position => {
    const tr = document.createElement('tr');
    const sell = document.createElement('button');
    sell.type = 'button';
    sell.textContent = '模拟卖出';
    sell.className = 'holding-action';
    sell.dataset.paperSell = position.code;
    sell.setAttribute('aria-label', '准备模拟卖出 ' + position.name);
    sell.addEventListener('click', () => prepareSale(position));
    if (focusedCode === position.code) focusTarget = sell;
    const action = cell('');
    action.append(sell);
    const today = cell(profit(position.daily_pnl), position.previous_close != null ? '昨收 ' + price(position.previous_close) : null);
    today.className = tone(position.daily_pnl);
    const contest = cell(profit(position.contest_pnl));
    contest.className = tone(position.contest_pnl);
    tr.append(cell(position.name, position.code + ' · ' + (position.opening_inventory ? '承接开赛持仓' : '虚拟新买')),
      cell(quantity(position.quantity) + ' / ' + quantity(position.sellable)), cell(price(position.cost)),
      cell(price(position.price) + ' / ' + cash(position.value), time(position.source_timestamp)),
      today, contest, cell(price(position.stop_loss), position.stop_loss == null ? '固定退出条件待核验' : null), action);
    return tr;
  });
  table('holdings', holdings, '暂无模拟持仓', 8);
  focusTarget?.focus({preventScroll: true});
  table('trades', [...d.trades].reverse().map(trade => {
    const tr = document.createElement('tr');
    const reason = cell(trade.reason);
    reason.className = 'reason';
    tr.append(cell(time(trade.at)), cell(trade.name, trade.code), cell(sideName(trade.side) + ' / ' + quantity(trade.quantity)),
      cell(price(trade.price)), cell(cash(trade.fee.total) + ' / ' + cash(trade.slippage_estimate)), reason);
    return tr;
  }), '尚未发生虚拟成交');
  $('costs').textContent = d.fees.note;
  $('opening').textContent = '开赛前券商总盈亏 ' + cash(d.opening_broker_total_pnl) + ' 单独保留，不计入本次比赛收益。';
  const feed = d.refresh || {};
  $('refresh-status').textContent = (feed.status === 'error' ? '最近取价失败：' + feed.message + '；保留上次有效估值。' : d.phase_message) +
    ' · 最近取价成功 ' + time(feed.last_success_at) + ' · 页面读取 ' + time(new Date().toISOString());
  updateOrderState();
}

async function refresh(pullQuotes = false, afterTrade = false) {
  if (refreshing) {
    if (!afterTrade) return refreshing;
    await refreshing;
  }
  const version = snapshotVersion;
  $('refresh').disabled = true;
  refreshing = (async () => {
    try {
      const path = '/api/paper-contest' + (pullQuotes ? '/refresh' : '');
      const data = await api(path, pullQuotes ? {} : undefined);
      if (version === snapshotVersion) render(data.snapshot || data);
    } catch (error) {
      if (version !== snapshotVersion) return;
      $('status').className = 'status warn';
      $('status').textContent = `模拟数据读取失败：${error.message}。页面上的金额为上次记录。`;
      snapshot = null;
      clearPreview();
      updateOrderState();
    }
  })();
  try {
    await refreshing;
  } finally {
    refreshing = null;
    $('refresh').disabled = false;
  }
}

function updateExpiry() {
  if (!pending || confirming) return;
  const remaining = Math.ceil((pending.deadline - performance.now()) / 1000);
  if (remaining <= 0) {
    const restoreFocus = document.activeElement === $('paper-confirm-button');
    clearPreview();
    orderStatus('预览已过期，请重新预览。', 'error');
    if (restoreFocus) $('paper-preview-button').focus({preventScroll: true});
    return;
  }
  $('paper-preview-expiry').textContent = `剩余 ${remaining} 秒`;
}

function drawPreview(item, trade) {
  const values = [
    ['操作', `${sideName(trade.side)} ${item.name || ''} ${trade.code} · ${quantity(item.quantity ?? trade.quantity)} 股／份`],
    ['估计成交价', price(item.price)],
    ['估计手续费', cash(item.fee_total)],
    ['可用现金', `${cash(item.cash_before)} → ${cash(item.cash_after)}`],
    ['证券持仓', `${quantity(item.position_before)} → ${quantity(item.position_after)} 股／份`],
    ['报价时间', time(item.source_asof)],
  ];
  if (item.cash_delta != null) values.push(['本次现金变动', profit(item.cash_delta)]);
  if (trade.side === 'sell' && item.sellable_after != null) {
    values.push(['成交后可卖', quantity(item.sellable_after) + ' 股／份']);
  }
  if (item.slippage_estimate != null) values.push(['价格已含滑点', cash(item.slippage_estimate)]);
  if (item.max_quantity != null) values.push(['本次可复核上限', quantity(item.max_quantity) + ' 股／份']);
  const details = values.map(([label, value]) => {
    const group = document.createElement('div');
    const term = document.createElement('dt');
    const detail = document.createElement('dd');
    term.textContent = label;
    detail.textContent = value;
    group.append(term, detail);
    return group;
  });
  $('paper-preview-details').replaceChildren(...details);
  $('paper-preview-note').textContent = '确认时重新核验盘口和账本，价格或成交条件发生不利变化时需重新预览。';
  $('paper-preview').hidden = false;
  updateExpiry();
  updateOrderState();
  if (pending) $('paper-preview-title').focus();
}

form.addEventListener('input', editOrder);
form.addEventListener('change', editOrder);
$('paper-fill-sellable').addEventListener('click', () => {
  const fillable = fillableSellQuantity(selectedPosition());
  if (!fillable) return;
  fields.quantity.value = fillable;
  editOrder();
  fields.reason.focus();
});

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (orderBlocker() || previewing || confirming) return;
  clearPreview();
  const trade = {code: fields.code.value.trim(), side: fields.side.value,
    quantity: Number(fields.quantity.value), reason: fields.reason.value.trim()};
  if (!trade.reason) {
    orderStatus('请填写操作依据。', 'error');
    return;
  }
  const version = formVersion;
  const started = performance.now();
  previewing = true;
  updateOrderState();
  orderStatus('');
  try {
    const data = await api('/api/paper-contest/trade/preview', trade);
    if (version !== formVersion) return;
    if (!data.token || !data.preview || !Number.isFinite(data.expires_in) || data.expires_in <= 0) throw Error('成交预览数据不完整，请重试');
    pending = {token: data.token, trade, deadline: started + data.expires_in * 1000};
    drawPreview(data.preview, trade);
  } catch (error) {
    if (version !== formVersion) return;
    const maximum = error.details?.max_quantity;
    const sellable = error.details?.sellable_before;
    const detail = Number.isInteger(maximum) ? `。本次可复核上限 ${quantity(maximum)} 股／份。` :
      (trade.side === 'sell' && Number.isInteger(sellable) ? `。账本可卖 ${quantity(sellable)} 股／份。` : '');
    orderStatus(error.message.replace(/。$/, '') + detail, 'error');
  } finally {
    previewing = false;
    updateOrderState();
  }
});

$('paper-confirm-button').addEventListener('click', async () => {
  updateExpiry();
  if (!pending || orderBlocker() || confirming) return;
  const {token, trade} = pending;
  const version = formVersion;
  confirming = true;
  updateOrderState();
  orderStatus('正在用新盘口核对本次模拟操作…');
  try {
    const result = await api('/api/paper-contest/trade/confirm', {token, trade});
    snapshotVersion++;
    clearPreview();
    if (version === formVersion) {
      fields.quantity.value = '';
      fields.reason.value = '';
    }
    if (result.snapshot) render(result.snapshot);
    const done = result.trade;
    const message = result.message || '模拟成交已记入本次比赛账本';
    orderStatus(done ? `${message}。${done.name || trade.code} ${sideName(trade.side)} ${quantity(done.quantity)} 股／份，估计成交价 ${price(done.price)}，费用 ${cash(done.fee?.total ?? done.fee_total)}。` : message, 'success');
    if (!result.snapshot) await refresh(false, true);
  } catch (error) {
    clearPreview();
    orderStatus(`${error.message}。请核对成交记录后重新预览。`, 'error');
    await refresh(false, true);
  } finally {
    confirming = false;
    updateOrderState();
  }
});

$('refresh').addEventListener('click', () => refresh(true));
refresh();
setInterval(() => refresh(), 15000);
setInterval(updateExpiry, 1000);
