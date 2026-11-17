(() => {
'use strict';
window.hengshiActionCenter = true;

const node = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, char =>
  ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
const amount = value => Number.isFinite(value) ? '¥' + value.toFixed(2) : '—';
const price = value => Number.isFinite(value) ? value.toFixed(4).replace(/(\.\d{3})0$/, '$1') : '—';
const metric = (label, value) => '<div><dt>' + esc(label) + '</dt><dd>' + esc(value) + '</dd></div>';
const list = values => values.length ? '<ul>' + values.map(value => '<li>' + esc(value) + '</li>').join('') + '</ul>' : '';
const quantity = value => value == null ? '—' : value + ' 股/份';

let busy = false, audio = null, sound = false, preferencesDirty = false, loadedPrefs = false;
let current = null, generation = 0, desktopTest = '';
let token = document.querySelector?.('meta[name="csrf-token"]')?.content || '';
const notices = new Map();
try {
  for (const [key, value] of JSON.parse(localStorage.getItem('hengshi_strong_alert_counts') || '[]')) notices.set(key, value);
} catch {}

// Standalone pages do not share the dashboard's request helpers or its token.
async function actionApi(path, method = 'GET', payload) {
  for (let attempt = 0; attempt < 2; attempt++) {
    if (method !== 'GET' && !token) {
      const session = await fetch('/api/session', {credentials:'same-origin', cache:'no-store'});
      const data = await session.json();
      if (!session.ok || !data.csrf_token) throw Error('安全会话恢复失败，请刷新页面后重试');
      token = data.csrf_token;
    }
    const options = {method, credentials:'same-origin', cache:'no-store'};
    if (method !== 'GET') options.headers = {'X-CSRF-Token':token};
    if (payload !== undefined) {
      options.headers = {...options.headers, 'Content-Type':'application/json'};
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(path, options);
    const data = await response.json();
    if (attempt === 0 && response.status === 403 && data.code === 'csrf_expired') {
      token = '';
      continue;
    }
    if (!response.ok || data.error) throw Error(data.error || '读取失败');
    return data;
  }
}

function beep() {
  if (!sound || !audio || ['suspended','interrupted','closed'].includes(audio.state)) return false;
  const oscillator = audio.createOscillator(), gain = audio.createGain();
  oscillator.connect(gain);
  gain.connect(audio.destination);
  oscillator.frequency.value = 760;
  gain.gain.value = .08;
  oscillator.start();
  gain.gain.exponentialRampToValueAtTime(.001, audio.currentTime + .35);
  oscillator.stop(audio.currentTime + .4);
  return true;
}

function resetAlerts(message) {
  current = null;
  node('strong-alerts')?.replaceChildren();
  const bottom = node('action-bottom-alert');
  if (bottom) {
    bottom.replaceChildren();
    bottom.hidden = true;
  }
  if (node('action-status')) node('action-status').textContent = message;
  if (node('action-cards')) node('action-cards').innerHTML = '<p class="tip">实时行动卡已撤销，恢复数据后重新核验。</p>';
  if (node('action-note')) node('action-note').textContent = '';
}

function renderThreshold(item) {
  const direction = item.direction === 'at_or_above' ? '≥' : '≤';
  let detail = '等待有效报价';
  if (Number.isFinite(item.distance)) {
    detail = item.distance > 0 ? '还差 ' + price(item.distance) + ' 元' :
      Number.isFinite(item.max_price) && -item.distance > item.max_price - item.price + 1e-8 ?
        '已超过最高接受价' : '价格已到位';
  }
  return '<li><b>' + esc(item.label + ' ' + direction + ' ' + price(item.price)) +
    '</b>' + (item.max_price == null ? '' : ' · 最高接受价 ' + price(item.max_price)) +
    ' · ' + esc(detail) + '</li>';
}

function renderT(t) {
  if (!t) return '';
  const values = [];
  if (t.cycle) {
    values.push(metric('实际卖出均价', price(t.sell_price)), metric('尚未接回', quantity(t.remaining_quantity)));
    if (t.buyback_cap != null) values.push(metric('接回最高参考价', price(t.buyback_cap)));
  } else if (t.opening_price_floor != null) {
    values.push(metric('分时均价', price(t.vwap)), metric('按成本推算的高抛参考门槛', price(t.opening_price_floor)));
  }
  if (t.minimum_spread != null) {
    values.push(metric('至少需要的价差（元/股或份）', price(t.minimum_spread)),
                metric('本次测算数量', quantity(t.break_even_quantity)));
  }
  const cost = t.minimum_gross == null ? '' :
    '<p>毛价差门槛 ' + amount(t.minimum_gross) + '，其中往返费用测算 ' +
    amount(t.estimated_roundtrip_fee) + '、滑点等缓冲 ' + amount(t.extra_cost_buffer) +
    (t.fee_basis?.includes('reserve') ? '。至少一边费用暂按预留算。' : '。') + '</p>';
  const distance = t.distance_to_cap > 0 ?
    '<p>现价高于接回上限 ' + price(t.distance_to_cap) + ' 元，等待；尾盘不追价接回。</p>' : '';
  const checks = (t.checks || []).map(check => check.detail).filter(detail => detail && detail !== t.reason);
  return '<div class="action-t"><h4>做 T · ' + esc(t.status) + '</h4>' +
    (values.length ? '<dl>' + values.join('') + '</dl>' : '') + cost + distance +
    (t.reason ? '<p>' + esc(t.reason) + '</p>' : '') + list(checks) + '</div>';
}

function renderCard(card, open) {
  const thresholds = card.thresholds || [];
  const expireLocal = card.expires_at ? new Date(card.expires_at).toLocaleTimeString('zh-CN',
    {timeZone:'Asia/Shanghai', hour12:false}) : '';
  const context = [
    card.quote_at && '报价时间 ' + new Date(card.quote_at).toLocaleTimeString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false}),
    expireLocal && '本轮核验有效至 ' + expireLocal,
    card.plan?.valid_on && '计划有效日 ' + card.plan.valid_on
  ].filter(Boolean).join(' · ');
  return '<article class="action-card ' + esc(card.state) + '"><div class="action-heading"><strong>' +
    esc(card.name) + ' ' + esc(card.code) + '</strong><b>' + esc(card.label) + '</b></div><p>' + esc(card.reason) +
    '</p><dl>' + metric(card.side === 'sell' ? '卖出参考价' : '最新参考价', price(card.price)) +
    metric('本次核验上限', quantity(card.quantity)) +
    (card.available_quantity == null ? '' : metric('券商记录可卖', quantity(card.available_quantity))) +
    (card.estimated_cash_after == null ? '' : metric('扣预计费用后现金', amount(card.estimated_cash_after))) +
    (card.cash_after == null ? '' : metric('预留后保守可用现金', amount(card.cash_after))) +
    '</dl>' + (thresholds.length ? '<ul class="action-thresholds">' + thresholds.map(renderThreshold).join('') + '</ul>' : '') +
    list(card.blockers || []) + (context ? '<p>' + esc(context) + '</p>' : '') +
    (card.notification ? '<p class="action-notice">' + esc(card.notification) + '</p>' : '') +
    (card.validation_status ? '<p class="action-notice">' + esc(({pending:'待验证观察',excluded:'已停用强提醒',verified:'本次分段验证通过',risk:'保留风险提醒'})[card.validation_status]) + '：' + esc(card.validation_reason) + '</p>' : '') +
    (card.followup ? '<p class="action-notice">原目标 ' + esc(card.original_target) + '，已成交 ' + esc(card.filled_before) + '，剩余目标 ' + esc(card.remaining_target) + ' 股/份。每次执行前重新核对可卖数量。</p>' : '') +
    renderT(card.t) + '<details data-card-code="' + esc(card.code) + '"' + (open ? ' open' : '') +
    '><summary>有效期、失效条件与费用</summary>' +
    (card.plan?.signal_date ? '<p>计划信号日 ' + esc(card.plan.signal_date) + '。</p>' : '') +
    (card.reserved_cost != null ? '<p>预计手续费 ' + amount(card.estimated_fee) + ' · 额外安全预留 ' + amount(card.safety_reserve) + '。' + esc(card.fee_note) + '</p>' : '') +
    list(card.cancel || []) + (card.inventory_at ? '<p>券商数量记录于 ' + esc(card.inventory_at) + '</p>' : '') +
    '</details>' + (card.alert_id && card.quantity != null ? '<a href="/trade-reconcile?alert_id=' + encodeURIComponent(card.alert_id) +
      '">登记实际成交</a>' : '') + '</article>';
}

function drawCards(data) {
  if (!node('action-cards')) return;
  node('action-status').textContent = (data.valid ? '盘中行情已核验' : data.pause_reason || '暂停实时提醒') +
    (data.mode === 'exit_only' ? ' · 退出休息模式' : '');
  node('action-note').textContent = (data.cash_floor == null ? '' :
    '总仓规则对应现金至少 ' + amount(data.cash_floor) + '。') + data.note;
  node('action-validation').textContent = [data.validation_note, data.validation_policy?.reason].filter(Boolean).join('。');
  node('action-desktop-status').textContent = [data.desktop_status, desktopTest].filter(Boolean).join('。');
  if (!loadedPrefs && !preferencesDirty) {
    node('execution-mode').value = data.preferences.mode;
    node('native-sound').checked = data.preferences.native_sound;
    loadedPrefs = true;
  }
  const container = node('action-cards');
  const open = new Set([...container.querySelectorAll('details[open]')].map(item => item.dataset.cardCode));
  container.innerHTML = data.cards.map(card => renderCard(card, open.has(card.code))).join('') || '<p>暂无行动卡。</p>';
}

function alertMarkup(alert) {
  const c = alert.closeout;
  const closeout = alert.ledger_notice && c ? '<dl class="action-closeout">' +
    metric('已卖出', quantity(c.sold_quantity)) + metric('已接回', quantity(c.buyback_quantity)) +
    metric('未接回', quantity(c.remaining_quantity)) + metric(c.cash_basis === 'actual' ? '本轮实际净现金流' : '本轮净现金流估算',
      amount(c.net_cash_actual ?? c.net_cash_estimate)) + '</dl><p>' + esc(c.cash_note) + '</p><p>' + esc(c.fee_note) + '</p>' : '';
  return '<article class="strong-alert ' + (alert.side === 'sell' ? 'sell' : alert.side === 'buy' ? 'buy' : 'risk') +
    '" data-alert-id="' + esc(alert.id) + '"><div><strong>' + esc(alert.name) + ' · ' + esc(alert.kind) + '</strong><p>' + esc(alert.reason) +
    '</p>' + closeout + '<span>' + (alert.ledger_notice ? '实际成交账本收尾；本条不依赖行情，不提供买卖指令' : alert.side ? '参考价 ' + price(alert.price) + ' · 核验数量 ' + quantity(alert.quantity) :
      '条件观察，请在行动卡核对条件；当前不提供下单数量') +
    '</span></div><div class="action-buttons"><button data-id="' + esc(alert.id) + '" data-day="' + esc(alert.day) +
    '" data-feedback="seen">已看到</button><button data-id="' + esc(alert.id) + '" data-day="' + esc(alert.day) +
    '" data-feedback="deferred">暂不执行</button>' + (alert.ledger_notice && c ?
      '<button data-close-cycle="' + esc(alert.cycle_id) + '" data-remaining="' + esc(c.remaining_quantity) + '">确认保留现金结束本轮</button>' : '') + (alert.side ?
      '<a href="/trade-reconcile?alert_id=' + encodeURIComponent(alert.id) + '">登记成交</a>' : '') + '</div></article>';
}

function drawAlerts(data, allowSound = true) {
  const now = Date.now();
  const globalFresh = data.deadline == null || (Number.isFinite(data.deadline) && data.deadline > now);
  const active = (data.alerts || []).filter(alert =>
    (alert.ledger_notice || data.valid && globalFresh) &&
    Number.isFinite(alert.deadline ?? data.deadline) && (alert.deadline ?? data.deadline) > now &&
    (alert.ledger_notice || !(data.cards || []).some(card => card.code === alert.code &&
      (card.state === 'paused' || (card.deadline != null && card.deadline <= now)))) &&
    !['seen','deferred'].includes(alert.feedback?.action) &&
    !['seen','deferred','partial'].includes(alert.status) && alert.native);
  if (node('strong-alerts')) node('strong-alerts').innerHTML = active.map(alertMarkup).join('');
  const bottom = node('action-bottom-alert');
  if (bottom) {
    const html = active.length ? '<div class="action-bottom-heading"><b>当前盘中提醒</b><span>共 ' + active.length +
      ' 条</span></div>' + active.map(alertMarkup).join('') + '<small>行情或条件过期即撤销；反馈会同步到行动卡和收盘复盘。</small>' +
      '<div class="action-bottom-sound"><button type="button" data-bottom-sound>' + (sound ? '网页声音已启用 · 试听' : '启用网页声音') +
      '</button><span id="action-bottom-status" role="status"></span></div>' : '';
    if (bottom.innerHTML !== html) bottom.innerHTML = html;
    bottom.hidden = !active.length;
  }
  if (!allowSound || !sound) return;
  const due = active.filter(alert => {
    const previous = notices.get(alert.id) || {count:0, last:0};
    return previous.count < ((alert.repeatable ?? Boolean(alert.side)) ? 3 : 1) && now - previous.last >= 60000;
  });
  if (due.length && beep()) {
    for (const alert of due) notices.set(alert.id, {count:(notices.get(alert.id)?.count || 0) + 1, last:now});
    try { localStorage.setItem('hengshi_strong_alert_counts', JSON.stringify([...notices].slice(-250))); } catch {}
  }
}

function expire(data, now) {
  const elapsed = at => at != null && at <= now;
  let changed = false;
  if (elapsed(data.deadline)) {
    if (!(data.alerts || []).some(a => a.ledger_notice && !elapsed(a.deadline))) {
      resetAlerts('行情、持仓或交易时段核验已过期，等待刷新。');
      return false;
    }
    data.valid = false;
    data.alerts = data.alerts.filter(a => a.ledger_notice);
    for (const card of data.cards) card.deadline = now;
    data.deadline = null;
    changed = true;
  }
  for (const card of data.cards) {
    const cardExpired = elapsed(card.deadline);
    if (cardExpired) {
      changed = true;
      Object.assign(card, {deadline:null, expires_at:null, state:'paused', label:'行情或条件已过期',
        reason:'等待新一轮行情与条件核验。', price:null, quantity:null, cash_after:null, fee:null,
        side:null, alert_id:null, notification:null, blockers:[], quote_at:null});
      for (const threshold of card.thresholds) threshold.distance = null;
    }
    if (card.t && (cardExpired || elapsed(card.t.deadline))) {
      changed = true;
      const {cycle, remaining_quantity, sell_price} = card.t;
      card.t = {cycle, remaining_quantity, sell_price, status:'等待最新分时复核'};
    }
  }
  const remaining = data.alerts.filter(alert => !elapsed(alert.deadline));
  changed ||= remaining.length !== data.alerts.length;
  data.alerts = remaining;
  return changed;
}

async function refresh() {
  if (busy) return;
  busy = true;
  const requested = Date.now(), version = generation;
  try {
    const data = await actionApi('/api/action-center');
    if (version !== generation) return;
    // Anchor the server's remaining TTL to request start so network latency can
    // shorten a card's lifetime but cannot extend it.
    const deadline = at => at ? requested + Date.parse(at) - Date.parse(data.asof) : null;
    data.deadline = deadline(data.expires_at);
    for (const item of [...data.cards, ...data.alerts]) {
      item.deadline = deadline(item.expires_at || data.expires_at);
      if (item.t) item.t.deadline = deadline(item.t.expires_at);
    }
    current = data;
    expire(data, Date.now());
    if (current) {
      drawCards(data);
      drawAlerts(data);
    }
  } catch (error) {
    if (version === generation) resetAlerts('读取失败，暂停提醒。' + error.message);
  } finally {
    busy = false;
  }
}

function checkExpiry() {
  if (!current) return;
  if (expire(current, Date.now()) && current) {
    drawCards(current);
    drawAlerts(current, false);
  }
}

async function review() {
  try {
    const data = await actionApi('/api/t-performance'), summary = data.summary;
    node('t-performance-summary').innerHTML = '<div class="action-metrics"><span>实际配对净收益 <b>' +
      amount(summary.actual_paired_net) + '</b></span><span>费用待补的估算 <b>' +
      amount(summary.estimated_paired_net) + '</b></span><span>已结束且费用完整样本 <b>' +
      esc(summary.closed_actual_samples ?? 0) + '</b></span></div><p>' + (data.notes || []).map(esc).join('；') +
      '</p><p>统计窗口 ' + esc(data.window_start) + ' 至 ' + esc(data.window_end) +
      '。未接回部分的持有对比：' + amount(summary.unmatched_mark_comparison) + '（独立估算，非已实现T收益）。</p>';
  } catch (error) {
    node('t-performance-summary').textContent = '复盘暂不可用：' + error.message;
  }
}

async function feedback(event) {
  const close = event.target.closest('[data-close-cycle]');
  if (close) {
    close.disabled = true;
    generation++;
    try {
      await actionApi('/api/t-cycles/' + encodeURIComponent(close.dataset.closeCycle) + '/close', 'POST',
        {leave_cash:true, remaining_quantity:Number(close.dataset.remaining)});
      resetAlerts('本轮已确认保留现金，等待刷新账本。');
      await refresh();
    } catch (error) {
      if (typeof showToast === 'function') showToast(error.message, true);
      else if (node('action-bottom-status')) node('action-bottom-status').textContent = error.message;
    } finally { close.disabled = false; }
    return;
  }
  const button = event.target.closest('[data-feedback]');
  if (!button) return;
  // Do not let an older, in-flight refresh restore a reminder after feedback.
  generation++;
  button.disabled = true;
  try {
    await actionApi('/api/action-feedback/' + encodeURIComponent(button.dataset.id), 'POST',
      {action:button.dataset.feedback, day:button.dataset.day});
    if (current) {
      current.alerts = current.alerts.filter(alert => alert.id !== button.dataset.id);
      drawAlerts(current, false);
    }
    await refresh();
  } catch (error) {
    if (typeof showToast === 'function') showToast(error.message, true);
    else if (node('action-bottom-status')) node('action-bottom-status').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function soundStatus() {
  const message = !sound ? '网页声音已关闭，可点击试听。' :
    audio.state === 'running' ? '网页声音已启用，有效强提醒最多响 3 次，间隔至少 60 秒。' :
      '网页声音暂不可用，请重新点击试听或检查浏览器的音频权限。';
  if (node('action-sound-status')) node('action-sound-status').textContent = message;
  if (node('action-bottom-status')) node('action-bottom-status').textContent = message;
}

async function enableSound() {
  try {
    if (!audio || audio.state === 'closed') {
      audio = new (window.AudioContext || window.webkitAudioContext)();
      audio.onstatechange = soundStatus;
    }
    await audio.resume();
    sound = true;
    if (!beep()) throw Error('声音暂不可用');
    if (current) drawAlerts(current, false);
    soundStatus();
  } catch {
    sound = false;
    const message = '试听失败，请检查浏览器是否允许播放声音。';
    if (node('action-sound-status')) node('action-sound-status').textContent = message;
    if (node('action-bottom-status')) node('action-bottom-status').textContent = message;
  }
}

function livePolling() {
  refresh();
  setInterval(refresh, 10000);
  setInterval(checkExpiry, 1000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      checkExpiry();
      refresh();
    }
  });
}

function init() {
  let bottom = node('action-bottom-alert');
  if (!bottom) {
    bottom = document.createElement('aside');
    bottom.id = 'action-bottom-alert';
    bottom.className = 'action-bottom-alert';
    bottom.hidden = true;
    bottom.setAttribute('role', 'region');
    bottom.setAttribute('aria-label', '当前盘中提醒');
    bottom.setAttribute('aria-live', 'polite');
    document.body.appendChild(bottom);
  }
  bottom.addEventListener('click', event => {
    if (event.target.closest('[data-bottom-sound]')) enableSound();
    else feedback(event);
  });
  if (!node('action-cards')) {
    livePolling();
    return;
  }
  node('strong-alerts').addEventListener('click', feedback);
  node('action-refresh').addEventListener('click', refresh);
  for (const id of ['execution-mode','native-sound']) node(id).addEventListener('change', () => { preferencesDirty = true; });
  node('save-execution').addEventListener('click', async () => {
    try {
      const result = await actionApi('/api/execution-preferences', 'PUT',
        {mode:node('execution-mode').value, native_sound:node('native-sound').checked});
      preferencesDirty = false;
      loadedPrefs = false;
      showToast(result.message);
      generation++;
      resetAlerts('设置已改变，等待重新核验。');
      await refresh();
    } catch (error) { showToast(error.message, true); }
  });
  node('enable-action-sound').addEventListener('click', enableSound);
  node('mute-action-sound').addEventListener('click', () => { sound = false; soundStatus(); });
  node('test-action-desktop').addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    node('action-desktop-status').textContent = '正在测试桌面通知…';
    try { desktopTest = (await actionApi('/api/action-desktop-test', 'POST')).message; }
    catch (error) { desktopTest = error.message; }
    finally {
      node('action-desktop-status').textContent = [current?.desktop_status, desktopTest].filter(Boolean).join('。');
      button.disabled = false;
    }
  });
  livePolling();
  review();
  setInterval(review, 60000);
}
document.addEventListener('DOMContentLoaded',init);
})();
