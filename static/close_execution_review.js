(() => {
  'use strict';

  const endpoint = '/api/close-execution-review';
  const byId = id => document.getElementById(id);
  const dateField = byId('execution-review-date');
  const historyField = byId('execution-review-history');
  const alertStates = [
    ['filled', '已成交'], ['partial', '部分成交'], ['missed', '错过'], ['skipped', '主动跳过'],
    ['expired', '已失效'], ['needs_reconcile', '待对账'], ['pending', '待确认'], ['observe', '观察记录'],
  ];
  const stateLabels = Object.fromEntries(alertStates);
  const stateClasses = {filled:'good', partial:'good', missed:'warn', needs_reconcile:'warn', pending:'warn'};
  const comparisonLabels = {
    quote_missing_or_stale:'缺少本日有效收盘价',
    sell_fee_unknown:'卖出实付费用待补',
  };
  const clock = new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai', hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
  const numeric = new Intl.NumberFormat('zh-CN');
  const prices = new Intl.NumberFormat('zh-CN', {minimumFractionDigits:3, maximumFractionDigits:4});
  const amounts = new Intl.NumberFormat('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});
  let selectedDay = new URLSearchParams(location.search).get('day') || '';
  let csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  let loadedDay = '';
  let requestVersion = 0;

  const number = value => Number.isFinite(value) ? numeric.format(value) : '—';
  const price = value => Number.isFinite(value) ? prices.format(value) : '—';
  const amount = (value, signed = false) => {
    if (!Number.isFinite(value)) return '—';
    const sign = value < 0 ? '−' : value > 0 && signed ? '+' : '';
    return sign + '¥' + amounts.format(Math.abs(value));
  };
  const clockTime = value => Number.isFinite(Date.parse(value)) ? clock.format(new Date(value)) : '—';
  const pnlColor = value => value > 0 ? 'gain' : value < 0 ? 'loss' : '';

  function el(tag, text = '', cls = '') {
    const node = document.createElement(tag);
    node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }

  function td(content, details = [], cls = '') {
    const cell = el('td', '', cls);
    cell.append(content instanceof Node ? content : document.createTextNode(content));
    for (const detail of details.filter(Boolean)) cell.append(el('small', detail));
    return cell;
  }

  function tr(...cells) {
    const row = el('tr');
    row.append(...cells);
    return row;
  }

  function table(id, items, empty, makeRow) {
    const body = byId(id);
    const placeholder = el('td', empty, 'empty');
    placeholder.colSpan = 5;
    body.replaceChildren(...(items.length ? items.map(makeRow) : [tr(placeholder)]));
  }

  function feedback(message) {
    const node = byId('execution-review-feedback');
    node.textContent = message;
    node.hidden = !message;
  }

  async function read(path, options) {
    const response = await fetch(path, {cache:'no-store', ...options});
    let value;
    try { value = await response.json(); }
    catch { throw new Error('服务器未返回复盘数据'); }
    if (!response.ok) {
      const error = new Error(value?.error || '读取或保存失败');
      error.code = value?.code;
      throw error;
    }
    return value;
  }

  async function write(row, outcome, reason) {
    if (!csrf) csrf = (await read('/api/session')).csrf_token;
    const send = () => read(endpoint + '/' + encodeURIComponent(row.id), {
      method:'POST', headers:{'Content-Type':'application/json', 'X-CSRF-Token':csrf},
      body:JSON.stringify({day:row.day, outcome, reason}),
    });
    try {
      return await send();
    } catch (error) {
      if (error.code !== 'csrf_expired') throw error;
      csrf = (await read('/api/session')).csrf_token;
      return send();
    }
  }

  function dispositionForm(row) {
    const details = el('details', '', 'execution-disposition');
    details.append(el('summary', row.manual_review ? '修改复盘记录' : '补记错过或跳过'));
    const form = el('form');
    const outcome = el('select');
    outcome.required = true;
    for (const [key, label] of [['','选择结果'], ['missed','错过'], ['skipped','主动跳过']]) {
      const option = el('option', label);
      option.value = key;
      outcome.append(option);
    }
    outcome.value = row.manual_review?.outcome || '';
    const reason = el('input');
    reason.type = 'text';
    reason.maxLength = 200;
    reason.required = true;
    reason.placeholder = '填写当时没有成交的原因';
    reason.value = row.manual_review?.reason || '';
    const label = (name, field) => {
      const node = el('label');
      node.append(el('span', name), field);
      return node;
    };
    const actions = el('div');
    const save = el('button', '保存结果');
    save.type = 'submit';
    actions.append(save);
    const clear = el('button', '撤销补记');
    clear.type = 'button';
    if (row.manual_review) actions.append(clear);
    const notice = el('p', '', 'execution-form-error');
    notice.setAttribute('role', 'status');
    notice.hidden = true;
    form.append(label('复盘结果', outcome), label('原因', reason), actions, notice);
    details.append(form);

    async function saveDisposition(action, text) {
      save.disabled = true;
      clear.disabled = true;
      notice.hidden = true;
      try {
        await write(row, action, text);
        if (form.isConnected && loadedDay === row.day) {
          feedback(action === 'clear' ? '补记已撤销。' : '复盘结果已保存。');
          await refresh();
        }
      } catch (error) {
        notice.textContent = error.message;
        notice.hidden = false;
      } finally {
        save.disabled = false;
        clear.disabled = false;
      }
    }

    form.addEventListener('submit', event => {
      event.preventDefault();
      const text = reason.value.trim();
      if (!text) {
        notice.textContent = '请填写原因。';
        notice.hidden = false;
        reason.focus();
        return;
      }
      saveDisposition(outcome.value, text);
    });
    clear.addEventListener('click', () => saveDisposition('clear', ''));
    return details;
  }

  function alertRow(row) {
    const {review_status:state} = row;
    const result = el('span', row.review_label || stateLabels[state] || '待确认',
      ['tag', stateClasses[state]].filter(Boolean).join(' '));
    const fills = Array.isArray(row.fills) ? row.fills : [];
    const resultCell = td(result, row.filled_quantity > 0 ? ['实际成交 ' + number(row.filled_quantity) + ' 股/份'] : []);
    if (fills.length) {
      const details = el('details', '', 'execution-fills');
      const list = el('ul');
      const fillLine = fill => [clockTime(fill.executed_at), number(fill.quantity) + ' 股/份',
        '成交价 ' + price(fill.price), Number.isFinite(fill.fee) ? '实付费用 ' + amount(fill.fee) : '费用待补'].join(' · ');
      list.append(...fills.map(fill => el('li', fillLine(fill))));
      details.append(el('summary', '查看 ' + fills.length + ' 笔成交'), list);
      resultCell.append(details);
    }

    const trigger = row.trigger_reason && row.trigger_reason !== row.review_reason ? '触发依据 ' + row.trigger_reason : '';
    const explanation = td(row.review_reason || '等待核对处理结果', [trigger], 'explanation');
    if (row.can_link) {
      const link = el('a', '关联实际成交', 'execution-link');
      link.href = '/trade-reconcile?alert_id=' + encodeURIComponent(row.id);
      explanation.append(link);
    }
    if (row.can_review) explanation.append(dispositionForm(row));
    const levels = [
      Number.isFinite(row.level) ? '触发价 ' + price(row.level) : '',
      Number.isFinite(row.quantity) ? '提醒数量 ' + number(row.quantity) + ' 股/份' : '',
    ];
    return tr(td(clockTime(row.at), [row.notification_label]), td(row.name || row.code, [row.code, row.kind]),
      td(Number.isFinite(row.price) ? '参考价 ' + price(row.price) : '—', levels), resultCell, explanation);
  }

  function comparativePnl(value) {
    const label = Number.isFinite(value) ? value > 0 ? '多赚 ' : value < 0 ? '少赚 ' : '持平 ' : '';
    return el('strong', label + amount(Number.isFinite(value) ? Math.abs(value) : null), pnlColor(value));
  }

  function netHint(record) {
    if (Number.isFinite(record.net_vs_hold)) return '';
    if (record.net_status_label) return record.net_status_label;
    return ({waiting_close:'等待当日收盘', no_samples:'当日未记录做 T',
      incomplete:'部分费用、收盘价或成交记录待核对'})[record.net_status] || '待核对费用或收盘价';
  }

  function tCycle(row) {
    const pairedPrice = Number.isFinite(row.paired_buy_price) ? '接回均价 ' + price(row.paired_buy_price) : '';
    const paired = row.paired_quantity > 0 ? [
      pairedPrice,
      Number.isFinite(row.paired_gross_estimate) ? '毛价差 ' + amount(row.paired_gross_estimate, true) : '',
      Number.isFinite(row.paired_fee_actual) ? '分摊实付费用 ' + amount(row.paired_fee_actual) : '实付费用待补',
      Number.isFinite(row.paired_net_actual) ? '' :
        Number.isFinite(row.paired_net_estimate) ? '配对净额估算 ' + amount(row.paired_net_estimate, true) : '',
    ] : [];
    const pairedValue = row.paired_quantity > 0 ?
      '已接回 ' + number(row.paired_quantity) + ' 股/份 · 配对净额 ' + amount(row.paired_net_actual, true) : '无配对成交';
    const remaining = row.unmatched_quantity > 0 ? [
      Number.isFinite(row.unmatched_price) ? '收盘参考价 ' + price(row.unmatched_price) +
        (row.unmatched_quote_at ? ' · ' + clockTime(row.unmatched_quote_at) : '') : '',
      Number.isFinite(row.unmatched_sell_fee_actual) ? '分摊卖出费用 ' + amount(row.unmatched_sell_fee_actual) : '',
      Number.isFinite(row.unmatched_comparison) ? '收盘差额 ' + amount(row.unmatched_comparison, true) :
        row.comparison_status_label || comparisonLabels[row.comparison_status] || '暂无法计算收盘差额',
    ] : [];
    const unmatched = row.unmatched_quantity > 0 ? number(row.unmatched_quantity) + ' 股/份未接回' : '0 股/份未接回';
    const status = row.status === 'closed' ? '已全部接回' : row.status === 'cash' ? '已记为留现金' : '仍有未接回份额';
    return tr(td(row.name || row.code, [row.code, status]),
      td(number(row.quantity) + ' 股/份', ['卖出均价 ' + price(row.sell_price)]),
      td(el('span', pairedValue, pnlColor(row.paired_net_actual)), paired),
      td(el('span', unmatched, pnlColor(row.unmatched_comparison)), remaining),
      td(comparativePnl(row.net_vs_hold), [netHint(row)]));
  }

  function metric(label, content, note) {
    const node = el('div');
    const value = el('dd');
    value.append(content instanceof Node ? content : document.createTextNode(content));
    if (note) value.append(el('small', note));
    node.append(el('dt', label), value);
    return node;
  }

  function tSummary(data) {
    const summary = data.summary || {};
    const grid = byId('execution-t-summary');
    grid.hidden = !(summary.cycle_count > 0);
    grid.replaceChildren(
      metric('配对实付净额', el('span', amount(summary.paired_net_actual, true), pnlColor(summary.paired_net_actual))),
      metric('费用待补的配对估算', el('span', amount(summary.paired_net_estimated, true), pnlColor(summary.paired_net_estimated))),
      metric('未接回数量合计', number(summary.unmatched_quantity) + ' 股/份'),
      metric('未接回收盘差额', el('span', amount(summary.unmatched_comparison, true), pnlColor(summary.unmatched_comparison)),
        summary.unmatched_unpriced_count > 0 ? number(summary.unmatched_unpriced_count) + ' 轮未计入' : ''),
      metric('相对原仓不动合计', comparativePnl(summary.net_vs_hold), netHint(summary)),
    );
    byId('execution-t-notes').replaceChildren(...(data.notes || []).map(note => el('li', note)));
  }

  function render(data) {
    loadedDay = data.day || '';
    dateField.value = loadedDay;
    const days = (data.available_days || []).filter(day => typeof day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(day));
    const picker = byId('execution-history-picker');
    picker.hidden = !days.length;
    const sorted = [...new Set(days)].sort().reverse();
    historyField.replaceChildren(...['', ...sorted].map(day => {
      const option = el('option', day || '选择有记录的日期');
      option.value = day;
      return option;
    }));
    historyField.value = days.includes(loadedDay) ? loadedDay : '';
    const status = byId('execution-review-status');
    status.classList.remove('warn');
    status.textContent = data.status || (loadedDay ? loadedDay + ' 收盘记录' : '暂无完整交易日的复盘资料');
    const alerts = data.alerts || {};
    const alertRows = alerts.rows || [];
    if (alertRows.length) {
      byId('execution-review-summary').replaceChildren(...alertStates.map(([key, label]) => {
        const count = alerts.summary?.[key] ?? alertRows.filter(row => row.review_status === key).length;
        return el('li', label + ' ' + number(count), ['tag', stateClasses[key]].filter(Boolean).join(' '));
      }));
    }
    table('execution-review-alerts', alertRows, '该日暂无盘中提醒记录', alertRow);
    const t = data.t_review || {};
    tSummary(t);
    table('execution-review-t', t.rows || [], '该日暂无已登记的实际做 T 卖出', tCycle);
  }

  async function refresh() {
    const version = ++requestVersion;
    const button = byId('execution-review-refresh');
    const status = byId('execution-review-status');
    const wait = '正在读取提醒和成交记录…';
    button.disabled = true;
    status.classList.remove('warn');
    status.textContent = wait;
    byId('execution-review-summary').replaceChildren();
    byId('execution-t-summary').hidden = true;
    byId('execution-t-notes').replaceChildren();
    table('execution-review-alerts', [], wait);
    table('execution-review-t', [], wait);
    try {
      const query = selectedDay ? '?day=' + encodeURIComponent(selectedDay) : '';
      const data = await read(endpoint + query);
      if (version === requestVersion) render(data);
    } catch (error) {
      if (version !== requestVersion) return;
      status.classList.add('warn');
      status.textContent = '复盘读取失败 · ' + error.message;
      table('execution-review-alerts', [], '提醒记录暂不可用');
      table('execution-review-t', [], '做 T 对比暂不可用');
    } finally {
      if (version === requestVersion) button.disabled = false;
    }
  }

  function choose(day) {
    selectedDay = day;
    const url = new URL(location.href);
    if (day) url.searchParams.set('day', day);
    else url.searchParams.delete('day');
    history.replaceState(null, '', url);
    feedback('');
    refresh();
  }

  dateField.addEventListener('change', () => choose(dateField.value));
  historyField.addEventListener('change', () => choose(historyField.value));
  byId('execution-review-refresh').addEventListener('click', () => { feedback(''); refresh(); });
  refresh();
})();
