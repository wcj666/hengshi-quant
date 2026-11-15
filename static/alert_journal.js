const byId = id => document.getElementById(id);
const prices = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 });
const dates = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
});
const price = value => Number.isFinite(value) ? prices.format(value) : '—';
const time = value => {
  const moment = value && new Date(value);
  return moment && Number.isFinite(moment.getTime()) ? dates.format(moment) : '—';
};
const labels = {
  pending: '待处理', expired: '已失效', filled: '已成交', partial: '部分成交',
  linked: '已有对账成交', blocked: '条件受阻',
};
const decisionLabels = {
  open_above_cap: '高开超过成交上限',
  entry_outside_range: '模拟价格超出区间',
  entry_price_limit: '触及涨跌停限制',
  risk_blocked: '风控条件未通过',
  minute_gap: '分钟数据缺失',
  missing_execution_session: '整日成交数据缺失',
  point_in_time_review_missing: '缺信号日复核',
  t_plus_one: 'T+1 可卖限制',
  exit_price_limit: '触及退出价格限制',
};
let records = [];

function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}

function stack(cell, main, rest) {
  cell.append(element('div', main));
  for (const line of rest.filter(Boolean)) cell.append(element('div', line, 'subtle'));
}

function replay(cell, evidence) {
  if (evidence.status === 'match') {
    const delta = evidence.alert_minus_simulated_seconds;
    const difference = Number.isFinite(delta)
      ? delta === 0 ? '与模拟触发同秒' : `比模拟触发${delta > 0 ? '晚' : '早'} ${Math.abs(delta)} 秒`
      : null;
    stack(cell, `模拟触发 ${time(evidence.signal_at)}`, [
      `模拟成交 ${time(evidence.fill_at)} · ¥${price(evidence.price)}`,
      difference, '回放使用分钟数据和虚拟账户。',
    ]);
  } else if (evidence.status === 'no_match' || evidence.status === 'incomplete') {
    stack(cell, evidence.status === 'incomplete' ? '回放证据不完整' : '无匹配模拟成交',
      (evidence.decisions ?? []).map(reason => decisionLabels[reason] ?? reason));
  } else {
    cell.textContent = {
      unsupported: '此类提醒无分钟回放映射',
      missing: '缺此日期的真实回放',
      ambiguous: '存在多笔模拟成交，待人工核对',
    }[evidence.status] ?? '待核验';
  }
}

function renderDay() {
  const day = byId('day').value;
  const rows = records.filter(row => row.day === day).map(row => {
    const tr = element('tr');
    const cells = Array.from({length: 7}, () => element('td', null, 'wrap-cell'));
    stack(cells[0], row.kind, [`${row.name} · ${row.code === 'PORT' ? '组合' : row.code}`]);
    cells[1].textContent = time(row.at);
    stack(cells[2], row.quote_at ? time(row.quote_at) : '—', [
      row.price == null ? null : `观察价 ¥${price(row.price)}`,
      row.level == null ? null : `参考位 ¥${price(row.level)}`,
      row.source,
    ]);
    stack(cells[3], row.reason || '—', row.blockers ?? []);
    const state = labels[row.status] ?? '待核验';
    stack(cells[4], state + (row.status === 'partial' && row.expired ? ' · 剩余已失效' : ''), [
      row.quantity == null ? null : `累计对账 ${row.filled_quantity} / ${row.quantity} 股或份`,
      ...(row.fills ?? []).map(fill => `${time(fill.executed_at)} · ${fill.quantity} 股或份 · ¥${price(fill.price)}`),
    ]);
    if (row.side && row.status !== 'filled' && /^[0-9a-f]{20}$/.test(row.id)) {
      const link = element('a', `${row.side === 'buy' ? '买入' : '卖出'}成交对账`, 'subtle');
      link.href = '/trade-reconcile?alert_id=' + encodeURIComponent(row.id);
      cells[4].append(link);
    }
    const notice = row.notification;
    cells[5].textContent = notice.submitted ? '已提交系统' :
      ['sending', 'unknown'].includes(notice.outcome) ? '提交结果待核验' :
      notice.attempts ? `提交失败 · 已尝试 ${notice.attempts} 次` : '网页记录';
    replay(cells[6], row.replay ?? {});
    tr.append(...cells);
    return tr;
  });
  byId('rows').replaceChildren(...rows);
}

async function refresh() {
  const button = byId('refresh');
  const status = byId('status');
  button.disabled = true;
  status.className = 'status';
  try {
    const response = await fetch('/api/alert-journal', { cache: 'no-store' });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '提醒记录读取失败。');
    if (!Array.isArray(payload.rows)) throw new Error('提醒账本格式无效。');
    records = payload.rows;
    const day = byId('day');
    const selected = day.value;
    const days = [...new Set(records.map(row => row.day))];
    day.replaceChildren(...days.map(value => {
      const option = element('option', value);
      option.value = value;
      return option;
    }));
    if (days.includes(selected)) day.value = selected;
    byId('report').hidden = !days.length;
    status.textContent = days.length ? '时间按北京时间显示。' : '暂无新提醒。下一次有效盘中记录会显示在这里。';
    renderDay();
  } catch (error) {
    byId('report').hidden = true;
    status.className = 'status warn';
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

byId('day').addEventListener('change', renderDay);
byId('refresh').addEventListener('click', refresh);
refresh();
