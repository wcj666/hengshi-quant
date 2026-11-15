const byId = id => document.getElementById(id);
const number = (value, digits = 0) => Number.isFinite(value) ? value.toLocaleString('zh-CN', {
  minimumFractionDigits: digits, maximumFractionDigits: digits,
}) : '—';
const suffixed = (value, suffix) => Number.isFinite(value) ? number(value, 2) + suffix : '—';
const money = value => Number.isFinite(value) ? '¥' + number(value, 2) : '—';
const labels = { development: '开发区间', validation: '验证区间', holdout: '留出区间' };

function element(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value ?? '—';
  return node;
}

function row(label, values) {
  const tr = document.createElement('tr');
  const heading = element('th', label);
  heading.scope = 'row';
  tr.append(heading, ...values.map(value => element('td', value)));
  return tr;
}

function list(id, messages) {
  byId(id).replaceChildren(...messages.map(message => element('li', message)));
  byId(id).hidden = messages.length === 0;
}

function render(report) {
  const { meta, periods } = report;
  const sample = byId('sample');
  sample.textContent = meta.demo ? '合成演示' : '历史数据回放';
  sample.className = 'sample-type' + (meta.demo ? ' demo' : '');
  byId('source').textContent = `${meta.source} · 日历 ${meta.calendar_source}`;
  byId('initial-cash').textContent = `每段以 ${money(meta.initial_cash)} 现金重新开始。期末未平仓按已核验收盘价估值。`;
  byId('results').replaceChildren(...periods.map(period => {
    const stats = period.summary;
    const phase = labels[period.key];
    const result = row(phase, [
      `${period.start} 至 ${period.end}`, number(stats.session_count), number(stats.closed_trade_count),
      number(period.open_position_count), money(stats.net_pnl), suffixed(stats.return_pct, '%'),
      suffixed(stats.avg_net_r, ' R'), suffixed(stats.max_drawdown_pct, '%'),
    ]);
    if (phase && /^[0-9a-f]{32}$/.test(meta.run_id ?? '')) {
      const link = element('a', phase);
      link.href = `/backtest/validation/download/${meta.run_id}/${period.key}/result.json`;
      link.title = '下载区间逐笔报告';
      result.firstElementChild.replaceChildren(link);
    }
    return result;
  }));
  const fields = [
    'symbol_sessions', 'complete_minutes_sessions', 'suspended_sessions', 'missing_sessions',
    'partial_minute_sessions', 'corporate_action_unverified_sessions', 'daily_unready_sessions',
    'benchmark_unready_days', 'universe_missing_days',
  ];
  byId('coverage').replaceChildren(...periods.map(period =>
    row(labels[period.key], fields.map(field => number(period.input_coverage?.[field])))
  ));
  list('warnings', [
    ...(meta.warnings ?? []),
    ...(meta.gaps ?? []).map(gap =>
      `未纳入对照 ${gap.start} 至 ${gap.end}（${number(gap.session_count)} 个交易日）`
    ),
  ]);
  const periodWarnings = periods.flatMap(period =>
    (period.warnings ?? []).map(message => labels[period.key] + ' · ' + message)
  );
  list('period-warnings', periodWarnings);
  byId('evidence').replaceChildren(...[
    ['历史输入 SHA-256', meta.bundle_sha256], ['回放引擎 SHA-256', meta.engine_sha256],
  ].map(([label, value]) => {
    const item = document.createElement('div');
    const digest = element('dd', value);
    digest.className = 'digest';
    item.append(element('dt', label), digest);
    return item;
  }));
  byId('status').textContent = meta.demo ? '合成演示仅用于检查分段和报告流程。'
    : periodWarnings.length ? '区间结果附有数据或样本提示，请结合下方记录查看。' : '已加载最近一次区间对照。';
  byId('status').className = meta.demo || periodWarnings.length ? 'status warn' : 'status';
  byId('report').hidden = false;
}

async function refresh() {
  const button = byId('refresh');
  const status = byId('status');
  button.disabled = true;
  byId('report').hidden = true;
  status.className = 'status';
  status.textContent = '正在读取区间对照…';
  try {
    const response = await fetch('/api/backtest/validation', { cache: 'no-store' });
    const report = await response.json();
    if (!response.ok) throw new Error(report.error || '读取区间对照失败。');
    render(report);
  } catch (error) {
    status.className = 'status warn';
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

byId('refresh').addEventListener('click', refresh);
refresh();
