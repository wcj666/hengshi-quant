(() => {
  const byId = id => document.getElementById(id);
  const endpoint = '/api/backtest/reminder-validation';
  const root = byId('reminder-validation');
  if (!root) return;

  const fields = ['net_return_pct', 'net_pnl', 'max_drawdown_pct', 'fees'];
  const tradeReminders = [
    ['breakout', '突破买入'],
    ['stop_loss', '止损'],
    ['take_profit_1', '第一止盈'],
    ['take_profit_2', '第二止盈'],
  ];
  const tReminders = [['sell_ready', '做 T 高抛'], ['buyback_ready', '做 T 接回']];
  const statusClasses = {
    '验证通过': 'good',
    '移出强提醒候选': 'warn',
    '待验证': 'warn',
  };
  const format = new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const integer = new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 0});
  const money = new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  let loading = false;

  function node(tag, value, className) {
    const element = document.createElement(tag);
    if (value != null) element.textContent = value;
    if (className) element.className = className;
    return element;
  }

  function numeric(value, kind, signed = false) {
    if (!Number.isFinite(value)) return '—';
    const formatter = kind === 'count' ? integer : kind === 'money' ? money : format;
    const prefix = value > 0 && signed ? '+' : '';
    return prefix + formatter.format(value) + (kind === 'pct' ? '%' : '');
  }

  function titledTable(label, headings, rows) {
    const region = node('div', null, 'table-wrap');
    region.tabIndex = 0;
    region.setAttribute('role', 'region');
    region.setAttribute('aria-label', label);
    const table = node('table');
    const thead = node('thead');
    const tr = node('tr');
    for (const title of headings) {
      const th = node('th', title);
      th.scope = 'col';
      tr.append(th);
    }
    const tbody = node('tbody');
    tbody.append(...rows);
    thead.append(tr);
    table.append(thead, tbody);
    region.append(table);
    return region;
  }

  function labeledRow(label, cells) {
    const row = node('tr');
    const head = node('th', label);
    head.scope = 'row';
    row.append(head, ...cells.map(value => node('td', value)));
    return row;
  }

  function datedNote(value) {
    if (!value) return '';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('zh-CN', {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: 'Asia/Shanghai',
    }).format(date);
  }

  function valueRow(label, data) {
    const kinds = ['pct', 'money', 'pct', 'money'];
    return labeledRow(label, fields.map((key, index) => numeric(data?.[key], kinds[index])));
  }

  function conciseList(messages) {
    return messages.filter(message => typeof message === 'string' && message.trim());
  }

  function renderWarnings(target, messages) {
    const items = conciseList(messages);
    target.replaceChildren(...items.map(message => node('li', message)));
    target.hidden = !items.length;
  }

  function reminderPanel(title, data, strategyName, baselineName, reminderLabels) {
    const panel = node('section', null, 'reminder-comparison');
    const heading = node('h4', title);
    panel.append(heading);
    const information = [];
    if (Number.isFinite(data?.sample_count)) information.push(`样本 ${numeric(data.sample_count, 'count')}`);
    if (data?.covered === false) information.push('证据不全，待验证');
    const strategy = data?.strategy;
    const baseline = data?.baseline;
    const gap = Number.isFinite(strategy?.net_return_pct) && Number.isFinite(baseline?.net_return_pct)
      ? strategy.net_return_pct - baseline.net_return_pct : null;
    if (gap !== null) information.push(`相对基准 ${numeric(gap, 'decimal', true)} 个百分点`);
    if (Number.isFinite(data?.relative_net_pnl)) information.push(`扣费净差额 ${numeric(data.relative_net_pnl, 'money', true)}`);
    if (data?.unmatched_cycles > 0) information.push(`期末 ${numeric(data.unmatched_cycles, 'count')} 轮未完全接回`);
    if (information.length) panel.append(node('p', information.join(' · '), 'reminder-note'));
    panel.append(titledTable(title + '策略与基准', ['对照', '净收益率', '净盈亏', '最大日终回撤', '费用'], [
      valueRow(strategyName, strategy),
      valueRow(baselineName, baseline),
    ]));
    const triggers = reminderLabels.map(([name, label]) => {
      const counts = data?.reminders?.[name];
      const rate = counts?.triggers > 0 ? counts.fill_rate_pct : null;
      return labeledRow(label, [
        numeric(counts?.triggers, 'count'),
        numeric(counts?.filled, 'count'),
        numeric(counts?.partial, 'count'),
        numeric(rate, 'pct'),
      ]);
    });
    panel.append(titledTable(title + '触发与模拟成交', ['提醒', '触发次数', '有成交的触发', '其中部分成交', '可成交比例'], triggers));
    const caveats = node('ul', null, 'limitations');
    const examples = (data?.coverage_examples || []).slice(0, 3).map(item =>
      [item.date, item.code, item.reason].filter(Boolean).join(' · '));
    renderWarnings(caveats, [...(data?.warnings || []), ...examples]);
    panel.append(caveats);
    return panel;
  }

  function renderPeriod(period, meta) {
    const panel = node('article', null, 'reminder-period');
    const periodLabel = period.label || period.key || '区间';
    panel.append(node('h3', periodLabel));
    panel.append(node('p', `${period.start ?? '—'} 至 ${period.end ?? '—'}`, 'reminder-period-date'));
    const comparisons = node('div', null, 'reminder-comparisons');
    comparisons.append(
      reminderPanel('买卖规则', period.trade, 'V1 买卖规则', meta.baseline_label || '简单基准', tradeReminders),
      reminderPanel('做 T', period.t, '做 T 回放', meta.t_baseline_label || '原仓不动', tReminders),
    );
    panel.append(comparisons);
    const reference = period.trade?.index_reference;
    if (Number.isFinite(reference?.return_pct)) {
      panel.append(node('p', `沪深 300 参考涨跌 ${numeric(reference.return_pct, 'pct', true)} · 最大日终回撤 ${numeric(reference.max_drawdown_pct, 'pct')} · 指数观察口径`, 'reminder-rate-note'));
    }
    const variants = period.trade?.ablation;
    if (variants && Object.keys(variants).length) {
      const details = node('details', null, 'reminder-ablation');
      details.append(node('summary', '查看单项移除对照'));
      const labels = Object.fromEntries(tradeReminders);
      details.append(titledTable(periodLabel + '单项移除对照', ['对照', '净收益率', '净盈亏', '最大日终回撤', '费用'], [
        valueRow('完整买卖规则', period.trade.strategy),
        ...Object.entries(variants).map(([name, metric]) => valueRow(metric?.label || `移除 ${labels[name] || name}`, metric)),
      ]));
      panel.append(details);
    }
    panel.append(node('p', '可成交比例为有模拟成交的触发数除以触发次数。', 'reminder-rate-note'));
    return panel;
  }

  function renderRecommendations(recommendations) {
    const section = byId('reminder-validation-recommendations');
    section.hidden = !recommendations.length;
    const body = byId('reminder-validation-recommendation-rows');
    body.replaceChildren(...recommendations.map(item => {
      const row = node('tr');
      const name = node('th', item.label || item.key || '—');
      name.scope = 'row';
      const badgeCell = node('td');
      const category = item.status || '待验证';
      const accent = statusClasses[category] || '';
      badgeCell.append(node('span', category, `reminder-tag ${accent}`.trim()));
      row.append(name, badgeCell, node('td', item.reason || '—', 'wrap-cell'));
      return row;
    }));
  }

  function render(report) {
    const { meta = {}, periods, recommendations = [] } = report;
    if (!Array.isArray(periods) || !Array.isArray(recommendations)) {
      throw new Error('提醒验证报告的区间数据无法读取。');
    }
    if (!periods.length) {
      showEmpty(report.status || '暂无提醒验证结果。');
      return;
    }
    const demo = meta.demo === true;
    const sample = byId('reminder-validation-sample');
    sample.textContent = demo ? '合成样例' : '历史回放';
    sample.className = 'sample-type' + (demo ? ' demo' : '');
    sample.hidden = false;
    const status = byId('reminder-validation-status');
    const warning = demo || !recommendations.length || recommendations.some(item => item.status === '待验证');
    status.classList.toggle('warn', warning);
    status.textContent = demo ? '合成样例用于检查回放流程，策略仍待验证。'
      : warning ? '已读取历史回放。部分提醒缺少可用证据，仍待验证。'
        : '已读取最近一次提醒验证结果。';
    const source = [meta.source, datedNote(meta.generated_at)].filter(Boolean);
    byId('reminder-validation-source').textContent = source.join(' · ');
    renderWarnings(byId('reminder-validation-warnings'), meta.warnings ?? []);
    renderRecommendations(recommendations);
    byId('reminder-validation-periods').replaceChildren(...periods.map(period => renderPeriod(period, meta)));
    byId('reminder-validation-results').hidden = false;
  }

  function showEmpty(message) {
    const status = byId('reminder-validation-status');
    status.classList.add('warn');
    status.textContent = message;
    byId('reminder-validation-empty').hidden = false;
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    const status = byId('reminder-validation-status');
    status.classList.remove('warn');
    status.textContent = '正在读取提醒验证结果…';
    byId('reminder-validation-results').hidden = true;
    byId('reminder-validation-empty').hidden = true;
    byId('reminder-validation-sample').hidden = true;
    try {
      const response = await fetch(endpoint, { cache: 'no-store' });
      const report = await response.json();
      if (response.status === 404) {
        showEmpty(report.error || '暂无提醒验证报告。');
      } else if (response.ok) {
        render(report);
      } else {
        throw new Error(report.error || '提醒验证结果读取失败。');
      }
    } catch (error) {
      status.classList.add('warn');
      status.textContent = error.message || '提醒验证结果读取失败。';
    } finally {
      loading = false;
    }
  }

  byId('refresh').addEventListener('click', refresh);
  refresh();
})();
