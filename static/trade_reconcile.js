const byId = id => document.getElementById(id);
const form = byId('reconcile-form');
const fields = form.elements;
let token = document.querySelector('meta[name="csrf-token"]').content;
let pending = null;
let editVersion = 0;
const holdings = new Map();
const alertId = new URLSearchParams(location.search).get('alert_id');
const money = value => typeof value === 'number' ? '¥' + value.toFixed(2) : '—';
const sideNames = { buy: '买入', sell: '卖出', deposit: '资金转入', withdraw: '资金转出' };

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text ?? '—';
  return node;
}

function status(text, warning = false) {
  byId('status').textContent = text;
  byId('status').className = 'status' + (warning ? ' warn' : '');
}

async function api(path, payload, retry = true) {
  const options = { cache: 'no-store', credentials: 'same-origin' };
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json', 'X-CSRF-Token': token };
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (retry && response.status === 403 && data.code === 'csrf_expired') {
    token = (await api('/api/session')).csrf_token;
    return api(path, payload, false);
  }
  if (!response.ok) throw new Error(data.error || '读取失败');
  return data;
}

function addHolding(code, label, quantity) {
  byId('holdings-wrap').hidden = false;
  const row = holdings.get(code);
  if (row) {
    row.expected.textContent = quantity;
    return;
  }
  const tr = element('tr');
  const expected = element('td', quantity);
  function input(name) {
    const field = element('input');
    field.type = 'number';
    field.min = '0';
    field.step = '1';
    field.setAttribute('aria-label', code + ' ' + name);
    const td = element('td');
    td.append(field);
    tr.append(td);
    return field;
  }
  tr.append(element('td', label || code), expected);
  const holding = { expected, quantity: input('券商持仓'), available: input('券商可卖') };
  holdings.set(code, holding);
  byId('broker-positions').append(tr);
}

function renderRows(target, events) {
  target.replaceChildren();
  if (!events.length) {
    target.append(element('p', '暂无流水。'));
    return;
  }
  const headers = ['北京时间', '证券／流水编号', '方向', '数量', '成交价', '实际费用', '资金变动', '卖出净盈亏估算', '净 R 估算', '不利价差'];
  const table = element('table');
  const header = element('tr');
  header.append(...headers.map(title => element('th', title)));
  const thead = element('thead');
  thead.append(header);
  const tbody = element('tbody');
  for (const event of events) {
    const line = element('tr');
    line.append(...[
      event.executed_at?.slice(0, 19).replace('T', ' '),
      [event.code, event.fill_id].filter(Boolean).join(' · ') || '—',
      sideNames[event.side], event.quantity, event.price, money(event.fee),
      money(event.net_cash_change ?? event.amount), money(event.net_pnl_estimate),
      event.net_r_estimate, money(event.adverse_slippage),
    ].map(value => element('td', value)));
    tbody.append(line);
  }
  table.append(thead, tbody);
  const wrap = element('div');
  wrap.className = 'table-wrap';
  wrap.tabIndex = 0;
  wrap.setAttribute('aria-label', '成交与资金变动明细');
  wrap.append(table);
  target.append(wrap);
}

async function refresh(message) {
  const data = await api('/api/trade-reconcile');
  byId('broker-positions').replaceChildren();
  holdings.clear();
  for (const row of data.positions) {
    if (row.quantity > 0) addHolding(row.code, row.code + ' ' + row.name, row.quantity);
  }
  byId('holdings-wrap').hidden = holdings.size === 0;
  const history = [...data.trades, ...data.flows].sort((a, b) => b.executed_at.localeCompare(a.executed_at));
  renderRows(byId('history'), history);
  const pnl = data.period_pnl;
  byId('account-change').hidden = !pnl;
  if (pnl) byId('account-change').textContent = '自上次台账核对调流后盈亏 ' + money(pnl.amount) +
    ' · 期间净转入 ' + money(pnl.net_external_flow) + ' · 起点 ' + pnl.from.slice(0, 19).replace('T', ' ');
  const checked = [data.last_checked, data.last_snapshot].filter(Boolean).sort().at(-1);
  status(message || '本地可用现金 ' + money(data.cash) +
    (checked ? ' · 最近台账核对 ' + checked.slice(0, 19).replace('T', ' ') : ''));
  form.hidden = false;
}

function updateMode() {
  const csv = fields.mode.value === 'csv';
  const trade = ['buy', 'sell'].includes(fields.side.value);
  byId('manual').hidden = byId('manual').disabled = csv;
  byId('csv').hidden = byId('csv').disabled = !csv;
  byId('trade-fields').hidden = byId('trade-fields').disabled = !trade;
  byId('flow-field').hidden = fields.amount.disabled = trade;
}

function invalidate() {
  pending = null;
  byId('confirm').hidden = true;
}

function collectBalances() {
  const positions = {};
  for (const [code, row] of holdings) {
    if (row.quantity.value === '' && row.available.value === '') continue;
    positions[code] = { quantity: row.quantity.value, available_quantity: row.available.value };
  }
  return { asof: fields.asof.value, available_cash: fields.available_cash.value,
    withdrawable_cash: fields.withdrawable_cash.value, total_assets: fields.total_assets.value, positions };
}

form.addEventListener('input', invalidate);
form.addEventListener('input', () => { editVersion += 1; });
form.addEventListener('change', updateMode);
form.addEventListener('submit', async event => {
  event.preventDefault();
  invalidate();
  byId('preview').hidden = true;
  const button = byId('preview-button');
  const version = editVersion;
  button.disabled = true;
  try {
    const data = { broker: collectBalances() };
    if (fields.mode.value === 'csv') {
      data.csv = await fields.file.files[0].text();
    } else {
      const row = {};
      const keys = ['executed_at', 'side', 'fill_id', 'code', 'quantity', 'price', 'fee', 'name', 'type',
        'reference_price', 'planned_risk', 'amount'];
      for (const key of keys) row[key] = fields[key].value;
      if (alertId) row.alert_id = alertId;
      data.events = [row];
    }
    const result = await api('/api/trade-reconcile/preview', data);
    if (version !== editVersion) {
      status('表单在核对期间已修改，请重新预览。', true);
      return;
    }
    for (const row of result.positions) addHolding(row.code, row.code, row.quantity);
    byId('preview-summary').textContent = '台账可用现金 ' + money(result.cash_before) +
      ' → ' + money(result.cash_after) + ' · ' + result.events.length + ' 条新增 · ' +
      result.duplicates.length + ' 条已计入或重复';
    const differences = byId('differences');
    differences.replaceChildren(...result.differences.map(text => element('li', text)));
    differences.hidden = !result.differences.length;
    renderRows(byId('preview-rows'), result.events);
    byId('preview').hidden = false;
    if (result.token) pending = { token: result.token, data };
    byId('confirm').hidden = !pending;
    status(result.differences.length ? '请核对差异后重新预览。' : '预览完成。', !!result.differences.length);
  } catch (error) {
    status(error.message, true);
  } finally {
    button.disabled = false;
  }
});

byId('confirm').addEventListener('click', async () => {
  if (!pending) return;
  const button = byId('confirm');
  button.disabled = true;
  try {
    const result = await api('/api/trade-reconcile/confirm', pending);
    invalidate();
    byId('preview').hidden = true;
    for (const key of ['fill_id', 'price', 'quantity', 'fee', 'amount', 'reference_price', 'planned_risk']) fields[key].value = '';
    fields.file.value = '';
    await refresh(result.message);
  } catch (error) {
    status(error.message, true);
  } finally {
    button.disabled = false;
  }
});

const clock = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
}).format(new Date()).replace(' ', 'T');
fields.asof.value = clock;
fields.executed_at.value = clock.slice(0, 10) + 'T';
updateMode();
refresh().catch(error => status(error.message, true));
if (alertId) {
  api('/api/alert-journal').then(data => {
    const alert = data.rows.find(row => row.id === alertId);
    if (!alert || !alert.side) throw new Error('该提醒暂不可关联，请从提醒记录打开对应流水。');
    fields.code.value = alert.code;
    fields.side.value = alert.side;
    byId('alert-context').textContent = '关联提醒 · ' + alert.name + ' · ' + alert.kind;
    byId('alert-context').hidden = false;
    updateMode();
  }).catch(error => status(error.message, true));
}
