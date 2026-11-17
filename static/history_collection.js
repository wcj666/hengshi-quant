(() => {
  'use strict';

  const id = suffix => document.getElementById('history-collection-' + suffix);
  if (!id('status')) return;

  const endpoint = '/api/backtest/history-collection';
  const integer = new Intl.NumberFormat('zh-CN', {maximumFractionDigits:0});
  const decimal = new Intl.NumberFormat('zh-CN', {maximumFractionDigits:2});
  const dateTime = new Intl.DateTimeFormat('zh-CN', {
    dateStyle:'medium', timeStyle:'short', timeZone:'Asia/Shanghai',
  });
  const securities = [
    {code:'159971', name:'创业100'},
    {code:'588170', name:'科创半导'},
  ];
  let loading = false;

  function element(tag, value, classes) {
    const node = document.createElement(tag);
    node.textContent = value ?? '';
    if (classes) node.className = classes;
    return node;
  }

  function asText(value) {
    return typeof value === 'string' ? value.trim() : '';
  }

  function count(value) {
    return Number.isSafeInteger(value) && value >= 0 ? integer.format(value) : '—';
  }

  function asDay(value) {
    return typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : '';
  }

  function formatTime(value) {
    const stamp = Date.parse(value);
    return Number.isFinite(stamp) ? dateTime.format(stamp) : '';
  }

  function setOptional(idSuffix, message) {
    const node = id(idSuffix);
    node.textContent = message;
    node.hidden = !message;
  }

  function cleanWarnings(values) {
    return Array.isArray(values) ? values.map(asText).filter(Boolean) : [];
  }

  function positionRow(item) {
    const row = element('tr');
    const head = element('th', asText(item.name) || item.code);
    head.scope = 'row';
    head.append(element('span', item.code));
    row.append(head);
    for (const field of ['daily_count', 'amount_daily_count', 'complete_minute_days', 'turnover_days', 'matched_amount_days', 'replay_days']) {
      row.append(element('td', count(item[field])));
    }
    row.append(element('td', asDay(item.latest_day) || '—'));
    const warnings = cleanWarnings(item.warnings);
    const details = element('td', warnings.length ? '' : '—', 'wrap-cell');
    if (warnings.length) {
      const list = element('ul', '', 'history-position-warnings');
      list.append(...warnings.map(message => element('li', message)));
      details.append(list);
    }
    row.append(details);
    return row;
  }

  function linkToReport(value) {
    if (!asText(value)) return '';
    try {
      const url = new URL(value, location.origin);
      if (url.origin === location.origin && /^\/(?:api\/)?backtest(?:\/|$)/.test(url.pathname)) {
        return url.pathname + url.search + url.hash;
      }
    } catch {
      // Invalid or remote links stay hidden.
    }
    return '';
  }

  function describeTrial(summary) {
    if (typeof summary === 'string') return asText(summary);
    if (!summary || typeof summary !== 'object') return '';
    const parts = [asText(summary.message) || asText(summary.status)];
    if (Number.isSafeInteger(summary.fill_count) && summary.fill_count >= 0) {
      parts.push('模拟成交 ' + count(summary.fill_count) + ' 笔');
    }
    if (Number.isFinite(summary.net_return_pct)) parts.push('扣费净收益 ' + decimal.format(summary.net_return_pct) + '%');
    if (Number.isFinite(summary.max_drawdown_pct)) parts.push('最大回撤 ' + decimal.format(summary.max_drawdown_pct) + '%');
    return parts.filter(Boolean).join(' · ');
  }

  function render(data) {
    const byCode = new Map((Array.isArray(data.positions) ? data.positions : [])
      .filter(item => item && securities.some(security => security.code === item.code))
      .map(item => [item.code, item]));
    id('positions').replaceChildren(...securities.map(security =>
      positionRow({...security, ...byCode.get(security.code)})));
    const complete = asDay(data.latest_completed_day);
    const updated = formatTime(data.updated_at);
    setOptional('updated', [
      complete && '最近已核验交易日 ' + complete,
      updated && '进度更新 ' + updated + '（北京时间）',
    ].filter(Boolean).join(' · '));

    const warnings = cleanWarnings(data.warnings);
    const notes = id('warnings');
    notes.replaceChildren(...warnings.map(message => element('li', message)));
    notes.hidden = !warnings.length;

    const trial = data.trial || {};
    const validation = data.validation || {};
    const trialLabel = [asText(trial.status) || '等待可回放数据', asText(trial.day_or_range)];
    id('trial').textContent = trialLabel.filter(Boolean).join(' · ');
    setOptional('trial-summary', describeTrial(trial.summary));
    const link = id('trial-link');
    const report = linkToReport(trial.report_url);
    link.hidden = !report;
    if (report) link.href = report;
    else link.removeAttribute('href');
    id('validation').textContent = asText(validation.status) || '待验证';
    setOptional('validation-message', asText(validation.message) || '等真实数据和复核证据达到分段要求后再验证。');
    id('phases').hidden = false;
  }

  async function refreshHistory() {
    if (loading) return;
    loading = true;
    const status = id('status');
    try {
      const response = await fetch(endpoint, {cache:'no-store'});
      const result = await response.json();
      if (response.status === 404) {
        render({});
        status.classList.add('warn');
        status.textContent = '等待真实历史数据首次归档。';
        return;
      }
      if (!response.ok || !result || typeof result !== 'object') throw new Error('采集进度暂时无法读取。');
      render(result);
      status.classList.toggle('warn', !result.updated_at);
      status.textContent = asText(result.status) || '等待真实历史数据首次归档。';
    } catch {
      render({});
      status.classList.add('warn');
      status.textContent = '采集进度暂时无法读取，刷新后重试。';
    } finally {
      loading = false;
    }
  }

  document.getElementById('refresh')?.addEventListener('click', refreshHistory);
  refreshHistory();
  setInterval(() => { if (!document.hidden) refreshHistory(); }, 60000);
})();
