'use strict';

const byId = id => document.getElementById(id);
const all = selector => Array.from(document.querySelectorAll(selector));
const selected = new Set();
const rendered = new Map();
const pageNames = ['jobs', 'matching', 'batches', 'activity', 'browser', 'settings'];
const actionNames = {
  greet: '发送自定义问候语',
  start_chat: '首次沟通（使用 BOSS 账号预设招呼）',
  resume: '发送已有附件简历',
};
const modeNames = {dry_run: '演练', live: '真实执行'};
const browserStates = {
  closed: ['浏览器未打开', 'neutral'],
  login_required: ['等待扫码登录', 'attention'],
  verification_required: ['等待人工验证', 'attention'],
  ready: ['浏览器已就绪', 'good'],
  unknown: ['浏览器状态待确认', 'attention'],
};
const statusNames = {
  draft: '待确认', approved: '已确认', queued: '排队中', pending: '待处理',
  running: '运行中', sending: '发送中', collecting: '采集中', paused: '已暂停',
  completed: '已完成', done: '已完成', succeeded: '已完成', finished: '已完成',
  failed: '失败', blocked: '已拦截', skipped: '已跳过', uncertain: '结果待核对',
  needs_review: '结果待核对',
  canceled: '已取消', cancelled: '已取消', interrupted: '已中止', ready: '通过前置检查',
  checked: '通过前置检查', dry_run_ok: '演练通过', dry_run_passed: '演练通过',
  greeted: '问候已发送', chat_started: '已发起沟通', started_chat: '已发起沟通',
  resume_sent: '附件已发送', contacted: '已有沟通',
  dry_run: '已演练', would_send: '通过前置检查', uncontacted: '暂无沟通',
  untouched: '暂无沟通', none: '暂无沟通', unknown: '待确认',
};
const statusTones = {
  approved: 'good', running: 'active', sending: 'active', collecting: 'active',
  completed: 'good', done: 'good', succeeded: 'good', finished: 'good',
  checked: 'good', dry_run_ok: 'good', dry_run_passed: 'good', would_send: 'good',
  greeted: 'good', resume_sent: 'good', paused: 'attention', uncertain: 'attention',
  needs_review: 'attention', blocked: 'attention', failed: 'bad',
};
const taskNames = {
  collect: '岗位采集', match: '简历匹配', batch: '执行清单', run_batch: '执行清单', execute: '执行清单',
};
const eventNames = {
  jobs: '岗位更新', batch: '清单创建', approval: '清单确认', item: '岗位处理结果',
  settings: '设置更新', pause: '全局暂停', resume: '解除全局暂停',
  recovery: '发送结果待核对', quota: '今日执行上限', task: '任务记录',
  collect_started: '采集开始', collect_completed: '采集完成', collect_failed: '采集失败',
  batch_created: '清单已生成', batch_approved: '清单已确认', batch_started: '清单开始执行',
  batch_completed: '清单执行结束', batch_paused: '清单已暂停', batch_failed: '清单执行失败',
  item_processed: '岗位处理结果', settings_updated: '设置已更新', imported: '岗位已导入',
  task_started: '任务开始', task_completed: '任务完成', task_failed: '任务失败',
  paused: '全局暂停', resumed: '解除全局暂停', browser_opened: '浏览器已打开',
};
const dateTime = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
});

let currentPage = 'jobs';
let status = null;
let jobs = [];
let batches = [];
let activeBatch = null;
let connection = false;
let mutating = false;
let refreshing = false;
let settingsInitialized = false;
let settingsDirty = false;
let screenshotUrl = null;
let jobsRequest = 0;
let searchTimer;

function el(tag, value, className) {
  const node = document.createElement(tag);
  if (value !== undefined) node.textContent = value;
  if (className) node.className = className;
  return node;
}

function button(label, onClick, className = 'link-button') {
  const node = el('button', label, className);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function text(id, value) {
  byId(id).textContent = value;
}

function formatTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : dateTime.format(date);
}

function readableDetail(value, fallback = '请查看当前状态后重试。') {
  if (typeof value !== 'string' || !value.trim()) return fallback;
  if (/traceback|playwright[.:]|call log:|internal server error|validation error|\bBearer\b|\bException\b|at .+\.py:\d+/i.test(value)) {
    return fallback;
  }
  return value.trim().slice(0, 360);
}

function showNotice(value, tone = 'neutral', target = 'notice') {
  const notice = byId(target);
  notice.textContent = value;
  notice.dataset.tone = tone;
  notice.hidden = !value;
}

function statusLabel(value) {
  return statusNames[value] ?? '状态待确认';
}

function badge(value) {
  const node = el('span', statusLabel(value), 'badge');
  node.dataset.tone = statusTones[value] ?? 'neutral';
  return node;
}

function setConnection(connected) {
  connection = connected;
  const node = byId('connection-state');
  node.textContent = connected ? '本地服务已连接' : '本地服务暂不可用';
  node.dataset.tone = connected ? 'good' : 'attention';
  updateControls();
}

async function api(path, options = {}) {
  let response;
  try {
    const headers = {'Authorization': `Bearer ${API_TOKEN}`};
    const init = {headers, cache: 'no-store', redirect: 'error'};
    if (options.method) {
      init.method = options.method;
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(options.body ?? {});
    }
    response = await fetch(`/api${path}`, init);
  } catch {
    throw new Error('无法连接本地服务，请检查服务是否在运行。');
  }
  if (response.ok) {
    return options.blob ? response.blob() : response.json();
  }
  if (response.status === 401 || response.status === 403) {
    throw new Error('访问校验失败，请重新打开工作台。');
  }
  if (response.status >= 500) {
    throw new Error('本地服务处理失败。请刷新状态后再决定是否重试。');
  }
  const problem = await response.json().catch(() => ({}));
  throw new Error(readableDetail(problem.detail, '请求未完成，请检查填写内容或刷新状态。'));
}

function post(path, body) {
  return api(path, {method: 'POST', body});
}

function updateControls() {
  const unavailable = !connection || mutating;
  const taskRunning = Boolean(status?.active_task);
  const browserStatus = status?.browser?.state ?? 'closed';
  const isPaused = Boolean(status?.paused);

  byId('pause-button').disabled = !status || unavailable;
  text('pause-button', isPaused ? '解除暂停' : '暂停执行');
  byId('collect-button').disabled = !status || unavailable || taskRunning || isPaused || browserStatus !== 'ready';
  byId('collect-button').title = taskRunning ? '请等待当前任务结束。' :
    isPaused ? '请先解除全局暂停。' :
      browserStatus !== 'ready' ? '请先打开 BOSS 浏览器并登录。' : '';
  byId('browser-open').disabled = unavailable || taskRunning;
  byId('screenshot-button').disabled = unavailable || browserStatus === 'closed';
  byId('match-run').disabled = unavailable || taskRunning || isPaused;
  byId('settings-save').disabled = unavailable || !settingsInitialized || !settingsDirty;
  byId('draft-button').disabled = unavailable || !selected.size;
  byId('draft-submit').disabled = unavailable;
  byId('clear-selection').disabled = !selected.size;
  updateBatchControls();
}

function renderStatus(value) {
  status = value;
  const browser = value.browser ?? {};
  const [label, tone] = browserStates[browser.state] ?? browserStates.unknown;
  byId('browser-dot').dataset.tone = tone;
  text('browser-state', label);
  byId('browser-badge').dataset.tone = tone;
  text('browser-badge', label);
  text('browser-message', readableDetail(browser.message, '请在独立的 BOSS 浏览器窗口中操作。'));

  const running = value.active_task;
  const summary = running ? `${taskNames[running.kind] ?? '任务'}运行中` : '当前无运行任务';
  text('run-state', value.paused ? `执行已暂停 · ${summary}` : summary);
  const dailyLimit = value.settings?.daily_limit;
  text('quota', Number.isInteger(value.daily_used) && Number.isInteger(dailyLimit)
    ? `${value.daily_used} / ${dailyLimit}` : '—');
  if (Number.isInteger(value.counts?.jobs)) text('job-count', value.counts.jobs);
  const cities = value.settings?.cities ?? [];
  text('active-cities', cities.length ? cities.join('、') : '—');
  if (value.settings && !settingsDirty) fillSettings(value.settings);
  if (browser.state === 'closed') clearScreenshot();
  updateControls();
}

function clearScreenshot() {
  if (!screenshotUrl) return;
  URL.revokeObjectURL(screenshotUrl);
  screenshotUrl = null;
  byId('browser-screenshot').removeAttribute('src');
  byId('screenshot-frame').hidden = true;
  byId('screenshot-empty').hidden = false;
}

function fillSettings(settings) {
  const next = JSON.stringify(settings);
  if (rendered.get('settings') === next) return;
  rendered.set('settings', next);
  for (const input of all('#settings-form input[name="cities"]')) {
    input.checked = settings.cities.includes(input.value);
  }
  byId('keywords').value = settings.keywords.join('\n');
  byId('exclude-companies').value = settings.exclude_companies.join('\n');
  byId('collect-limit').value = settings.collect_limit;
  byId('daily-limit').value = settings.daily_limit;
  byId('interval-seconds').value = settings.interval_seconds;
  byId('settings-fields').disabled = false;
  byId('execution-fields').disabled = false;
  settingsInitialized = true;
}

function listEmpty(items, prefix, message) {
  byId(`${prefix}-empty`).hidden = Boolean(items.length);
  if (!items.length) text(`${prefix}-empty`, message);
  byId(`${prefix}-table-wrap`).hidden = !items.length;
}

function unchanged(key, value) {
  const next = JSON.stringify(value);
  if (rendered.get(key) === next) return true;
  rendered.set(key, next);
  return false;
}

async function loadJobs() {
  const requestId = ++jobsRequest;
  const params = new URLSearchParams();
  const query = byId('job-query').value.trim();
  const city = byId('job-city').value;
  if (query) params.set('q', query);
  if (city) params.set('city', city);
  const response = await api(`/jobs${params.size ? `?${params}` : ''}`);
  if (requestId !== jobsRequest) return;
  if (!params.size && !Number.isInteger(status?.counts?.jobs)) text('job-count', response.length);
  const ineligible = new Set(response.filter(job => !job.eligible).map(job => job.id));
  const found = new Set(response.map(job => job.id));
  const selectionBeforeUpdate = selected.size;
  for (const id of selected) {
    if (ineligible.has(id) || (!params.size && !found.has(id))) selected.delete(id);
  }
  jobs = response;
  if (!unchanged('jobs', [query, city, jobs])) {
    text('job-result-count', `${jobs.length} 个结果`);
    listEmpty(jobs, 'jobs', params.size
      ? '没有符合搜索条件的岗位。试试切换城市或清空搜索词。'
      : '岗位库还没有记录。打开 BOSS 浏览器并登录后，即可采集岗位。');
    byId('jobs-rows').replaceChildren(...jobs.map(jobRow));
    if (!jobs.length && !params.size) {
      byId('jobs-empty').append(button('打开浏览器页面', () => navigate('browser'), 'button small'));
    }
  }
  if (selected.size < selectionBeforeUpdate) {
    showNotice('已从选择中移除不再符合筛选条件的岗位。', 'attention');
  }
  renderSelection();
}

function jobRow(job) {
  const row = el('tr');
  const pick = el('td', undefined, 'pick-col');
  const input = el('input');
  input.type = 'checkbox';
  input.dataset.jobId = job.id;
  input.setAttribute('aria-label', `选择 ${job.title} · ${job.company}`);
  input.disabled = !job.eligible;
  input.addEventListener('change', () => {
    if (input.checked && selected.size >= 100) {
      showNotice('每份清单最多选 100 个岗位。', 'attention');
    } else if (input.checked) {
      selected.add(job.id);
    } else {
      selected.delete(job.id);
    }
    renderSelection();
  });
  pick.append(input);
  const role = el('td', undefined, 'role-cell');
  role.append(button(job.title || '查看岗位详情', () => openJob(job), 'link-button row-title'));
  role.append(el('div', job.company || '公司待确认', 'muted'));
  const compensation = el('td');
  compensation.append(el('div', job.salary || '—'), el('div', job.experience || '—', 'muted'));
  const eligibility = el('td', undefined, 'eligibility-cell');
  const label = el('span', job.eligible ? '可选' : '不可选', 'badge');
  label.dataset.tone = job.eligible ? 'good' : 'neutral';
  eligibility.append(label);
  if (job.filter_reason) eligibility.append(el('div', job.filter_reason, 'muted filter-reason'));
  if (job.contact_status && !['none', 'uncontacted', 'untouched', '未执行'].includes(job.contact_status)) {
    eligibility.append(el('div', statusLabel(job.contact_status), 'muted'));
  }
  row.append(pick, role, el('td', job.city || '待确认'), compensation, eligibility);
  return row;
}

function renderSelection() {
  const eligible = jobs.filter(job => job.eligible);
  const visiblePicked = eligible.filter(job => selected.has(job.id)).length;
  const hiddenCount = selected.size - visiblePicked;
  text('selection-count', `已选 ${selected.size} 个岗位${hiddenCount ? ` · ${hiddenCount} 个在当前筛选之外` : ''}`);
  const selectVisible = byId('select-visible');
  selectVisible.disabled = !eligible.length;
  selectVisible.checked = Boolean(eligible.length) && visiblePicked === eligible.length;
  selectVisible.indeterminate = visiblePicked > 0 && visiblePicked < eligible.length;
  for (const input of all('#jobs-rows input[data-job-id]')) {
    input.checked = selected.has(Number(input.dataset.jobId));
  }
  updateControls();
}

function setVisibleSelection(event) {
  let limitReached = false;
  for (const job of jobs.filter(job => job.eligible)) {
    if (!event.target.checked) {
      selected.delete(job.id);
    } else if (selected.size < 100) {
      selected.add(job.id);
    } else if (!selected.has(job.id)) {
      limitReached = true;
    }
  }
  if (limitReached) showNotice('每份清单最多选 100 个岗位。', 'attention');
  renderSelection();
}

function sourceLink(raw) {
  if (typeof raw !== 'string') return null;
  try {
    const url = new URL(raw);
    if (url.origin !== 'https://www.zhipin.com' || url.username || url.password ||
      !url.pathname.startsWith('/job_detail/')) return null;
    const link = el('a', '查看 BOSS 原始岗位', 'button source-link');
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  } catch {
    return null;
  }
}

function fields(entries) {
  const node = el('dl', undefined, 'fields');
  for (const [label, value] of entries) {
    const group = el('div');
    group.append(el('dt', label), el('dd', value || '—'));
    node.append(group);
  }
  return node;
}

function jobContent(job, {includeFilter = false} = {}) {
  const section = el('div', undefined, 'job-content');
  section.append(fields([
    ['招聘者', job.recruiter], ['城市', job.city], ['薪资', job.salary],
    ['经验', job.experience], ['学历', job.education],
    ['采集时间', job.collected_at ? formatTime(job.collected_at) : ''],
  ]));
  if (includeFilter && job.filter_reason) section.append(el('p', job.filter_reason, 'callout'));
  if (job.tags?.length) {
    const tags = el('ul', undefined, 'tags');
    tags.append(...job.tags.map(tag => el('li', tag, 'badge')));
    section.append(tags);
  }
  section.append(el('h3', '职位详情'), el('div', job.description || '本次记录中没有职位描述。', 'long-text'));
  const link = sourceLink(job.url);
  if (link) section.append(link);
  return section;
}

function openJob(job) {
  text('job-dialog-title', job.title || '岗位详情');
  text('job-dialog-company', job.company || '公司待确认');
  byId('job-dialog-content').replaceChildren(jobContent(job, {includeFilter: true}));
  byId('job-dialog').showModal();
}

async function loadBatches() {
  batches = await api('/batches');
  if (!unchanged('batches', batches)) {
    listEmpty(batches, 'batches', '还没有执行清单。到岗位库选中候选岗位，即可生成清单。');
    const rows = batches.map(batch => {
      const row = el('tr');
      const title = el('td');
      title.append(button(`清单 #${batch.id}`, () => openBatch(batch), 'link-button row-title'));
      const stateCell = el('td');
      stateCell.append(badge(batch.status));
      row.append(title, el('td', actionNames[batch.action] ?? '—'), el('td', modeNames[batch.mode] ?? '—'),
        el('td', batch.items.length), stateCell, el('td', formatTime(batch.created_at)));
      return row;
    });
    byId('batches-rows').replaceChildren(...rows);
    if (!batches.length) {
      byId('batches-empty').append(button('前往岗位库', () => navigate('jobs'), 'button small'));
    }
  }
  if (byId('batch-dialog').open && activeBatch) {
    const latest = batches.find(batch => batch.id === activeBatch.id);
    if (latest) renderBatch(latest);
  }
}

function openBatch(batch) {
  activeBatch = null;
  rendered.delete('activeBatch');
  byId('live-ack').checked = false;
  showNotice('', 'neutral', 'batch-notice');
  renderBatch(batch);
  if (!byId('batch-dialog').open) byId('batch-dialog').showModal();
}

function renderBatch(batch) {
  if (activeBatch?.digest !== batch.digest) byId('live-ack').checked = false;
  activeBatch = batch;
  if (!unchanged('activeBatch', batch)) {
    const openItems = new Set(all('#batch-details .batch-item[open]').map(item => item.dataset.itemId));
    text('batch-dialog-title', `执行清单 #${batch.id}`);
    text('batch-dialog-caption', `${batch.items.length} 个岗位 · ${statusLabel(batch.status)} · ${formatTime(batch.created_at)}`);
    const group = fields([
      ['执行动作', actionNames[batch.action]],
      ['模式', modeNames[batch.mode]],
    ]);
    const exact = el('div', undefined, 'exact-content');
    if (batch.action === 'start_chat') {
      exact.append(el('p', byId('start-chat-help').textContent, 'callout'));
    } else {
      const exactLabel = batch.action === 'greet' ? '问候内容' : '附件简历名称';
      const value = batch.action === 'greet' ? batch.message : batch.resume_name;
      exact.append(el('h3', exactLabel), el('div', value || '—', 'long-text'));
    }
    const heading = el('h3', '岗位与招聘者快照');
    const items = el('div', undefined, 'batch-items');
    items.append(...batch.items.map(item => batchItem(item, openItems.has(String(item.id)))));
    byId('batch-details').replaceChildren(group, exact, heading, items);
  }
  updateBatchControls();
}

function batchItem(item, open) {
  const job = item.job;
  const panel = el('details', undefined, 'batch-item');
  panel.dataset.itemId = item.id;
  panel.open = open;
  const summary = el('summary');
  const role = el('span', undefined, 'batch-role');
  role.append(el('strong', job.title || '岗位待确认'),
    el('span', [job.company, job.city, job.recruiter || '招聘者待确认'].filter(Boolean).join(' · '), 'muted'));
  summary.append(role, badge(item.status));
  panel.append(summary);
  if (item.detail) panel.append(el('p', readableDetail(item.detail, '请在任务记录中核对进度。'), 'item-detail'));
  panel.append(jobContent(job));
  return panel;
}

function updateBatchControls() {
  if (!activeBatch) return;
  const draft = activeBatch.status === 'draft';
  const live = activeBatch.mode === 'live';
  const runnable = ['approved', 'paused'].includes(activeBatch.status);
  const browserReady = status?.browser?.state === 'ready';
  byId('live-ack-label').hidden = !(draft && live);
  byId('batch-approve').hidden = !draft;
  byId('batch-approve').disabled = !connection || mutating || (live && !byId('live-ack').checked);
  byId('batch-run').hidden = !runnable;
  byId('batch-run').disabled = !connection || mutating || !browserReady || Boolean(status?.paused || status?.active_task);
  text('batch-approve', live ? '确认真实清单' : '确认演练清单');
  text('batch-run', live ? '开始真实执行' : '开始演练');
  let note = '';
  if (runnable) {
    if (status?.paused) note = '运行前请先在工作台解除全局暂停。';
    else if (status?.active_task) note = '请等待当前任务结束。';
    else if (!browserReady) note = '请先在 BOSS 浏览器登录，等待浏览器就绪。';
    else note = live ? '开始后将按已确认内容执行。' : '演练只检查发送条件。';
  } else if (draft) {
    note = live ? '勾选前请核对招聘者与发送内容。' : '确认后即可启动演练。';
  }
  text('batch-action-note', note);
  byId('batch-footer').hidden = !draft && !runnable;
}

function renderActivity(tasks, events) {
  if (!unchanged('tasks', tasks)) {
    listEmpty(tasks, 'tasks', '暂无任务。可到岗位库发起采集，或运行已确认的清单。');
    const rows = tasks.map(task => {
      const row = el('tr');
      const state = el('td');
      state.append(badge(task.status));
      row.append(el('td', taskNames[task.kind] ?? '工作台任务'), state,
        el('td', readableDetail(task.detail, '—')), el('td', formatTime(task.created_at)));
      return row;
    });
    byId('tasks-rows').replaceChildren(...rows);
  }
  if (!unchanged('events', events)) {
    const list = byId('events-list');
    byId('events-empty').hidden = Boolean(events.length);
    if (!events.length) text('events-empty', '暂无服务事件。');
    list.hidden = !events.length;
    const items = events.map(event => {
      const item = el('li');
      const note = el('div');
      note.append(el('strong', eventNames[event.kind] ?? '工作台事件'));
      if (event.detail) note.append(el('div', readableDetail(event.detail, '详情暂不可用。'), 'muted'));
      item.append(el('time', formatTime(event.created_at)), note);
      return item;
    });
    list.replaceChildren(...items);
  }
}

function visiblePageData() {
  if (currentPage === 'jobs') return loadJobs();
  if (currentPage === 'matching') return loadMatching();
  if (currentPage === 'batches') return loadBatches();
  if (currentPage === 'activity') {
    return Promise.all([api('/tasks'), api('/events?limit=100')]).then(([tasks, events]) => renderActivity(tasks, events));
  }
  if (currentPage === 'settings' && !settingsInitialized) {
    return api('/settings').then(settings => {
      if (!settingsDirty) fillSettings(settings);
      updateControls();
    });
  }
}

async function loadStatus() {
  try {
    renderStatus(await api('/status'));
    setConnection(true);
  } catch (error) {
    setConnection(false);
    throw error;
  }
}

async function refresh({showErrors = true} = {}) {
  if (refreshing) return;
  refreshing = true;
  try {
    await loadStatus();
    await visiblePageData();
  } catch (error) {
    if (showErrors) showNotice(error.message, 'bad');
  } finally {
    refreshing = false;
  }
}

async function mutate(operation, target = 'notice') {
  if (mutating) return;
  mutating = true;
  updateControls();
  showNotice('', 'neutral', target);
  try {
    await operation();
  } catch (error) {
    showNotice(error.message, 'bad', target);
  } finally {
    mutating = false;
    updateControls();
  }
}

function navigate(page, {moveFocus = true, load = true} = {}) {
  currentPage = page;
  for (const name of pageNames) byId(`page-${name}`).hidden = name !== page;
  for (const nav of all('[data-page]')) {
    if (nav.dataset.page === page) nav.setAttribute('aria-current', 'page');
    else nav.removeAttribute('aria-current');
  }
  if (moveFocus) byId(`${page}-heading`).focus();
  if (load) Promise.resolve(visiblePageData()).catch(error => showNotice(error.message, 'bad'));
}

function updateDraftFields() {
  const action = byId('draft-action').value;
  const greet = action === 'greet';
  const resume = action === 'resume';
  byId('message-field').hidden = !greet;
  byId('draft-message').disabled = !greet;
  byId('draft-message').required = greet;
  byId('resume-field').hidden = !resume;
  byId('draft-resume').disabled = !resume;
  byId('draft-resume').required = resume;
  byId('start-chat-help').hidden = action !== 'start_chat';
}

async function createBatch() {
  const action = byId('draft-action').value;
  const message = action === 'greet' ? byId('draft-message').value : '';
  const resumeName = action === 'resume' ? byId('draft-resume').value : '';
  if (action === 'greet' && !message.trim()) throw new Error('请填写完整的问候语。');
  if (action === 'resume' && !resumeName.trim()) throw new Error('请填写已有附件简历的名称。');
  const mode = new FormData(byId('draft-form')).get('mode');
  const batch = await post('/batches', {
    job_ids: Array.from(selected), action, message, resume_name: resumeName, mode,
  });
  byId('draft-dialog').close();
  selected.clear();
  renderSelection();
  navigate('batches', {load: false});
  openBatch(batch);
  await loadBatches().catch(error => showNotice(error.message, 'bad', 'batch-notice'));
}

function lines(id) {
  return byId(id).value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
}

async function saveSettings() {
  const cities = all('#settings-form input[name="cities"]:checked').map(input => input.value);
  const keywords = lines('keywords');
  if (!cities.length) throw new Error('请选择至少一个采集城市。');
  if (!keywords.length) throw new Error('请填写至少一个关键词。');
  const excluded = lines('exclude-companies');
  if (keywords.length > 20 || keywords.some(value => value.length > 100)) {
    throw new Error('最多填写 20 个关键词，每个关键词最多 100 个字符。');
  }
  if (excluded.length > 100 || excluded.some(value => value.length > 100)) {
    throw new Error('最多排除 100 家公司，每个名称最多 100 个字符。');
  }
  const settings = await api('/settings', {method: 'PUT', body: {
    cities, keywords, exclude_companies: excluded,
    daily_limit: byId('daily-limit').valueAsNumber,
    interval_seconds: byId('interval-seconds').valueAsNumber,
    collect_limit: byId('collect-limit').valueAsNumber,
  }});
  settingsDirty = false;
  rendered.delete('settings');
  fillSettings(settings);
  showNotice('设置已保存。', 'good');
  await loadStatus();
}

byId('browser-shortcut').addEventListener('click', () => navigate('browser'));
for (const navigation of all('[data-page], [data-goto]')) {
  navigation.addEventListener('click', () => navigate(navigation.dataset.page ?? navigation.dataset.goto));
}
for (const control of all('[data-close]')) {
  control.addEventListener('click', () => {
    if (!mutating) byId(control.dataset.close).close();
  });
}
for (const dialog of all('dialog')) {
  dialog.addEventListener('cancel', event => {
    if (mutating) event.preventDefault();
  });
}
byId('refresh-button').addEventListener('click', () => {
  showNotice('');
  refresh();
});
byId('pause-button').addEventListener('click', () => mutate(async () => {
  const result = await post(status.paused ? '/resume' : '/pause');
  showNotice(result.paused ? '已暂停后续执行。' : '已解除全局暂停。', 'good');
  await loadStatus();
}));
byId('collect-button').addEventListener('click', () => mutate(async () => {
  await post('/collect');
  showNotice('采集任务已提交，进度可在「任务与活动」查看。');
  await refresh();
}));
byId('job-filters').addEventListener('submit', event => {
  event.preventDefault();
  clearTimeout(searchTimer);
  loadJobs().catch(error => showNotice(error.message, 'bad'));
});
byId('job-query').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadJobs().catch(error => showNotice(error.message, 'bad')), 220);
});
byId('job-city').addEventListener('change', () => {
  clearTimeout(searchTimer);
  loadJobs().catch(error => showNotice(error.message, 'bad'));
});
byId('select-visible').addEventListener('change', setVisibleSelection);
byId('clear-selection').addEventListener('click', () => {
  selected.clear();
  renderSelection();
});
byId('draft-button').addEventListener('click', () => {
  text('draft-count', `${selected.size} 个候选岗位。生成后可逐项检查清单快照。`);
  byId('draft-form').reset();
  updateDraftFields();
  showNotice('', 'neutral', 'draft-notice');
  byId('draft-dialog').showModal();
});
byId('draft-action').addEventListener('change', updateDraftFields);
byId('draft-form').addEventListener('submit', event => {
  event.preventDefault();
  mutate(createBatch, 'draft-notice');
});
byId('live-ack').addEventListener('change', updateBatchControls);
byId('batch-approve').addEventListener('click', () => mutate(async () => {
  const {id, digest} = activeBatch;
  const result = await post(`/batches/${id}/approve`, {
    digest, acknowledge_live: byId('live-ack').checked,
  });
  renderBatch(result);
  showNotice('已确认清单。请点击开始按钮启动任务。', 'good', 'batch-notice');
  await loadBatches();
}, 'batch-notice'));
byId('batch-run').addEventListener('click', () => mutate(async () => {
  const batch = activeBatch;
  await post(`/batches/${batch.id}/run`);
  showNotice('执行任务已提交，结果会更新到清单。', 'neutral', 'batch-notice');
  await loadStatus();
  await loadBatches();
}, 'batch-notice'));
byId('browser-open').addEventListener('click', () => mutate(async () => {
  await post('/browser/open');
  await loadStatus();
}));
byId('screenshot-button').addEventListener('click', () => mutate(async () => {
  const image = await api('/browser/screenshot', {blob: true});
  clearScreenshot();
  screenshotUrl = URL.createObjectURL(image);
  byId('browser-screenshot').src = screenshotUrl;
  byId('screenshot-empty').hidden = true;
  byId('screenshot-frame').hidden = false;
  text('screenshot-time', `获取于 ${formatTime(new Date())}`);
}));
byId('settings-form').addEventListener('input', () => {
  settingsDirty = true;
  updateControls();
});
byId('settings-form').addEventListener('submit', event => {
  event.preventDefault();
  mutate(saveSettings);
});
const resumeFields = {skills: '技能', projects: '项目', duties: '职责', outcomes: '成果', experience: '工作年限', education: '学历'};
const matchTier = {priority: '优先看', consider: '可考虑', verify: '需核对'};
const evidenceState = {supported: '有证据', partial: '部分相关', unknown: '待确认', mismatch: '明确不符'};
const evidenceKind = {core: '核心职责', must: '必备条件', bonus: '加分项'};
let matchReady = false;
let matchDirty = false;

function fillMatchProfile(profile) {
  byId('match-profile').reset();
  byId('match-resume').value = profile?.text ?? '';
  byId('match-consent').checked = Boolean(profile?.external_consent);
  byId('match-exclusions').value = (profile?.exclude_companies ?? []).join('\n');
  byId('match-salary').value = profile?.min_salary_k ?? '';
  byId('match-outsourcing').value = profile?.outsourcing ?? 'verify';
  for (const checkbox of all('[name="match-cities"]')) {
    checkbox.checked = (profile?.cities ?? ['杭州', '上海']).includes(checkbox.value);
  }
  for (const name of Object.keys(resumeFields)) {
    byId(`match-field-${name}`).value = (profile?.fields?.[name] ?? []).map(row => row.value).join('\n');
  }
  byId('match-fields').open = Boolean(profile);
  matchReady = true;
  matchDirty = false;
}

async function saveMatchProfile() {
  const profile = await api('/matching/profile', {method: 'PUT', body: {
    text: byId('match-resume').value,
    external_consent: byId('match-consent').checked,
    fields: Object.fromEntries(Object.keys(resumeFields).map(name => [name, lines(`match-field-${name}`)])),
    cities: all('[name="match-cities"]:checked').map(input => input.value),
    exclude_companies: lines('match-exclusions'),
    min_salary_k: byId('match-salary').value ? byId('match-salary').valueAsNumber : null,
    outsourcing: byId('match-outsourcing').value,
  }});
  fillMatchProfile(profile);
  const names = Object.entries(profile.fields).flatMap(([key, rows]) => rows.some(row => row.source === 'user') ? [resumeFields[key]] : []);
  const note = names.length ? `。${names.join('、')}中未匹配原文的片段已记为用户补充` : '';
  showNotice(`简历与偏好已保存${note}`, 'good');
  await loadMatching();
}

function renderMatching(result) {
  const signature = JSON.stringify(result);
  if (rendered.get('matching') === signature) return;
  rendered.set('matching', signature);
  const rows = [];
  for (const job of result?.jobs ?? []) {
    const detail = el('details');
    detail.append(el('summary', `${matchTier[job.tier]} · ${job.title} · ${job.company} · ${job.city || '城市待核对'} · ${job.salary || '薪资待核对'}`));
    const source = el('a', 'BOSS 岗位原页');
    source.href = job.url;
    source.target = '_blank';
    source.rel = 'noopener noreferrer';
    detail.append(source, el('p', `采集于 ${formatTime(job.collected_at)} · ${statusLabel(job.contact_status)}`));
    if (job.stale) detail.append(el('p', '岗位有变更，请重新匹配。'));
    for (const check of job.checks) detail.append(el('p', check));
    for (const item of job.evidence) {
      detail.append(el('p', `${evidenceKind[item.kind]} · ${evidenceState[item.status]}：${item.jd_excerpt}`));
      if (item.resume_excerpt) detail.append(el('blockquote', item.resume_excerpt));
    }
    rows.push(detail);
  }
  for (const failure of result?.errors ?? []) rows.push(el('p', `${failure.title} · ${failure.reason}`));
  if (result?.unprocessed) rows.push(el('p', `还有 ${result.unprocessed} 条未评估，恢复后可重新匹配。`));
  byId('match-results').replaceChildren(...rows);
  const stale = result?.stale ? '简历或偏好有变更，请重新匹配。' : '';
  text('match-summary', result ? `${stale}基于已有岗位，${result.jobs.length} 条推荐 · ${formatTime(result.created_at)}` : '保存简历后即可开始匹配。');
}

async function loadMatching() {
  if (!matchReady && !matchDirty) {
    const profile = await api('/matching/profile');
    if (!matchDirty && !matchReady) fillMatchProfile(profile);
  }
  renderMatching(await api('/matching/latest'));
}

function setupMatching() {
  for (const [key, label] of Object.entries(resumeFields)) {
    const field = el('label', `${label}原文，每行一个片段`);
    const input = el('textarea');
    input.id = `match-field-${key}`;
    input.rows = 2;
    field.append(input);
    byId('match-field-list').append(field);
  }
  byId('match-profile').addEventListener('input', () => { matchDirty = true; });
  byId('match-profile').addEventListener('submit', event => {
    event.preventDefault();
    mutate(saveMatchProfile);
  });
  byId('match-extract').addEventListener('click', () => mutate(async () => {
    const result = await post('/matching/extract', {text: byId('match-resume').value, external_consent: byId('match-consent').checked});
    for (const name of Object.keys(resumeFields)) byId(`match-field-${name}`).value = (result.fields[name] ?? []).join('\n');
    matchDirty = true;
    byId('match-fields').open = true;
    showNotice('请核对并保存提取结果。', 'good');
  }));
  byId('match-run').addEventListener('click', () => mutate(async () => {
    if (!matchReady || matchDirty) throw new Error('请先保存当前简历与偏好。');
    await post('/matching/run');
    showNotice('匹配任务已提交，可在「任务与活动」查看进度。');
    await loadStatus();
  }));
  byId('match-delete').addEventListener('click', () => {
    if (!window.confirm('删除本机简历及关联推荐？')) return;
    mutate(async () => {
      await api('/matching/profile', {method: 'DELETE'});
      fillMatchProfile(null);
      renderMatching(null);
      showNotice('简历与推荐已删除。', 'good');
    });
  });
}

// Match controls share the same refresh and mutation lifecycle.
setupMatching();
window.addEventListener('pagehide', clearScreenshot);

refresh();
setInterval(() => {
  if (!document.hidden && !mutating) refresh({showErrors: false});
}, 4000);
