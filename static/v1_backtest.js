const byId = id => document.getElementById(id);
const numberFormatter = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const integerFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 });
const priceFormatter = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 3 });
const clockFormatter = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', hourCycle: 'h23', year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit',
});
const money = value => Number.isFinite(value) ? '¥' + numberFormatter.format(value) : '—';
const integer = value => Number.isFinite(value) ? integerFormatter.format(value) : '—';
const price = value => Number.isFinite(value) ? priceFormatter.format(value) : '—';
const percent = value => Number.isFinite(value) ? numberFormatter.format(value) + '%' : '—';
const ratio = value => Number.isFinite(value) ? numberFormatter.format(value) + ' R' : '—';
const tone = value => value > 0 ? 'gain' : value < 0 ? 'loss' : '';
const types = { stock: '股票', etf: 'ETF' };
const reasons = {
  breakout: '突破触发',
  stop_loss: '固定止损',
  take_profit_1: '第一止盈',
  take_profit_2: '第二止盈',
  signal_data_missing: '信号日线缺失',
  historical_universe_missing: '信号日股票池缺失',
  point_in_time_review_missing: '当时的人工核验缺失',
  point_in_time_review_rejected: '当时的人工核验未通过',
  open_above_cap: '当日开盘超限',
  entry_outside_range: '入场价超出触发区间',
  entry_price_limit: '触及买入价格限制',
  slippage_above_cap: '计入滑点后高于成交上限',
  held_quotes_missing: '持仓报价缺失',
  risk_blocked: '风险条件未通过',
  round_lot_cost_or_depth: '整手、成本或成交量条件未通过',
  t_plus_one: 'T+1 可卖限制',
  exit_price_limit: '触及卖出价格限制',
  target_not_executable_at_next_open: '下一分钟开盘未到目标价',
  exit_depth_insufficient: '可卖成交量不足一手',
  missing_execution_session: '整日成交数据缺失',
  corporate_action_unverified: '公司行动未核验',
  suspended: '停牌',
  minute_gap: '分钟数据缺失',
  closing_mark_missing: '日终估值缺失',
  daily_entry_limit: '已达每日开仓上限',
  trigger_not_filled: '有效时段未成交',
};
let curve = [];

function node(tag, value, className = '') {
  const element = document.createElement(tag);
  element.textContent = value ?? '—';
  if (className) element.className = className;
  return element;
}

function reasonLabel(value) {
  return reasons[value] ?? value ?? '—';
}

function clock(row) {
  const time = row.at ? new Date(row.at) : null;
  return time && Number.isFinite(time.getTime()) ? clockFormatter.format(time) : row.date ?? '—';
}

function securities(row) {
  return [row.code, types[row.type]].filter(Boolean).join(' · ') || '—';
}

function renderTable(id, rows, columns, empty) {
  const table = byId(id);
  const elements = rows.map(row => {
    const tr = document.createElement('tr');
    for (const column of row) {
      const [value, className] = Array.isArray(column) ? column : [column];
      tr.append(node('td', value, className));
    }
    return tr;
  });
  if (!elements.length) {
    const tr = document.createElement('tr');
    const cell = node('td', empty, 'empty');
    cell.colSpan = columns;
    tr.append(cell);
    elements.push(tr);
  }
  table.replaceChildren(...elements);
}

function metric(label, value, detail, className = '') {
  const item = document.createElement('div');
  item.append(node('dt', label), node('dd', value, 'metric-value ' + className));
  if (detail) item.append(node('dd', detail, 'metric-detail'));
  return item;
}

function renderMetrics(summary) {
  byId('metrics').replaceChildren(
    metric('期末净值', money(summary.end_equity), '期初 ' + money(summary.initial_cash)),
    metric('净盈亏', money(summary.net_pnl), percent(summary.return_pct) + ' 区间收益率', tone(summary.net_pnl)),
    metric('最大日终回撤', percent(summary.max_drawdown_pct), money(summary.max_drawdown) + ' 最大回撤金额'),
    metric('平均净 R', ratio(summary.avg_net_r), integer(summary.closed_trade_count) + ' 笔已结束交易'),
    metric('成交笔数', integer(summary.fill_count), `买入 ${integer(summary.buy_fill_count)} · 卖出 ${integer(summary.sell_fill_count)}`),
    metric('费用合计', money(summary.fee_total), '滑点估算 ' + money(summary.slippage_total) + ' 已计入成交价'),
    metric('已结束交易胜率', percent(summary.win_rate_pct), `盈利 ${integer(summary.winner_count)} · 亏损 ${integer(summary.loser_count)}`),
    metric('样本交易日', integer(summary.session_count), `${integer(summary.symbol_count)} 只证券 · ${integer(summary.technical_signal_count)} 次技术信号`)
  );
}

function drawCurve() {
  const area = byId('equity-chart');
  if (area.hidden) return;
  const canvas = byId('equity-plot');
  const context = canvas.getContext('2d');
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!context || width < 100 || height < 100) return;
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(width * pixelRatio);
  canvas.height = Math.round(height * pixelRatio);
  context.scale(pixelRatio, pixelRatio);
  context.font = '12px sans-serif';

  const equities = curve.map(row => row.missing_marks?.length ? null : row.equity);
  const known = equities.filter(Number.isFinite);
  const low = known.reduce((minimum, value) => Math.min(minimum, value), Infinity);
  const high = known.reduce((maximum, value) => Math.max(maximum, value), -Infinity);
  const margin = high > low ? (high - low) * 0.1 : 1;
  const lower = Math.max(0, low - margin);
  const upper = high + margin;
  const left = Math.ceil(context.measureText(money(upper)).width) + 8;
  const right = width - 9;
  const top = 13;
  const bottom = height - 24;
  if (right <= left) return;
  const x = i => curve.length > 1 ? left + i * (right - left) / (curve.length - 1) : (left + right) / 2;
  const y = value => bottom - (value - lower) * (bottom - top) / (upper - lower);

  for (let i = 0; i < 4; i++) {
    const value = lower + (upper - lower) * i / 3;
    const line = y(value);
    context.strokeStyle = '#e2e8f0';
    context.beginPath();
    context.moveTo(left, line);
    context.lineTo(right, line);
    context.stroke();
    context.fillStyle = '#556780';
    context.textAlign = 'right';
    context.fillText(money(value), left - 5, line + 4, left - 5);
  }

  context.strokeStyle = '#326a9b';
  context.lineWidth = 2;
  context.beginPath();
  let connected = false;
  for (const [index, value] of equities.entries()) {
    if (!Number.isFinite(value)) { connected = false; continue; }
    if (connected) context.lineTo(x(index), y(value));
    else context.moveTo(x(index), y(value));
    connected = true;
  }
  context.stroke();
  context.fillStyle = '#326a9b';
  if (curve.length < 50) {
    for (const [index, value] of equities.entries()) {
      if (!Number.isFinite(value)) continue;
      context.beginPath();
      context.arc(x(index), y(value), 2, 0, Math.PI * 2);
      context.fill();
    }
  }
  const selected = Number(byId('equity-day').value);
  const point = x(selected);
  context.strokeStyle = '#719bbc';
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(point, top);
  context.lineTo(point, bottom);
  context.stroke();
  if (Number.isFinite(equities[selected])) {
    context.beginPath();
    context.arc(point, y(equities[selected]), 4, 0, Math.PI * 2);
    context.fill();
  }
  context.fillStyle = '#556780';
  context.textAlign = 'left';
  context.fillText(curve[0].date, left, height - 5);
  if (curve.length > 1 && right - left > 155) {
    context.textAlign = 'right';
    context.fillText(curve.at(-1).date, right, height - 5);
  }
}

function selectEquityDay() {
  const row = curve[Number(byId('equity-day').value)];
  if (!row) return;
  const label = row.date + ' · ' + (Number.isFinite(row.equity) && !row.missing_marks?.length
    ? '净值 ' + money(row.equity) : '估值缺失');
  byId('equity-readout').textContent = label;
  byId('equity-day').setAttribute('aria-valuetext', label);
  drawCurve();
}

function renderCurve(rows) {
  curve = rows;
  const visible = rows.some(row => Number.isFinite(row.equity) && !row.missing_marks?.length);
  byId('equity-chart').hidden = !visible;
  byId('equity-empty').hidden = visible;
  const slider = byId('equity-day');
  slider.max = Math.max(0, rows.length - 1);
  slider.value = slider.max;
  slider.disabled = rows.length < 2;
  if (visible) selectEquityDay();
}

function renderProvenance(meta, summary) {
  const cost = meta.costs ?? {};
  const fields = [
    ['行情来源', meta.source],
    ['交易日历来源', meta.calendar_source],
    ['价格口径', meta.price_basis === 'raw' ? '原始价格（未复权）' : meta.price_basis],
    ['引擎版本', meta.engine],
    ['数据包 SHA-256', meta.bundle_sha256, 'digest'],
    ['引擎 SHA-256', meta.engine_sha256, 'digest'],
  ];
  if (Number.isFinite(cost.commission_rate) && Number.isFinite(cost.minimum_commission)) {
    fields.push(['佣金假设', `${percent(cost.commission_rate * 100)} · 每笔最低 ${money(cost.minimum_commission)}`]);
  }
  if (Number.isFinite(cost.prior_minute_participation)) {
    fields.push(['前一分钟成交量参与上限', percent(cost.prior_minute_participation * 100)]);
  }
  byId('provenance').replaceChildren(...fields.map(([label, value, className]) => {
    const item = document.createElement('div');
    item.append(node('dt', label), node('dd', value, className));
    return item;
  }));
  byId('execution-model').textContent = meta.execution_model ?? '';
  const limitations = [...(meta.limitations ?? [])];
  if (summary.missing_valuation_dates?.length) {
    const missing = summary.missing_valuation_dates;
    limitations.unshift(`缺估值 ${missing.length} 个交易日。最近日期 ${missing.slice(-6).join('、')}。`);
  }
  byId('limitations').replaceChildren(...limitations.map(value => node('li', value)));
}

function renderReport(report) {
  const { meta, summary } = report;
  const incomplete = summary.execution_coverage_complete === false;
  const missingValuations = summary.valuations_complete === false;
  const messages = [];
  const coverage = meta.input_coverage;
  byId('coverage').hidden = !coverage;
  if (coverage) {
    const checks = [
      ['缺整日行情', coverage.missing_sessions], ['分钟缺口', coverage.partial_minute_sessions],
      ['公司行动待核验', coverage.corporate_action_unverified_sessions], ['标的日线不足', coverage.daily_unready_sessions],
      ['基准日线不足', coverage.benchmark_unready_days], ['缺历史名单', coverage.universe_missing_days],
    ].filter(([, count]) => count > 0);
    byId('coverage-summary').textContent =
      `${integer(coverage.symbol_sessions)} 个证券交易日 · ${integer(coverage.complete_minutes_sessions)} 个分钟完整 · ` +
      `${integer(coverage.suspended_sessions)} 个有停牌记录 · ${coverage.universe_mode === 'dated' ? '按信号日名单' : '使用固定名单'}`;
    byId('coverage-issues').replaceChildren(...checks.map(([label, count]) => node('li', `${label} · ${integer(count)}`)));
    if (checks.length) messages.push('历史输入覆盖存在缺口，见下方核对结果。');
  }
  if (meta.demo === true) messages.push('合成样本用于检查规则与成交逻辑，收益仅反映演示路径。');
  if (meta.halted) {
    const { date, code, reason } = meta.halted;
    messages.push(`回放在 ${date} 因 ${code} ${reasonLabel(reason)}暂停，统计截至已模拟的成交。`);
  } else if (incomplete) {
    messages.push(`${integer(summary.unverified_symbol_sessions)} 个证券交易日的执行数据待核验，统计基于已模拟的成交。`);
  }
  if (missingValuations) messages.push('估值存在缺口，完整区间回撤留空。');
  byId('status').className = 'status' + (messages.length ? ' warn' : '');
  byId('status').textContent = messages.join(' ') || '已加载最近一次回测报告。';
  byId('sample-range').textContent = `${meta.start ?? '—'} 至 ${meta.end ?? '—'}`;
  byId('sample-type').textContent = meta.demo === true ? '合成演示' : '历史数据回放';
  byId('sample-type').className = 'sample-type' + (meta.demo === true ? ' demo' : '');
  byId('report').hidden = false;
  renderMetrics(summary);
  renderCurve(report.equity_curve ?? []);

  const assetStats = summary.by_asset_type ?? {};
  renderTable('asset-stats', ['stock', 'etf'].filter(kind => assetStats[kind]).map(kind => {
    const value = assetStats[kind];
    return [
      types[kind], integer(value.fill_count), integer(value.closed_trade_count),
      [money(value.realized_net_pnl), tone(value.realized_net_pnl)], percent(value.win_rate_pct),
      [ratio(value.avg_net_r), tone(value.avg_net_r)], money(value.fee_total),
    ];
  }), 7, '暂无分品种统计');

  const fills = report.fills ?? [];
  const fillsLimit = 50;
  byId('fill-count').textContent = `共 ${integer(fills.length)} 笔` +
    (fills.length > fillsLimit ? `，显示最近 ${fillsLimit} 笔。` : '，按成交时间倒序。');
  renderTable('fills', fills.slice(-fillsLimit).reverse().map(row => [
    clock(row), securities(row), row.side === 'buy' ? '买入' : '卖出',
    integer(row.quantity), price(row.price), money(row.notional), money(row.fees),
    money(row.slippage), money(row.initial_risk), reasonLabel(row.reason),
  ]), 10, '本次回测没有模拟成交');

  const closed = report.closed_trades ?? [];
  renderTable('closed-trades', closed.slice(-50).reverse().map(row => [
    securities(row), row.opened_on, row.closed_on,
    [money(row.gross_pnl), tone(row.gross_pnl)], [money(row.net_pnl), tone(row.net_pnl)],
    money(row.initial_risk), [ratio(row.net_r), tone(row.net_r)], reasonLabel(row.exit_reason),
  ]), 8, '本次回测没有已结束交易');
  const positions = report.open_positions ?? [];
  byId('open-positions').hidden = positions.length === 0;
  byId('open-positions').textContent = '期末持仓 ' + positions.map(row =>
    `${row.code} · ${integer(row.quantity)} 股/份${row.stop_pending ? '（止损待成交）' : ''}`
  ).join('、');

  const decisions = report.decisions ?? [];
  const decisionLimit = 40;
  const counts = Object.entries(summary.rejection_counts ?? {});
  byId('decision-count').textContent = `共 ${integer(decisions.length)} 条检查记录` +
    (decisions.length > decisionLimit ? `，显示最近 ${decisionLimit} 条。` : '。');
  byId('rejection-counts').hidden = counts.length === 0;
  byId('rejection-counts').replaceChildren(...counts.map(([reason, count]) =>
    node('li', reasonLabel(reason) + ' · ' + integer(count))
  ));
  renderTable('decisions', decisions.slice(-decisionLimit).reverse().map(row => [
    clock(row), row.code, reasonLabel(row.reason), [reasonLabel(row.detail), 'wrap-cell'],
  ]), 4, '本次回测没有未成交或数据待核验记录');
  renderProvenance(meta, summary);
}

async function refresh() {
  const button = byId('refresh');
  button.disabled = true;
  byId('status').className = 'status';
  byId('status').textContent = '正在读取最近一次回测…';
  byId('report').hidden = true;
  byId('empty-help').hidden = true;
  try {
    const response = await fetch('/api/backtest', { cache: 'no-store' });
    if (response.status === 404) {
      byId('status').textContent = '尚无可查看的回测报告。';
      byId('empty-help').hidden = false;
      return;
    }
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    if (!data || typeof data.meta !== 'object' || !data.meta || typeof data.summary !== 'object' || !data.summary) {
      throw new Error('报告格式无效');
    }
    renderReport(data);
  } catch (error) {
    byId('report').hidden = true;
    byId('status').className = 'status warn';
    byId('status').textContent = '读取回测报告失败。' + error.message;
  } finally {
    button.disabled = false;
  }
}

byId('refresh').addEventListener('click', refresh);
byId('equity-day').addEventListener('input', selectEquityDay);
window.addEventListener('resize', drawCurve);
refresh();
