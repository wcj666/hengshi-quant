(() => {
  'use strict';

  const ENDPOINT = '/api/reduction-preview';
  const byId = name => document.getElementById('reduction-preview-' + name);
  const num = (value, digits = 0) => Number.isFinite(value) ? value.toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }) : '—';
  const money = value => Number.isFinite(value) ? '¥' + num(value, 2) : '—';
  const pct = value => Number.isFinite(value) ? num(value, 2) + '%' : '—';
  const when = value => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', hour12: false,
  }) : '';
  const make = (tag, value = '', style = '') => {
    const item = document.createElement(tag);
    item.textContent = value;
    item.className = style;
    return item;
  };

  const draft = new Map();
  const tableRows = new Map();
  let current = null;
  let deadline = null;
  let timer = null;
  let revision = 0;
  let inFlight = 0;

  function cell(row) {
    const first = make('span');
    const note = make('small');
    const item = make('td');
    item.append(first, note);
    row.append(item);
    return { first, note };
  }

  function textCell(cell, text, extra = '') {
    cell.first.textContent = text;
    cell.note.textContent = extra;
    cell.note.hidden = !extra;
  }

  function createRow(code) {
    const row = make('tr');
    const pick = make('input');
    pick.type = 'checkbox';
    const checkCell = make('td');
    checkCell.append(pick);
    row.append(checkCell);
    const name = cell(row);
    const reference = cell(row);
    const available = cell(row);
    const amountCell = make('td');
    const field = make('input', '', 'reduction-quantity');
    field.type = 'number';
    field.inputMode = 'numeric';
    field.min = '1';
    field.step = '1';
    field.placeholder = '—';
    const hint = make('small');
    amountCell.append(field, hint);
    row.append(amountCell);
    const remaining = cell(row);
    const proceeds = cell(row);

    pick.addEventListener('change', () => {
      if (pick.checked) {
        const position = current?.positions.find(item => item.code === code);
        const suggested = current?.suggested?.quantities?.[code];
        const firstLot = Math.min(position?.max_quantity || 0, position?.lot || 100);
        draft.set(code, String(suggested || firstLot || ''));
      } else {
        draft.delete(code);
      }
      renderPick({ pick, field }, code);
      if (pick.checked) field.focus();
      changedDraft();
    });
    field.addEventListener('input', () => {
      if (!pick.checked) return;
      draft.set(code, field.value);
      changedDraft();
    });

    const entry = { row, pick, field, hint, name, reference, available, remaining, proceeds };
    tableRows.set(code, entry);
    return entry;
  }

  function renderPick(entry, code) {
    entry.pick.checked = draft.has(code);
    entry.field.disabled = !entry.pick.checked || !entry.field.max || Number(entry.field.max) <= 0;
    const amount = draft.get(code) || '';
    if (entry.field.value !== amount) entry.field.value = amount;
  }

  function clearEstimates(message, statusLevel = '') {
    const status = byId('status');
    if (status.textContent !== message) status.textContent = message;
    status.dataset.level = statusLevel;
    byId('notes').replaceChildren();
    byId('metrics').hidden = true;
    byId('metrics').replaceChildren();
    for (const entry of tableRows.values()) {
      textCell(entry.remaining, '—');
      textCell(entry.proceeds, '—');
    }
  }

  function renderRows(response) {
    const body = byId('positions');
    const held = new Set(response.positions.map(item => item.code));
    for (const [code, entry] of tableRows) {
      if (!held.has(code)) {
        entry.row.remove();
        tableRows.delete(code);
      }
    }
    body.querySelector('.reduction-empty')?.remove();
    if (!held.size) {
      const row = make('tr', '', 'reduction-empty');
      const item = make('td', '暂无持仓记录');
      item.colSpan = 7;
      row.append(item);
      body.append(row);
    }

    // Keep inputs mounted across polls so an in-progress edit retains focus.
    let index = 0;
    for (const position of response.positions) {
      const code = position.code;
      const entry = tableRows.get(code) || createRow(code);
      const at = body.children[index++];
      if (at !== entry.row) body.insertBefore(entry.row, at || null);
      textCell(entry.name, position.name || code, code + ' · 持有 ' + num(position.quantity));
      const reference = Number.isFinite(position.price) ? '¥' + num(position.price, 3) : '—';
      textCell(entry.reference, reference, Number.isFinite(position.weight_pct)
        ? '仓位 ' + pct(position.weight_pct) + ' · ' + money(position.value) : '');
      const availableAt = when(position.available_asof);
      const historical = Number.isFinite(position.archive_available_quantity)
        ? '历史记录 ' + num(position.archive_available_quantity) : '';
      textCell(entry.available, position.available_quantity == null ? '待核对' : num(position.available_quantity),
        [position.eligibility, availableAt, historical].filter(Boolean).join(' · '));

      const suggested = response.suggested?.quantities?.[code];
      const canSelect = response.mode !== 'unavailable' && position.max_quantity > 0;
      entry.pick.disabled = !canSelect && !draft.has(code);
      entry.pick.setAttribute('aria-label', '纳入组合减仓：' + (position.name || code));
      entry.field.setAttribute('aria-label', (position.name || code) + '拟卖股数或份额');
      entry.field.max = canSelect ? String(position.max_quantity) : '0';
      entry.hint.textContent = suggested ? '规则草案 ' + num(suggested) + ' · ' +
        (response.suggested.reasons?.[code] || '') : canSelect ? '按 ' + (position.lot || 100) + ' 股/份为单位' : '';
      renderPick(entry, code);
    }
  }

  function metric(label, value, note = '') {
    const node = make('div');
    node.append(make('dt', label), make('dd', value), make('small', note));
    return node;
  }

  function noteList(title, items, style) {
    const result = make('div', '', style);
    const list = make('ul');
    list.append(...items.map(item => make('li', item)));
    result.append(make('strong', title), list);
    return result;
  }

  function render(response, localIssue, message = '') {
    renderRows(response);
    const { preview } = response;
    const estimates = response.mode !== 'unavailable' && preview?.valid && !localIssue;
    const statusLevel = estimates && !preview?.unverified?.length && response.mode === 'live' ? 'ready' : 'watch';
    const execution = estimates && draft.size ? preview?.execution_status : '';
    clearEstimates([response.status, execution].filter(Boolean).join(' · '), statusLevel);
    byId('source').textContent = [
      [response.source, when(response.asof)].filter(Boolean).join(' '),
      [response.account_source || '持仓台账', when(response.account_asof)].filter(Boolean).join(' '),
    ].filter(Boolean).join(' · ');
    byId('draft').textContent = [response.suggested?.status, message].filter(Boolean).join(' · ');
    byId('suggest').disabled = response.mode === 'unavailable' ||
      !Object.values(response.suggested?.quantities || {}).some(value => value > 0);
    byId('clear').disabled = !draft.size;

    const notes = byId('notes');
    if (estimates && draft.size) {
      if (!execution?.includes('按假设全部成交测算')) {
        notes.append(make('p', '按假设全部成交测算', 'reduction-scenario'));
      }
      if (preview.unverified?.length) notes.append(noteList('券商待核对', preview.unverified, 'reduction-warning'));
      if (preview.over_limits?.length) notes.append(noteList('卖后仍高于规则上限', preview.over_limits, 'reduction-warning'));
    }
    if (localIssue || preview?.errors?.length) {
      const errors = localIssue ? [localIssue] : preview.errors;
      notes.append(noteList('暂未计算', errors, 'reduction-warning'));
    }
    if (!estimates) return;

    for (const row of preview.rows) {
      const entry = tableRows.get(row.code);
      if (!entry) continue;
      textCell(entry.remaining, num(row.remaining_quantity), '仓位 ' + pct(row.remaining_weight_pct));
      if (row.sell_quantity > 0) textCell(entry.proceeds, money(row.net_proceeds), '费用 ' + money(row.fee));
    }
    const gap = preview.cash_shortfall;
    const metrics = byId('metrics');
    metrics.replaceChildren(
      metric('预计净回款', money(preview.net_proceeds), '费用合计 ' + money(preview.fees)),
      metric('卖后可用现金', money(preview.cash_after), '当前 ' + money(preview.cash_before)),
      metric('卖后权益仓位', pct(preview.exposure_after_pct), '当前 ' + pct(preview.exposure_before_pct)),
      metric('卖后持仓市值', money(preview.market_value_after), '剩余 ' + num(preview.remaining_count) + ' 只'),
      metric('距离现金目标', Number.isFinite(gap) && gap === 0 ? '已达目标' : money(gap),
        '现金目标 ' + money(preview.cash_target) + ' · ' + pct(response.cash_target_pct)),
    );
    if (response.fee_note) notes.append(make('p', response.fee_note, 'tip'));
    metrics.hidden = false;
  }

  function revoke(message) {
    const positions = (current?.positions || []).map(position => ({
      ...position, price:null, weight_pct:null, value:null, available_quantity:null, available_asof:null,
      max_quantity:null, archive_available_quantity:null, eligibility:'等待重新核验',
    }));
    current = null;
    deadline = null;
    render({ positions, mode:'unavailable', status:message }, null);
  }

  async function load(version) {
    const requested = Date.now();
    const present = [...draft];
    const invalid = present.some(([, value]) => !Number.isSafeInteger(Number(value)) || Number(value) <= 0);
    const quantities = Object.fromEntries(present.map(([code, value]) => [code, Number(value)]));
    inFlight++;
    try {
      const response = present.length && !invalid ? await api(ENDPOINT, 'POST', { quantities }) : await api(ENDPOINT);
      if (version !== revision) return;
      current = response;
      const lifetime = response.expires_at ? Date.parse(response.expires_at) -
        Date.parse(response.server_time || response.asof) : 30000;
      deadline = response.mode === 'unavailable' ? null : requested + Math.min(lifetime, 30000);
      if (deadline !== null && (!Number.isFinite(deadline) || Date.now() >= deadline)) {
        revoke('行情核验已过期，等待刷新。');
        return;
      }

      const codes = new Set(response.positions.map(position => position.code));
      const removed = present.filter(([code]) => !codes.has(code));
      if (removed.length && response.mode !== 'unavailable') {
        for (const [code] of removed) draft.delete(code);
        render(response, '持仓已变更，正在复核现有草案。', '已移除不在台账中的持仓');
        schedule();
      } else {
        render(response, invalid ? '请为每只选中的持仓填写正整数数量。' : null);
      }
    } catch (error) {
      if (version === revision) revoke('组合预览读取失败。' + error.message);
    } finally {
      inFlight--;
    }
  }

  function schedule(delay = 0) {
    clearTimeout(timer);
    const version = ++revision;
    timer = setTimeout(() => {
      timer = null;
      load(version);
    }, delay);
  }

  function changedDraft() {
    byId('clear').disabled = !draft.size;
    clearEstimates('正在重新测算拟卖组合。');
    schedule(240);
  }

  function checkExpiry() {
    if (deadline !== null && Date.now() >= deadline) revoke('行情核验已过期，等待刷新。');
  }

  function init() {
    if (!byId('positions')) return;
    byId('refresh').addEventListener('click', () => {
      clearEstimates('正在刷新组合预览。');
      schedule();
    });
    byId('clear').addEventListener('click', () => {
      draft.clear();
      for (const [code, entry] of tableRows) renderPick(entry, code);
      changedDraft();
    });
    byId('suggest').addEventListener('click', () => {
      const suggested = current?.suggested?.quantities;
      if (!suggested) return;
      draft.clear();
      for (const [code, value] of Object.entries(suggested)) {
        if (value > 0) draft.set(code, String(value));
      }
      for (const [code, entry] of tableRows) renderPick(entry, code);
      changedDraft();
    });
    setInterval(() => {
      if (!document.hidden && !inFlight && timer === null) schedule();
    }, 10000);
    setInterval(checkExpiry, 1000);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) return;
      checkExpiry();
      if (!inFlight && timer === null) schedule();
    });
    schedule();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
