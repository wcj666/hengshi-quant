const byId = id => document.getElementById(id);
const money = value => value == null ? '—' : '¥' + value.toLocaleString('zh-CN', {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const price = value => value == null ? '—' : value.toFixed(3);
const pct = value => value == null ? '—' : value.toFixed(2) + '%';
const quantity = value => value == null ? '待核对' : value.toLocaleString('zh-CN');
const time = value => value ? new Date(value).toLocaleString('zh-CN', {
  timeZone: 'Asia/Shanghai', hour12: false,
}) : '—';

function cell(value, note) {
  const node = document.createElement('td');
  node.textContent = value;
  if (note) {
    const small = document.createElement('small');
    small.textContent = note;
    node.append(small);
  }
  return node;
}

function tableRow(...cells) {
  const row = document.createElement('tr');
  row.append(...cells);
  return row;
}

function explanation(value, note) {
  const description = cell(value, note);
  description.className = 'explanation';
  return description;
}

function rows(id, list, empty, build, columns = 5) {
  const body = byId(id);
  if (list.length) {
    body.replaceChildren(...list.map(build));
    return;
  }
  const message = cell(empty);
  message.colSpan = columns;
  message.className = 'empty';
  body.replaceChildren(tableRow(message));
}

function render(data) {
  const { cash_policy: cash, sector_exposure: sectors, theme_exposure: themes } = data;
  byId('status').className = 'status' + (data.data_verified ? '' : ' warn');
  byId('status').textContent = [data.execution_status, ...data.errors].join(' · ');
  byId('source').textContent = '账户快照 ' + time(data.account_asof) + ' · 收盘行情 ' + time(data.generated_at);
  byId('equity').textContent = money(data.equity);
  byId('exposure').textContent = '估算仓位 ' + pct(data.current_exposure_pct);
  byId('cash').textContent = money(cash.broker_available_cash);
  byId('withdrawable').textContent = '券商可取 ' + money(cash.broker_withdrawable_cash);
  byId('proceeds').textContent = money(cash.estimated_proceeds_sellable);
  byId('after-sales').textContent = '可卖部分成交后预计现金 ' + money(cash.cash_after_planned_sales);
  byId('buy-budget').textContent = '全部成交后预计现金 ' + money(cash.cash_after_all_planned_sales);
  byId('reserve').textContent = money(cash.minimum_reserve);
  byId('count').textContent = '当前持仓 ' + data.position_count + ' 只';

  rows('sells', data.sell_plan, '当前没有符合规则的卖出计划', item => tableRow(
    cell(item.name, [item.code, item.theme || item.sector].filter(Boolean).join(' · ')),
    cell(quantity(item.quantity)),
    cell(price(item.price)),
    cell(money(item.value), '仓位预计减少 ' + item.exposure_reduction_pct.toFixed(2) + ' 个百分点'),
    explanation(item.reason),
  ));
  rows('pending-sells', data.pending_sell_plan, '当前没有待确认可卖计划', item => tableRow(
    cell(item.name, item.code), cell(quantity(item.quantity)), cell(quantity(item.available_now)),
    cell(price(item.price)), explanation(item.reason, item.detail),
  ));

  const groups = [
    ...Object.entries(sectors).map(([name, item]) => ({ ...item, name, group: '行业' })),
    ...Object.entries(themes).map(([name, item]) => ({ ...item, name, group: '手工主题' })),
  ];
  const unknown = data.unclassified_codes;
  const note = [
    '行业上限 ' + pct(data.max_sector_pct),
    'ETF 按手工填写的归组汇总',
  ];
  if (unknown.length) note.push('行业待补 ' + unknown.join('、'));
  byId('concentration-note').textContent = note.join(' · ');
  rows('concentrations', groups, data.data_verified ? '暂无已归组持仓' : '待核验收盘行情', item => tableRow(
    cell(item.name, item.group), cell(item.codes.join('、')), cell(money(item.value)),
    cell(pct(item.pct)), cell(pct(item.pct_after_available)), cell(pct(item.pct_after_all)),
  ), 6);

  rows('buys', data.buy_plan, '暂无次日研究候选', item => tableRow(
    cell(item.name, item.code), cell('待盘中核验'),
    cell(price(item.trigger_price) + ' / ' + price(item.entry_price)),
    cell([item.stop_loss, item.take_profit_1, item.take_profit_2].map(price).join(' / ')),
    cell(item.reason),
  ));
  rows('positions', data.positions, '暂无持仓', item => {
    const daily = cell(pct(item.daily_pct));
    daily.className = item.daily_pct > 0 ? 'gain' : item.daily_pct < 0 ? 'loss' : '';
    const exit = item.fixed_exit;
    const levels = exit.stop_loss == null ? '' : '止损 ' + price(exit.stop_loss)
      + ' · 目标 ' + [exit.take_profit_1, exit.take_profit_2].map(price).join(' / ');
    return tableRow(
      cell(item.name, [item.code, item.sector || '行业待补'].join(' · ')),
      cell(quantity(item.quantity), '可卖 ' + quantity(item.sellable_verified ? item.sellable : null)),
      cell(price(item.price)), daily, cell(money(item.value)), cell(exit.status, levels),
    );
  }, 6);

  byId('notes').replaceChildren(...data.notes.map(note => {
    const item = document.createElement('li');
    item.textContent = note;
    return item;
  }));
}

async function refresh() {
  const button = byId('refresh');
  const results = byId('review-results');
  button.disabled = true;
  results.hidden = true;
  byId('status').className = 'status';
  byId('status').textContent = '正在读取收盘复盘…';
  byId('source').textContent = '—';
  try {
    const response = await fetch('/api/close-review', { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '读取失败');
    render(data);
    results.hidden = false;
  } catch (error) {
    byId('status').className = 'status warn';
    byId('status').textContent = '复盘读取失败 · ' + error.message;
  } finally {
    button.disabled = false;
  }
}

byId('refresh').addEventListener('click', refresh);
refresh();
