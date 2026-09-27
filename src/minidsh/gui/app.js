'use strict';

const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) {
  sessionStorage.setItem('minidsh-session-token', fragment.get('token'));
  history.replaceState(null, '', '/');
}
const token = sessionStorage.getItem('minidsh-session-token') || '';
window.addEventListener('hashchange', () => {
  if (new URLSearchParams(location.hash.slice(1)).has('token')) location.reload();
});

let current = null;
let pending = false;
let lastMessages = '';
let lastEvents = '';
let lastAgents = '';
let diffRequested = false;
const statuses = {
  idle: '等待任务', running: '正在执行', finished: '运行完成',
  failed: '运行失败', cancelled: '已取消', budget_exhausted: '预算耗尽'
};

async function api(path, data) {
  const response = await fetch('/api/' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'X-MiniDSH-Token': token, 'Content-Type': 'application/json'},
    ...(data === undefined ? {} : {body: JSON.stringify(data)})
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(response.status === 403
      ? '连接凭证无效，请使用终端输出的完整 GUI 地址打开页面。'
      : '操作未完成：请检查输入，并等待当前任务结束。');
  }
  return result;
}

function setText(id, value) { $(id).textContent = value; }
function showError(message) {
  setText('error', message || '');
  $('error').hidden = !message;
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}
function setView(view) {
  $('main').dataset.view = view;
  const chat = view === 'chat';
  $('conversation').hidden = !chat;
  $('trajectory').hidden = chat;
  for (const [id, selected] of [['conversation-tab', chat], ['trace-tab', !chat]]) {
    $(id).classList.toggle('active', selected);
    $(id).setAttribute('aria-selected', String(selected));
    $(id).tabIndex = selected ? 0 : -1;
  }
}
function toggleSuggestions(open) {
  $('suggestions').hidden = !open;
  $('prompt-menu').setAttribute('aria-expanded', String(open));
}
function controls() {
  const busy = pending || !!current?.busy;
  for (const id of ['clear', 'resume', 'diff', 'open-resume']) $(id).disabled = busy;
  $('send').disabled = busy || !current || !$('task').value.trim();
  $('cancel').hidden = !current?.busy;
  $('cancel').disabled = pending;
  $('send').hidden = !!current?.busy;
  for (const button of document.querySelectorAll('[data-task]')) button.disabled = busy;
  const editable = !!current?.collaboration?.editable;
  for (const id of ['multi-agent', 'max-agents', 'max-parallel', 'save-settings']) $(id).disabled = busy || !editable;
  $('api-key').disabled = busy || !current?.credentials?.editable;
}
function fillSettings() {
  $('multi-agent').checked = !!current?.multi_agent_enabled;
  $('max-agents').value = current?.collaboration?.max_agents || 4;
  $('max-parallel').value = current?.collaboration?.max_parallel || 2;
}
function openSettings() {
  fillSettings();
  $('api-key').value = '';
  setText('settings-feedback', '');
  $('settings-dialog').showModal();
}

function renderAgents(state) {
  const agents = state.sub_agents || [];
  $('agent-board').hidden = !agents.length;
  setText('agent-count', agents.filter(agent => agent.status === 'finished').length + ' / ' + agents.length + ' 完成');
  const serialized = JSON.stringify(agents);
  if (serialized === lastAgents) return;
  lastAgents = serialized;
  const container = $('sub-agents');
  const expanded = new Set([...container.querySelectorAll('details[open]')].map(item => item.dataset.run));
  container.replaceChildren();
  for (const agent of agents) {
    const card = document.createElement('details');
    card.className = 'agent-card';
    card.dataset.run = agent.run_id;
    card.open = expanded.has(agent.run_id);
    const summary = document.createElement('summary');
    for (const [className, value] of [['agent-name', agent.name], ['agent-role', agent.role], ['agent-status', statuses[agent.status] || agent.status]]) {
      const label = document.createElement('span');
      label.className = className;
      label.textContent = value;
      summary.append(label);
    }
    const answer = document.createElement('pre');
    answer.textContent = agent.error || agent.answer || '等待子 Agent 返回结果…';
    const run = document.createElement('code');
    run.textContent = agent.run_id + ' · ' + agent.tokens + ' tokens';
    card.append(summary, answer, run);
    container.append(card);
  }
}

function renderMessages(state) {
  const messages = JSON.stringify(state.messages);
  if (messages === lastMessages) return;
  lastMessages = messages;
  const container = $('messages');
  container.replaceChildren();
  if (state.messages_truncated) {
    const note = document.createElement('p');
    note.className = 'truncated';
    note.textContent = '仅显示最近 100 条消息，完整记录保存在 Session 与 Trace 中。';
    container.append(note);
  }
  for (const message of state.messages) {
    const row = document.createElement('article');
    row.className = 'message ' + message.role;
    const body = document.createElement('div');
    body.className = 'message-content';
    if (message.role !== 'user') {
      const label = document.createElement('span');
      label.className = 'message-label';
      label.append(icon('terminal'), document.createTextNode('Mini-dsh'));
      body.append(label);
    }
    const text = document.createElement('div');
    text.className = 'message-text';
    text.textContent = message.content;
    body.append(text);
    if (message.role !== 'user') {
      const copy = document.createElement('button');
      copy.className = 'copy-button';
      copy.setAttribute('aria-label', '复制回答');
      copy.title = '复制回答';
      copy.append(icon('copy'));
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(message.content);
          copy.textContent = '✓';
          copy.title = '已复制';
          setTimeout(() => {copy.replaceChildren(icon('copy')); copy.title = '复制回答';}, 1800);
        } catch {
          showError('无法访问剪贴板，请选择回答内容后手动复制。');
        }
      });
      body.append(copy);
    }
    row.append(body);
    container.append(row);
  }
  $('conversation').scrollTop = state.messages.length ? $('conversation').scrollHeight : 0;
}

function renderEvents(state) {
  const events = JSON.stringify(state.events);
  if (events === lastEvents) return;
  lastEvents = events;
  const container = $('events');
  const expanded = new Set([...container.querySelectorAll('details[open]')].map(item => item.dataset.key));
  container.replaceChildren();
  for (const [index, event] of state.events.entries()) {
    const detail = document.createElement('details');
    detail.className = 'event';
    detail.dataset.key = index + ':' + event.run_id + ':' + event.step_id + ':' + event.name;
    detail.open = expanded.has(detail.dataset.key);
    const summary = document.createElement('summary');
    summary.textContent = '[' + event.step_id + '] ' + event.name + (event.run_id && event.run_id !== state.run_id ? ' · Sub Agent ' + event.run_id.slice(0, 8) : '');
    const data = document.createElement('pre');
    data.textContent = event.data;
    detail.append(summary, data);
    container.append(detail);
  }
}

function render(state) {
  current = state;
  const workspace = state.workspace.split(/[\\/]/).filter(Boolean).pop() || state.workspace;
  const firstTask = state.messages.find(message => message.role === 'user')?.content;
  const title = firstTask ? firstTask.replace(/\s+/g, ' ').slice(0, 40) : '新会话';
  setText('workspace-name', workspace);
  setText('composer-workspace', workspace);
  $('composer-workspace').title = state.workspace;
  setText('workspace', state.workspace);
  $('workspace').title = state.workspace;
  setText('session-title', title);
  setText('header-title', title);
  setText('session-state', state.run_id ? (state.busy ? '执行中' : statuses[state.status]) : '');
  $('main').classList.toggle('has-conversation', state.messages.length > 0 || state.busy);
  $('task').placeholder = state.messages.length ? '给智能体发消息' : '描述你想要构建的内容';
  setText('model', state.model);
  setText('composer-model', state.model);
  $('composer-model').title = state.model;
  setText('context', state.context);
  const mode = state.multi_agent_enabled ? 'Multi-Agent 协作' : '标准模式';
  setText('header-mode', mode);
  setText('composer-mode', mode);
  setText('credential-status', state.credentials?.configured ? '已提供凭证' : (state.credentials?.editable ? '尚未提供' : '当前模型无需输入'));
  setText('write', state.permissions.write ? '允许写入' : '只读');
  setText('shell', state.permissions.shell ? '允许 · ' + state.permissions.commands.join(', ') : '未启用');
  setText('permission-pill', state.permissions.write ? '允许写入' : '只读模式');
  setText('status', state.busy && state.status !== 'running' ? '正在读取 Diff' : (statuses[state.status] || state.status));
  setText('status-caption', state.busy ? '执行中，可随时停止' : '准备好开始下一次探索');
  for (const id of ['steps', 'tools', 'tokens', 'seconds']) {
    setText(id, state.metrics[id] === undefined ? '—' : state.metrics[id].toLocaleString());
  }
  setText('cost', state.metrics.cost_usd == null ? '成本未配置' : '$' + state.metrics.cost_usd.toFixed(6));
  $('usage-line').hidden = state.metrics.steps === undefined;
  setText('run-id', state.run_id || '尚无运行记录');
  setText('trace', state.trace || '—');
  showError(state.error);
  $('running').hidden = !state.busy;
  $('welcome').hidden = state.messages.length > 0 || state.busy;
  renderMessages(state);
  renderEvents(state);
  renderAgents(state);
  setText('event-count', state.events.length);
  $('event-empty').hidden = state.events.length > 0;
  $('event-truncated').hidden = !state.events_truncated;
  if (diffRequested && !state.busy) {
    setText('diff-content', state.diff || state.error || '没有已跟踪文件的改动。');
    $('diff-panel').hidden = false;
    diffRequested = false;
  }
  setText('connection', '● 本地已连接' + (state.model === 'scripted' ? ' · 离线演示' : ''));
  controls();
}

async function action(name, data = {}) {
  if (pending) return false;
  pending = true;
  controls();
  try {
    await api(name, data);
    render(await api('state'));
    return true;
  } catch (error) {
    showError(error.message);
    if ($('resume-dialog').open) {
      setText('resume-error', error.message);
      $('resume-error').hidden = false;
    }
    return false;
  } finally {
    pending = false;
    controls();
  }
}

$('task').addEventListener('input', controls);
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (pending || current?.busy) return;
  const data = {
    multi_agent_enabled: $('multi-agent').checked,
    subagent_max_agents: Number($('max-agents').value),
    subagent_max_parallel: Number($('max-parallel').value)
  };
  if ($('api-key').value.trim()) data.api_key = $('api-key').value.trim();
  const saved = await action('settings', data);
  $('api-key').value = '';
  if (saved) { fillSettings(); setText('settings-feedback', '已保存，下一次任务生效'); }
});
$('task-form').addEventListener('submit', async event => {
  event.preventDefault();
  const task = $('task').value.trim();
  if (!task || pending || current?.busy || !current) return;
  setView('chat');
  toggleSuggestions(false);
  if (await action('run', {task})) { $('task').value = ''; controls(); }
});
$('task').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) {
    event.preventDefault();
    $('task-form').requestSubmit();
  }
});
$('cancel').addEventListener('click', () => action('cancel'));
$('clear').addEventListener('click', async () => {
  if (await action('clear')) {
    setView('chat');
    toggleSuggestions(false);
    $('diff-panel').hidden = true;
    $('task').focus();
  }
});
$('open-resume').addEventListener('click', () => {
  $('resume-error').hidden = true;
  $('resume-dialog').showModal();
});
$('resume-form').addEventListener('submit', async event => {
  event.preventDefault();
  const run_id = $('resume-id').value.trim();
  if (run_id && await action('resume', {run_id})) {
    $('resume-dialog').close();
    setView('chat');
  }
});
$('diff').addEventListener('click', async () => {
  diffRequested = true;
  if (!await action('diff')) diffRequested = false;
});
$('close-diff').addEventListener('click', () => { $('diff-panel').hidden = true; });
$('chat-tab').addEventListener('click', () => {
  setView('chat');
  $('diff-panel').hidden = true;
  hideMobileSidebar();
  $('task').focus();
});
$('conversation-tab').addEventListener('click', () => setView('chat'));
$('trace-tab').addEventListener('click', () => setView('trace'));
for (const id of ['conversation-tab', 'trace-tab']) {
  $(id).addEventListener('keydown', event => {
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault();
      const target = id === 'conversation-tab' ? 'trace-tab' : 'conversation-tab';
      setView(target === 'trace-tab' ? 'trace' : 'chat');
      $(target).focus();
    }
  });
}
for (const id of ['open-settings', 'run-info', 'mode-details', 'permission-details']) {
  $(id).addEventListener('click', openSettings);
}
for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => $(button.dataset.close).close());
}
$('prompt-menu').addEventListener('click', () => toggleSuggestions($('suggestions').hidden));
for (const button of document.querySelectorAll('[data-task]')) {
  button.addEventListener('click', () => {
    $('task').value = button.dataset.task;
    toggleSuggestions(false);
    controls();
    $('task').focus();
  });
}
function hideMobileSidebar() {
  document.body.classList.remove('mobile-sidebar-open');
  if (window.matchMedia('(max-width: 540px)').matches) {
    $('expand-sidebar').setAttribute('aria-expanded', 'false');
    $('collapse-sidebar').setAttribute('aria-expanded', 'false');
  }
}
function collapseSidebar(collapse) {
  document.body.classList.toggle('sidebar-collapsed', collapse);
  document.body.classList.toggle('mobile-sidebar-open', !collapse);
  $('expand-sidebar').hidden = !collapse;
  $('collapse-sidebar').setAttribute('aria-expanded', String(!collapse));
  $('expand-sidebar').setAttribute('aria-expanded', String(!collapse));
}
$('collapse-sidebar').addEventListener('click', () => collapseSidebar(true));
$('expand-sidebar').addEventListener('click', () => collapseSidebar(false));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    toggleSuggestions(false);
    $('diff-panel').hidden = true;
    hideMobileSidebar();
  }
});
document.addEventListener('click', event => {
  if (!event.target.closest('.composer-wrap')) toggleSuggestions(false);
  if (event.target.closest('#main') && !event.target.closest('#expand-sidebar')) {
    hideMobileSidebar();
  }
});

async function poll() {
  try {
    if (!pending) {
      const state = await api('state');
      if (!pending) render(state);
    }
  } catch (error) {
    setText('connection', '连接已断开');
    showError(error.message);
  } finally {
    setTimeout(poll, 700);
  }
}
setView('chat');
controls();
poll();
