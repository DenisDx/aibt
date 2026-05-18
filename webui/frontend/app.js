/* aibt WebUI — dashboard application script */
'use strict';

let _ws = null;
let _wsLogType = 'all';
let _statusInterval = null;
let _reconnectTimer = null;
let _currentUser = null;
let _langGraphInfo = null;
let _agentDialogs = {};

// ── HTTP helpers ─────────────────────────────────────────────────────────────

async function apiGet(url) {
  const r = await fetch(url, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`GET ${url} → ${r.status}`);
  return r.json();
}

async function apiPost(url, body) {
  const r = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let detail = '';
    try {
      const payload = await r.json();
      detail = payload?.detail ? ` (${payload.detail})` : '';
    } catch (_) {}
    throw new Error(`POST ${url} → ${r.status}${detail}`);
  }
  return r.json();
}

// ── Screen / page navigation ──────────────────────────────────────────────────

function showScreen(name) {
  document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
  const el = document.getElementById('screen-' + name);
  if (el) el.classList.add('active');
}

function showPage(name) {
  document.querySelectorAll('.page').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(el => el.classList.remove('active'));
  const page = document.getElementById('page-' + name);
  if (page) page.classList.add('active');
  document.querySelectorAll('.nav-tab').forEach(btn => {
    if (btn.textContent.toLowerCase().trim() === name) btn.classList.add('active');
  });
  if (name === 'dashboard') {
    loadDashboard();
    setupLogPanel();
  } else if (name === 'agent') {
    _closeWs();
    loadAgentPage();
  } else if (name === 'langgraph') {
    _closeWs();
    loadLangGraphPage();
  } else {
    _closeWs();
  }
}

// ── Auth ──────────────────────────────────────────────────────────────────────

async function login() {
  const loginVal = document.getElementById('inp-login').value.trim();
  const passVal  = document.getElementById('inp-pass').value;
  const errEl    = document.getElementById('login-error');
  errEl.textContent = '';
  if (!loginVal || !passVal) { errEl.textContent = 'Enter login and password.'; return; }
  try {
    await apiPost('/api/auth/login', { login: loginVal, password: passVal });
    const me = await apiGet('/api/auth/me');
    _currentUser = me.login;
    document.getElementById('nav-whoami').textContent = me.login;
    showScreen('app');
    loadDashboard();
    setupLogPanel();
    _startStatusPoll();
  } catch (e) {
    errEl.textContent = 'Invalid credentials.';
  }
}

async function logout() {
  try { await apiPost('/api/auth/logout'); } catch (_) {}
  _cleanup();
  showScreen('login');
}


// ── Dashboard status & agents ────────────────────────────────────────────────

async function loadDashboard() {
  try {
    const s = await apiGet('/api/status');
    _updateServiceBadge(s.service_state);
    const cardEl = document.getElementById('card-service');
    if (cardEl) {
      cardEl.textContent = s.service_state === 'online' ? 'Online' : 'Restarting…';
      cardEl.className = 'card-value ' + (s.service_state === 'online' ? 'ok' : 'warn');
    }
    // Load agents
    const agents = await apiGet('/api/agents/list');
    const cardAgents = document.getElementById('card-agents');
    if (cardAgents) {
      cardAgents.textContent = agents.ok ? agents.agents.join(', ') : '—';
    }
  } catch (_) {
    _updateServiceBadge('offline');
  }
}

function _dialogFor(agent) {
  if (!_agentDialogs[agent]) {
    _agentDialogs[agent] = [];
  }
  return _agentDialogs[agent];
}

function _pushDialogMessage(agent, role, text) {
  _dialogFor(agent).push({ role, text: String(text || ''), ts: new Date().toISOString() });
}

function _renderAgentDialog() {
  const sel = document.getElementById('agent-page-select');
  const body = document.getElementById('agent-chat-body');
  const head = document.getElementById('agent-chat-head');
  if (!sel || !body || !head) return;

  const agent = sel.value;
  head.textContent = agent ? `Dialogue with ${agent}` : 'Dialogue';
  body.innerHTML = '';

  if (!agent) {
    const empty = document.createElement('div');
    empty.className = 'chat-msg system';
    empty.textContent = 'No agents available.';
    body.appendChild(empty);
    return;
  }

  const dialog = _dialogFor(agent);
  if (!dialog.length) {
    const starter = document.createElement('div');
    starter.className = 'chat-msg system';
    starter.textContent = `Start dialogue with agent "${agent}".`;
    body.appendChild(starter);
  } else {
    dialog.forEach(msg => {
      const el = document.createElement('div');
      el.className = `chat-msg ${msg.role}`;
      el.textContent = msg.text;
      body.appendChild(el);
    });
  }

  body.scrollTop = body.scrollHeight;
}

function _renderAgentStats(stats) {
  const el = document.getElementById('agent-stats');
  if (!el) return;
  const safe = stats || {};
  const keys = ['total', 'pending', 'running', 'retrying', 'done', 'error'];
  el.innerHTML = '';
  keys.forEach(k => {
    const box = document.createElement('div');
    box.className = 'agent-stat';
    box.innerHTML = `<div class="k">${k}</div><div class="v">${safe[k] ?? 0}</div>`;
    el.appendChild(box);
  });
}

async function loadAgentPage(force = false) {
  try {
    const list = await apiGet('/api/agents/list');
    const sel = document.getElementById('agent-page-select');
    if (!sel) return;

    const prev = sel.value;
    sel.innerHTML = '';
    (list.agents || []).forEach(a => {
      const opt = document.createElement('option');
      opt.value = a;
      opt.textContent = a;
      sel.appendChild(opt);
    });

    if (prev && (list.agents || []).includes(prev)) {
      sel.value = prev;
    }
    if (!sel.value && sel.options.length) {
      sel.selectedIndex = 0;
    }

    if (force || document.getElementById('page-agent')?.classList.contains('active')) {
      await onAgentSelectionChange();
    }
  } catch (e) {
    const meta = document.getElementById('agent-meta');
    if (meta) meta.textContent = 'Failed to load agents: ' + (e.message || e);
  }
}

async function onAgentSelectionChange() {
  const sel = document.getElementById('agent-page-select');
  const meta = document.getElementById('agent-meta');
  if (!sel || !meta) return;

  if (!sel.value) {
    meta.textContent = 'No agent selected.';
    _renderAgentStats({});
    _renderAgentDialog();
    return;
  }

  try {
    const info = await apiGet('/api/agents/info?agent=' + encodeURIComponent(sel.value) + '&limit=10');
    if (!info.ok) {
      meta.textContent = 'Agent details unavailable.';
      _renderAgentStats({});
    } else {
      meta.innerHTML = `
        <div><strong>ID:</strong> ${info.agent.id}</div>
        <div><strong>Type:</strong> ${info.agent.type}</div>
        <div><strong>Module:</strong> ${info.agent.module}</div>
        <div><strong>Recent tasks:</strong> ${(info.recent_tasks || []).length}</div>
      `;
      _renderAgentStats(info.stats || {});
    }
  } catch (e) {
    meta.textContent = 'Agent details error: ' + (e.message || e);
    _renderAgentStats({});
  }

  _renderAgentDialog();
}

function _agentResultToText(result) {
  if (result == null) return '';
  if (typeof result === 'string') return result;
  if (typeof result === 'object') {
    if (typeof result.result === 'string') return result.result;
    return JSON.stringify(result, null, 2);
  }
  return String(result);
}

async function submitAgentMessage(e) {
  e.preventDefault();
  const sel = document.getElementById('agent-page-select');
  const inp = document.getElementById('agent-chat-input');
  if (!sel || !inp) return false;

  const agent = sel.value;
  const query = inp.value.trim();
  if (!agent || !query) return false;

  _pushDialogMessage(agent, 'user', query);
  _renderAgentDialog();
  inp.value = '';

  const pendingText = 'Thinking...';
  _pushDialogMessage(agent, 'agent', pendingText);
  _renderAgentDialog();

  try {
    const r = await apiPost('/api/agents/query', { agent, query });
    if (!r.ok || !r.task_id) {
      throw new Error(r.error || 'query failed');
    }
    const final = await waitAgentTask(r.task_id);
    const dialog = _dialogFor(agent);
    if (dialog.length && dialog[dialog.length - 1].text === pendingText) {
      dialog.pop();
    }
    _pushDialogMessage(agent, 'agent', _agentResultToText(final));
  } catch (err) {
    const dialog = _dialogFor(agent);
    if (dialog.length && dialog[dialog.length - 1].text === pendingText) {
      dialog.pop();
    }
    _pushDialogMessage(agent, 'system', 'Error: ' + (err.message || err));
  }

  _renderAgentDialog();
  loadAgentPage(true);
  return false;
}

async function waitAgentTask(task_id) {
  if (!task_id) throw new Error('No task id.');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws/agents?task_id=${encodeURIComponent(task_id)}`;
  return new Promise((resolve, reject) => {
    let ws;
    try {
      ws = new WebSocket(url);
    } catch (_) {
      reject(new Error('WS error'));
      return;
    }

    ws.onmessage = evt => {
      let msg;
      try { msg = JSON.parse(evt.data); } catch (_) { return; }
      if (msg.type === 'status') {
        if (msg.status === 'done') {
          resolve(msg.result);
          ws.close();
        } else if (msg.status === 'error') {
          const text = msg.result && msg.result.error ? msg.result.error : 'unknown';
          reject(new Error(text));
          ws.close();
        }
      } else if (msg.type === 'error') {
        reject(new Error(msg.message || 'unknown'));
        ws.close();
      }
    };

    ws.onerror = () => {
      reject(new Error('WS connection error'));
      try { ws.close(); } catch (_) {}
    };
  });
}

function _updateServiceBadge(state) {
  const badge = document.getElementById('svc-badge');
  const banner = document.getElementById('restart-banner');
  if (!badge) return;
  if (state === 'online') {
    badge.textContent = 'Online';
    badge.className = '';
    if (banner) banner.classList.remove('visible');
  } else if (state === 'restarting') {
    badge.textContent = 'Restarting';
    badge.className = 'warn';
    if (banner) banner.classList.add('visible');
  } else {
    badge.textContent = 'Offline';
    badge.className = 'err';
  }
}

function _startStatusPoll() {
  if (_statusInterval) clearInterval(_statusInterval);
  _statusInterval = setInterval(() => {
    loadDashboard();
    if (document.getElementById('page-langgraph')?.classList.contains('active')) {
      loadLangGraphPage(true);
    }
  }, 5000);
}

// ── LangGraph page ───────────────────────────────────────────────────────────

async function loadLangGraphPage(silent = false) {
  try {
    const s = await apiGet('/api/langgraph/status');
    _langGraphInfo = s;
    const st = document.getElementById('lg-status');
    const bind = document.getElementById('lg-bind');
    const pid = document.getElementById('lg-pid');
    const url = document.getElementById('lg-url');
    const msg = document.getElementById('lg-msg');
    if (st) {
      st.textContent = s.running ? 'Running' : 'Stopped';
      st.className = 'card-value ' + (s.running ? 'ok' : 'err');
    }
    if (bind) bind.textContent = `${s.host}:${s.port}`;
    if (pid) pid.textContent = s.pid || '—';
    if (url) url.textContent = s.base_url || '—';
    if (msg && !silent) msg.textContent = s.running ? 'LangGraph is healthy.' : 'LangGraph is not running.';
    refreshLangGraphLogs(true);
  } catch (e) {
    const msg = document.getElementById('lg-msg');
    if (msg) msg.textContent = 'Status error: ' + (e.message || e);
  }
}

async function restartLangGraph() {
  const msg = document.getElementById('lg-msg');
  if (msg) msg.textContent = 'Restarting LangGraph...';
  try {
    const r = await apiPost('/api/langgraph/restart', {});
    _langGraphInfo = r;
    await loadLangGraphPage();
    if (msg) msg.textContent = 'LangGraph restarted successfully.';
  } catch (e) {
    if (msg) msg.textContent = 'Restart failed: ' + (e.message || e);
  }
}

function openLangGraphStudio() {
  const url = _langGraphInfo?.studio_url || 'https://smith.langchain.com/';
  window.open(url, '_blank', 'noopener');
}

function openLangGraphDocs() {
  const url = _langGraphInfo?.docs_url;
  if (!url) return;
  window.open(url, '_blank', 'noopener');
}

async function copyLangGraphApiUrl() {
  const url = _langGraphInfo?.base_url;
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    const msg = document.getElementById('lg-msg');
    if (msg) msg.textContent = 'API URL copied: ' + url;
  } catch (_) {}
}

async function refreshLangGraphLogs(silent = false) {
  try {
    const r = await apiGet('/api/langgraph/logs?lines=200');
    const out = document.getElementById('lg-log-output');
    if (out) out.textContent = r.text || '';
  } catch (e) {
    if (!silent) {
      const msg = document.getElementById('lg-msg');
      if (msg) msg.textContent = 'Log read failed: ' + (e.message || e);
    }
  }
}

// ── Log panel ─────────────────────────────────────────────────────────────────

async function setupLogPanel() {
  // Populate log type selector
  try {
    const data = await apiGet('/api/logs/types');
    const sel = document.getElementById('log-type-sel');
    if (sel && data.types) {
      const prev = sel.value;
      sel.innerHTML = '';
      data.types.forEach(t => {
        const opt = document.createElement('option');
        opt.value = t; opt.textContent = t;
        if (t === prev || (!prev && t === 'all')) opt.selected = true;
        sel.appendChild(opt);
      });
      _wsLogType = sel.value || 'all';
    }
  } catch (_) {}
  connectLogWS(_wsLogType);
}

function onLogTypeChange() {
  const sel = document.getElementById('log-type-sel');
  if (sel) {
    _wsLogType = sel.value;
    connectLogWS(_wsLogType);
  }
}

function refreshLog() {
  _closeWs();
  connectLogWS(_wsLogType);
}

function connectLogWS(logType) {
  _closeWs();
  if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws/logs?log_type=${encodeURIComponent(logType)}`;

  let ws;
  try { ws = new WebSocket(url); } catch (e) { return; }
  _ws = ws;

  ws.onopen = () => {};

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (_) { return; }
    const autoUpdate = document.getElementById('chk-autoupdate');
    if (!autoUpdate || !autoUpdate.checked) return;

    const out = document.getElementById('log-output');
    if (!out) return;

    if (msg.type === 'init') {
      out.textContent = Array.isArray(msg.lines) ? msg.lines.join('\n') : '';
      _scrollLog();
    } else if (msg.type === 'line') {
      out.textContent += (out.textContent ? '\n' : '') + (msg.text || '');
      _scrollLog();
    }
    // ping: ignore
  };

  ws.onclose = () => {
    if (_ws === ws) _ws = null;
    // Auto-reconnect after 3s if still on dashboard
    _reconnectTimer = setTimeout(() => {
      if (document.getElementById('page-dashboard')?.classList.contains('active')) {
        connectLogWS(_wsLogType);
      }
    }, 3000);
  };

  ws.onerror = () => { ws.close(); };
}

function _scrollLog() {
  const chk = document.getElementById('chk-autoscroll');
  if (chk && chk.checked) {
    const out = document.getElementById('log-output');
    if (out) out.scrollTop = out.scrollHeight;
  }
}

function _closeWs() {
  if (_ws) {
    const ws = _ws; _ws = null;
    try { ws.close(); } catch (_) {}
  }
}

// ── Service restart ───────────────────────────────────────────────────────────

async function restartService() {
  if (!confirm('Restart the aibt service?')) return;
  try {
    await apiPost('/api/service/restart');
    _updateServiceBadge('restarting');
    // Poll until back online
    const poll = setInterval(async () => {
      try {
        const s = await apiGet('/api/status');
        if (s.service_state === 'online') {
          clearInterval(poll);
          _updateServiceBadge('online');
          setupLogPanel();
        }
      } catch (_) {}
    }, 2000);
    // Give up after 2 min
    setTimeout(() => clearInterval(poll), 120000);
  } catch (e) {
    alert('Restart request failed: ' + e.message);
  }
}

// ── Cleanup ───────────────────────────────────────────────────────────────────

function _cleanup() {
  _closeWs();
  if (_statusInterval) { clearInterval(_statusInterval); _statusInterval = null; }
  if (_reconnectTimer) { clearTimeout(_reconnectTimer); _reconnectTimer = null; }
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────

document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.getElementById('screen-login')?.classList.contains('active')) {
    login();
  }
});

// ── Init: check existing session ──────────────────────────────────────────────

(async () => {
  try {
    const me = await apiGet('/api/auth/me');
    _currentUser = me.login;
    document.getElementById('nav-whoami').textContent = me.login;
    showScreen('app');
    loadDashboard();
    loadAgentPage();
    setupLogPanel();
    _startStatusPoll();
  } catch (_) {
    showScreen('login');
  }
})();
