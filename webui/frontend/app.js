/* aibt WebUI — dashboard application script */
'use strict';

let _ws = null;
let _wsLogType = 'all';
let _statusInterval = null;
let _reconnectTimer = null;
let _currentUser = null;
let _langGraphInfo = null;
let _agentDialogs = {};
let _memoryDocs = [];
let _memoryDocTotal = 0;
let _memoryDocOffset = 0;
let _memoryDocLimit = 20;
let _memoryDocQuery = '';
let _memoryDocTag = '';
let _memoryCurrentCorpus = '';
let _memoryDocSortBy = 'updated_at';
let _memoryDocSortDir = 'desc';
let _memoryFilterTimer = null;
let _memoryNamespaceTimer = null;
let _memorydEnvids = [];
let _memorydMuids = [];
let _memorydRecords = [];
let _memorydSelectedRecordId = '';
let _memorydSelectedTypes = [];
let _memorydCurrentEnvid = '';
let _memoryExecutorTasks = [];
let _memoryExecutorSelectedId = '';
let _memoryExecutorTemplates = [];
let _memoryExecutorSplitterReady = false;
let _memoryExecutorEnvidItems = [];
let _agentLogItems = [];
let _agentLogSelectedId = '';
let _agentLogRawVisible = false;

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

async function apiDelete(url) {
  const r = await fetch(url, {
    method: 'DELETE',
    credentials: 'same-origin',
  });
  if (!r.ok) {
    let detail = '';
    try {
      const payload = await r.json();
      detail = payload?.detail ? ` (${payload.detail})` : '';
    } catch (_) {}
    throw new Error(`DELETE ${url} -> ${r.status}${detail}`);
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
    const click = String(btn.getAttribute('onclick') || '');
    if (click.includes(`showPage('${name}')`)) btn.classList.add('active');
  });
  if (name === 'dashboard') {
    loadDashboard();
    setupLogPanel();
  } else if (name === 'agent') {
    _closeWs();
    loadAgentPage();
  } else if (name === 'agentlog') {
    _closeWs();
    loadAgentLogPage();
  } else if (name === 'memory') {
    _closeWs();
    loadMemoryPage();
  } else if (name === 'memoryd') {
    _closeWs();
    loadMemorydPage();
  } else if (name === 'memoryexecutor') {
    _closeWs();
    loadMemoryExecutorPage();
  } else if (name === 'langgraph') {
    _closeWs();
    loadLangGraphPage();
  } else {
    _closeWs();
  }
}

function updateFeatureTabs(capabilities = {}) {
  const memoryExecutorEnabled = capabilities?.memory_executor !== false;
  const memoryExecutorTab = document.getElementById('nav-tab-memoryexecutor');
  const memoryExecutorPage = document.getElementById('page-memoryexecutor');
  if (memoryExecutorTab) {
    memoryExecutorTab.style.display = memoryExecutorEnabled ? '' : 'none';
  }
  if (memoryExecutorPage && !memoryExecutorEnabled && memoryExecutorPage.classList.contains('active')) {
    showPage('dashboard');
  }
}

// ── Agent log page ─────────────────────────────────────────────────────────

async function loadAgentLogPage(force = false) {
  const fileSel = document.getElementById('alog-file-select');
  const meta = document.getElementById('alog-meta');
  if (!fileSel) return;

  try {
    const files = await apiGet('/api/agent-logs/files');
    const prev = fileSel.value;
    fileSel.innerHTML = '';
    (files.items || []).forEach((name) => {
      const opt = document.createElement('option');
      opt.value = String(name || '');
      opt.textContent = String(name || '');
      fileSel.appendChild(opt);
    });

    if (!fileSel.options.length) {
      if (meta) meta.textContent = 'No *_llm.jsonl logs found in ./logs.';
      _agentLogItems = [];
      _agentLogSelectedId = '';
      _renderAgentLogList();
      _renderAgentLogDetail(null);
      return;
    }

    if (prev && Array.from(fileSel.options).some(o => o.value === prev)) {
      fileSel.value = prev;
    }

    if (force || document.getElementById('page-agentlog')?.classList.contains('active')) {
      await onAgentLogFileChange();
    }
  } catch (e) {
    if (meta) meta.textContent = 'Agent log files error: ' + (e.message || e);
  }
}

async function onAgentLogFileChange() {
  const fileSel = document.getElementById('alog-file-select');
  const limitEl = document.getElementById('alog-limit');
  const meta = document.getElementById('alog-meta');
  if (!fileSel) return;

  const file = String(fileSel.value || '').trim();
  const rawLimit = Number(limitEl?.value || 120);
  const limit = Number.isFinite(rawLimit) ? Math.max(10, Math.min(500, Math.trunc(rawLimit))) : 120;
  if (limitEl) limitEl.value = String(limit);

  if (!file) {
    _agentLogItems = [];
    _agentLogSelectedId = '';
    _renderAgentLogList();
    _renderAgentLogDetail(null);
    if (meta) meta.textContent = 'Select agent log file.';
    return;
  }

  try {
    const data = await apiGet('/api/agent-logs/view?file=' + encodeURIComponent(file) + '&limit=' + encodeURIComponent(limit));
    _agentLogItems = Array.isArray(data.items) ? data.items : [];
    if (_agentLogItems.length) {
      if (!_agentLogItems.some(i => i.entry_id === _agentLogSelectedId)) {
        _agentLogSelectedId = String(_agentLogItems[0].entry_id || '');
      }
    } else {
      _agentLogSelectedId = '';
    }
    if (meta) {
      meta.textContent = `${data.file || file}: ${Number(data.total || 0)} entries, showing ${_agentLogItems.length}`;
    }
    _renderAgentLogList();
    _renderAgentLogDetail(_getSelectedAgentLogItem());
  } catch (e) {
    if (meta) meta.textContent = 'Agent log view error: ' + (e.message || e);
    _agentLogItems = [];
    _agentLogSelectedId = '';
    _renderAgentLogList();
    _renderAgentLogDetail(null);
  }
}

function _getSelectedAgentLogItem() {
  return _agentLogItems.find(i => String(i.entry_id || '') === String(_agentLogSelectedId || '')) || null;
}

function _renderAgentLogList() {
  const list = document.getElementById('alog-list');
  if (!list) return;
  list.innerHTML = '';

  if (!_agentLogItems.length) {
    const empty = document.createElement('div');
    empty.className = 'alog-item';
    empty.textContent = 'No entries.';
    list.appendChild(empty);
    return;
  }

  _agentLogItems.forEach((item) => {
    const id = String(item.entry_id || '');
    const row = document.createElement('div');
    row.className = 'alog-item' + (id === _agentLogSelectedId ? ' active' : '');
    row.onclick = () => {
      _agentLogSelectedId = id;
      _renderAgentLogList();
      _renderAgentLogDetail(item);
    };

    const time = document.createElement('div');
    time.className = 'alog-time';
    time.textContent = item.time || '—';

    const user = document.createElement('div');
    user.className = 'alog-preview';
    user.textContent = 'role:user  ' + String(item.user_preview || '').slice(0, 180);

    const resp = document.createElement('div');
    resp.className = 'alog-preview';
    resp.textContent = 'role:assistant  ' + String(item.response_preview || '').slice(0, 180);

    row.appendChild(time);
    row.appendChild(user);
    row.appendChild(resp);
    list.appendChild(row);
  });
}

function _renderAgentLogDetail(item) {
  const tech = document.getElementById('alog-tech');
  const req = document.getElementById('alog-request');
  const resp = document.getElementById('alog-response');
  const raw = document.getElementById('alog-raw');
  const rawWrap = document.getElementById('alog-raw-wrap');
  if (!tech || !req || !resp || !raw || !rawWrap) return;

  if (!item) {
    tech.textContent = 'No entry selected.';
    req.textContent = '';
    resp.textContent = '';
    raw.textContent = '';
    rawWrap.style.display = _agentLogRawVisible ? '' : 'none';
    return;
  }

  tech.innerHTML =
    `<div><strong>time:</strong> ${escapeHtml(item.time || '—')}</div>` +
    `<div><strong>agent:</strong> ${escapeHtml(item.agent_id || '—')}</div>` +
    `<div><strong>envid:</strong> ${escapeHtml(item.envid == null ? '' : String(item.envid))}</div>` +
    `<div><strong>entry:</strong> ${escapeHtml(item.entry_id || '—')}</div>` +
    `<div><strong>input/output line:</strong> ${escapeHtml(String(item.input_line_no || '—'))} / ${escapeHtml(String(item.output_line_no || '—'))}</div>`;

  const requestPayload = {
    request_messages_exact: item.request_messages_exact ?? null,
    request_messages_raw: item.request_messages_raw ?? null,
    request_prompts_exact: item.request_prompts_exact ?? null,
    request_prompts_raw: item.request_prompts_raw ?? null,
    invocation_params: item.invocation_params ?? null,
    invocation_params_raw: item.invocation_params_raw ?? null,
    query: item.query || '',
    messages_payload_fallback: item.request_messages || [],
    memory_context: item.memory_context || '',
    context: item.context || {},
  };
  req.textContent = JSON.stringify(requestPayload, null, 2);
  const hasRaw = item.response_raw !== undefined && item.response_raw !== null;
  if (hasRaw) {
    resp.textContent = JSON.stringify({
      response: item.response ?? null,
      response_raw: item.response_raw,
    }, null, 2);
  } else {
    resp.textContent = typeof item.response === 'string'
      ? item.response
      : JSON.stringify(item.response ?? null, null, 2);
  }

  const inputPretty = _tryPrettifyJson(item.raw_input_line);
  const outputPretty = _tryPrettifyJson(item.raw_output_line);
  raw.textContent =
    'INPUT LINE\n' +
    '----------\n' +
    (inputPretty || '(empty)') +
    '\n\n' +
    'OUTPUT LINE\n' +
    '-----------\n' +
    (outputPretty || '(empty)');
  rawWrap.style.display = _agentLogRawVisible ? '' : 'none';
}

function _tryPrettifyJson(text) {
  const src = String(text || '').trim();
  if (!src) return '';
  try {
    return JSON.stringify(JSON.parse(src), null, 2);
  } catch (_) {
    return src;
  }
}

function toggleAgentLogRaw() {
  _agentLogRawVisible = !_agentLogRawVisible;
  const wrap = document.getElementById('alog-raw-wrap');
  if (wrap) {
    wrap.style.display = _agentLogRawVisible ? '' : 'none';
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
    updateFeatureTabs(s.capabilities || {});
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

    const cardMemory = document.getElementById('card-memory');
    if (cardMemory) {
      const mem = s.memory || {};
      if (!mem.enabled) {
        cardMemory.textContent = 'Disabled';
        cardMemory.className = 'card-value muted';
      } else {
        const corpora = Number(mem.corpora || 0);
        const docs = Number(mem.documents || 0);
        cardMemory.textContent = `${corpora} corpora / ${docs} docs`;
        cardMemory.className = 'card-value ok';
      }
    }

    const cardProcessing = document.getElementById('card-processing');
    if (cardProcessing) {
      const proc = s.processing || {};
      const total = Number(proc.total || 0);
      const running = Number(proc.running || 0);
      const retrying = Number(proc.retrying || 0);
      cardProcessing.textContent = `${total} (run:${running} retry:${retrying})`;
      cardProcessing.className = 'card-value ' + (total > 0 ? 'warn' : 'ok');
    }
  } catch (_) {
    _updateServiceBadge('offline');
  }
}

// ── Memory page ─────────────────────────────────────────────────────────────

async function loadMemoryPage(force = false) {
  const meta = document.getElementById('memory-meta');
  try {
    await loadMemoryAgents();
    await loadMemoryCorpora();
    await loadMemoryNamespaceBrowser();
    if (force || document.getElementById('page-memory')?.classList.contains('active')) {
      await loadMemoryDocuments();
    }
    const st = await apiGet('/api/memory/status');
    if (meta) {
      if (!st.enabled) {
        meta.textContent = 'Memory status: disabled';
      } else {
        meta.textContent = `Memory status: ${st.corpora} corpora / ${st.documents} docs`;
      }
    }
  } catch (e) {
    if (meta) meta.textContent = 'Memory status error: ' + (e.message || e);
  }
}

async function loadMemoryAgents() {
  const sel = document.getElementById('memory-agent-select');
  if (!sel) return;
  const prev = sel.value;
  try {
    const list = await apiGet('/api/agents/list');
    sel.innerHTML = '';

    const anyOpt = document.createElement('option');
    anyOpt.value = '';
    anyOpt.textContent = '(no ACL filter)';
    sel.appendChild(anyOpt);

    (list.agents || []).forEach(a => {
      const opt = document.createElement('option');
      opt.value = a;
      opt.textContent = a;
      sel.appendChild(opt);
    });

    if (prev && Array.from(sel.options).some(o => o.value === prev)) {
      sel.value = prev;
    }
  } catch (_) {}
}

async function loadMemoryCorpora() {
  const sel = document.getElementById('memory-corpus-select');
  const agentSel = document.getElementById('memory-agent-select');
  if (!sel) return;
  const prev = sel.value;
  const agent = agentSel?.value || '';
  const qs = agent ? ('?agent=' + encodeURIComponent(agent)) : '';
  const data = await apiGet('/api/memory/corpora' + qs);
  sel.innerHTML = '';
  (data.items || []).forEach(c => {
    const id = c.corpus_id || '';
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = `${id} (${Number(c.documents || 0)} docs)`;
    sel.appendChild(opt);
  });
  if (prev && Array.from(sel.options).some(o => o.value === prev)) {
    sel.value = prev;
  }
  const ingestCorpus = document.getElementById('memory-ingest-corpus');
  if (ingestCorpus && !ingestCorpus.value && sel.value) {
    ingestCorpus.value = sel.value;
  }
}

async function loadMemoryDocuments() {
  const sel = document.getElementById('memory-corpus-select');
  const agentSel = document.getElementById('memory-agent-select');
  if (!sel) return;
  const corpus = sel.value;
  if (!corpus) {
    _memoryDocs = [];
    _memoryDocTotal = 0;
    _renderMemoryDocSelect();
    _renderMemoryDocTable();
    _renderMemoryDocPageInfo();
    return;
  }

  if (_memoryCurrentCorpus !== corpus) {
    _memoryCurrentCorpus = corpus;
    _memoryDocOffset = 0;
  }

  const agent = agentSel?.value || '';
  const qs =
    `?corpus_id=${encodeURIComponent(corpus)}&limit=${encodeURIComponent(_memoryDocLimit)}&offset=${encodeURIComponent(_memoryDocOffset)}` +
    `&q=${encodeURIComponent(_memoryDocQuery)}&tag=${encodeURIComponent(_memoryDocTag)}` +
    `&sort_by=${encodeURIComponent(_memoryDocSortBy)}&sort_dir=${encodeURIComponent(_memoryDocSortDir)}` +
    (agent ? `&agent=${encodeURIComponent(agent)}` : '');
  try {
    const data = await apiGet('/api/memory/documents' + qs);
    _memoryDocs = Array.isArray(data.items) ? data.items : [];
    _memoryDocTotal = Number(data.total || 0);

    if (_memoryDocOffset > 0 && _memoryDocOffset >= _memoryDocTotal) {
      _memoryDocOffset = Math.max(0, _memoryDocOffset - _memoryDocLimit);
      await loadMemoryDocuments();
      return;
    }

    _renderMemoryDocSelect();
    _renderMemoryDocTable();
    _renderMemoryDocPageInfo();
  } catch (e) {
    _memoryDocs = [];
    _memoryDocTotal = 0;
    _renderMemoryDocSelect();
    _renderMemoryDocTable('Documents error: ' + (e.message || e));
    _renderMemoryDocPageInfo();
  }
}

async function loadMemoryNamespaceBrowser() {
  const agentSel = document.getElementById('memory-namespace-agent');
  const nsSel = document.getElementById('memory-namespace-select');
  const profileInp = document.getElementById('memory-namespace-profile');
  if (!agentSel || !nsSel) return;

  const prevAgent = agentSel.value;
  const prevNs = nsSel.value;

  try {
    const list = await apiGet('/api/agents/list');
    agentSel.innerHTML = '';
    (list.agents || []).forEach(a => {
      const opt = document.createElement('option');
      opt.value = a;
      opt.textContent = a;
      agentSel.appendChild(opt);
    });
    if (prevAgent && Array.from(agentSel.options).some(o => o.value === prevAgent)) {
      agentSel.value = prevAgent;
    }
  } catch (_) {}

  nsSel.innerHTML = '';
  ['semantic', 'episodic', 'procedural', 'summaries', 'profiles'].forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    nsSel.appendChild(opt);
  });
  if (prevNs && Array.from(nsSel.options).some(o => o.value === prevNs)) {
    nsSel.value = prevNs;
  }
  if (profileInp) {
    profileInp.style.display = nsSel.value === 'profiles' ? '' : 'none';
  }

  await loadMemoryNamespaceItems();
}

async function loadMemoryNamespaceItems() {
  const agentSel = document.getElementById('memory-namespace-agent');
  const nsSel = document.getElementById('memory-namespace-select');
  const profileInp = document.getElementById('memory-namespace-profile');
  const out = document.getElementById('memory-namespace-output');
  if (!agentSel || !nsSel || !out) return;

  const agent = String(agentSel.value || '').trim();
  const namespace = String(nsSel.value || '').trim();
  if (!agent || !namespace) {
    out.textContent = 'Select agent and namespace.';
    return;
  }

  const profileId = namespace === 'profiles' ? String(profileInp?.value || '').trim() : '';
  if (namespace === 'profiles' && !profileId) {
    out.textContent = 'Enter profile id for profiles namespace.';
    return;
  }

  const qs = `?limit=30${profileId ? `&profile_id=${encodeURIComponent(profileId)}` : ''}`;
  try {
    const data = await apiGet(`/api/memory/agent/${encodeURIComponent(agent)}/namespace/${encodeURIComponent(namespace)}${qs}`);
    out.textContent = JSON.stringify(data.items || [], null, 2);
  } catch (e) {
    out.textContent = 'Namespace error: ' + (e.message || e);
  }
}

function _renderMemoryDocTable(errorText) {
  const body = document.getElementById('memory-doc-table-body');
  if (!body) return;
  body.innerHTML = '';

  if (errorText) {
    const row = document.createElement('tr');
    row.innerHTML = `<td colspan="6" style="padding:10px; color:#e05555; border-bottom:1px solid var(--border);">${escapeHtml(errorText)}</td>`;
    body.appendChild(row);
    return;
  }

  if (!_memoryDocs.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td colspan="6" style="padding:10px; color:var(--muted); border-bottom:1px solid var(--border);">No documents found.</td>';
    body.appendChild(row);
    return;
  }

  const terms = _memoryHighlightTerms();

  _memoryDocs.forEach((d) => {
    const row = document.createElement('tr');
    row.style.cursor = 'pointer';
    row.onclick = () => {
      const sel = document.getElementById('memory-doc-select');
      if (sel) sel.value = String(d.doc_id || '');
    };

    const tags = Array.isArray(d.tags) ? d.tags.join(', ') : '';
    const updated = d.updated_at ? String(d.updated_at) : '';
    const summary = String(d.content_summary || d.summary || '');
    row.innerHTML =
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${highlightHtml(String(d.doc_id || ''), terms)}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${highlightHtml(String(d.title || ''), terms)}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${d.version != null ? highlightHtml(String(d.version), terms) : ''}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${highlightHtml(tags, terms)}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${highlightHtml(summary, terms)}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(updated)}</td>`;
    body.appendChild(row);
  });
}

function _renderMemoryDocPageInfo() {
  const info = document.getElementById('memory-doc-page-info');
  if (!info) return;

  if (_memoryDocTotal <= 0) {
    info.textContent = 'Page 0 / 0 (0 items)';
    return;
  }

  const page = Math.floor(_memoryDocOffset / _memoryDocLimit) + 1;
  const pages = Math.max(1, Math.ceil(_memoryDocTotal / _memoryDocLimit));
  info.textContent = `Page ${page} / ${pages} (${_memoryDocTotal} items)`;
}

function applyMemoryDocFilters() {
  const qEl = document.getElementById('memory-doc-filter-q');
  const tagEl = document.getElementById('memory-doc-filter-tag');
  const pageSizeEl = document.getElementById('memory-doc-page-size');
  const sortByEl = document.getElementById('memory-doc-sort-by');
  const sortDirEl = document.getElementById('memory-doc-sort-dir');

  _memoryDocQuery = String(qEl?.value || '').trim();
  _memoryDocTag = String(tagEl?.value || '').trim();
  const raw = Number(pageSizeEl?.value || _memoryDocLimit);
  _memoryDocLimit = Number.isFinite(raw) ? Math.max(5, Math.min(200, Math.trunc(raw))) : 20;
  _memoryDocSortBy = String(sortByEl?.value || 'updated_at');
  _memoryDocSortDir = String(sortDirEl?.value || 'desc').toLowerCase() === 'asc' ? 'asc' : 'desc';

  if (pageSizeEl) pageSizeEl.value = String(_memoryDocLimit);
  if (sortByEl) sortByEl.value = _memoryDocSortBy;
  if (sortDirEl) sortDirEl.value = _memoryDocSortDir;

  _memoryDocOffset = 0;
  loadMemoryDocuments();
}

function memoryDocFilterInputChanged() {
  if (_memoryFilterTimer) {
    clearTimeout(_memoryFilterTimer);
    _memoryFilterTimer = null;
  }
  _memoryFilterTimer = setTimeout(() => {
    _memoryFilterTimer = null;
    applyMemoryDocFilters();
  }, 400);
}

function memoryDocFilterChangedInstant() {
  if (_memoryFilterTimer) {
    clearTimeout(_memoryFilterTimer);
    _memoryFilterTimer = null;
  }
  applyMemoryDocFilters();
}

function prevMemoryDocPage() {
  if (_memoryDocOffset <= 0) return;
  _memoryDocOffset = Math.max(0, _memoryDocOffset - _memoryDocLimit);
  loadMemoryDocuments();
}

function nextMemoryDocPage() {
  if ((_memoryDocOffset + _memoryDocLimit) >= _memoryDocTotal) return;
  _memoryDocOffset += _memoryDocLimit;
  loadMemoryDocuments();
}

function _renderMemoryDocSelect() {
  const sel = document.getElementById('memory-doc-select');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';

  if (!_memoryDocs.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '(no documents)';
    sel.appendChild(opt);
    return;
  }

  _memoryDocs.forEach(d => {
    const docId = String(d.doc_id || '');
    const title = String(d.title || docId || 'document');
    const version = d.version != null ? `v${d.version}` : 'v?';
    const opt = document.createElement('option');
    opt.value = docId;
    opt.textContent = `${title} (${version})`;
    sel.appendChild(opt);
  });

  if (prev && Array.from(sel.options).some(o => o.value === prev)) {
    sel.value = prev;
  }
}

async function viewSelectedMemoryDocument(mode) {
  const sel = document.getElementById('memory-doc-select');
  const out = document.getElementById('memory-doc-view-output');
  if (!sel || !out) return;
  const docId = sel.value;
  if (!docId) {
    out.textContent = 'No document selected.';
    return;
  }
  const view = (mode || 'source').toLowerCase();
  try {
    const current = (_memoryDocs || []).find(d => String(d.doc_id || '') === String(docId));
    const version = current && current.version != null ? `&version=${encodeURIComponent(current.version)}` : '';
    const data = await apiGet(`/api/memory/document/${encodeURIComponent(docId)}?mode=${encodeURIComponent(view)}${version}`);
    const text = JSON.stringify(data.item || {}, null, 2);
    out.innerHTML = highlightHtml(text, _memoryHighlightTerms());
  } catch (e) {
    out.textContent = 'Document view error: ' + (e.message || e);
  }
}

async function deleteSelectedMemoryDocument() {
  const sel = document.getElementById('memory-doc-select');
  const out = document.getElementById('memory-doc-view-output');
  if (!sel) return;
  const docId = sel.value;
  if (!docId) {
    if (out) out.textContent = 'No document selected.';
    return;
  }
  if (!confirm(`Delete document ${docId} from retrieval index?`)) {
    return;
  }
  try {
    const res = await apiDelete(`/api/memory/document/${encodeURIComponent(docId)}`);
    if (out) out.textContent = JSON.stringify(res, null, 2);
    await loadMemoryDocuments();
    await loadMemoryPage(false);
  } catch (e) {
    if (out) out.textContent = 'Delete error: ' + (e.message || e);
  }
}

async function runMemoryIngestBatch() {
  const limitEl = document.getElementById('memory-batch-limit');
  const msgEl = document.getElementById('memory-batch-msg');
  const limitRaw = Number(limitEl?.value || 0);
  const limit = Number.isFinite(limitRaw) ? Math.max(1, Math.min(100, Math.trunc(limitRaw))) : 3;

  if (limitEl) limitEl.value = String(limit);
  if (msgEl) msgEl.textContent = 'Running batch...';

  try {
    const res = await apiPost('/api/memory/ingest/run', { limit });
    const processed = Number(res.processed || 0);
    const failed = Number(res.failed || 0);
    if (msgEl) msgEl.textContent = `Done. processed=${processed}, failed=${failed}, limit=${res.limit}`;
    await loadMemoryPage(true);
  } catch (e) {
    if (msgEl) msgEl.textContent = 'Batch error: ' + (e.message || e);
  }
}

async function queueMemoryIngestText() {
  const corpusEl = document.getElementById('memory-ingest-corpus');
  const titleEl = document.getElementById('memory-ingest-title');
  const textEl = document.getElementById('memory-ingest-text');
  const msgEl = document.getElementById('memory-ingest-msg');
  if (!corpusEl || !textEl || !msgEl) return;

  const corpus = corpusEl.value.trim();
  const title = titleEl?.value.trim() || null;
  const text = textEl.value.trim();
  if (!corpus || !text) {
    msgEl.textContent = 'corpus_id and text are required';
    return;
  }

  msgEl.textContent = 'Queueing...';
  try {
    const r = await apiPost('/api/memory/ingest', {
      corpus_id: corpus,
      title,
      source: { type: 'text', text, title },
      tags: [],
    });
    msgEl.textContent = 'Queued: ' + (r.job_id || 'ok');
    textEl.value = '';
    await loadMemoryPage(true);
  } catch (e) {
    msgEl.textContent = 'Ingest error: ' + (e.message || e);
  }
}

async function runMemorySearch() {
  const qEl = document.getElementById('memory-search-query');
  const corpusSel = document.getElementById('memory-corpus-select');
  const agentSel = document.getElementById('memory-agent-select');
  const out = document.getElementById('memory-search-output');
  if (!qEl || !out) return;

  const query = qEl.value.trim();
  if (!query) {
    out.textContent = 'Enter search query.';
    return;
  }
  const corpora = corpusSel?.value ? [corpusSel.value] : null;
  const agent = agentSel?.value || null;
  try {
    const r = await apiPost('/api/memory/search', {
      query,
      corpora,
      agent,
      limit: 12,
    });
    out.innerHTML = highlightHtml(JSON.stringify(r.items || [], null, 2), [query]);
  } catch (e) {
    out.textContent = 'Search error: ' + (e.message || e);
  }
}

// ── Memoryd page ───────────────────────────────────────────────────────────

async function loadMemorydPage(force = false) {
  const meta = document.getElementById('memoryd-meta');
  try {
    await loadMemorydEnvids();
    await loadMemorydMuids();
    await loadMemorydTypes();
    if (force || document.getElementById('page-memoryd')?.classList.contains('active')) {
      await loadMemorydRecords();
    }
    if (meta) {
      const envid = document.getElementById('memoryd-envid-select')?.value || '';
      const muid = document.getElementById('memoryd-muid-select')?.value || '';
      meta.textContent = `Memoryd ready${envid ? ` · envid=${envid}` : ''}${muid ? ` · muid=${muid}` : ''}`;
    }
  } catch (e) {
    if (meta) meta.textContent = 'Memoryd error: ' + (e.message || e);
  }
}

async function loadMemorydEnvids() {
  const sel = document.getElementById('memoryd-envid-select');
  if (!sel) return;
  const prev = sel.value;
  const data = await apiGet('/api/memoryd/envids');
  _memorydEnvids = Array.isArray(data.items) ? data.items : [];
  sel.innerHTML = '';

  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = '(base config)';
  sel.appendChild(empty);

  _memorydEnvids.forEach(item => {
    const opt = document.createElement('option');
    opt.value = String(item.envid || '');
    opt.textContent = `${item.title || item.envid}${item.enabled ? ' · on' : ' · off'}`;
    sel.appendChild(opt);
  });

  if (prev && Array.from(sel.options).some(o => o.value === prev)) {
    sel.value = prev;
  }
  if (!_memorydCurrentEnvid && sel.value) {
    _memorydCurrentEnvid = sel.value;
  }
}

async function loadMemorydMuids() {
  const sel = document.getElementById('memoryd-muid-select');
  const envidSel = document.getElementById('memoryd-envid-select');
  if (!sel) return;
  const envid = String(envidSel?.value || '').trim();
  const prev = sel.value;
  const qs = envid ? `?envid=${encodeURIComponent(envid)}` : '';
  const data = await apiGet('/api/memoryd/muids' + qs);
  _memorydMuids = Array.isArray(data.items) ? data.items : [];
  sel.innerHTML = '';

  const defaultMuid = String(data.default_muid || 'default').trim();
  const defaultOpt = document.createElement('option');
  defaultOpt.value = defaultMuid;
  defaultOpt.textContent = `(default) ${defaultMuid}`;
  sel.appendChild(defaultOpt);

  _memorydMuids.forEach(muid => {
    const opt = document.createElement('option');
    opt.value = String(muid || '');
    opt.textContent = String(muid || '');
    sel.appendChild(opt);
  });

  if (prev && Array.from(sel.options).some(o => o.value === prev)) {
    sel.value = prev;
  } else if (!sel.value) {
    sel.value = defaultMuid;
  }
}

async function loadMemorydTypes() {
  const wrap = document.getElementById('memoryd-types');
  if (!wrap) return;
  const envidSel = document.getElementById('memoryd-envid-select');
  const envid = String(envidSel?.value || '').trim();
  const item = _memorydEnvids.find(x => String(x.envid || '') === envid) || null;
  const types = Array.isArray(item?.types) && item.types.length ? item.types : ['episodic', 'semantic', 'summaries', 'profiles'];
  const active = new Set(_memorydSelectedTypes.length ? _memorydSelectedTypes : types);
  wrap.innerHTML = '';
  types.forEach(typeName => {
    const label = document.createElement('label');
    label.style.display = 'inline-flex';
    label.style.alignItems = 'center';
    label.style.gap = '6px';
    label.style.padding = '4px 8px';
    label.style.border = '1px solid var(--border)';
    label.style.borderRadius = '999px';
    label.style.background = 'rgba(255,255,255,.03)';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = typeName;
    input.checked = active.has(typeName);
    input.onchange = () => {
      const checked = Array.from(wrap.querySelectorAll('input[type=checkbox]:checked')).map(el => String(el.value || '').trim()).filter(Boolean);
      _memorydSelectedTypes = checked;
      loadMemorydRecords();
    };
    const text = document.createElement('span');
    text.textContent = typeName;
    label.appendChild(input);
    label.appendChild(text);
    wrap.appendChild(label);
  });
  _memorydSelectedTypes = Array.from(wrap.querySelectorAll('input[type=checkbox]:checked')).map(el => String(el.value || '').trim()).filter(Boolean);
}

async function loadMemorydRecords() {
  const envidSel = document.getElementById('memoryd-envid-select');
  const muidSel = document.getElementById('memoryd-muid-select');
  const out = document.getElementById('memoryd-record-output');
  if (!muidSel || !out) return;
  const muid = String(muidSel.value || '').trim();
  const envid = String(envidSel?.value || '').trim();
  const types = _memorydSelectedTypes.length ? _memorydSelectedTypes : Array.from(document.querySelectorAll('#memoryd-types input[type=checkbox]:checked')).map(el => String(el.value || '').trim()).filter(Boolean);
  if (!muid) {
    out.textContent = 'Select MUID.';
    return;
  }
  const qs = new URLSearchParams();
  qs.set('muid', muid);
  if (types.length) qs.set('types', types.join(','));
  if (envid) qs.set('envid', envid);
  try {
    const data = await apiGet('/api/memoryd/records?' + qs.toString());
    _memorydRecords = Array.isArray(data.items) ? data.items : [];
    if (_memorydRecords.length) {
      if (!_memorydRecords.some(r => String(r.id || '') === String(_memorydSelectedRecordId || ''))) {
        _memorydSelectedRecordId = String(_memorydRecords[0].id || '');
      }
    } else {
      _memorydSelectedRecordId = '';
    }
    _renderMemorydRecords();
    _renderMemorydRecordDetail();
  } catch (e) {
    out.textContent = 'Memoryd records error: ' + (e.message || e);
  }
}

function _renderMemorydRecords() {
  const body = document.getElementById('memoryd-record-table-body');
  if (!body) return;
  body.innerHTML = '';
  if (!_memorydRecords.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td colspan="6" style="padding:10px; color:var(--muted); border-bottom:1px solid var(--border);">No records found.</td>';
    body.appendChild(row);
    return;
  }
  _memorydRecords.forEach(record => {
    const id = String(record.id || '');
    const row = document.createElement('tr');
    row.style.cursor = 'pointer';
    row.onclick = () => {
      _memorydSelectedRecordId = id;
      _renderMemorydRecords();
      _renderMemorydRecordDetail();
    };
    row.style.background = id === _memorydSelectedRecordId ? 'rgba(91,138,247,.12)' : 'transparent';
    row.innerHTML =
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(record.id || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(record.type || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(record.title || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(record.importance ?? ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(record.updated_at || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(record.body || '').slice(0, 120))}</td>`;
    body.appendChild(row);
  });
}

function _renderMemorydRecordDetail() {
  const out = document.getElementById('memoryd-record-output');
  const selected = _memorydRecords.find(r => String(r.id || '') === String(_memorydSelectedRecordId || '')) || null;
  if (!out) return;
  out.value = selected ? JSON.stringify(selected, null, 2) : '';
  const meta = document.getElementById('memoryd-record-meta');
  if (meta) {
    meta.textContent = selected ? `Selected record ${selected.id}` : 'No record selected.';
  }
}

async function memorydRunTick() {
  const limitEl = document.getElementById('memoryd-run-limit');
  const msg = document.getElementById('memoryd-msg');
  const raw = Number(limitEl?.value || 1);
  const limit = Number.isFinite(raw) ? Math.max(1, Math.min(100, Math.trunc(raw))) : 1;
  if (limitEl) limitEl.value = String(limit);
  if (msg) msg.textContent = 'Running...';
  try {
    const res = await apiPost('/api/memoryd/tasks/run', { limit });
    if (msg) msg.textContent = `picked=${res.picked || 0} started=${res.started || 0} done=${res.done || 0} failed=${res.failed || 0} pruned=${res.pruned || 0}`;
    await loadMemorydPage(true);
  } catch (e) {
    if (msg) msg.textContent = 'Run error: ' + (e.message || e);
  }
}

async function memorydEnqueueTask() {
  const envidSel = document.getElementById('memoryd-envid-select');
  const muidSel = document.getElementById('memoryd-muid-select');
  const callerTagEl = document.getElementById('memoryd-caller-tag');
  const srcEl = document.getElementById('memoryd-source-context');
  const finalEl = document.getElementById('memoryd-final-response');
  const msg = document.getElementById('memoryd-msg');
  const envid = String(envidSel?.value || '').trim();
  const muid = String(muidSel?.value || '').trim();
  const caller_tag = String(callerTagEl?.value || '').trim() || null;
  const sourceRaw = String(srcEl?.value || '').trim();
  const final_response = String(finalEl?.value || '').trim();
  const types = _memorydSelectedTypes.length ? _memorydSelectedTypes : Array.from(document.querySelectorAll('#memoryd-types input[type=checkbox]:checked')).map(el => String(el.value || '').trim()).filter(Boolean);
  if (!muid || !sourceRaw || !final_response) {
    if (msg) msg.textContent = 'muid, source context and final response are required';
    return;
  }
  let source_context;
  try {
    source_context = JSON.parse(sourceRaw);
  } catch (e) {
    if (msg) msg.textContent = 'Source context must be valid JSON';
    return;
  }
  if (msg) msg.textContent = 'Queueing...';
  try {
    const res = await apiPost('/api/memoryd/tasks/enqueue', {
      source_context: { ...source_context, envid },
      final_response,
      muid,
      caller_tag,
      types,
    });
    if (msg) msg.textContent = `Queued ${res.task_id || 'ok'}`;
    await loadMemorydPage(true);
  } catch (e) {
    if (msg) msg.textContent = 'Enqueue error: ' + (e.message || e);
  }
}

async function memorydRefreshEnvids() {
  const msg = document.getElementById('memoryd-msg');
  if (msg) msg.textContent = 'Reloading config...';
  try {
    const data = await apiPost('/api/memoryd/config/reload');
    _memorydEnvids = Array.isArray(data.items) ? data.items : [];
    const sel = document.getElementById('memoryd-envid-select');
    if (sel) {
      const prev = sel.value;
      sel.innerHTML = '';
      const empty = document.createElement('option');
      empty.value = '';
      empty.textContent = '(base config)';
      sel.appendChild(empty);
      _memorydEnvids.forEach(item => {
        const opt = document.createElement('option');
        opt.value = String(item.envid || '');
        opt.textContent = `${item.title || item.envid}${item.enabled ? ' · on' : ' · off'}`;
        sel.appendChild(opt);
      });
      if (prev && Array.from(sel.options).some(o => o.value === prev)) sel.value = prev;
    }
    await loadMemorydMuids();
    await loadMemorydTypes();
    if (msg) msg.textContent = `Config reloaded — ${_memorydEnvids.length} envid(s) found`;
  } catch (e) {
    if (msg) msg.textContent = 'Config reload error: ' + (e.message || e);
  }
}

async function memorydDeleteSelectedRecord() {
  const selected = _memorydRecords.find(r => String(r.id || '') === String(_memorydSelectedRecordId || '')) || null;
  const msg = document.getElementById('memoryd-msg');
  if (!selected) return;
  if (!confirm(`Delete memoryd record ${selected.id}?`)) return;
  try {
    await apiDelete(`/api/memoryd/records/${encodeURIComponent(selected.id)}`);
    if (msg) msg.textContent = `Deleted ${selected.id}`;
    await loadMemorydPage(true);
  } catch (e) {
    if (msg) msg.textContent = 'Delete error: ' + (e.message || e);
  }
}

async function memorydSaveSelectedRecord() {
  const out = document.getElementById('memoryd-record-output');
  const msg = document.getElementById('memoryd-msg');
  if (!out) return;
  let payload;
  try {
    payload = JSON.parse(out.value || '{}');
  } catch (e) {
    if (msg) msg.textContent = 'Selected record JSON is invalid';
    return;
  }
  try {
    await apiPost('/api/memoryd/records/upsert', { payload });
    if (msg) msg.textContent = 'Record saved';
    await loadMemorydPage(true);
  } catch (e) {
    if (msg) msg.textContent = 'Save error: ' + (e.message || e);
  }
}

// ── MemoryExecutor page ───────────────────────────────────────────────────

async function loadMemoryExecutorPage(force = false) {
  const meta = document.getElementById('memory-executor-meta');
  try {
    setupMemoryExecutorSplitter();
    await loadMemoryExecutorEnvidSelect();
    await loadMemoryExecutorTemplates();
    await loadMemoryExecutorTasks();
    if (force || document.getElementById('page-memoryexecutor')?.classList.contains('active')) {
      renderMemoryExecutorTaskList();
      renderMemoryExecutorSelectedTask();
    }
    if (meta) {
      meta.textContent = `Tasks: ${_memoryExecutorTasks.length}`;
    }
  } catch (e) {
    if (meta) meta.textContent = 'MemoryExecutor error: ' + (e.message || e);
  }
}

async function loadMemoryExecutorEnvidSelect() {
  const sel = document.getElementById('memory-executor-envid-select');
  const filterInput = document.getElementById('memory-executor-envid-filter');
  if (!sel) return;

  const prevSelected = String(sel.value || '').trim();
  const prevFilter = String(filterInput?.value || '').trim();
  const data = await apiGet('/api/memoryd/envids');
  _memoryExecutorEnvidItems = Array.isArray(data.items) ? data.items : [];

  sel.innerHTML = '';
  const emptyOpt = document.createElement('option');
  emptyOpt.value = '';
  emptyOpt.textContent = '(select envid)';
  sel.appendChild(emptyOpt);

  _memoryExecutorEnvidItems.forEach((item) => {
    const envid = String(item.envid || '').trim();
    if (!envid) return;
    const opt = document.createElement('option');
    opt.value = envid;
    opt.textContent = `${item.title || envid}${item.enabled ? ' · on' : ' · off'}`;
    sel.appendChild(opt);
  });

  const availableValues = Array.from(sel.options).map((o) => String(o.value || ''));
  if (prevSelected && availableValues.includes(prevSelected)) {
    sel.value = prevSelected;
  } else if (prevFilter && availableValues.includes(prevFilter)) {
    sel.value = prevFilter;
  } else {
    sel.value = '';
  }
}

function memoryExecutorApplyEnvidFromSelect() {
  const sel = document.getElementById('memory-executor-envid-select');
  const filterInput = document.getElementById('memory-executor-envid-filter');
  if (!sel || !filterInput) return;
  filterInput.value = String(sel.value || '').trim();
  loadMemoryExecutorPage(true);
}

function memoryExecutorSetSelectValue(selectEl, value, labelIfMissing = '') {
  if (!selectEl) return;
  const wanted = String(value || '').trim();
  const options = Array.from(selectEl.options || []);
  const found = options.find((o) => String(o.value || '') === wanted);
  if (found) {
    selectEl.value = wanted;
    return;
  }
  if (wanted) {
    const opt = document.createElement('option');
    opt.value = wanted;
    opt.textContent = labelIfMissing || wanted;
    selectEl.appendChild(opt);
    selectEl.value = wanted;
  } else {
    selectEl.value = '';
  }
}

function memoryExecutorRenderAllowedTypesHint() {
  const hintEl = document.getElementById('memory-executor-types-hint');
  const envidSel = document.getElementById('memory-executor-field-envid-select');
  if (!hintEl) return;

  const envid = String(envidSel?.value || '').trim();
  const item = _memoryExecutorEnvidItems.find((x) => String(x.envid || '').trim() === envid) || null;
  const contextTypes = Array.isArray(item?.memory_executor_context_types) && item.memory_executor_context_types.length
    ? item.memory_executor_context_types.map((t) => String(t || '').trim()).filter(Boolean)
    : (Array.isArray(item?.types) && item.types.length
      ? item.types.map((t) => String(t || '').trim()).filter(Boolean)
      : ['episodic', 'semantic', 'summaries', 'profiles']);
  const updateTypes = Array.isArray(item?.memory_executor_update_types) && item.memory_executor_update_types.length
    ? item.memory_executor_update_types.map((t) => String(t || '').trim()).filter(Boolean)
    : ['episodic', 'semantic', 'summaries', 'profiles'];
  const scopeText = envid ? `envid "${envid}"` : 'base config';
  hintEl.innerHTML =
    `Allowed Context types for ${escapeHtml(scopeText)}: ${escapeHtml(contextTypes.join(', '))}<br>` +
    `Allowed Update types for ${escapeHtml(scopeText)}: ${escapeHtml(updateTypes.join(', '))}`;
}

async function memoryExecutorLoadEditorEnvidSelect(preferredEnvid = '') {
  const sel = document.getElementById('memory-executor-field-envid-select');
  if (!sel) return;

  if (!_memoryExecutorEnvidItems.length) {
    const data = await apiGet('/api/memoryd/envids');
    _memoryExecutorEnvidItems = Array.isArray(data.items) ? data.items : [];
  }

  const prev = String(sel.value || '').trim();
  sel.innerHTML = '';
  const baseOpt = document.createElement('option');
  baseOpt.value = '';
  baseOpt.textContent = '(base config)';
  sel.appendChild(baseOpt);

  _memoryExecutorEnvidItems.forEach((item) => {
    const envid = String(item.envid || '').trim();
    if (!envid) return;
    const opt = document.createElement('option');
    opt.value = envid;
    opt.textContent = `${item.title || envid}${item.enabled ? ' · on' : ' · off'}`;
    sel.appendChild(opt);
  });

  const candidate = String(preferredEnvid || prev || '').trim();
  memoryExecutorSetSelectValue(sel, candidate, candidate);
  memoryExecutorRenderAllowedTypesHint();
}

async function memoryExecutorLoadEditorMuids(preferredMuid = '') {
  const envidSel = document.getElementById('memory-executor-field-envid-select');
  const muidSel = document.getElementById('memory-executor-field-muid-select');
  if (!muidSel) return;

  const envid = String(envidSel?.value || '').trim();
  const qs = envid ? `?envid=${encodeURIComponent(envid)}` : '';
  const data = await apiGet('/api/memoryd/muids' + qs);
  const items = Array.isArray(data.items) ? data.items : [];
  const defaultMuid = String(data.default_muid || 'default').trim() || 'default';

  muidSel.innerHTML = '';
  const emptyOpt = document.createElement('option');
  emptyOpt.value = '';
  emptyOpt.textContent = '(select muid)';
  muidSel.appendChild(emptyOpt);

  const defOpt = document.createElement('option');
  defOpt.value = defaultMuid;
  defOpt.textContent = `(default) ${defaultMuid}`;
  muidSel.appendChild(defOpt);

  items.forEach((muid) => {
    const text = String(muid || '').trim();
    if (!text) return;
    const opt = document.createElement('option');
    opt.value = text;
    opt.textContent = text;
    muidSel.appendChild(opt);
  });

  memoryExecutorSetSelectValue(muidSel, preferredMuid, preferredMuid || defaultMuid);
}

function memoryExecutorApplyEditorEnvidFromSelect() {
  memoryExecutorRenderAllowedTypesHint();
  memoryExecutorLoadEditorMuids('').catch(() => {});
}

function memoryExecutorApplyMuidFromSelect() {
  const muidSel = document.getElementById('memory-executor-field-muid-select');
  const muidInput = document.getElementById('memory-executor-field-muid');
  if (!muidSel || !muidInput) return;
  const value = String(muidSel.value || '').trim();
  if (!value) return;
  muidInput.value = value;
}

function memoryExecutorSyncEditorEnvidAndMuids(envidValue = '', muidValue = '') {
  memoryExecutorLoadEditorEnvidSelect(envidValue)
    .then(() => memoryExecutorLoadEditorMuids(muidValue))
    .catch(() => {});
}

async function loadMemoryExecutorTemplates() {
  const sel = document.getElementById('memory-executor-template-select');
  if (!sel) return;
  const prev = sel.value;
  const data = await apiGet('/api/memory-executor/templates');
  _memoryExecutorTemplates = Array.isArray(data.items) ? data.items : [];
  sel.innerHTML = '';

  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = '(no template)';
  sel.appendChild(empty);

  _memoryExecutorTemplates.forEach((item) => {
    const name = String(item.name || '').trim();
    if (!name) return;
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  });

  if (prev && Array.from(sel.options).some(o => o.value === prev)) {
    sel.value = prev;
  }
}

async function loadMemoryExecutorTasks() {
  const envidEl = document.getElementById('memory-executor-envid-filter');
  const envid = String(envidEl?.value || '').trim();
  const qs = envid ? '?envid=' + encodeURIComponent(envid) : '';
  const data = await apiGet('/api/memory-executor/tasks' + qs);
  _memoryExecutorTasks = Array.isArray(data.items) ? data.items : [];
  if (_memoryExecutorTasks.length) {
    if (!_memoryExecutorTasks.some(t => String(t.id || '') === String(_memoryExecutorSelectedId || ''))) {
      _memoryExecutorSelectedId = String(_memoryExecutorTasks[0].id || '');
    }
  } else {
    _memoryExecutorSelectedId = '';
  }
}

function renderMemoryExecutorTaskList() {
  const body = document.getElementById('memory-executor-table-body');
  if (!body) return;
  body.innerHTML = '';

  if (!_memoryExecutorTasks.length) {
    const row = document.createElement('tr');
    row.innerHTML = '<td colspan="8" style="padding:10px; color:var(--muted); border-bottom:1px solid var(--border);">No tasks found.</td>';
    body.appendChild(row);
    return;
  }

  _memoryExecutorTasks.forEach((task) => {
    const id = String(task.id || '');
    const row = document.createElement('tr');
    row.style.cursor = 'pointer';
    row.style.background = id === _memoryExecutorSelectedId ? 'rgba(91,138,247,.12)' : 'transparent';
    row.onclick = () => {
      _memoryExecutorSelectedId = id;
      renderMemoryExecutorTaskList();
      renderMemoryExecutorSelectedTask();
    };
    row.innerHTML =
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(task.id || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(task.name || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(task.envid || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(task.muid || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(task.todo_title || ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(task.period_sec ?? ''))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${escapeHtml(String(task.execution_policy || 'idle'))}</td>` +
      `<td style="padding:8px; border-bottom:1px solid var(--border);">${task.enabled ? 'yes' : 'no'}</td>`;
    body.appendChild(row);
  });
}

function renderMemoryExecutorSelectedTask() {
  const selected = _memoryExecutorTasks.find(t => String(t.id || '') === String(_memoryExecutorSelectedId || '')) || null;
  const task = selected || memoryExecutorNewTaskTemplate();

  const setText = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = value == null ? '' : String(value);
  };

  const setCheck = (id, value) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.checked = Boolean(value);
  };

  const idTextEl = document.getElementById('memory-executor-field-id-text');
  if (idTextEl) {
    idTextEl.textContent = task.id ? String(task.id) : '(new task)';
  }
  setText('memory-executor-field-name', task.name || '');
  setText('memory-executor-field-muid', task.muid || '');
  setText('memory-executor-field-period', task.period_sec == null ? '' : task.period_sec);
  setText('memory-executor-field-todo-title', task.todo_title || '');
  setText('memory-executor-field-provider', task.provider || '');
  setText('memory-executor-field-model', task.model || '');
  setText('memory-executor-field-enqueue-key', task.enqueue_key || '');
  setText('memory-executor-field-request-text', task.request_text || '');
  setText('memory-executor-field-tools', Array.isArray(task.tools) ? task.tools.join(',') : '');
  setText('memory-executor-field-context-types', Array.isArray(task.context_types) ? task.context_types.join(',') : '');
  setText('memory-executor-field-update-types', Array.isArray(task.update_types) ? task.update_types.join(',') : '');
  setText('memory-executor-field-policy', task.execution_policy || 'idle');
  setCheck('memory-executor-field-enabled', task.enabled !== false);
  memoryExecutorSyncEditorEnvidAndMuids(String(task.envid || '').trim(), String(task.muid || '').trim());
}

function memoryExecutorNewTaskTemplate() {
  return {
    name: 'task-name',
    enabled: true,
    envid: null,
    muid: 'default',
    period_sec: 3600,
    todo_title: null,
    request_text: 'Summarize recent dialogue and update semantic/profile memories.',
    provider: null,
    model: null,
    tools: null,
    context_types: ['semantic', 'profiles', 'todo'],
    update_types: ['semantic', 'profiles'],
    execution_policy: 'idle',
    enqueue_key: null
  };
}

function memoryExecutorPrepareNewTask() {
  _memoryExecutorSelectedId = '';
  renderMemoryExecutorTaskList();
  renderMemoryExecutorSelectedTask();
}

function memoryExecutorApplyTemplate() {
  const sel = document.getElementById('memory-executor-template-select');
  const textEl = document.getElementById('memory-executor-field-request-text');
  const msg = document.getElementById('memory-executor-msg');
  if (!sel || !textEl) return;

  const selectedName = String(sel.value || '').trim();
  if (!selectedName) {
    if (msg) msg.textContent = 'Select template first.';
    return;
  }
  const tpl = _memoryExecutorTemplates.find(item => String(item.name || '').trim() === selectedName);
  if (!tpl) {
    if (msg) msg.textContent = 'Template not found.';
    return;
  }
  textEl.value = String(tpl.text || '');
  if (msg) msg.textContent = `Template ${selectedName} copied into task.request_text`;
}

function memoryExecutorReadTaskFromForm() {
  const readText = (id) => {
    const el = document.getElementById(id);
    return String(el?.value || '').trim();
  };
  const readList = (id) => readText(id).split(',').map(v => v.trim()).filter(Boolean);
  const readOptionalText = (id) => {
    const v = readText(id);
    return v ? v : null;
  };

  const enabledEl = document.getElementById('memory-executor-field-enabled');
  const periodRaw = readText('memory-executor-field-period');
  const period = periodRaw === '' ? null : Number(periodRaw);
  const idText = String(_memoryExecutorSelectedId || '').trim();
  const envidSel = document.getElementById('memory-executor-field-envid-select');

  const task = {
    name: readText('memory-executor-field-name'),
    enabled: Boolean(enabledEl?.checked),
    envid: String(envidSel?.value || '').trim() || null,
    muid: readText('memory-executor-field-muid'),
    period_sec: Number.isFinite(period) && period !== null ? Math.max(0, Math.trunc(period)) : null,
    todo_title: readOptionalText('memory-executor-field-todo-title'),
    request_text: String(document.getElementById('memory-executor-field-request-text')?.value || ''),
    provider: readOptionalText('memory-executor-field-provider'),
    model: readOptionalText('memory-executor-field-model'),
    tools: (() => {
      const text = readText('memory-executor-field-tools');
      if (!text) return null;
      return text.split(',').map(v => v.trim()).filter(Boolean);
    })(),
    context_types: readList('memory-executor-field-context-types'),
    update_types: readList('memory-executor-field-update-types'),
    execution_policy: readText('memory-executor-field-policy') || 'idle',
    enqueue_key: readOptionalText('memory-executor-field-enqueue-key'),
  };
  if (idText) task.id = idText;
  return task;
}

async function memoryExecutorSaveTask() {
  const msg = document.getElementById('memory-executor-msg');
  const task = memoryExecutorReadTaskFromForm();

  if (!task.name || !task.muid || !String(task.request_text || '').trim()) {
    if (msg) msg.textContent = 'Required fields: name, muid, request_text';
    return;
  }

  try {
    if (task.id) {
      await fetch('/api/memory-executor/tasks/' + encodeURIComponent(String(task.id)), {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task }),
      }).then(async (r) => {
        if (!r.ok) {
          let detail = '';
          try {
            const payload = await r.json();
            detail = payload?.detail ? ` (${payload.detail})` : '';
          } catch (_) {}
          throw new Error(`PUT /api/memory-executor/tasks/${encodeURIComponent(String(task.id))} -> ${r.status}${detail}`);
        }
        return r.json();
      });
      if (msg) msg.textContent = `Updated ${task.id}`;
    } else {
      const res = await apiPost('/api/memory-executor/tasks', { task });
      if (msg) msg.textContent = `Created ${res.item?.id || 'task'}`;
      if (res.item?.id) _memoryExecutorSelectedId = String(res.item.id);
    }
    await loadMemoryExecutorTasks();
    renderMemoryExecutorTaskList();
    renderMemoryExecutorSelectedTask();
  } catch (e) {
    if (msg) msg.textContent = 'Save error: ' + (e.message || e);
  }
}

async function memoryExecutorDeleteTask() {
  const selected = _memoryExecutorTasks.find(t => String(t.id || '') === String(_memoryExecutorSelectedId || '')) || null;
  const msg = document.getElementById('memory-executor-msg');
  if (!selected) {
    if (msg) msg.textContent = 'No selected task.';
    return;
  }
  if (!confirm(`Delete memory executor task ${selected.id}?`)) return;
  try {
    await apiDelete('/api/memory-executor/tasks/' + encodeURIComponent(String(selected.id)));
    if (msg) msg.textContent = `Deleted ${selected.id}`;
    _memoryExecutorSelectedId = '';
    await loadMemoryExecutorTasks();
    renderMemoryExecutorTaskList();
    renderMemoryExecutorSelectedTask();
  } catch (e) {
    if (msg) msg.textContent = 'Delete error: ' + (e.message || e);
  }
}

async function memoryExecutorRunNow() {
  const envidEl = document.getElementById('memory-executor-envid-filter');
  const msg = document.getElementById('memory-executor-msg');
  const envid = String(envidEl?.value || '').trim() || null;
  if (msg) msg.textContent = 'Running memory_executor hooks...';
  try {
    const res = await apiPost('/api/memory-executor/tasks/run', { envid });
    if (msg) msg.textContent = `Hooks: called=${res.called || 0}, failed=${res.failed || 0}`;
  } catch (e) {
    if (msg) msg.textContent = 'Run error: ' + (e.message || e);
  }
}

function setupMemoryExecutorSplitter() {
  if (_memoryExecutorSplitterReady) return;
  const layout = document.getElementById('memory-executor-layout');
  const splitter = document.getElementById('memory-executor-splitter');
  const right = document.querySelector('.memory-executor-right');
  if (!layout || !splitter || !right) return;

  let dragging = false;

  const applyWidthFromX = (clientX) => {
    const rect = layout.getBoundingClientRect();
    const total = rect.width;
    if (total <= 0) return;
    const rightWidth = Math.max(280, Math.min(total * 0.75, rect.right - clientX));
    const rightPct = (rightWidth / total) * 100;
    right.style.flexBasis = `${rightPct.toFixed(2)}%`;
  };

  const onMove = (ev) => {
    if (!dragging) return;
    applyWidthFromX(ev.clientX);
  };

  const stopDrag = () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove('dragging');
    document.body.style.cursor = '';
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('mouseup', stopDrag);
  };

  splitter.addEventListener('mousedown', (ev) => {
    dragging = true;
    splitter.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', stopDrag);
    ev.preventDefault();
  });

  _memoryExecutorSplitterReady = true;
}

function _memoryHighlightTerms() {
  const terms = [];
  const qEl = document.getElementById('memory-doc-filter-q');
  const tagEl = document.getElementById('memory-doc-filter-tag');
  const searchEl = document.getElementById('memory-search-query');
  [qEl?.value, tagEl?.value, searchEl?.value].forEach(value => {
    const term = String(value || '').trim();
    if (term) terms.push(term);
  });
  return terms;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function highlightHtml(text, terms) {
  const source = String(text ?? '');
  const list = Array.isArray(terms) ? terms.map(t => String(t || '').trim()).filter(Boolean) : [];
  if (!list.length || !source) return escapeHtml(source);

  const unique = [...new Set(list)].sort((a, b) => b.length - a.length);
  const pattern = unique.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  if (!pattern) return escapeHtml(source);

  const re = new RegExp(`(${pattern})`, 'gi');
  return escapeHtml(source).replace(re, '<mark style="background: rgba(91,138,247,.35); color: inherit; padding: 0 2px; border-radius: 3px;">$1</mark>');
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

const AGENT_TEMPLATE_TELEGRAM = `{
  "adapter": "telegram",
  "chat_id": -5090882532,
  "chat_type": "group",
  "chat_username": "my_group",
  "message_id": 12345,
  "user_id": 777000111,
  "username": "john_doe",
  "display_name": "John Doe",
  "mentioned": false,
  "direct_address": false,
  "recent_messages": [
    {
      "role": "user",
      "message_id": 12340,
      "date": "2026-05-26 10:00:00+00:00",
      "user_id": 111,
      "display_name": "Alice",
      "username": "alice",
      "text": "Hello everyone"
    },
    {
      "role": "assistant",
      "message_id": null,
      "date": "",
      "user_id": null,
      "display_name": "@your_bot",
      "username": "your_bot",
      "text": "Hello"
    },
    {
      "role": "user",
      "message_id": 12345,
      "date": "2026-05-26 10:01:00+00:00",
      "user_id": 777000111,
      "display_name": "John Doe",
      "username": "john_doe",
      "text": "Your new question"
    }
  ]
}`;

function insertAgentTemplateTelegram() {
  const inp = document.getElementById("agent-chat-input");
  if (!inp) return;
  const suffix = inp.value.trim() ? "\n\n" : "";
  inp.value = inp.value + suffix + AGENT_TEMPLATE_TELEGRAM;
  inp.focus();
  inp.selectionStart = inp.selectionEnd = inp.value.length;
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
  if (_memoryFilterTimer) { clearTimeout(_memoryFilterTimer); _memoryFilterTimer = null; }
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
    loadAgentLogPage();
    loadMemoryPage();
    setupLogPanel();
    _startStatusPoll();
  } catch (_) {
    showScreen('login');
  }
})();
