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
let traceFilter = 'all';
let dialogReturnFocus = null;
let lastDiffText = '';
let lastDiffSummary = '';
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
let toastTimer = null;
function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 2600);
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
function appendPlainText(parent, text) {
  const parts = String(text).split('\n');
  parts.forEach((part, index) => {
    if (index) parent.append(document.createElement('br'));
    if (part) parent.append(document.createTextNode(part));
  });
}
function appendInline(parent, text) {
  const source = String(text);
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]\n]+\]\([^)\s]+\))/g;
  let last = 0;
  for (const match of source.matchAll(pattern)) {
    if (match.index > last) appendPlainText(parent, source.slice(last, match.index));
    const token = match[0];
    if (token.startsWith('`')) {
      const code = document.createElement('code');
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else if (token.startsWith('**')) {
      const strong = document.createElement('strong');
      strong.textContent = token.slice(2, -2);
      parent.append(strong);
    } else if (token.startsWith('*')) {
      const emphasis = document.createElement('em');
      emphasis.textContent = token.slice(1, -1);
      parent.append(emphasis);
    } else {
      const split = token.indexOf('](');
      const label = token.slice(1, split);
      const url = token.slice(split + 2, -1);
      if (/^https?:\/\//i.test(url)) {
        const link = document.createElement('a');
        link.href = url;
        link.textContent = label;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        parent.append(link);
      } else {
        appendPlainText(parent, token);
      }
    }
    last = match.index + token.length;
  }
  if (last < source.length) appendPlainText(parent, source.slice(last));
}
function markdownFragment(source) {
  const fragment = document.createDocumentFragment();
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const isBlockStart = line => /^```/.test(line) || /^(#{1,3})\s+/.test(line) || /^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line) || /^>\s?/.test(line) || /^\s*(---|\*\*\*|___)\s*$/.test(line);
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    if (/^```/.test(line)) {
      const language = line.replace(/^```/, '').trim();
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      if (language) code.dataset.language = language;
      code.textContent = codeLines.join('\n');
      pre.append(code);
      fragment.append(pre);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    if (heading) {
      const node = document.createElement('h' + heading[1].length);
      appendInline(node, heading[2]);
      fragment.append(node);
      index += 1;
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      fragment.append(document.createElement('hr'));
      index += 1;
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = document.createElement('blockquote');
      const parts = [];
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        parts.push(lines[index].replace(/^>\s?/, ''));
        index += 1;
      }
      appendInline(quote, parts.join('\n'));
      fragment.append(quote);
      continue;
    }
    const ordered = /^\s*\d+[.)]\s+/.test(line);
    const unordered = /^\s*[-*+]\s+/.test(line);
    if (ordered || unordered) {
      const list = document.createElement(ordered ? 'ol' : 'ul');
      const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
      while (index < lines.length && pattern.test(lines[index])) {
        const item = document.createElement('li');
        appendInline(item, lines[index].replace(pattern, ''));
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    const node = document.createElement('p');
    appendInline(node, paragraph.join('\n'));
    fragment.append(node);
  }
  return fragment;
}
function messageActions(content) {
  const actions = document.createElement('div');
  actions.className = 'message-actions';
  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'message-action';
  copy.title = '复制回答';
  copy.setAttribute('aria-label', '复制回答');
  copy.append(icon('copy'));
  copy.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(content);
      copy.replaceChildren(document.createTextNode('已复制'));
      setTimeout(() => copy.replaceChildren(icon('copy')), 1600);
    } catch {
      showError('无法访问剪贴板，请选择回答内容后手动复制。');
    }
  });
  actions.append(copy);
  return actions;
}
function nearBottom() {
  const conversation = $('conversation');
  return conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 140;
}
function scrollToBottom() {
  const conversation = $('conversation');
  conversation.scrollTop = conversation.scrollHeight;
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
  const blocked = !!current?.busy;
  for (const id of ['clear', 'diff', 'open-resume']) {
    const button = $(id);
    button.disabled = pending;
    button.classList.toggle('is-blocked', blocked);
    if (blocked) button.title = '任务执行中，先停止或等待完成';
    else if (id === 'open-resume') button.title = '恢复会话';
    else button.removeAttribute('title');
  }
  $('resume').disabled = busy;
  $('send').disabled = busy || !current || !$('task').value.trim();
  $('cancel').hidden = !current?.busy;
  $('cancel').disabled = pending;
  $('send').hidden = !!current?.busy;
  for (const button of document.querySelectorAll('[data-task]')) button.disabled = busy;
  const editable = !!current?.collaboration?.editable;
  for (const id of ['multi-agent', 'max-agents', 'max-parallel', 'save-settings']) $(id).disabled = busy || !editable;
  $('api-key').disabled = busy || !current?.credentials?.editable;
  for (const button of document.querySelectorAll('[data-approval]')) button.disabled = pending;
}
function fillSettings() {
  $('multi-agent').checked = !!current?.multi_agent_enabled;
  $('max-agents').value = current?.collaboration?.max_agents || 4;
  $('max-parallel').value = current?.collaboration?.max_parallel || 2;
}
function openSettings(view = 'settings') {
  const dialog = $('settings-dialog');
  dialog.dataset.view = view;
  setText('settings-title', view === 'info' ? '运行信息' : '运行设置');
  if (view === 'settings') {
    fillSettings();
    $('api-key').value = '';
    setText('settings-feedback', '');
  }
  openDialog(dialog);
}
function togglePopover(kind) {
  const target = kind === 'permission' ? $('permission-popover') : $('mode-popover');
  const other = kind === 'permission' ? $('mode-popover') : $('permission-popover');
  other.hidden = true;
  target.hidden = !target.hidden;
  if (target.hidden) return;
  if (kind === 'permission') renderPermissionPopover();
  else renderModePopover();
}
function renderPermissionPopover() {
  if (!current) return;
  setText('popover-write', current.permissions.write ? '允许写入' : '只读');
  setText('popover-shell', current.permissions.shell ? '允许 · ' + current.permissions.commands.join(', ') : '未启用');
  const command = [
    'uv run mini-dsh gui',
    '--workspace "' + current.workspace + '"',
    ...(current.permissions.write ? [] : ['--allow-write']),
    ...(current.permissions.shell ? [] : ['--allow-shell', '--allow-command python'])
  ].join(' ');
  $('copy-launch').dataset.command = command;
}
function renderModePopover() {
  if (!current) return;
  const multi = !!current.multi_agent_enabled;
  const editable = !!current.collaboration?.editable;
  const scripted = current.model === 'scripted';
  for (const button of document.querySelectorAll('[data-mode]')) {
    button.classList.toggle('active', (button.dataset.mode === 'multi') === multi);
    button.disabled = !editable || !!current.busy || (button.dataset.mode === 'multi' && scripted);
  }
  setText('mode-note', !editable
    ? '当前入口不支持修改协作设置。'
    : scripted
      ? '离线 scripted 演示不支持 Multi-Agent；连接真实 provider 后可切换。'
      : '切换模式只影响后续任务；运行中不可修改。');
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
  const stick = nearBottom();
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
    if (message.role === 'user') text.textContent = message.content;
    else text.append(markdownFragment(message.content));
    body.append(text);
    if (message.role !== 'user') body.append(messageActions(message.content));
    row.append(body);
    container.append(row);
  }
  if (stick) scrollToBottom();
}

function eventData(event) {
  try {
    return JSON.parse(event.data);
  } catch {
    return null;
  }
}
function isToolEvent(event) {
  return event.name.startsWith('tool_') || event.name.startsWith('approval_');
}
function isErrorEvent(event) {
  if (/(error|failed|denied|timeout|violation|mismatch)/.test(event.name)) return true;
  const data = eventData(event);
  if (!data) return false;
  const status = String(data.status || '');
  return ['failed', 'denied', 'timeout', 'error'].includes(status) || Boolean(data.error);
}
function eventSummary(event) {
  const data = eventData(event);
  if (!data) return '';
  if (event.name === 'tool_call_completed') {
    return [data.tool || data.name, data.status, data.duration_ms == null ? '' : Math.round(data.duration_ms) + 'ms'].filter(Boolean).join(' · ');
  }
  if (event.name === 'model_call_completed') {
    return [
      data.tokens,
      data.input_tokens == null ? '' : 'in ' + data.input_tokens,
      data.output_tokens == null ? '' : 'out ' + data.output_tokens,
      data.duration_ms == null ? '' : Math.round(data.duration_ms) + 'ms'
    ].filter(Boolean).join(' · ');
  }
  if (event.name === 'approval_requested') return data.tool || '';
  if (event.name === 'context_built') {
    return [data.strategy, data.estimated_tokens == null ? '' : data.estimated_tokens + ' tok'].filter(Boolean).join(' · ');
  }
  if (data.status) return String(data.status);
  return '';
}
function renderEvents(state) {
  const events = state.events || [];
  const signature = JSON.stringify(events) + traceFilter;
  if (signature === lastEvents) return;
  lastEvents = signature;
  const container = $('events');
  const expanded = new Set([...container.querySelectorAll('details[open]')].map(item => item.dataset.key));
  container.replaceChildren();
  const visible = events.filter(event => traceFilter === 'all' || (traceFilter === 'tool' ? isToolEvent(event) : isErrorEvent(event)));
  setText('trace-summary', visible.length + ' / ' + events.length + ' 条');
  if (!visible.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = events.length ? '当前筛选下没有事件。' : '开始一个任务后，执行事件会显示在这里。';
    container.append(empty);
    return;
  }
  let groupKey = '';
  let group = null;
  for (const [index, event] of visible.entries()) {
    const key = event.run_id + ':' + event.step_id;
    if (key !== groupKey) {
      groupKey = key;
      group = document.createElement('section');
      group.className = 'event-group';
      const heading = document.createElement('h3');
      heading.className = 'event-group-head';
      heading.textContent = '步骤 ' + event.step_id + (event.run_id && event.run_id !== state.run_id ? ' · Sub Agent ' + event.run_id.slice(0, 8) : '');
      group.append(heading);
      container.append(group);
    }
    const detail = document.createElement('details');
    detail.className = 'event' + (isErrorEvent(event) ? ' event-error' : '');
    detail.dataset.key = index + ':' + event.run_id + ':' + event.step_id + ':' + event.name;
    detail.open = expanded.has(detail.dataset.key);
    const summary = document.createElement('summary');
    const name = document.createElement('span');
    name.className = 'event-name';
    name.textContent = event.name;
    const extra = document.createElement('span');
    extra.className = 'event-extra';
    extra.textContent = eventSummary(event);
    summary.append(name, extra);
    const data = document.createElement('pre');
    data.textContent = event.data;
    detail.append(summary, data);
    group.append(detail);
  }
}

function renderStreaming(state) {
  const text = state.streaming_text || '';
  const node = $('streaming-message');
  node.hidden = !text;
  if (!text) {
    $('streaming-body').replaceChildren();
    return;
  }
  const stick = nearBottom();
  $('streaming-body').replaceChildren(markdownFragment(text));
  const cursor = document.createElement('span');
  cursor.className = 'streaming-cursor';
  cursor.setAttribute('aria-hidden', 'true');
  $('streaming-body').append(cursor);
  if (stick) scrollToBottom();
}

function renderApproval(state) {
  const approvals = state.approvals || [];
  const approval = approvals[0];
  const card = $('approval-card');
  const wasHidden = card.hidden;
  card.hidden = !approval;
  if (!approval) return;
  setText('approval-tool', approval.tool || '未知工具');
  setText('approval-meta', '步骤 ' + approval.step_id + (approval.scope ? ' · ' + approval.scope : ''));
  setText('approval-preview', approval.preview || '（没有可展示的预览）');
  setText('approval-queue', approvals.length > 1 ? '还有 ' + (approvals.length - 1) + ' 个待审批' : '');
  if (wasHidden) {
    const first = card.querySelector('[data-approval="allow"]');
    if (first) first.focus({preventScroll: true});
  }
}

function renderDiff(text) {
  const content = $('diff-content');
  content.replaceChildren();
  lastDiffText = String(text || '');
  const lines = lastDiffText.split('\n');
  for (const line of lines) {
    const span = document.createElement('span');
    span.className = 'diff-line';
    if (line.startsWith('diff --git') || line.startsWith('+++') || line.startsWith('---')) span.classList.add('diff-file');
    else if (line.startsWith('@@')) span.classList.add('diff-hunk');
    else if (line.startsWith('+')) span.classList.add('diff-add');
    else if (line.startsWith('-')) span.classList.add('diff-del');
    span.textContent = line || ' ';
    content.append(span);
  }
  const added = lines.filter(line => line.startsWith('+') && !line.startsWith('+++')).length;
  const removed = lines.filter(line => line.startsWith('-') && !line.startsWith('---')).length;
  lastDiffSummary = lines.length > 1 ? '+' + added + ' / −' + removed : '';
  setText('diff-summary', lastDiffSummary);
  $('copy-diff').disabled = !lastDiffText;
}

function renderSessionList(saved) {
  const list = $('session-list');
  list.replaceChildren();
  const sessions = saved.sessions || [];
  if (!sessions.length) {
    const empty = document.createElement('p');
    empty.className = 'session-empty';
    empty.textContent = '还没有保存的会话。完成一轮任务后会自动保存。';
    list.append(empty);
  }
  for (const session of sessions) {
    const row = document.createElement('div');
    row.className = 'session-row-wrap';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'session-row';
    if (current?.session_id === session.session_id) button.classList.add('active');
    const title = document.createElement('strong');
    title.textContent = session.title || '未命名会话';
    const meta = document.createElement('span');
    meta.className = 'session-meta';
    meta.textContent = session.turn_count + ' 轮 · ' + (statuses[session.status] || session.status || '已保存');
    button.append(title, meta);
    button.addEventListener('click', async () => {
      if (await action('session', {session_id: session.session_id})) closeDialog($('resume-dialog'));
    });
    row.append(button);
    if (session.latest_run_id) {
      const fork = document.createElement('button');
      fork.type = 'button';
      fork.className = 'session-fork';
      fork.textContent = '分叉';
      fork.title = '从该会话最新轮次分叉，不回滚文件';
      fork.addEventListener('click', async () => {
        if (await action('fork', {run_id: session.latest_run_id})) closeDialog($('resume-dialog'));
      });
      row.append(fork);
    }
    list.append(row);
  }
  const legacy = saved.legacy_runs || [];
  $('legacy-runs').hidden = !legacy.length;
  const legacyList = $('legacy-list');
  legacyList.replaceChildren();
  for (const runId of legacy) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'legacy-run';
    button.textContent = runId;
    button.addEventListener('click', async () => {
      if (await action('resume', {run_id: runId})) closeDialog($('resume-dialog'));
    });
    legacyList.append(button);
  }
}

function render(state) {
  current = state;
  renderStreaming(state);
  renderApproval(state);
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
  if (state.streaming_capable === false) setText('status-caption', '该模型使用完整响应模式');
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
    renderDiff(state.diff || state.error || '没有已跟踪文件的改动。');
    $('diff-panel').hidden = false;
    diffRequested = false;
  }
  setText('connection', '● 本地已连接' + (state.model === 'scripted' ? ' · 离线演示' : ''));
  if (!$('permission-popover').hidden) renderPermissionPopover();
  if (!$('mode-popover').hidden) renderModePopover();
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
    toast(error.message);
    if ($('resume-dialog').open) {
      setText('resume-error', error.message);
      $('resume-error').hidden = false;
    }
    if ($('settings-dialog').open) setText('settings-feedback', error.message);
    return false;
  } finally {
    pending = false;
    controls();
  }
}

$('task').addEventListener('input', () => {
  controls();
  resizeComposer();
});
function resizeComposer() {
  const task = $('task');
  task.style.height = 'auto';
  task.style.height = Math.min(task.scrollHeight, 210) + 'px';
}
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
  if (saved) {
    $('api-key').value = '';
    fillSettings();
    setText('settings-feedback', '已保存，下一次任务生效');
  }
});
$('task-form').addEventListener('submit', async event => {
  event.preventDefault();
  const task = $('task').value.trim();
  if (!task || pending || current?.busy || !current) return;
  setView('chat');
  toggleSuggestions(false);
  if (await action('run', {task})) { $('task').value = ''; resizeComposer(); controls(); }
});
$('task').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.isComposing) {
    event.preventDefault();
    $('task-form').requestSubmit();
  }
});
$('cancel').addEventListener('click', () => action('cancel'));
for (const button of document.querySelectorAll('[data-approval]')) {
  button.addEventListener('click', () => {
    const request = current?.approvals?.[0];
    if (request) action('approval', {request_id: request.request_id, run_id: request.run_id,
      choice: button.dataset.approval});
  });
}
$('clear').addEventListener('click', async () => {
  if (pending) return;
  if (current?.busy) { toast('任务执行中，先停止或等待完成。'); return; }
  if (await action('clear')) {
    setView('chat');
    toggleSuggestions(false);
    $('diff-panel').hidden = true;
    resizeComposer();
    $('task').focus();
  }
});
$('open-resume').addEventListener('click', openResume);
async function openResume() {
  if (pending) return;
  if (current?.busy) { toast('任务执行中，先停止或等待完成。'); return; }
  $('resume-error').hidden = true;
  openDialog($('resume-dialog'));
  $('session-list').replaceChildren();
  try {
    renderSessionList(await api('sessions'));
  } catch (error) {
    showError(error.message);
    setText('resume-error', error.message);
    $('resume-error').hidden = false;
  }
  $('resume-id').focus();
}
$('resume-form').addEventListener('submit', async event => {
  event.preventDefault();
  const run_id = $('resume-id').value.trim();
  if (run_id && await action('resume', {run_id})) {
    closeDialog($('resume-dialog'));
    setView('chat');
  }
});
$('diff').addEventListener('click', async () => {
  if (pending) return;
  if (current?.busy) { toast('任务执行中，先停止或等待完成。'); return; }
  diffRequested = true;
  if (!await action('diff')) {
    diffRequested = false;
    toast('无法读取工作区改动。');
  }
});
$('close-diff').addEventListener('click', () => { $('diff-panel').hidden = true; });
$('copy-diff').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(lastDiffText);
    setText('diff-summary', '已复制');
    setTimeout(() => { if (!$('diff-panel').hidden) setText('diff-summary', lastDiffSummary); }, 1600);
  } catch {
    showError('无法访问剪贴板，请选择 Diff 内容后手动复制。');
  }
});
$('chat-tab').addEventListener('click', () => {
  setView('chat');
  $('diff-panel').hidden = true;
  hideMobileSidebar();
  $('task').focus();
});
$('conversation-tab').addEventListener('click', () => setView('chat'));
$('trace-tab').addEventListener('click', () => setView('trace'));
for (const button of document.querySelectorAll('[data-filter]')) {
  button.addEventListener('click', () => {
    traceFilter = button.dataset.filter;
    for (const item of document.querySelectorAll('[data-filter]')) {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', String(active));
    }
    lastEvents = '';
    if (current) renderEvents(current);
  });
}
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
$('open-settings').addEventListener('click', () => openSettings('settings'));
$('run-info').addEventListener('click', () => openSettings('info'));
$('permission-details').addEventListener('click', () => togglePopover('permission'));
$('mode-details').addEventListener('click', () => togglePopover('mode'));
$('copy-launch').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText($('copy-launch').dataset.command);
    toast('启动命令已复制');
  } catch {
    toast('无法访问剪贴板，请手动复制启动命令');
  }
});
$('permission-settings').addEventListener('click', () => {
  $('permission-popover').hidden = true;
  openSettings('settings');
});
for (const button of document.querySelectorAll('[data-mode]')) {
  button.addEventListener('click', async () => {
    if (!current || current.busy || pending) {
      toast('任务执行中，先停止或等待完成。');
      return;
    }
    const multi = button.dataset.mode === 'multi';
    const saved = await action('settings', {
      multi_agent_enabled: multi,
      subagent_max_agents: current.collaboration?.max_agents || 4,
      subagent_max_parallel: current.collaboration?.max_parallel || 2
    });
    if (saved) {
      $('mode-popover').hidden = true;
      toast(multi ? '已切换到 Multi-Agent 协作' : '已切换到标准模式');
    }
  });
}
for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => closeDialog($(button.dataset.close)));
}
$('prompt-menu').addEventListener('click', () => toggleSuggestions($('suggestions').hidden));
for (const button of document.querySelectorAll('[data-task]')) {
  button.addEventListener('click', () => {
    $('task').value = button.dataset.task;
    toggleSuggestions(false);
    resizeComposer();
    controls();
    $('task').focus();
  });
}
function isMobile() {
  return window.matchMedia('(max-width: 540px)').matches;
}
function openDialog(dialog) {
  if (dialog.open) return;
  dialogReturnFocus = document.activeElement;
  dialog.showModal();
}
function closeDialog(dialog) {
  if (dialog.open) dialog.close();
}
for (const dialog of document.querySelectorAll('dialog')) {
  dialog.addEventListener('close', () => {
    if (dialogReturnFocus && document.contains(dialogReturnFocus)) {
      dialogReturnFocus.focus({preventScroll: true});
    }
    dialogReturnFocus = null;
  });
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    const outside = event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
    if (outside) closeDialog(dialog);
  });
}
function setMobileSidebar(open) {
  document.body.classList.toggle('mobile-sidebar-open', open);
  $('sidebar-backdrop').hidden = !open;
  $('expand-sidebar').setAttribute('aria-expanded', String(open));
  $('collapse-sidebar').setAttribute('aria-expanded', String(open));
  if (open) {
    const first = $('sidebar').querySelector('button, a');
    if (first) first.focus({preventScroll: true});
  }
}
function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  $('expand-sidebar').hidden = !collapsed;
  $('collapse-sidebar').setAttribute('aria-expanded', String(!collapsed));
  $('expand-sidebar').setAttribute('aria-expanded', String(!collapsed));
}
function hideMobileSidebar() {
  if (isMobile()) setMobileSidebar(false);
}
$('collapse-sidebar').addEventListener('click', () => {
  if (isMobile()) setMobileSidebar(false);
  else setSidebarCollapsed(true);
});
$('expand-sidebar').addEventListener('click', () => {
  if (isMobile()) setMobileSidebar(true);
  else setSidebarCollapsed(false);
});
$('sidebar-backdrop').addEventListener('click', () => setMobileSidebar(false));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    toggleSuggestions(false);
    $('diff-panel').hidden = true;
    hideMobileSidebar();
  }
});
document.addEventListener('click', event => {
  if (!event.target.closest('.composer-wrap')) toggleSuggestions(false);
  if (!event.target.closest('#permission-popover') && !event.target.closest('#permission-details')) $('permission-popover').hidden = true;
  if (!event.target.closest('#mode-popover') && !event.target.closest('#mode-details')) $('mode-popover').hidden = true;
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
async function follow() {
  try {
    const response = await fetch('/api/events', {headers: {'X-MiniDSH-Token': token}});
    if (!response.ok || !response.body) throw new Error('增量连接失败');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let end;
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        if (frame.startsWith('data: ') && !pending) render(JSON.parse(frame.slice(6)));
      }
    }
  } catch {
    // State polling remains a bounded reconnect fallback.
  } finally {
    setTimeout(follow, 300);
  }
}
setView('chat');
controls();
poll();
follow();
