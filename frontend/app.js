const API_BASE = (location.hostname === 'localhost' && location.port === '8000') ? '' : 'http://localhost:8000';
const $ = id => document.getElementById(id);
let state = {
  ontologies: [], candidates: [], graphTab: 'schema',
  currentOntology: null, editingId: null, filter: 'all', editorMode: 'form', formObjects: [],
  projects: [], currentProject: null,
  namespaces: [], currentNamespace: null,
  actions: [], currentAction: null, actionToken: null,
  logs: [], logSource: 'server',
  selectedIngestObject: null,
  instances: [], selectedInstance: null,
  expandedTreeNodes: new Set(['__root__']),
  llmSettings: {}, healthDetails: null,
};

const CLIENT_LOG_KEY = 'op_client_logs';
const LOG_RETENTION_MS = 2 * 24 * 60 * 60 * 1000;

function clientLog(level, message, extra) {
  const entry = {
    ts: new Date().toISOString(),
    level: level.toUpperCase(),
    src: 'frontend',
    msg: message,
    url: location.href,
    ...(extra || {}),
  };
  let logs = JSON.parse(localStorage.getItem(CLIENT_LOG_KEY) || '[]');
  logs.unshift(entry);
  const cutoff = Date.now() - LOG_RETENTION_MS;
  logs = logs.filter(l => new Date(l.ts).getTime() > cutoff);
  localStorage.setItem(CLIENT_LOG_KEY, JSON.stringify(logs.slice(0, 2000)));
  if (level === 'error' || level === 'warning') {
    // 静默上报后端，不阻塞
    try {
      api('/api/client-logs', {
        method: 'POST',
        body: JSON.stringify({
          level: entry.level,
          message: entry.msg,
          stack: extra?.stack,
          url: entry.url,
          user_agent: navigator.userAgent,
        }),
        projectId: null,
      }).catch(() => {});
    } catch (e) { /* ignore */ }
  }
}

function getClientLogs() {
  const cutoff = Date.now() - LOG_RETENTION_MS;
  return JSON.parse(localStorage.getItem(CLIENT_LOG_KEY) || '[]')
    .filter(l => new Date(l.ts).getTime() > cutoff);
}

function clearClientLogs() {
  localStorage.removeItem(CLIENT_LOG_KEY);
  state.logs = [];
  renderLogs();
}

async function loadServerLogs() {
  state.logSource = 'server';
  $('logPanelTitle').textContent = '后端日志';
  try {
    const j = await api('/api/logs?days=2&limit=2000', { projectId: null, namespace: null });
    state.logs = j.data.items || [];
    renderLogs();
  } catch (e) { toast('加载后端日志失败: ' + e.message, 'err'); }
}

function loadClientLogs() {
  state.logSource = 'client';
  $('logPanelTitle').textContent = '本地日志';
  state.logs = getClientLogs();
  renderLogs();
}

function renderLogs() {
  const filter = ($('logFilter').value || '').toLowerCase();
  const level = $('logLevel').value;
  let items = state.logs || [];
  if (level !== 'ALL') items = items.filter(l => (l.level || '').toUpperCase() === level);
  if (filter) items = items.filter(l =>
    (l.msg || '').toLowerCase().includes(filter) ||
    (l.src || '').toLowerCase().includes(filter) ||
    (l.level || '').toLowerCase().includes(filter)
  );
  $('logCount').textContent = `共 ${items.length} 条`;
  if (!items.length) { $('logList').innerHTML = '<div class="text-slate-500">暂无日志</div>'; return; }
  $('logList').innerHTML = items.slice(0, 500).map(l => {
    const color = ({ ERROR: 'text-red-400', WARNING: 'text-amber-400', INFO: 'text-slate-300', DEBUG: 'text-slate-500' })[l.level] || 'text-slate-300';
    const ts = l.ts ? new Date(l.ts).toLocaleString() : '';
    const detail = JSON.stringify({ ...l, ts: undefined, msg: undefined }, null, 0).slice(1, -1);
    return `<div class="border-b border-slate-800 pb-1"><span class="text-slate-500">${esc(ts)}</span> <span class="${color} font-bold">${esc(l.level || '')}</span> <span class="text-slate-400">[${esc(l.src || '')}]</span> ${esc(l.msg || '')}<div class="text-slate-600 pl-4">${esc(detail)}</div></div>`;
  }).join('');
}

function _currentProjectId() {
  return state.currentProject?.id || localStorage.getItem('op_currentProject') || 'default';
}

function _currentNamespace() {
  return state.currentNamespace?.id || localStorage.getItem('op_currentNamespace') || _currentProjectId();
}

async function api(path, opts = {}) {
  const start = performance.now();
  const isForm = opts.body instanceof FormData;
  const headers = isForm ? {} : { 'Content-Type': 'application/json' };
  const sep = path.includes('?') ? '&' : '?';
  const projectId = opts.projectId ?? _currentProjectId();
  const namespace = opts.namespace ?? _currentNamespace();
  let url = API_BASE + path + sep + 'project_id=' + encodeURIComponent(projectId);
  if (namespace) url += '&namespace=' + encodeURIComponent(namespace);
  clientLog('info', `API ${opts.method || 'GET'} ${path}`, { project_id: projectId, namespace });
  try {
    const r = await fetch(url, { headers, ...opts });
    const j = await r.json().catch(() => ({}));
    const elapsed = Math.round(performance.now() - start);
    if (!r.ok) {
      clientLog('error', `API ${opts.method || 'GET'} ${path} failed`, { status: r.status, message: j.error?.message, elapsed_ms: elapsed });
      const err = new Error(j.error?.message || `HTTP ${r.status}`);
      err.status = r.status;
      err.code = j.error?.code || null;
      err.details = j.error?.details || null; // 2.2：保留后端 details（如 violations 明细）
      throw err;
    }
    clientLog('info', `API ${opts.method || 'GET'} ${path} ok`, { status: r.status, elapsed_ms: elapsed });
    return j;
  } catch (e) {
    clientLog('error', `API ${opts.method || 'GET'} ${path} exception`, { message: e.message, stack: e.stack });
    throw e;
  }
}

async function checkHealth() {
  try {
    const h = await api('/health', { projectId: null, namespace: null });
    const ok = h.data?.status === 'ok';
    $('healthDot').className = 'w-2 h-2 rounded-full ' + (ok ? 'bg-emerald-400' : 'bg-amber-400');
    $('healthText').textContent = ok ? '后端健康' : '降级运行';
    $('ver').textContent = 'v' + (h.data?.version || '?');
    state.healthDetails = h.data || {};
    renderHealthDetails();
    if (!ok) clientLog('info', 'health degraded: optional dependencies unavailable', state.healthDetails.dependencies);
  } catch (e) {
    $('healthDot').className = 'w-2 h-2 rounded-full bg-red-500';
    $('healthText').textContent = '后端不可达';
    state.healthDetails = null;
    clientLog('error', 'health check failed', { message: e.message });
  }
}

function renderHealthDetails() {
  const box = $('healthDetails');
  if (!box) return;
  if (!state.healthDetails) {
    box.innerHTML = '<div class="text-red-400">后端不可达</div>';
    return;
  }
  const deps = state.healthDetails.dependencies || {};
  const rows = Object.entries(deps).map(([k, v]) => {
    const color = v === 'healthy' ? 'text-emerald-400' : 'text-amber-400';
    return '<div class="flex justify-between"><span>' + k + '</span><span class="' + color + '">' + v + '</span></div>';
  }).join('');
  box.innerHTML = '<div class="font-medium mb-2">可选依赖状态</div>' + rows +
    '<div class="mt-2 text-slate-500">Jena/Neo4j/Postgres 未启动时，后端会自动降级到本地 JSONL/文件存储，核心功能仍可运行。</div>';
}

function toggleHealthDetails() {
  const box = $('healthDetails');
  if (!box) return;
  box.classList.toggle('hidden');
  if (!box.classList.contains('hidden')) renderHealthDetails();
}

async function loadProjects() {
  try {
    const j = await api('/api/projects?page=1&pageSize=100', { namespace: null });
    // 归档项目不进顶部选择器（首页管理区可恢复/删除）
    state.projects = (j.data.items || []).filter(p => (p.status || 'active') !== 'archived');
    const savedId = localStorage.getItem('op_currentProject');
    if (!state.currentProject && state.projects.length) {
      state.currentProject = state.projects.find(p => p.id === savedId) || state.projects[0];
      localStorage.setItem('op_currentProject', state.currentProject.id);
    }
    renderProjectSelector();
    await loadNamespaces();
  } catch (e) { toast('加载项目失败: ' + e.message, 'err'); }
}

function renderProjectSelector() {
  const sel = $('projectSelect');
  if (!sel) return;
  const current = _currentProjectId();
  sel.innerHTML = '<option value="">选择项目</option>' +
    state.projects.map(p => '<option value="' + p.id + '" ' + (p.id === current ? 'selected' : '') + '>' + esc(p.name) + '</option>').join('');
  renderContextLabel();
}

function renderContextLabel() {
  const label = $('currentProjectLabel');
  if (!label) return;
  const current = _currentProjectId();
  const p = state.projects.find(x => x.id === current);
  const projectName = p ? p.name : (current === 'default' ? '默认项目' : current);
  const ns = _currentNamespace();
  const nsName = ns === current ? '默认空间' : ns;
  label.textContent = projectName + ' / ' + nsName;
  label.title = '项目: ' + projectName + '\n部门空间: ' + nsName;
}

async function loadNamespaces() {
  const projectId = _currentProjectId();
  let nsSet = new Set([projectId]);
  try {
    const j = await api('/api/ontologies?page=1&pageSize=1000', { namespace: null });
    (j.data.items || []).forEach(o => { if (o.namespace) nsSet.add(o.namespace); });
  } catch (e) { /* ignore */ }
  state.namespaces = Array.from(nsSet).map(id => ({ id, name: id === projectId ? '默认空间 (' + id + ')' : id }));
  const savedNs = localStorage.getItem('op_currentNamespace');
  if (!state.currentNamespace || !nsSet.has(state.currentNamespace.id)) {
    state.currentNamespace = state.namespaces.find(n => n.id === savedNs) || state.namespaces[0];
    localStorage.setItem('op_currentNamespace', state.currentNamespace.id);
  }
  renderProjectSelector();
  renderNamespaceSelector();
}

function renderNamespaceSelector() {
  const sel = $('namespaceSelect');
  if (!sel) return;
  const current = _currentNamespace();
  sel.innerHTML = state.namespaces.map(n => '<option value="' + n.id + '" ' + (n.id === current ? 'selected' : '') + '>' + esc(n.name) + '</option>').join('');
  renderContextLabel();
}

async function setProject(id) {
  if (!id) return;
  const p = state.projects.find(x => x.id === id);
  if (!p) return;
  state.currentProject = p;
  localStorage.setItem('op_currentProject', id);
  state.currentNamespace = { id: id, name: '默认空间 (' + id + ')' };
  localStorage.setItem('op_currentNamespace', id);
  renderProjectSelector();
  renderNamespaceSelector();
  state.currentOntology = null;
  // 清空 ontology 选择器，避免旧项目选中的本体 ID 残留到新项目
  ['ingestOntology', 'graphOntology', 'inferOntology'].forEach(selId => {
    const sel = $(selId);
    if (sel) sel.innerHTML = '';
  });
  // 清空推理页两行下拉的残留选择（实例/属性属于旧项目数据）
  if (impactInstanceLS) impactInstanceLS.setValue('');
  if (impactAttributeLS) impactAttributeLS.setValue('');
  impactInstances = [];
  await loadNamespaces();
  await loadOntologies();
  await loadActions();
  toast('已切换到项目：' + p.name, 'ok');
}

async function setNamespace(id) {
  if (!id) return;
  state.currentNamespace = state.namespaces.find(n => n.id === id) || { id, name: id };
  localStorage.setItem('op_currentNamespace', id);
  renderProjectSelector();
  renderNamespaceSelector();
  if (state.currentOntology) state.currentOntology = null;
  await loadOntologies();
  await loadActions();
}

async function createNamespace(name) {
  const n = name || prompt('请输入部门空间名（namespace），例如 sales/rd');
  if (!n?.trim()) return;
  const id = n.trim();
  if (!state.namespaces.find(ns => ns.id === id)) {
    state.namespaces.push({ id, name: id });
  }
  state.currentNamespace = { id, name: id };
  localStorage.setItem('op_currentNamespace', id);
  renderNamespaceSelector();
  await loadOntologies();
  toast('已切换到部门空间：' + id, 'ok');
}

async function createProject(name) {
  const n = name || prompt('请输入项目名称');
  if (!n?.trim()) return;
  try {
    const j = await api('/api/projects', { method: 'POST', body: JSON.stringify({ name: n.trim() }) });
    state.currentProject = j.data;
    localStorage.setItem('op_currentProject', j.data.id);
    await loadProjects();
    if (!name) toast('项目已创建', 'ok');
  } catch (e) { if (!name) toast('创建项目失败: ' + e.message, 'err'); throw e; }
}

// ---- 首页：项目总览与项目管理 ----
let homeProjects = [];

function escAttr(s) { return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'"); }

function fmtSize(n) {
  if (n == null) return '-';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

async function loadHome() {
  try {
    const j = await api('/api/projects?with_stats=true&pageSize=100', { projectId: null, namespace: null });
    homeProjects = j.data.items || [];
    renderHomeStats();
    renderProjects();
  } catch (e) { toast('加载项目列表失败: ' + e.message, 'err'); }
}

function renderHomeStats() {
  const active = homeProjects.filter(p => (p.status || 'active') !== 'archived');
  const archived = homeProjects.length - active.length;
  $('homeStatProjects').textContent = homeProjects.length;
  $('homeStatProjectsSub').textContent = active.length + ' 进行中 · ' + archived + ' 已归档';
  $('homeStatOntologies').textContent = homeProjects.reduce((s, p) => s + ((p.stats && p.stats.ontology_count) || 0), 0);
  $('homeStatInstances').textContent = homeProjects.reduce((s, p) => s + ((p.stats && p.stats.instance_count) || 0), 0);
  $('homeStatMcp').textContent = homeProjects.filter(p => p.mcp_published).length;
}

function renderProjects() {
  const filter = $('projectFilter') ? $('projectFilter').value : 'all';
  const items = homeProjects.filter(p => {
    const st = p.status || 'active';
    if (filter === 'active') return st !== 'archived';
    if (filter === 'archived') return st === 'archived';
    return true;
  });
  const tb = $('projectTableBody');
  if (!tb) return;
  if (!items.length) { tb.innerHTML = '<tr><td colspan="9" class="p-3 text-center text-slate-500">暂无项目</td></tr>'; return; }
  tb.innerHTML = items.map(p => {
    const st = p.status || 'active';
    const archived = st === 'archived';
    const stats = p.stats || {};
    const badge = archived ? '<span class="badge draft">已归档</span>' : '<span class="badge active">进行中</span>';
    const isDefault = p.id === 'default' || p.id === '_shared';
    const acts = ['<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="enterProject(\'' + p.id + '\')">进入</button>'];
    if (!isDefault) {
      if (archived) acts.push('<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="restoreProject(\'' + p.id + '\', \'' + escAttr(p.name) + '\')">恢复</button>');
      else acts.push('<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="archiveProject(\'' + p.id + '\', \'' + escAttr(p.name) + '\')">归档</button>');
      acts.push('<button class="btn danger" style="padding:2px 8px;font-size:12px" onclick="deleteProjectRow(\'' + p.id + '\', \'' + escAttr(p.name) + '\')">删除</button>');
    }
    return '<tr class="border-b border-slate-700/40 hover:bg-slate-800/30">' +
      '<td class="p-2">' + esc(p.name) + (p.description ? '<div class="text-xs text-slate-500">' + esc(p.description) + '</div>' : '') + '</td>' +
      '<td class="p-2 font-mono text-xs text-slate-500 whitespace-nowrap">' + esc(p.id.slice(0, 8)) + '…' + copyIcon(p.id, '项目ID') + '</td>' +
      '<td class="p-2">' + badge + '</td>' +
      '<td class="p-2">' + (stats.ontology_count != null ? stats.ontology_count : '-') + '</td>' +
      '<td class="p-2">' + (stats.instance_count != null ? stats.instance_count : '-') + '</td>' +
      '<td class="p-2">' + (p.mcp_published ? '<span class="text-emerald-400">● 在线</span>' : '<span class="text-slate-500">未发布</span>') + '</td>' +
      '<td class="p-2 text-xs text-slate-400">' + fmtSize(stats.data_size) + (stats.size_kind === 'archive' ? '（压缩）' : '') + '</td>' +
      '<td class="p-2 text-xs text-slate-400 whitespace-nowrap">' + (p.created_at ? new Date(p.created_at).toLocaleDateString() : '-') + '</td>' +
      '<td class="p-2 whitespace-nowrap">' + acts.join(' ') + '</td>' +
      '</tr>';
  }).join('');
}

function toggleProjectForm(show) {
  const box = $('projectForm');
  const willShow = show != null ? show : box.classList.contains('hidden');
  box.classList.toggle('hidden', !willShow);
  if (willShow) $('newProjectName').focus();
}

async function submitNewProject() {
  const name = $('newProjectName').value.trim();
  if (!name) { toast('项目名称不能为空', 'err'); return; }
  const desc = $('newProjectDesc').value.trim();
  try {
    const j = await api('/api/projects', { method: 'POST', body: JSON.stringify({ name, description: desc }), projectId: null, namespace: null });
    $('newProjectName').value = '';
    $('newProjectDesc').value = '';
    toggleProjectForm(false);
    toast('项目「' + name + '」已创建', 'ok');
    await loadProjects();
    await setProject(j.data.id);
    await loadHome();
  } catch (e) { toast('创建失败: ' + e.message, 'err'); }
}

async function enterProject(id) {
  const p = homeProjects.find(x => x.id === id);
  if (!p) return;
  if ((p.status || 'active') === 'archived') { toast('该项目已归档，请先「恢复」后再进入', 'err'); return; }
  if (!state.projects.some(x => x.id === id)) await loadProjects();
  if (!state.projects.some(x => x.id === id)) { toast('项目不在进行中列表中', 'err'); return; }
  await setProject(id);
  switchTab('modeling');
}

async function archiveProject(id, name) {
  const ok = confirm('归档项目「' + name + '」？\n\n归档后：\n· 项目只保留索引（本体清单与计数），仍显示在首页\n· 本体定义、实例等重数据压缩为归档包，释放磁盘空间\n· 从顶部项目选择器移除，MCP 发布状态自动下线\n\n可随时「恢复」，数据完整还原。');
  if (!ok) return;
  try {
    const j = await api('/api/projects/' + id + '/archive', { method: 'POST', body: '{}', projectId: null, namespace: null });
    const info = (j.data && j.data.archive_info) || {};
    let msg = '项目已归档';
    if (info.original_size) {
      const saved = Math.max(0, Math.round(100 - info.archive_size / info.original_size * 100));
      msg += '：' + fmtSize(info.original_size) + ' → ' + fmtSize(info.archive_size) + '（节省约 ' + saved + '% 空间）';
    }
    toast(msg, 'ok', 6000);
    await afterProjectStateChange();
  } catch (e) { toast('归档失败: ' + e.message, 'err'); }
}

async function restoreProject(id, name) {
  try {
    await api('/api/projects/' + id + '/restore', { method: 'POST', body: '{}', projectId: null, namespace: null });
    toast('项目「' + name + '」已恢复', 'ok');
    await afterProjectStateChange();
  } catch (e) { toast('恢复失败: ' + e.message, 'err'); }
}

async function deleteProjectRow(id, name) {
  const ok = confirm('删除项目「' + name + '」？\n\n此操作不可恢复，将级联清理：\n· 该项目全部本体模型、实例、候选、动作记录\n· MCP 发布状态（自动下线）\n· 压缩归档包（若已归档）\n\n如只想释放空间，建议「归档」而非「删除」。');
  if (!ok) return;
  try {
    await api('/api/projects/' + id, { method: 'DELETE', projectId: null, namespace: null });
    toast('项目已删除', 'ok');
    await afterProjectStateChange();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

// 项目状态变化（新建/归档/恢复/删除）后的统一刷新：
// 首页重渲染；若当前项目已不可用则自动切到第一个进行中项目
async function afterProjectStateChange() {
  await loadHome();
  const curId = state.currentProject ? state.currentProject.id : localStorage.getItem('op_currentProject');
  const cur = homeProjects.find(p => p.id === curId);
  if (!cur || (cur.status || 'active') === 'archived') {
    const first = homeProjects.find(p => (p.status || 'active') !== 'archived');
    if (first) {
      await loadProjects();
      await setProject(first.id);
      return;
    }
    state.currentProject = null;
    localStorage.removeItem('op_currentProject');
    renderProjectSelector();
    return;
  }
  await loadProjects();
}

// ---- 外部 MCP 数据源连接（入）：对接业务系统 MCP Server ----
let connList = [];
let connEditId = null;      // null = 新建
let connEditAuth = { type: 'none' };
let connEditMaps = {};

async function loadConnections() {
  try {
    const j = await api('/api/mcp/connections');
    connList = j.data.items || [];
    renderConnections();
  } catch (e) { console.warn('loadConnections', e); }
}

function renderConnections() {
  const tb = $('connTableBody');
  if (!tb) return;
  if (!connList.length) {
    tb.innerHTML = '<tr><td colspan="7" class="p-3 text-center text-slate-500">暂无连接。点右上「＋ 新建连接」配置业务系统 MCP Server。</td></tr>';
    return;
  }
  tb.innerHTML = connList.map(c => {
    const maps = c.maps_to_ontology || {};
    const auth = c.auth || {};
    let authLabel = auth.type === 'none' ? '无' : esc(auth.type);
    const ref = auth.bearer_token_ref || auth.api_key_ref || auth.client_id_ref;
    if (auth.type !== 'none' && ref) authLabel += ' · ' + esc(ref);
    const allow = (c.tool_allowlist || []).join(', ');
    return '<tr class="border-b border-slate-700/40 hover:bg-slate-800/30 align-top">' +
      '<td class="p-1.5">' + esc(c.display_name) + '<div class="text-slate-500 font-mono text-[11px]">' + esc((c.connection_id || '').slice(0, 14)) + '</div></td>' +
      '<td class="p-1.5 text-xs whitespace-nowrap">' + esc(c.transport) + '</td>' +
      '<td class="p-1.5 font-mono text-xs text-slate-400 break-all max-w-[220px]">' + esc(c.endpoint || '-') + '</td>' +
      '<td class="p-1.5 text-xs">' + authLabel + '</td>' +
      '<td class="p-1.5 text-xs">' + esc(maps.ontology_id || '-') + (maps.object_type ? '<div class="text-slate-500">' + esc(maps.object_type) + ' · ' + Object.keys(maps.field_map || {}).length + ' 字段映射</div>' : '') + '</td>' +
      '<td class="p-1.5 font-mono text-xs text-slate-400">' + esc(allow || '（全部）') + '</td>' +
      '<td class="p-1.5 whitespace-nowrap">' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="testConnection(\'' + c.connection_id + '\')">测试</button> ' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="readConnection(\'' + c.connection_id + '\')">读取→候选</button> ' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="openConnEditor(\'' + c.connection_id + '\')">编辑</button> ' +
        '<button class="btn danger" style="padding:2px 8px;font-size:12px" onclick="deleteConnection(\'' + c.connection_id + '\')">删除</button>' +
      '</td></tr>';
  }).join('');
}

async function openConnEditor(id) {
  connEditId = id || null;
  const box = $('connEditor');
  box.classList.remove('hidden');
  box.innerHTML = '<div class="text-xs text-slate-500">加载本体选项…</div>';
  try { await loadOntologies(); } catch (e) { console.warn(e); }
  const c = connList.find(x => x.connection_id === id) || null;
  connEditAuth = (c && c.auth) ? { type: 'none', ...c.auth } : { type: 'none' };
  connEditMaps = (c && c.maps_to_ontology) || {};
  const transport = (c && c.transport) || 'streamable_http';
  box.innerHTML =
    '<div class="grid grid-cols-12 gap-2 text-sm">' +
      '<div class="col-span-4"><label class="text-xs text-slate-400">名称 *</label><input id="connName" class="w-full" value="' + esc((c && c.display_name) || '') + '" placeholder="如：CRM 系统"></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">传输方式</label><select id="connTransport" class="w-full">' +
        '<option value="streamable_http"' + (transport === 'streamable_http' ? ' selected' : '') + '>streamable_http</option>' +
        '<option value="stdio"' + (transport === 'stdio' ? ' selected' : '') + '>stdio（MVP 仅测可达性，读取未实现）</option>' +
      '</select></div>' +
      '<div class="col-span-5"><label class="text-xs text-slate-400">端点</label><input id="connEndpoint" class="w-full font-mono text-xs" value="' + esc((c && c.endpoint) || '') + '" placeholder="http://业务系统:端口/mcp 或 stdio 启动命令"></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">鉴权类型</label><select id="connAuthType" class="w-full" onchange="renderConnAuthFields()">' +
        ['none', 'bearer_token', 'api_key', 'oauth2_client_credentials'].map(t => '<option value="' + t + '"' + (connEditAuth.type === t ? ' selected' : '') + '>' + t + '</option>').join('') +
      '</select></div>' +
      '<div id="connAuthFields" class="col-span-9 grid grid-cols-2 gap-2"></div>' +
      '<div class="col-span-4"><label class="text-xs text-slate-400">目标本体 *</label><select id="connOntologyId" class="w-full" onchange="renderConnObjectOptions()">' +
        '<option value="">-- 选择 --</option>' + (state.ontologies || []).map(o => '<option value="' + o.id + '"' + (connEditMaps.ontology_id === o.id ? ' selected' : '') + '>' + esc(o.name) + '</option>').join('') +
      '</select></div>' +
      '<div class="col-span-4"><label class="text-xs text-slate-400">对象类型 *</label><select id="connObjectType" class="w-full" onchange="renderConnAttrOptions()"></select></div>' +
      '<div class="col-span-4"><label class="text-xs text-slate-400">缺省读取工具</label><input id="connReadTool" class="w-full" value="' + esc(connEditMaps.read_tool || '') + '" placeholder="如 query_accounts；「读取→候选」未显式指定工具时用此值"></div>' +
      '<div class="col-span-12" id="connFieldMapBox"></div>' +
      '<div class="col-span-6"><label class="text-xs text-slate-400">工具白名单（逗号分隔；留空=该连接工具全部可用）</label><input id="connAllowlist" class="w-full" value="' + esc(((c && c.tool_allowlist) || []).join(', ')) + '" placeholder="query_accounts, query_orders"></div>' +
      '<div class="col-span-6 flex items-end gap-2">' +
        '<button class="btn" type="button" onclick="saveConnection()">保存</button>' +
        '<button class="btn secondary" type="button" onclick="closeConnEditor()">取消</button>' +
      '</div>' +
    '</div>';
  renderConnAuthFields();
  renderConnObjectOptions();
}

function closeConnEditor() {
  connEditId = null;
  $('connEditor').classList.add('hidden');
  $('connEditor').innerHTML = '';
}

function connAuthRef(name) { return (connEditAuth && connEditAuth[name]) || ''; }

function renderConnAuthFields() {
  const t = $('connAuthType').value;
  const box = $('connAuthFields');
  if (!box) return;
  const inp = (id, label, val, ph) =>
    '<div><label class="text-xs text-slate-400">' + label + '</label><input id="' + id + '" class="w-full font-mono text-xs" value="' + esc(val) + '" placeholder="' + (ph || '') + '"></div>';
  if (t === 'none') { box.innerHTML = '<div class="col-span-2 text-xs text-slate-500 self-end pb-2">无需鉴权。</div>'; return; }
  if (t === 'bearer_token') { box.innerHTML = inp('connBearerRef', 'Bearer Token 环境变量引用', connAuthRef('bearer_token_ref'), '${CRM_TOKEN}（只存引用，不落明文）'); return; }
  if (t === 'api_key') { box.innerHTML = inp('connApiKeyRef', 'API Key 环境变量引用', connAuthRef('api_key_ref'), '${CRM_API_KEY}'); return; }
  box.innerHTML = inp('connTokenUrl', 'token_url', connAuthRef('token_url'), 'https://auth.example.com/oauth/token') +
    inp('connClientIdRef', 'client_id 环境变量引用', connAuthRef('client_id_ref'), '${CRM_CLIENT_ID}') +
    inp('connClientSecretRef', 'client_secret 环境变量引用', connAuthRef('client_secret_ref'), '${CRM_CLIENT_SECRET}') +
    inp('connScopes', 'scopes（逗号分隔）', (connEditAuth.scopes || []).join(', '), 'openid profile');
}

function _connCurrentObject() {
  const ontId = $('connOntologyId') ? $('connOntologyId').value : '';
  const ont = (state.ontologies || []).find(o => o.id === ontId);
  const objId = $('connObjectType') ? $('connObjectType').value : '';
  return { ont, obj: (ont && (ont.objects || []).find(o => o.id === objId)) || null };
}

function renderConnObjectOptions() {
  const sel = $('connObjectType');
  if (!sel) return;
  const { ont } = _connCurrentObject();
  const prev = sel.value;
  if (!ont) { sel.innerHTML = '<option value="">-- 先选本体 --</option>'; renderConnAttrOptions(); return; }
  sel.innerHTML = (ont.objects || []).map(o => '<option value="' + o.id + '">' + esc(o.name) + ' (' + o.id + ')</option>').join('');
  if ((ont.objects || []).some(o => o.id === prev)) sel.value = prev;
  renderConnAttrOptions();
}

function connFmRowHtml(ext, attrId) {
  const { obj } = _connCurrentObject();
  const opts = (obj && (obj.attributes || []).map(x => '<option value="' + x.id + '"' + (x.id === attrId ? ' selected' : '') + '>' + esc(x.name) + ' (' + x.id + ')</option>').join('')) || '';
  return '<div class="connFmRow flex gap-1 items-center max-w-2xl">' +
    '<input class="connFmK flex-1 font-mono text-xs" placeholder="外部字段名" value="' + esc(ext) + '">' +
    '<span class="text-slate-500 text-xs">→</span>' +
    '<select class="connFmV flex-1 text-xs"><option value="">-- 不映射 --</option>' + opts + '</select></div>';
}

function renderConnAttrOptions() {
  const box = $('connFieldMapBox');
  if (!box) return;
  const { obj } = _connCurrentObject();
  if (!obj || !(obj.attributes || []).length) {
    box.innerHTML = '<div class="text-xs text-slate-500">选择对象类型后可配置字段映射（外部字段 → 本体属性）。</div>';
    return;
  }
  const existing = connEditMaps.field_map || {};
  const rows = (obj.attributes || []).map(a => {
    const ext = Object.keys(existing).find(k => existing[k] === a.id) || '';
    return connFmRowHtml(ext, a.id);
  }).join('');
  box.innerHTML = '<label class="text-xs text-slate-400">字段映射（外部字段 → 本体属性；外部字段名留空=忽略该行）</label>' +
    '<div id="connFmRows" class="flex flex-col gap-1 mt-1">' + rows + '</div>' +
    '<button class="btn secondary mt-2" type="button" style="font-size:12px;padding:2px 10px" onclick="addConnFmRow()">＋ 添加映射行</button>';
}

function addConnFmRow() {
  const rows = $('connFmRows');
  if (!rows) return;
  const div = document.createElement('div');
  div.innerHTML = connFmRowHtml('', '');
  rows.appendChild(div.firstElementChild);
}

async function saveConnection() {
  const name = $('connName').value.trim();
  if (!name) { toast('连接名称不能为空', 'err'); return; }
  const ontId = $('connOntologyId').value;
  const objId = $('connObjectType').value;
  if (!ontId) { toast('请选择目标本体', 'err'); return; }
  if (!objId) { toast('请选择对象类型', 'err'); return; }
  const fieldMap = {};
  document.querySelectorAll('#connFmRows .connFmRow').forEach(row => {
    const k = row.querySelector('.connFmK').value.trim();
    const v = row.querySelector('.connFmV').value;
    if (k && v) fieldMap[k] = v;
  });
  const t = $('connAuthType').value;
  const auth = { type: t };
  if (t === 'bearer_token') auth.bearer_token_ref = $('connBearerRef').value.trim() || null;
  if (t === 'api_key') auth.api_key_ref = $('connApiKeyRef').value.trim() || null;
  if (t === 'oauth2_client_credentials') {
    auth.token_url = $('connTokenUrl').value.trim() || null;
    auth.client_id_ref = $('connClientIdRef').value.trim() || null;
    auth.client_secret_ref = $('connClientSecretRef').value.trim() || null;
    auth.scopes = $('connScopes').value.split(',').map(s => s.trim()).filter(Boolean);
  }
  const body = {
    display_name: name,
    transport: $('connTransport').value,
    endpoint: $('connEndpoint').value.trim() || null,
    auth,
    tool_allowlist: $('connAllowlist').value.split(',').map(s => s.trim()).filter(Boolean),
    maps_to_ontology: {
      ontology_id: ontId,
      read_tool: $('connReadTool').value.trim() || null,
      object_type: objId,
      field_map: fieldMap,
    },
    read_schedule: 'manual',
  };
  try {
    if (connEditId) {
      await api('/api/mcp/connections/' + connEditId, { method: 'PUT', body: JSON.stringify(body) });
    } else {
      await api('/api/mcp/connections', { method: 'POST', body: JSON.stringify(body) });
    }
    closeConnEditor();
    toast(connEditId ? '连接已更新' : '连接已创建', 'ok');
    await loadConnections();
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function testConnection(id) {
  toast('测试连通性与鉴权…', 'info');
  try {
    const j = await api('/api/mcp/connections/' + id + '/test', { method: 'POST', body: '{}' });
    const r = j.data || {};
    if (r.reachable && r.auth_ok) toast('连接成功：鉴权通过' + (r.latency_ms != null ? '（' + r.latency_ms + 'ms）' : ''), 'ok', 5000);
    else if (r.reachable) toast('网络可达但鉴权失败：' + (r.error || ''), 'err', 6000);
    else toast('连接失败：' + (r.error || '不可达'), 'err', 6000);
  } catch (e) { toast('测试失败: ' + e.message, 'err'); }
}

async function readConnection(id) {
  const ok = confirm('从该连接读取数据并生成候选实例？\n\n读取结果进入「实例导入」的待审列表，需人工审批后才真正入库。');
  if (!ok) return;
  try {
    const j = await api('/api/mcp/connections/' + id + '/read', { method: 'POST', body: '{}' });
    const d = j.data || {};
    if (d.candidate_id) toast('已生成候选实例（覆盖 ' + d.record_count + ' 条记录），请到「实例导入」审批', 'ok', 6000);
    else toast(d.message || '读取完成（无记录）', 'info', 5000);
  } catch (e) { toast('读取失败: ' + e.message, 'err', 6000); }
}

async function deleteConnection(id) {
  if (!confirm('删除该连接？删除后不影响已入库的候选/实例。')) return;
  try {
    await api('/api/mcp/connections/' + id, { method: 'DELETE' });
    toast('连接已删除', 'ok');
    await loadConnections();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

let _lastTab = '';

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(s => s.classList.add('hidden'));
  $(name).classList.remove('hidden');
  clientLog('info', 'switch tab', { tab: name });
  if (name === 'home') loadHome();
  if (name === 'mcp') { loadConnections(); }
  if (name === 'modeling') loadOntologies();
  if (name === 'ingest') {
    const fromGraph = _lastTab === 'graph';
    loadOntologies().then(() => {
      renderOntologySelects();
      // 从图谱页切回时跟随正在查看的本体，避免两页选择不同本体导致“图谱有实例、本页计数为 0”的困惑
      if (fromGraph && state.currentOntology) {
        const sel = $('ingestOntology');
        if (sel && Array.from(sel.options).some(op => op.value === state.currentOntology.id)) {
          sel.value = state.currentOntology.id;
        }
      }
      renderIngestTarget();
      loadInstances();
    });
    loadCandidates();
  }
  if (name === 'graph') { loadOntologies().then(() => { renderOntologySelects(); loadGraph(); }); }
  if (name === 'inference') { loadOntologies().then(() => { renderOntologySelects(); loadInference(); }); }
  if (name === 'action') { loadActions(); }
  if (name === 'mcp') { loadMcpOntologies(1); loadMcpTools(); }
  if (name === 'logs') { loadServerLogs(); }
  if (name === 'settings') { loadLLMSettings(); }
  _lastTab = name;
}

function switchSettingTab(name) {
  document.querySelectorAll('#settings .subtab').forEach(t => t.classList.toggle('active', t.id === 'settingTab' + name.toUpperCase()));
}

function toast(message, type = 'info', duration = 4000) {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  const title = type === 'ok' ? '成功' : type === 'err' ? '错误' : '提示';
  el.innerHTML = '<div class="font-medium mb-1">' + title + '</div><div>' + message + '</div>';
  document.body.appendChild(el);
  setTimeout(() => el.remove(), duration);
}

async function loadOntologies() {
  const j = await api('/api/ontologies?page=1&pageSize=100');
  state.ontologies = j.data.items || [];
  renderOntologyList();
  renderOntologySelects();
}

function setFilter(f) {
  state.filter = f;
  ['filterAll', 'filterActive', 'filterDraft'].forEach(id => $(id).classList.toggle('active', id === 'filter' + (f[0].toUpperCase() + f.slice(1))));
  renderOntologyList();
}

function renderOntologyList() {
  const el = $('ontologyList');
  let items = state.ontologies;
  if (state.filter !== 'all') items = items.filter(o => (o.status || 'active') === state.filter);
  if (!items.length) { el.innerHTML = '<div class="text-sm text-slate-500">暂无本体</div>'; return; }
  el.innerHTML = items.map(o => {
    const status = o.status || 'active';
    return '<div class="group p-2 rounded-lg hover:bg-slate-800/60 cursor-pointer border border-transparent hover:border-slate-700 ' + (state.currentOntology?.id === o.id ? 'bg-slate-800 border-slate-700' : '') + '" onclick="selectOntology(' + JSON.stringify(o).replace(/"/g, '&quot;') + ')">' +
      '<div class="flex justify-between items-center"><span class="font-medium text-sm">' + esc(o.name) + '</span><span class="badge ' + status + '">' + (status === 'draft' ? '草稿' : '已发布') + '</span></div>' +
      '<div class="text-xs text-slate-500">' + o.id + ' · ' + (o.objects?.length || 0) + ' 对象</div>' +
      '<div class="mt-1 flex gap-3 text-xs opacity-0 group-hover:opacity-100 hover:opacity-100">' +
        '<span class="text-sky-400" onclick="event.stopPropagation(); editListOntology(&quot;' + o.id + '&quot;)">编辑</span>' +
        '<span class="text-red-400" onclick="event.stopPropagation(); deleteListOntology(&quot;' + o.id + '&quot;)">删除</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function renderOntologySelects() {
  const active = state.ontologies.filter(o => (o.status || 'active') === 'active');
  const opts = active.map(o => '<option value="' + o.id + '">' + esc(o.name) + '</option>').join('');
  ['ingestOntology', 'graphOntology', 'inferOntology'].forEach(id => {
    const sel = $(id);
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = opts;
    // 若旧值仍存在于新选项中则保留，否则重置
    if (prev && active.find(o => o.id === prev)) {
      sel.value = prev;
    }
  });
}

function selectOntology(o) {
  state.currentOntology = o;
  renderOntologyList();
  $('detailPanel').classList.remove('hidden');
  $('editorCard').classList.add('hidden');
  $('detailTitle').textContent = o.name;
  const status = o.status || 'active';
  $('detailBadge').textContent = status === 'draft' ? '草稿' : '已发布';
  $('detailBadge').className = 'badge ' + status;
  $('detailMeta').textContent = o.id + ' · ' + (o.modeling_type || 'manual') + ' · ' + (o.objects?.length || 0) + ' 对象 · v' + o.version;
  $('detailBody').textContent = JSON.stringify(o, null, 2);
  $('validateResult').classList.add('hidden');
  $('btnPublish').classList.toggle('hidden', status !== 'draft');
}

function startCreate() {
  state.currentOntology = null;
  state.editingId = null;
  $('editorCard').classList.remove('hidden');
  $('detailPanel').classList.add('hidden');
  $('editorTitle').textContent = '新建本体';
  $('formName').value = '';
  $('formVersion').value = '1';
  $('formModelingType').value = 'manual';
  $('formDesc').value = '';
  state.formObjects = [{
    id: 'obj_customer', name: '客户', description: '',
    attributes: [{ id: 'attr_name', name: '名称', data_type: 'string', required: true, constraints: {} }],
    relations: []
  }];
  setEditorMode('form');
  renderObjectForms();
}

function closeEditor() { $('editorCard').classList.add('hidden'); state.editingId = null; }

function setEditorMode(mode) {
  state.editorMode = mode;
  ['form', 'ai', 'doc'].forEach(m => {
    $('mode' + m[0].toUpperCase() + m.slice(1)).classList.toggle('hidden', m !== mode);
    $('subtab' + m[0].toUpperCase() + m.slice(1)).classList.toggle('active', m === mode);
  });
}

function renderObjectForms() {
  const container = $('objectForms');
  if (!state.formObjects.length) { container.innerHTML = '<div class="text-sm text-slate-500">点击上方按钮添加对象</div>'; return; }
  container.innerHTML = state.formObjects.map((obj, idx) => {
    const attrHtml = (obj.attributes || []).map((a, aidx) =>
      '<div class="grid grid-cols-12 gap-2 items-end">' +
        '<div class="col-span-3"><input class="w-full attr-id" data-oidx="' + idx + '" data-aidx="' + aidx + '" value="' + esc(a.id) + '" placeholder="attr_name" /></div>' +
        '<div class="col-span-3"><input class="w-full attr-name" data-oidx="' + idx + '" data-aidx="' + aidx + '" value="' + esc(a.name) + '" placeholder="名称" /></div>' +
        '<div class="col-span-2">' + typeSelect('attr-type', idx, aidx, a.data_type) + '</div>' +
        '<div class="col-span-2"><label class="inline-flex items-center gap-1"><input type="checkbox" class="attr-required" data-oidx="' + idx + '" data-aidx="' + aidx + '" ' + (a.required ? 'checked' : '') + '/> 必填</label></div>' +
        '<div class="col-span-2"><span class="remove-btn" onclick="removeAttr(' + idx + ', ' + aidx + ')">删除</span></div>' +
      '</div>'
    ).join('');
    const relHtml = (obj.relations || []).map((r, ridx) =>
      '<div class="grid grid-cols-12 gap-2 items-end">' +
        '<div class="col-span-3"><label class="text-[10px] text-slate-500">关系 ID（英文/下划线）</label><input class="w-full rel-id" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.id) + '" placeholder="belongs_to" /></div>' +
        '<div class="col-span-3"><label class="text-[10px] text-slate-500">显示名称（中文）</label><input class="w-full rel-name" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.name) + '" placeholder="所属" /></div>' +
        '<div class="col-span-3"><label class="text-[10px] text-slate-500">目标对象 ID（自动补全）</label><input class="w-full rel-target" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.target_object) + '" placeholder="obj_department" list="relTargetList" /></div>' +
        '<div class="col-span-2">' + cardSelect('rel-card', idx, ridx, r.cardinality) + '</div>' +
        '<div class="col-span-1"><span class="remove-btn" onclick="removeRel(' + idx + ', ' + ridx + ')">删除</span></div>' +
      '</div>'
    ).join('');
    return '<div class="object-card" data-oidx="' + idx + '">' +
      '<div class="flex justify-between items-center mb-2"><span class="font-medium text-sm">对象 #' + (idx + 1) + '</span><span class="remove-btn" onclick="removeObject(' + idx + ')">删除对象</span></div>' +
      '<div class="grid grid-cols-2 gap-3 mb-3">' +
        '<div><label>对象 ID</label><input class="w-full obj-id" data-idx="' + idx + '" value="' + esc(obj.id) + '" placeholder="obj_customer" /></div>' +
        '<div><label>对象名称</label><input class="w-full obj-name" data-idx="' + idx + '" value="' + esc(obj.name) + '" placeholder="客户" /></div>' +
      '</div>' +
      '<div class="mb-2"><label>描述</label><input class="w-full obj-desc" data-idx="' + idx + '" value="' + esc(obj.description || '') + '" /></div>' +
      '<div class="form-section-title"><span>属性</span><button class="btn" onclick="addAttr(' + idx + ')">+ 属性</button></div>' +
      '<div class="space-y-2 mb-3">' + attrHtml + '</div>' +
      '<div class="form-section-title"><span>关系</span><button class="btn" onclick="addRel(' + idx + ')">+ 关系</button></div>' +
      '<div class="space-y-2">' + relHtml + '</div>' +
    '</div>';
  }).join('') + relTargetDatalist();
}

// 2.1：关系"目标对象 ID"自动补全（本表单内全部对象）
function relTargetDatalist() {
  const opts = state.formObjects
    .filter(o => o.id)
    .map(o => '<option value="' + esc(o.id) + '">' + esc(o.name || '') + '</option>')
    .join('');
  return '<datalist id="relTargetList">' + opts + '</datalist>';
}

function typeSelect(cls, oidx, aidx, val) {
  const types = ['string', 'integer', 'float', 'boolean', 'date'];
  return '<select class="w-full ' + cls + '" data-oidx="' + oidx + '" data-aidx="' + aidx + '">' + types.map(t => '<option value="' + t + '" ' + (val === t ? 'selected' : '') + '>' + t + '</option>').join('') + '</select>';
}
function cardSelect(cls, oidx, ridx, val) {
  const cards = [['one_to_one','1:1'],['one_to_many','1:N'],['many_to_one','N:1'],['many_to_many','N:N']];
  return '<select class="w-full ' + cls + '" data-oidx="' + oidx + '" data-ridx="' + ridx + '">' + cards.map(c => '<option value="' + c[0] + '" ' + (val === c[0] ? 'selected' : '') + '>' + c[1] + '</option>').join('') + '</select>';
}

function syncFormObjectsFromDOM() {
  const cards = $('objectForms').children;
  for (let i = 0; i < cards.length; i++) {
    const card = cards[i];
    const idx = parseInt(card.dataset.oidx);
    const obj = state.formObjects[idx];
    if (!obj) continue;
    obj.id = card.querySelector('.obj-id').value;
    obj.name = card.querySelector('.obj-name').value;
    obj.description = card.querySelector('.obj-desc').value;
    obj.attributes = [];
    card.querySelectorAll('.attr-id').forEach(el => {
      const aidx = parseInt(el.dataset.aidx);
      obj.attributes.push({
        id: el.value,
        name: card.querySelector('.attr-name[data-aidx="' + aidx + '"]').value,
        data_type: card.querySelector('.attr-type[data-aidx="' + aidx + '"]').value,
        required: card.querySelector('.attr-required[data-aidx="' + aidx + '"]').checked,
        constraints: {}
      });
    });
    obj.relations = [];
    card.querySelectorAll('.rel-id').forEach(el => {
      const ridx = parseInt(el.dataset.ridx);
      obj.relations.push({
        id: el.value,
        name: card.querySelector('.rel-name[data-ridx="' + ridx + '"]').value,
        target_object: card.querySelector('.rel-target[data-ridx="' + ridx + '"]').value,
        cardinality: card.querySelector('.rel-card[data-ridx="' + ridx + '"]').value
      });
    });
  }
}

function addObject() {
  syncFormObjectsFromDOM();
  state.formObjects.push({ id: '', name: '', description: '', attributes: [], relations: [] });
  renderObjectForms();
}
function removeObject(idx) {
  syncFormObjectsFromDOM();
  state.formObjects.splice(idx, 1);
  renderObjectForms();
}
function addAttr(idx) {
  syncFormObjectsFromDOM();
  state.formObjects[idx].attributes.push({ id: '', name: '', data_type: 'string', required: false, constraints: {} });
  renderObjectForms();
}
function removeAttr(idx, aidx) {
  syncFormObjectsFromDOM();
  state.formObjects[idx].attributes.splice(aidx, 1);
  renderObjectForms();
}
function addRel(idx) {
  syncFormObjectsFromDOM();
  state.formObjects[idx].relations.push({ id: '', name: '', target_object: '', cardinality: 'many_to_one' });
  renderObjectForms();
}
function removeRel(idx, ridx) {
  syncFormObjectsFromDOM();
  state.formObjects[idx].relations.splice(ridx, 1);
  renderObjectForms();
}

function collectFormOntology() {
  syncFormObjectsFromDOM();
  return {
    name: $('formName').value || '未命名本体',
    description: $('formDesc').value,
    modeling_type: $('formModelingType').value,
    version: $('formVersion').value || '1',
    objects: state.formObjects.map(o => ({
      id: o.id, name: o.name, description: o.description || '',
      attributes: o.attributes.map(a => ({ id: a.id, name: a.name, data_type: a.data_type, required: a.required, constraints: {} })),
      relations: o.relations.map(r => ({ id: r.id, name: r.name, target_object: r.target_object, cardinality: r.cardinality }))
    })),
    constraints: []
  };
}

async function saveOntology() {
  const payload = collectFormOntology();
  const isEdit = !!state.editingId;
  try {
    // 编辑既有本体走 PUT（原地更新，不再重复新建）；新建走 POST
    const j = isEdit
      ? await api('/api/ontologies/' + state.editingId, { method: 'PUT', body: JSON.stringify(payload) })
      : await api('/api/ontologies', { method: 'POST', body: JSON.stringify(payload) });
    toast((isEdit ? '本体已更新: ' : '本体已保存: ') + j.data.name, 'ok');
    state.editingId = null;
    closeEditor();
    await loadOntologies();
    const target = state.ontologies.find(x => x.id === j.data.id);
    if (target) selectOntology(target);
  } catch (e) {
    toast('保存失败: ' + _formatApiError(e), 'err');
  }
}

// 2.2：业务层校验失败时逐条展示具体失败项
function _formatApiError(e) {
  const v = e.details?.violations;
  if (Array.isArray(v) && v.length) {
    const lines = v.slice(0, 10).map(x => (typeof x === 'string' ? x : (x.message || JSON.stringify(x))));
    return '业务层校验未通过，具体失败项：\n• ' + lines.join('\n• ') + (v.length > 10 ? '\n• …（共 ' + v.length + ' 项）' : '');
  }
  return e.message;
}

async function generateFromText() {
  const text = $('aiText').value.trim();
  if (!text) return toast('请先输入业务描述', 'info');
  $('btnAiGen').disabled = true;
  toast('AI 正在生成本体…', 'info', 2000);
  try {
    const j = await api('/api/ontologies/from-text', { method: 'POST', body: JSON.stringify({ text }) });
    const draft = j.data;
    toast('已生成本体草稿: ' + draft.name, 'ok');
    closeEditor();
    await loadOntologies();
    selectOntology(draft);
  } catch (e) {
    toast('生成失败: ' + e.message, 'err');
  } finally {
    $('btnAiGen').disabled = false;
  }
}

async function uploadDoc() {
  const fileInput = $('docFile');
  if (!fileInput.files.length) return toast('请先选择文件', 'info');
  const form = new FormData();
  form.append('file', fileInput.files[0]);
  $('btnDocParse').disabled = true;
  toast('文档解析任务已提交…', 'info', 2000);
  try {
    const j = await api('/api/ontologies/from-doc', { method: 'POST', body: form });
    pollJob(j.data.job_id);
  } catch (e) {
    toast('解析失败: ' + e.message, 'err');
    $('btnDocParse').disabled = false;
  }
}

async function pollJob(jobId) {
  for (let i = 0; i < 30; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const j = await api('/api/ontologies/jobs/' + jobId);
      const st = j.data.status;
      if (st === 'done') {
        toast('文档解析完成', 'ok');
        $('btnDocParse').disabled = false;
        await loadOntologies();
        const draft = state.ontologies.find(o => o.id === j.data.draft_id);
        if (draft) selectOntology(draft);
        return;
      }
      if (st === 'error') {
        toast('解析失败: ' + (j.data.error || '未知错误'), 'err');
        $('btnDocParse').disabled = false;
        return;
      }
    } catch (e) { /* ignore poll error */ }
  }
  toast('解析任务超时，请稍后手动刷新', 'err');
  $('btnDocParse').disabled = false;
}

function editCurrentOntology() {
  if (!state.currentOntology) return;
  const o = state.currentOntology;
  state.editingId = o.id;
  $('editorCard').classList.remove('hidden');
  $('detailPanel').classList.add('hidden');
  $('editorTitle').textContent = '编辑本体';
  $('formName').value = o.name || '';
  $('formVersion').value = o.version || '1';
  $('formModelingType').value = o.modeling_type || 'manual';
  $('formDesc').value = o.description || '';
  state.formObjects = (o.objects || []).map(x => ({
    id: x.id, name: x.name, description: x.description || '',
    attributes: (x.attributes || []).map(a => ({ id: a.id, name: a.name, data_type: a.data_type, required: !!a.required, constraints: a.constraints || {} })),
    relations: (x.relations || []).map(r => ({ id: r.id, name: r.name, target_object: r.target_object, cardinality: r.cardinality }))
  }));
  setEditorMode('form');
  renderObjectForms();
}

async function publishCurrent() {
  if (!state.currentOntology) return;
  try {
    const j = await api('/api/ontologies/' + state.currentOntology.id + '/publish', { method: 'POST', body: '{}' });
    toast('本体已发布', 'ok');
    selectOntology(j.data);
    loadOntologies();
  } catch (e) {
    toast('发布失败: ' + e.message, 'err');
  }
}

// ---- 本体删除（后端级联：MCP 自动下线 + 实例 + 待审核候选 + RDF 图）----
function editListOntology(oid) {
  const o = state.ontologies.find(x => x.id === oid);
  if (!o) return toast('本体不存在（可能已删除）', 'err');
  selectOntology(o);
  editCurrentOntology();
}

function deleteListOntology(oid) { deleteOntologyById(oid); }

function deleteCurrentOntology() {
  if (!state.currentOntology) return;
  deleteOntologyById(state.currentOntology.id);
}

async function deleteOntologyById(oid) {
  const o = state.ontologies.find(x => x.id === oid)
    || (state.currentOntology && state.currentOntology.id === oid ? state.currentOntology : null);
  const name = (o && o.name) || oid;
  if (!confirm('确认删除本体「' + name + '」？\n\n将同时清理其 MCP 发布状态与待审核导入候选；若存在实例数据会再次确认。此操作不可恢复。')) return;
  try {
    await _doDeleteOntology(oid);
  } catch (e) {
    toast('删除失败: ' + e.message, 'err');
  }
}

async function _doDeleteOntology(oid) {
  let j;
  try {
    j = await api('/api/ontologies/' + oid, { method: 'DELETE' });
  } catch (e) {
    if (e.status === 409 && e.details && e.details.instance_count) {
      const n = e.details.instance_count;
      if (!confirm('该本体下还有 ' + n + ' 条实例数据。\n\n确认删除本体及其全部实例数据？此操作不可恢复。')) return;
      j = await api('/api/ontologies/' + oid + '?force=true', { method: 'DELETE' });
    } else {
      throw e;
    }
  }
  const d = j.data || {};
  toast('本体已删除' + (d.instances_deleted ? '，清理实例 ' + d.instances_deleted + ' 条' : '')
    + (d.candidates_deleted ? '，清理候选 ' + d.candidates_deleted + ' 条' : '')
    + (d.unpublished ? '，已自动取消 MCP 发布' : ''), 'ok');
  if (state.currentOntology && state.currentOntology.id === oid) {
    state.currentOntology = null;
    state.editingId = null;
    $('detailPanel').classList.add('hidden');
    closeEditor();
  }
  const activeTab = document.querySelector('.tab-content:not(.hidden)');
  if (activeTab && activeTab.id === 'mcp') loadMcpOntologies(mcpList.page);
  await loadOntologies();
}

async function validateOntology() {
  if (!state.currentOntology) return;
  try {
    const j = await api('/api/ontologies/' + state.currentOntology.id + '/validate', { method: 'POST', body: JSON.stringify({ level: 'full' }) });
    const el = $('validateResult');
    el.textContent = JSON.stringify(j.data, null, 2);
    el.classList.remove('hidden');
  } catch (e) {
    toast('校验失败: ' + e.message, 'err');
  }
}

async function exportOntology() {
  if (!state.currentOntology) return;
  try {
    const j = await api('/api/ontologies/' + state.currentOntology.id + '/export', { method: 'POST', body: JSON.stringify({ format: 'turtle' }) });
    const blob = new Blob([j.data.content], { type: 'text/turtle' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = j.data.filename || 'ontology.ttl';
    a.click();
  } catch (e) {
    toast('导出失败: ' + e.message, 'err');
  }
}

function viewGraph() {
  if (!state.currentOntology) return;
  switchTab('graph');
  $('graphOntology').value = state.currentOntology.id;
  loadGraph();
}

// ---- 归化 ----
function onIngestOntologyChange() {
  state.selectedIngestObject = null;
  state.selectedInstance = null;
  const sel = $('ingestOntology').value;
  const o = state.ontologies.find(x => x.id === sel);
  state.expandedTreeNodes = new Set(o ? (o.objects || []).map(x => x.id) : []);
  closeInstanceDetail();
  renderIngestTarget();
  renderIngestObjectMeta(null);
  refreshSingleImportContext();
}

// ---- 1.5 左侧实体层级树：按 many_to_one 关系构建父子层级（Protege 风格） ----
function buildObjectTree(ont) {
  const objs = ont.objects || [];
  const byId = new Map(objs.map(o => [o.id, o]));
  const childrenOf = new Map();
  const hasParent = new Set();
  objs.forEach(o => (o.relations || []).forEach(r => {
    if (r.cardinality === 'many_to_one' && byId.has(r.target_object)) {
      if (!childrenOf.has(r.target_object)) childrenOf.set(r.target_object, []);
      childrenOf.get(r.target_object).push(o.id);
      hasParent.add(o.id);
    }
  }));
  const placed = new Set();
  const walk = id => { placed.add(id); (childrenOf.get(id) || []).forEach(walk); };
  const roots = objs.filter(o => !hasParent.has(o.id)).map(o => o.id);
  roots.forEach(walk);
  objs.forEach(o => { if (!placed.has(o.id) && !roots.includes(o.id)) roots.push(o.id); });
  return { roots, childrenOf };
}

function renderIngestTarget() {
  const id = $('ingestOntology').value;
  const o = state.ontologies.find(x => x.id === id);
  const target = $('ingestTarget');
  if (!o) { target.innerHTML = ''; return; }
  const instances = state.instances || [];
  const { roots, childrenOf } = buildObjectTree(o);
  const byId = new Map((o.objects || []).map(x => [x.id, x]));
  const renderNode = objId => {
    const obj = byId.get(objId);
    if (!obj) return '';
    const objInstances = instances.filter(it => it.object_type === objId);
    const isExpanded = state.expandedTreeNodes.has(objId);
    const isSelected = state.selectedIngestObject?.id === objId;
    const hasChildren = (childrenOf.get(objId) || []).length > 0;
    const caret = (hasChildren || objInstances.length) ? (isExpanded ? '▼' : '▶') : '·';
    const headerCls = isSelected ? 'bg-slate-800 border-slate-600' : 'border-transparent hover:bg-slate-800/40 hover:border-slate-700';
    let html = '<div class="rounded-lg border ' + headerCls + '">' +
      '<div class="p-2 flex items-center gap-1 cursor-pointer" onclick="toggleObjectTree(' + JSON.stringify(obj).replace(/"/g, '&quot;') + ')">' +
        '<span class="text-slate-500 w-3">' + caret + '</span>' +
        '<div class="flex-1"><div class="flex justify-between"><span class="font-medium text-slate-200">' + esc(obj.name || objId) + '</span><span class="text-xs text-slate-500">' + objInstances.length + ' 实例</span></div></div>' +
      '</div>';
    if (isExpanded) {
      let body = '<div class="pb-1 pl-6">';
      (childrenOf.get(objId) || []).forEach(cid => { body += renderNode(cid); });
      const instRows = objInstances.map(inst => {
        const instId = esc(inst.id || inst.node_id || '');
        const active = state.selectedInstance?.id === inst.id ? 'bg-slate-800 text-slate-200' : 'text-slate-400 hover:bg-slate-800/40 hover:text-slate-200';
        return '<div class="py-1 px-2 rounded cursor-pointer ' + active + '" onclick="selectInstance(' + JSON.stringify(inst).replace(/"/g, '&quot;') + ')">' +
          '<span class="text-xs text-slate-500">' + instId.slice(0, 8) + '</span> ' + esc(_instanceLabel(inst)) + '</div>';
      }).join('');
      body += (instRows || (!hasChildren ? '<div class="py-1 px-2 text-slate-600">暂无实例</div>' : ''));
      body += '</div>';
      html += body;
    }
    html += '</div>';
    return '<div class="mb-1">' + html + '</div>';
  };
  target.innerHTML = roots.map(r => renderNode(r)).join('') || '<div class="text-xs text-slate-400">该本体尚未定义对象</div>';
}

function _instanceLabel(inst) {
  const data = inst.instance || inst.props || inst;
  if (typeof data !== 'object' || !data) return '';
  const nameKey = Object.keys(data).find(k => /^(name|title|nm)$/i.test(k));
  if (nameKey && data[nameKey]) return String(data[nameKey]).slice(0, 30);
  const candidates = Object.entries(data).filter(([k, v]) => typeof v === 'string' && v && !String(k).startsWith('_') && !['id','node_id','object_type','candidate_id','ontology_id','project_id','imported_at','tx_id'].includes(k));
  return String((candidates[0] && candidates[0][1]) || '').slice(0, 30);
}

function toggleObjectTree(obj) {
  if (state.expandedTreeNodes.has(obj.id)) {
    state.expandedTreeNodes.delete(obj.id);
  } else {
    state.expandedTreeNodes.add(obj.id);
  }
  state.selectedIngestObject = obj;
  state.selectedInstance = null;
  renderIngestTarget();
  renderIngestObjectMeta(obj);
  refreshSingleImportContext();
}

// ---- 1.2 单对象导入卡片：选中对象后才出现导入操作 ----
function refreshSingleImportContext() {
  const obj = state.selectedIngestObject;
  const hint = $('singleImportHint');
  const body = $('singleImportBody');
  const badge = $('singleImportTarget');
  if (!hint || !body) return;
  if (!obj) {
    hint.classList.remove('hidden');
    body.classList.add('hidden');
    if (badge) badge.textContent = '';
    return;
  }
  hint.classList.add('hidden');
  body.classList.remove('hidden');
  if (badge) badge.textContent = obj.name || obj.id;
}

function selectInstance(inst) {
  state.selectedInstance = inst;
  renderIngestTarget();
  renderInstanceDetail(inst);
}

function selectIngestObject(obj) {
  state.selectedIngestObject = obj;
  renderIngestTarget();
  renderIngestObjectMeta(obj);
  refreshSingleImportContext();
  if (!$('ingestFieldMap').dataset.touched) {
    $('ingestFieldMap').value = suggestFieldMapForObject(obj);
  }
  if (!$('ingestRecords').dataset.touched && getIngestSourceKind() === 'field_map') {
    $('ingestRecords').value = buildExampleRecords(obj);
  }
}

async function uploadIngestFile() {
  const ontologyId = $('ingestOntology').value;
  if (!ontologyId) return toast('请先选择本体', 'info');
  const objectType = state.selectedIngestObject?.id || '';
  if (!objectType) return toast('请先在左侧实体树选择目标对象', 'info');
  const fileInput = $('ingestFile');
  if (!fileInput.files || !fileInput.files[0]) return toast('请选择 Excel 或 CSV 文件', 'info');
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  try {
    const j = await api('/api/ingest/upload?ontology_id=' + encodeURIComponent(ontologyId) + '&object_type=' + encodeURIComponent(objectType), {
      method: 'POST',
      body: formData,
    });
    const list = j.data.candidates || [j.data];
    const total = j.data.total || list.length;
    toast('已为「' + (state.selectedIngestObject.name || objectType) + '」生成 ' + total + ' 个候选，共 ' + list.reduce((s, c) => s + (c.instances || []).length, 0) + ' 条实例，请在下方候选审核区确认后入库', 'ok');
    fileInput.value = '';
    loadCandidates();
  } catch (e) { toast('上传失败: ' + e.message, 'err'); }
}

// ---- 1.3 多对象批量导入（逆向建模：每个 sheet 自动匹配对象，全部命中对象生成候选） ----
async function uploadIngestFileGlobal() {
  const ontologyId = $('ingestOntology').value;
  if (!ontologyId) return toast('请先选择本体', 'info');
  const fileInput = $('ingestFileGlobal');
  const msg = $('ingestUploadMsgGlobal');
  if (!fileInput.files || !fileInput.files[0]) return toast('请选择 Excel 或 CSV 文件', 'info');
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  if (msg) msg.textContent = '上传中…';
  try {
    const j = await api('/api/ingest/upload?ontology_id=' + encodeURIComponent(ontologyId), { method: 'POST', body: formData });
    const list = j.data.candidates || [j.data];
    const total = j.data.total || list.length;
    const cnt = list.reduce((s, c) => s + (c.instances || []).length, 0);
    toast('多对象批量导入完成：命中 ' + total + ' 个对象、共 ' + cnt + ' 条实例（均为候选）。请在下方「候选审核」逐条确认入库', 'ok');
    if (msg) msg.textContent = '已上传 ' + total + ' 组候选 / ' + cnt + ' 条';
    fileInput.value = '';
    loadCandidates();
  } catch (e) {
    toast('上传失败: ' + e.message, 'err');
    if (msg) msg.textContent = '上传失败';
  }
}

function downloadIngestTemplate() {
  const ontologyId = $('ingestOntology').value;
  const objectType = state.selectedIngestObject?.id || '';
  if (!ontologyId || !objectType) return toast('请先选择本体和目标对象类型', 'info');
  const ontology = state.ontologies.find(o => o.id === ontologyId);
  const objDef = ontology && (ontology.objects || []).find(o => o.id === objectType);
  if (!objDef) return toast('未找到目标对象类型定义', 'err');
  const attrs = (objDef.attributes || []);
  const headers = attrs.map(a => a.id);
  const rows = [headers, attrs.map(a => {
    if (a.data_type === 'integer') return 1;
    if (a.data_type === 'float') return 1.0;
    if (a.data_type === 'boolean') return true;
    if (a.data_type === 'date') return '2024-01-01';
    return '示例' + (a.name || a.id);
  })];
  let csv = rows.map(r => r.map(v => {
    const s = String(v).replace(/"/g, '""');
    return s.includes(',') || s.includes('\n') || s.includes('"') ? '"' + s + '"' : s;
  }).join(',')).join('\n');
  csv = '\ufeff' + csv;
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = objectType + '_template.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function markTouched(id) { const el = $(id); if (el) el.dataset.touched = '1'; }

function renderIngestObjectMeta(obj) {
  setObjectMetaActions(false);
  if (!obj) { $('ingestObjectMeta').innerHTML = '<div class="text-sm text-slate-400">点击左侧任意对象查看属性、关系与约束。</div>'; return; }
  const attrs = (obj.attributes || []).map(a => '<tr><td class="border border-slate-700 p-1">' + esc(a.id) + '</td><td class="border border-slate-700 p-1">' + esc(a.name || '-') + '</td><td class="border border-slate-700 p-1">' + esc(a.data_type || 'string') + '</td><td class="border border-slate-700 p-1">' + (a.required ? '<span class="text-red-400">必填</span>' : '') + '</td></tr>').join('');
  const rels = (obj.relations || []).length ? '<table class="w-full text-xs border-collapse mt-2"><thead><tr><th class="border border-slate-700 p-1 text-left">关系 ID</th><th class="border border-slate-700 p-1 text-left">名称</th><th class="border border-slate-700 p-1 text-left">目标对象</th><th class="border border-slate-700 p-1 text-left">基数</th></tr></thead><tbody>' + (obj.relations || []).map(r => '<tr><td class="border border-slate-700 p-1">' + esc(r.id) + '</td><td class="border border-slate-700 p-1">' + esc(r.name || '-') + '</td><td class="border border-slate-700 p-1">' + esc(r.target_object) + '</td><td class="border border-slate-700 p-1">' + esc(r.cardinality || 'N:1') + '</td></tr>').join('') + '</tbody></table>' : '<div class="text-xs text-slate-600 mt-2">无关系定义</div>';
  $('ingestObjectMeta').innerHTML = '<div class="mb-2"><strong class="text-slate-200">' + esc(obj.name || obj.id) + '</strong> <span class="text-slate-500">(' + esc(obj.id) + ')</span></div>' +
    '<div class="text-xs text-slate-400 mb-2">' + esc(obj.description || '暂无描述') + '</div>' +
    '<table class="w-full text-xs border-collapse"><thead><tr><th class="border border-slate-700 p-1 text-left">属性 ID</th><th class="border border-slate-700 p-1 text-left">名称</th><th class="border border-slate-700 p-1 text-left">类型</th><th class="border border-slate-700 p-1 text-left">约束</th></tr></thead><tbody>' + attrs + '</tbody></table>' + rels;
}

function setObjectMetaActions(editing) {
  const el = $('objectMetaActions');
  if (!el) return;
  if (editing) {
    el.innerHTML = '<button class="btn text-xs" onclick="saveObjectMeta()">保存</button><button class="btn secondary text-xs" onclick="cancelEditObjectMeta()">取消</button>';
  } else {
    el.innerHTML = '<button class="btn text-xs" onclick="startEditObjectMeta()">编辑</button>';
  }
}

function startEditObjectMeta() {
  if (!state.selectedIngestObject) return toast('请先选择对象', 'info');
  ingestObjectEditing = true;
  editingObjectClone = JSON.parse(JSON.stringify(state.selectedIngestObject));
  renderIngestObjectEditor(editingObjectClone);
  setObjectMetaActions(true);
}

function cancelEditObjectMeta() {
  ingestObjectEditing = false;
  editingObjectClone = null;
  if (state.selectedIngestObject) renderIngestObjectMeta(state.selectedIngestObject);
  setObjectMetaActions(false);
}

function renderIngestObjectEditor(obj) {
  const attrRows = (obj.attributes || []).map((a, i) =>
    '<tr>' +
      '<td class="border border-slate-700 p-1"><input class="w-full text-xs" id="edit_attr_id_' + i + '" value="' + esc(a.id) + '" /></td>' +
      '<td class="border border-slate-700 p-1"><input class="w-full text-xs" id="edit_attr_name_' + i + '" value="' + esc(a.name || '') + '" /></td>' +
      '<td class="border border-slate-700 p-1">' +
        '<select class="w-full text-xs" id="edit_attr_type_' + i + '">' +
          '<option value="string" ' + (a.data_type === 'string' ? 'selected' : '') + '>string</option>' +
          '<option value="integer" ' + (a.data_type === 'integer' ? 'selected' : '') + '>integer</option>' +
          '<option value="float" ' + (a.data_type === 'float' ? 'selected' : '') + '>float</option>' +
          '<option value="boolean" ' + (a.data_type === 'boolean' ? 'selected' : '') + '>boolean</option>' +
          '<option value="date" ' + (a.data_type === 'date' ? 'selected' : '') + '>date</option>' +
        '</select>' +
      '</td>' +
      '<td class="border border-slate-700 p-1 text-center"><input type="checkbox" id="edit_attr_required_' + i + '" ' + (a.required ? 'checked' : '') + ' /></td>' +
      '<td class="border border-slate-700 p-1"><button class="btn secondary text-xs" onclick="removeEditAttr(' + i + ')">删</button></td>' +
    '</tr>').join('');

  const relRows = (obj.relations || []).map((r, i) =>
    '<tr>' +
      '<td class="border border-slate-700 p-1"><input class="w-full text-xs" id="edit_rel_id_' + i + '" value="' + esc(r.id) + '" /></td>' +
      '<td class="border border-slate-700 p-1"><input class="w-full text-xs" id="edit_rel_name_' + i + '" value="' + esc(r.name || '') + '" /></td>' +
      '<td class="border border-slate-700 p-1"><input class="w-full text-xs" id="edit_rel_target_' + i + '" value="' + esc(r.target_object) + '" placeholder="目标对象ID" /></td>' +
      '<td class="border border-slate-700 p-1"><input class="w-full text-xs" id="edit_rel_card_' + i + '" value="' + esc(r.cardinality || 'N:1') + '" /></td>' +
      '<td class="border border-slate-700 p-1"><button class="btn secondary text-xs" onclick="removeEditRel(' + i + ')">删</button></td>' +
    '</tr>').join('');

  const html = '<div class="mb-2"><label class="text-xs text-slate-500">对象名称</label><input id="edit_obj_name" class="w-full" value="' + esc(obj.name || '') + '" /></div>' +
    '<div class="mb-2"><label class="text-xs text-slate-500">描述</label><textarea id="edit_obj_desc" rows="2" class="w-full text-xs">' + esc(obj.description || '') + '</textarea></div>' +
    '<div class="flex justify-between items-center mb-1 mt-3"><strong class="text-sm text-slate-300">属性</strong><button class="btn secondary text-xs" onclick="addEditAttr()">+ 属性</button></div>' +
    '<table class="w-full text-xs border-collapse"><thead><tr><th class="border border-slate-700 p-1 text-left">ID</th><th class="border border-slate-700 p-1 text-left">名称</th><th class="border border-slate-700 p-1 text-left">类型</th><th class="border border-slate-700 p-1 text-center">必填</th><th class="border border-slate-700 p-1"></th></tr></thead><tbody>' + attrRows + '</tbody></table>' +
    '<div class="flex justify-between items-center mb-1 mt-3"><strong class="text-sm text-slate-300">关系</strong><button class="btn secondary text-xs" onclick="addEditRel()">+ 关系</button></div>' +
    '<table class="w-full text-xs border-collapse"><thead><tr><th class="border border-slate-700 p-1 text-left">ID</th><th class="border border-slate-700 p-1 text-left">名称</th><th class="border border-slate-700 p-1 text-left">目标对象</th><th class="border border-slate-700 p-1 text-left">基数</th><th class="border border-slate-700 p-1"></th></tr></thead><tbody>' + relRows + '</tbody></table>';
  $('ingestObjectMeta').innerHTML = html;
}

function addEditAttr() {
  if (!editingObjectClone) return;
  editingObjectClone.attributes = editingObjectClone.attributes || [];
  editingObjectClone.attributes.push({ id: 'attr_new_' + editingObjectClone.attributes.length, name: '', data_type: 'string', required: false, constraints: {} });
  renderIngestObjectEditor(editingObjectClone);
}

function removeEditAttr(i) {
  if (!editingObjectClone) return;
  editingObjectClone.attributes.splice(i, 1);
  renderIngestObjectEditor(editingObjectClone);
}

function addEditRel() {
  if (!editingObjectClone) return;
  editingObjectClone.relations = editingObjectClone.relations || [];
  editingObjectClone.relations.push({ id: 'rel_new_' + editingObjectClone.relations.length, name: '', target_object: '', cardinality: 'N:1' });
  renderIngestObjectEditor(editingObjectClone);
}

function removeEditRel(i) {
  if (!editingObjectClone) return;
  editingObjectClone.relations.splice(i, 1);
  renderIngestObjectEditor(editingObjectClone);
}

function collectEditedObject() {
  if (!editingObjectClone) return null;
  const attrs = [];
  for (let i = 0; i < (editingObjectClone.attributes || []).length; i++) {
    const id = $('edit_attr_id_' + i).value.trim();
    if (!id) continue;
    attrs.push({
      id: id,
      name: $('edit_attr_name_' + i).value.trim() || id,
      data_type: $('edit_attr_type_' + i).value,
      required: $('edit_attr_required_' + i).checked,
      constraints: (editingObjectClone.attributes[i].constraints) || {},
    });
  }
  const rels = [];
  for (let i = 0; i < (editingObjectClone.relations || []).length; i++) {
    const id = $('edit_rel_id_' + i).value.trim();
    if (!id) continue;
    rels.push({
      id: id,
      name: $('edit_rel_name_' + i).value.trim() || id,
      target_object: $('edit_rel_target_' + i).value.trim(),
      cardinality: $('edit_rel_card_' + i).value.trim() || 'N:1',
    });
  }
  return {
    name: $('edit_obj_name').value.trim() || editingObjectClone.id,
    description: $('edit_obj_desc').value.trim(),
    attributes: attrs,
    relations: rels,
  };
}

async function saveObjectMeta() {
  if (!editingObjectClone || !state.selectedIngestObject) return;
  const oid = $('ingestOntology').value;
  const updates = collectEditedObject();
  if (!updates) return;
  try {
    await api('/api/ontologies/' + oid + '/objects/' + state.selectedIngestObject.id, {
      method: 'PATCH',
      body: JSON.stringify(updates),
    });
    toast('对象约束已更新', 'ok');
    ingestObjectEditing = false;
    editingObjectClone = null;
    await loadOntologies();
    setObjectMetaActions(false);
    const o = state.ontologies.find(x => x.id === oid);
    const refreshed = o && (o.objects || []).find(obj => obj.id === state.selectedIngestObject.id);
    if (refreshed) {
      state.selectedIngestObject = refreshed;
      renderIngestTarget();
      renderIngestObjectMeta(refreshed);
      refreshSingleImportContext();
      suggestFieldMap(refreshed);
    }
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

function editOntologyInModeling() {
  const oid = $('ingestOntology').value;
  if (!oid) return toast('请先选择本体', 'info');
  switchTab('modeling');
  const o = state.ontologies.find(x => x.id === oid);
  if (o) {
    state.currentOntology = o;
    selectOntology(o);
    editCurrentOntology();
  }
}

function suggestFieldMap(obj) {
  const attrs = (obj.attributes || []);
  const map = {};
  attrs.forEach(a => {
    const key = a.id;
    if (key.startsWith('attr_')) {
      const short = key.replace('attr_', '');
      map[short] = key;
    }
  });
  $('ingestFieldMap').value = JSON.stringify(map, null, 2);
}

function validateRecordAgainstOntology(record, fieldMap, objDef) {
  const errors = [];
  const mapped = {};
  for (const [src, target] of Object.entries(fieldMap)) {
    if (src in record) mapped[target] = record[src];
  }
  for (const a of (objDef.attributes || [])) {
    if (a.required && (mapped[a.id] === undefined || mapped[a.id] === null || mapped[a.id] === '')) {
      errors.push('属性 ' + (a.name || a.id) + ' 必填，但源记录缺少对应字段');
    }
    if (mapped[a.id] !== undefined && mapped[a.id] !== null && mapped[a.id] !== '') {
      const v = mapped[a.id];
      if (a.data_type === 'integer' && !/^-?\d+$/.test(String(v))) errors.push('属性 ' + (a.name || a.id) + ' 应为整数');
      if (a.data_type === 'float' && isNaN(parseFloat(v))) errors.push('属性 ' + (a.name || a.id) + ' 应为数值');
      if (a.data_type === 'boolean' && !['true','false',true,false,0,1].includes(v)) errors.push('属性 ' + (a.name || a.id) + ' 应为布尔值');
    }
  }
  return errors;
}

function getIngestSourceKind() {
  const el = document.querySelector('input[name="ingestSourceKind"]:checked');
  return el ? el.value : 'field_map';
}

function onIngestSourceKindChange() {
  const kind = getIngestSourceKind();
  const isLlm = kind === 'llm';
  $('fieldMapWrap').classList.toggle('hidden', isLlm);
  $('llmEndpointWrap').classList.toggle('hidden', !isLlm);
  $('llmModelWrap').classList.toggle('hidden', !isLlm);
  $('ingestInputLabel').textContent = isLlm ? '待抽取文本' : '源记录 JSON 数组';
  $('ingestRecords').placeholder = isLlm ? '粘贴一段业务文档，LLM 将按右侧对象约束抽取实例。' : '[{"name":"腾讯"},{"name":"宝钢"}]';
  if (isLlm) {
    const s = state.llmSettings || {};
    $('ingestLlmEndpoint').value = s.base_url || $('ingestLlmEndpoint').value || '';
    $('ingestLlmModel').value = s.model || $('ingestLlmModel').value || '';
  }
}

function buildExampleRecords(objDef) {
  if (!objDef) return '[\n  {"name":"腾讯"}\n]';
  const attrs = (objDef.attributes || []).filter(a => a.data_type !== 'relation');
  const example = {};
  attrs.forEach(a => {
    if (a.data_type === 'integer') example[a.id] = 123;
    else if (a.data_type === 'float') example[a.id] = 1.23;
    else if (a.data_type === 'boolean') example[a.id] = true;
    else if (a.data_type === 'date') example[a.id] = '2024-01-01';
    else example[a.id] = '示例' + (a.name || a.id);
  });
  return '[\n  ' + JSON.stringify(example) + '\n]';
}

function suggestFieldMapForObject(objDef) {
  if (!objDef) return '{"name":"attr_name"}';
  const map = {};
  (objDef.attributes || []).forEach(a => {
    const key = a.name || a.id;
    if (key && key !== a.id) map[key] = a.id;
    map[a.id] = a.id;
  });
  return JSON.stringify(map);
}

async function previewIngest() {
  const ontologyId = $('ingestOntology').value;
  const objectType = state.selectedIngestObject?.id || '';
  if (!ontologyId || !objectType) return toast('请先在左侧实体树选择目标对象', 'info');
  const ontology = state.ontologies.find(o => o.id === ontologyId);
  const objDef = ontology && (ontology.objects || []).find(o => o.id === objectType);
  if (!objDef) return toast('未找到目标对象类型定义', 'err');

  const kind = getIngestSourceKind();
  let payload;
  if (kind === 'field_map') {
    const fieldMap = $('ingestFieldMap').value;
    const records = $('ingestRecords').value;
    if (!fieldMap || !records) return toast('请填写字段映射和源记录', 'info');
    let parsedRecords, parsedMap;
    try {
      parsedRecords = JSON.parse(records);
      parsedMap = JSON.parse(fieldMap);
    } catch (e) {
      return toast('JSON 格式错误: ' + e.message, 'err');
    }
    if (!Array.isArray(parsedRecords)) return toast('源记录必须是 JSON 数组', 'err');

    const allErrors = [];
    parsedRecords.forEach((rec, i) => {
      const errs = validateRecordAgainstOntology(rec, parsedMap, objDef);
      errs.forEach(msg => allErrors.push('记录 #' + i + ': ' + msg));
    });
    if (allErrors.length) {
      $('ingestPreviewResult').textContent = allErrors.join('\n');
      $('ingestPreviewResult').classList.remove('hidden');
      return toast('源记录不符合本体约束，请修正后重试', 'err');
    }
    payload = { ontology_id: ontologyId, source: { kind: 'field_map', records: parsedRecords }, mapping: { object_type: objectType, field_map: parsedMap } };
  } else {
    const text = $('ingestRecords').value.trim();
    const endpoint = ($('ingestLlmEndpoint').value || state.llmSettings?.base_url || '').trim();
    const model = ($('ingestLlmModel').value || state.llmSettings?.model || '').trim();
    if (!text) return toast('请粘贴待抽取文本', 'info');
    if (!endpoint) return toast('请填写 LLM Endpoint 或在设置页配置', 'info');
    payload = { ontology_id: ontologyId, source: { kind: 'llm', text: text, llm_endpoint: endpoint, model: model || undefined }, object_type: objectType };
  }

  try {
    const j = await api('/api/ingest/preview', { method: 'POST', body: JSON.stringify(payload) });
    $('ingestPreviewResult').textContent = JSON.stringify(j.data, null, 2);
    $('ingestPreviewResult').classList.remove('hidden');
    toast('已生成候选: ' + j.data.id, 'ok');
    loadCandidates();
  } catch (e) {
    toast('Preview 失败: ' + e.message, 'err');
  }
}

async function loadCandidates() {
  const ontologyId = $('ingestOntology').value;
  const status = $('candidateFilterStatus').value;
  let url = '/api/ingest/candidates?page=1&pageSize=50';
  if (ontologyId) url += '&ontology_id=' + encodeURIComponent(ontologyId);
  if (status) url += '&status=' + encodeURIComponent(status);
  try {
    const j = await api(url);
    state.candidates = j.data.items || [];
    renderCandidateTable();
  } catch (e) { toast('加载候选失败: ' + e.message, 'err'); }
}

// ---- 1.4 候选审核：表格 + 逐行按钮 + 复选框批量操作 ----
function renderCandidateTable() {
  const el = $('candidateList');
  const totalEl = $('candidateTotal');
  const list = state.candidates || [];
  if (totalEl) totalEl.textContent = '共 ' + list.length + ' 组候选';
  if (!list.length) {
    el.innerHTML = '<div class="text-sm text-slate-500">暂无候选。上传文件或 Preview 后，自动抽取的实例会出现在这里等待审核。</div>';
    return;
  }
  const statusBadge = {
    pending: '<span class="badge warning">待审核</span>',
    imported: '<span class="badge success">已入库</span>',
    rejected: '<span class="badge danger">已驳回</span>',
  };
  let html = '<table class="w-full text-xs border-collapse"><thead><tr class="text-slate-400">' +
    '<th class="border-b border-slate-700 p-1 w-8"><input type="checkbox" id="candCheckAll" title="全选" /></th>' +
    '<th class="border-b border-slate-700 p-1 text-left">候选 ID</th>' +
    '<th class="border-b border-slate-700 p-1 text-left">对象</th>' +
    '<th class="border-b border-slate-700 p-1 text-left">实例数</th>' +
    '<th class="border-b border-slate-700 p-1 text-left">来源</th>' +
    '<th class="border-b border-slate-700 p-1 text-left">状态</th>' +
    '<th class="border-b border-slate-700 p-1 text-left">行操作</th>' +
    '</tr></thead><tbody>';
  list.forEach(c => {
    const cid = esc(c.id);
    const isPending = c.status === 'pending';
    const isRejected = c.status === 'rejected';
    const isImported = c.status === 'imported';
    let actions = '';
    if (isPending) {
      actions = '<button class="btn text-xs" onclick="approveCandidate(\'' + cid + '\')">批准入库</button>' +
        '<button class="btn secondary text-xs" onclick="startReviewCandidate(\'' + cid + '\')">编辑</button>' +
        '<button class="btn secondary text-xs" onclick="rejectCandidate(\'' + cid + '\')">驳回</button>';
    } else if (isRejected) {
      actions = '<button class="btn secondary text-xs" onclick="startReviewCandidate(\'' + cid + '\')">查看</button>' +
        '<button class="btn secondary text-xs" onclick="revertCandidate(\'' + cid + '\')">恢复待审核</button>' +
        '<button class="btn secondary text-xs" style="background:#991b1b" onclick="deleteCandidateRow(\'' + cid + '\')">删除</button>';
    } else if (isImported) {
      actions = '<button class="btn secondary text-xs" onclick="startReviewCandidate(\'' + cid + '\')">查看</button>' +
        '<button class="btn secondary text-xs" style="background:#991b1b" onclick="deleteCandidateRow(\'' + cid + '\')">删除</button>';
    }
    html += '<tr class="border-b border-slate-800">' +
      '<td class="p-1"><input type="checkbox" class="cand-check" value="' + cid + '" /></td>' +
      '<td class="p-1 font-mono text-slate-500">' + cid.slice(0, 8) + '</td>' +
      '<td class="p-1 font-mono">' + esc(c.object_type || '-') + '</td>' +
      '<td class="p-1">' + (c.instances?.length || 0) + '</td>' +
      '<td class="p-1 text-slate-400">' + esc(c.source || '-') + '</td>' +
      '<td class="p-1">' + (statusBadge[c.status] || esc(c.status || '')) + '</td>' +
      '<td class="p-1"><div class="flex gap-1 flex-wrap">' + actions + '</div></td>' +
      '</tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
  const all = $('candCheckAll');
  if (all) all.onchange = () => document.querySelectorAll('.cand-check').forEach(cb => { cb.checked = all.checked; });
}

function _checkedCandidateIds() {
  return Array.from(document.querySelectorAll('.cand-check:checked')).map(cb => cb.value);
}

function _uncheckCandidates() {
  document.querySelectorAll('.cand-check').forEach(cb => { cb.checked = false; });
  const all = $('candCheckAll');
  if (all) all.checked = false;
}

async function batchCandidates(action) {
  const ids = _checkedCandidateIds();
  if (!ids.length) return toast('请先勾选要操作的候选行', 'info');
  const labels = { approve: '批量批准', reject: '批量驳回', restore: '批量恢复待审核', delete: '批量删除' };
  if (action === 'delete' && !confirm('确认删除选中的 ' + ids.length + ' 组候选？此操作不可恢复。')) return;
  try {
    if (action === 'approve' || action === 'reject') {
      const j = await api('/api/ingest/candidates/batch-' + action, { method: 'POST', body: JSON.stringify({ candidate_ids: ids }) });
      const d = j.data || {};
      toast(labels[action] + '完成：生效 ' + (d.affected || 0) + ' 组' + (d.skipped ? '，跳过 ' + d.skipped + ' 组（状态不符）' : ''), 'ok');
    } else {
      let done = 0, failed = 0;
      for (const id of ids) {
        try {
          if (action === 'restore') await api('/api/ingest/candidates/' + id + '/revert', { method: 'POST', body: '{}' });
          else await api('/api/ingest/candidates/' + id, { method: 'DELETE' });
          done++;
        } catch (e) { failed++; }
      }
      toast(labels[action] + '完成：' + done + ' 组' + (failed ? '，失败 ' + failed + ' 组' : ''), failed ? 'err' : 'ok');
    }
    _uncheckCandidates();
    loadCandidates();
    if (action === 'approve') loadInstances();
  } catch (e) { toast('批量操作失败: ' + e.message, 'err'); }
}

async function revertCandidate(id) {
  try {
    await api('/api/ingest/candidates/' + id + '/revert', { method: 'POST', body: '{}' });
    toast('候选已恢复为待审核', 'ok');
    loadCandidates();
  } catch (e) { toast('恢复失败: ' + e.message, 'err'); }
}

async function deleteCandidateRow(id) {
  if (!confirm('确认删除该候选（含其全部实例数据）？')) return;
  try {
    await api('/api/ingest/candidates/' + id, { method: 'DELETE' });
    toast('候选已删除', 'ok');
    loadCandidates();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

async function approveCandidate(cid) {
  try {
    const j = await api('/api/ingest/candidates/' + cid + '/approve', { method: 'POST', body: '{}' });
    const result = j.data || {};
    const msg = result.violations?.length ? '入库被 SHACL 拦截: ' + result.violations.map(v => v.message).join('; ') : '候选已入库 ' + (result.imported || 0) + ' 条';
    toast(msg, result.violations?.length ? 'err' : 'ok');
    loadCandidates();
    loadInstances();
  } catch (e) { toast('入库失败: ' + e.message, 'err'); }
}

async function rejectCandidate(cid) {
  try {
    await api('/api/ingest/candidates/' + cid + '/reject', { method: 'POST', body: '{}' });
    toast('候选已驳回', 'ok');
    loadCandidates();
  } catch (e) { toast('驳回失败: ' + e.message, 'err'); }
}

let reviewingCandidateId = null;
let ingestObjectEditing = false;
let editingObjectClone = null;

function getObjectDefForCandidate(c) {
  const o = state.ontologies.find(x => x.id === c.ontology_id);
  if (!o) return null;
  return (o.objects || []).find(obj => obj.id === c.object_type) || null;
}

function renderAttrInput(attr, value, idx) {
  const inputId = 'instEdit_' + idx + '_' + attr.id;
  const val = value !== undefined && value !== null ? value : '';
  const requiredMark = attr.required ? '<span class="text-red-400">*</span>' : '';
  const label = '<label class="text-xs text-slate-400">' + esc(attr.name || attr.id) + requiredMark + ' <span class="text-slate-600">' + esc(attr.id) + '</span></label>';
  let input = '';
  if (attr.data_type === 'boolean') {
    input = '<select id="' + inputId + '" class="w-full"><option value="" ' + (val === '' ? 'selected' : '') + '>--</option><option value="true" ' + (val === true || val === 'true' ? 'selected' : '') + '>是</option><option value="false" ' + (val === false || val === 'false' ? 'selected' : '') + '>否</option></select>';
  } else if (attr.data_type === 'integer' || attr.data_type === 'float') {
    input = '<input id="' + inputId + '" type="number" class="w-full" value="' + esc(val) + '" />';
  } else if (attr.data_type === 'date') {
    input = '<input id="' + inputId + '" type="date" class="w-full" value="' + esc(val) + '" />';
  } else {
    input = '<input id="' + inputId + '" type="text" class="w-full" value="' + esc(val) + '" />';
  }
  return '<div class="mb-2">' + label + input + '</div>';
}

function renderRelInput(rel, value, idx) {
  const inputId = 'instEdit_' + idx + '_' + rel.id;
  const val = value !== undefined && value !== null ? value : '';
  const label = '<label class="text-xs text-amber-400/90">' + esc(rel.name || rel.id) + ' <span class="text-slate-600">→ ' + esc(rel.target_object) + '</span></label>';
  return '<div class="mb-2">' + label + '<input id="' + inputId + '" type="text" class="w-full" value="' + esc(val) + '" placeholder="目标对象的业务主键值" /></div>';
}

function startReviewCandidate(cid) {
  const c = state.candidates.find(x => x.id === cid);
  if (!c) return toast('候选未找到', 'err');
  reviewingCandidateId = cid;
  const readOnly = c.status !== 'pending';
  const objDef = getObjectDefForCandidate(c);
  const attrs = objDef?.attributes || [];
  const rels = objDef?.relations || [];
  const rows = (c.instances || []).map((inst, idx) => {
    const inputs = attrs.map(a => renderAttrInput(a, inst[a.id], idx)).join('') +
      rels.map(r => renderRelInput(r, inst[r.id], idx)).join('');
    const delBtn = readOnly ? '' : ' <span class="cursor-pointer text-amber-400" onclick="deleteReviewInstance(' + idx + ')">删除</span>';
    return '<div class="p-3 rounded border border-slate-700/50 mb-2">' +
      '<div class="flex justify-between text-xs text-slate-500 mb-2">#' + idx + delBtn + '</div>' +
      '<div class="grid grid-cols-2 gap-2">' + inputs + '</div>' +
    '</div>';
  }).join('');
  const saveBtn = readOnly ? '' : '<button class="btn" onclick="saveReviewCandidate()">保存修改</button>';
  const readOnlyNote = readOnly ? '<div class="text-xs text-slate-500 mb-2">当前状态为「' + (c.status === 'imported' ? '已入库' : '已驳回') + '」，只读查看。</div>' : '';
  const content = readOnlyNote + '<div class="space-y-2 max-h-[420px] overflow-auto mb-3">' + rows + '</div>' +
    '<div class="flex gap-2">' + saveBtn + '<button class="btn secondary" onclick="closeModal()">关闭</button></div>';
  showModal(readOnly ? '查看候选实例' : '审核候选实例', content, { readOnly });
}

function readAttrValue(attr, raw) {
  if (raw === '' || raw === undefined || raw === null) return null;
  if (attr.data_type === 'boolean') {
    if (raw === 'true') return true;
    if (raw === 'false') return false;
    return null;
  }
  if (attr.data_type === 'integer') {
    const v = parseInt(raw, 10);
    return isNaN(v) ? raw : v;
  }
  if (attr.data_type === 'float') {
    const v = parseFloat(raw);
    return isNaN(v) ? raw : v;
  }
  return raw;
}

async function saveReviewCandidate() {
  if (!reviewingCandidateId) return;
  const c = state.candidates.find(x => x.id === reviewingCandidateId);
  if (!c) return;
  const objDef = getObjectDefForCandidate(c);
  const attrs = objDef?.attributes || [];
  const rels = objDef?.relations || [];
  const edits = [];
  for (let idx = 0; idx < (c.instances || []).length; idx++) {
    const inst = { object_type: c.object_type };
    for (const a of attrs) {
      const el = $('instEdit_' + idx + '_' + a.id);
      if (!el) continue;
      const v = readAttrValue(a, el.value.trim());
      if (v !== null) inst[a.id] = v;
    }
    for (const r of rels) {
      const el = $('instEdit_' + idx + '_' + r.id);
      if (!el) continue;
      if (el.value.trim()) inst[r.id] = el.value.trim();
    }
    edits.push(inst);
  }
  try {
    await api('/api/ingest/candidates/' + reviewingCandidateId + '/instances', {
      method: 'PUT',
      body: JSON.stringify({ instances: edits })
    });
    toast('候选实例已更新', 'ok');
    closeModal();
    loadCandidates();
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function deleteReviewInstance(idx) {
  if (!reviewingCandidateId) return;
  try {
    await api('/api/ingest/candidates/' + reviewingCandidateId + '/instances/' + idx, { method: 'DELETE' });
    toast('已删除实例 #' + idx, 'ok');
    startReviewCandidate(reviewingCandidateId);
    loadCandidates();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

async function loadInstances() {
  const ontologyId = $('ingestOntology').value;
  const objectType = state.selectedIngestObject?.id || '';
  let url = '/api/ingest/instances?ontology_id=' + encodeURIComponent(ontologyId);
  if (objectType) url += '&object_type=' + encodeURIComponent(objectType);
  try {
    const j = await api(url);
    state.instances = j.data.items || [];
    renderIngestTarget();
    renderInstanceList();
  } catch (e) { toast('加载实例失败: ' + e.message, 'err'); }
}

// ---- 1.1 独立实例列表区（静态信息下方，点击行可编辑） ----
function renderInstanceList() {
  const el = $('instanceList');
  if (!el) return;
  const countEl = $('instanceCount');
  const list = state.instances || [];
  const scope = state.selectedIngestObject ? '「' + (state.selectedIngestObject.name || state.selectedIngestObject.id) + '」' : '全部对象';
  if (countEl) countEl.textContent = scope + ' 共 ' + list.length + ' 条';
  if (!list.length) {
    el.innerHTML = '<div class="text-sm text-slate-500">暂无已入库实例。选中左侧对象后使用「导入 / 添加实例」，或使用上方「多对象批量导入」。</div>';
    return;
  }
  el.innerHTML = list.map(inst => {
    const data = inst.instance || inst.props || inst;
    const preview = Object.entries(data)
      .filter(([k]) => !String(k).startsWith('_'))
      .slice(0, 5)
      .map(([k, v]) => esc(k) + '=' + esc(typeof v === 'object' ? JSON.stringify(v) : v))
      .join('　');
    const active = state.selectedInstance?.id === inst.id ? 'border-amber-500/60 bg-slate-800/60' : 'border-slate-700/50 hover:border-slate-600';
    return '<div class="p-2 rounded-lg border ' + active + ' cursor-pointer" onclick="selectInstance(' + JSON.stringify(inst).replace(/"/g, '&quot;') + ')">' +
      '<div class="flex justify-between"><span class="text-sm font-medium">' + esc(inst.object_type || '') + '</span><span class="text-xs text-slate-500">' + esc((inst.id || inst.node_id || '').slice(0, 16)) + '</span></div>' +
      '<div class="text-xs text-slate-400 mt-1 truncate">' + preview + '</div></div>';
  }).join('');
}

function renderInstanceDetail(inst) {
  const panel = $('instanceDetailPanel');
  const body = $('instanceDetailBody');
  const meta = $('instanceDetailMeta');
  panel.classList.remove('hidden');
  state.selectedInstance = inst;
  const data = inst.instance || inst.props || inst;
  const ontology = state.ontologies.find(o => o.id === inst.ontology_id) || {};
  const objDef = (ontology.objects || []).find(o => o.id === inst.object_type) || {};

  meta.textContent = (inst.id || inst.node_id || '') + ' · ' + (inst.object_type || '');

  let html = '';

  // 可编辑属性
  const attrRows = (objDef.attributes || []).map(a => {
    const val = data[a.id];
    const inputVal = (val === undefined || val === null) ? '' : String(val);
    const input = '<input class="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200" ' +
      'data-attr="' + esc(a.id) + '" value="' + esc(inputVal) + '" />';
    return '<tr><td class="border border-slate-700 p-1 text-slate-400">' + esc(a.name || a.id) + (a.required ? '*' : '') + '</td><td class="border border-slate-700 p-1">' + input + '</td></tr>';
  }).join('');
  html += '<div><label>属性（点击表格可直接编辑）</label><table class="w-full text-xs border-collapse" id="instanceEditTable">' + attrRows + '</table></div>';

  // 数据血缘（来源）
  html += renderSourceLineage(inst);

  // 关系血缘
  html += renderLineage(inst, objDef, ontology);
  body.innerHTML = html;
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderSourceLineage(inst) {
  const src = (inst.instance || inst.props || inst)._source || inst._source || {};
  let html = '<div><label>数据血缘 / 来源</label><table class="w-full text-xs border-collapse">';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">来源类型</td><td class="border border-slate-700 p-1 text-slate-200">' + esc(src.kind || '-') + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">来源标识</td><td class="border border-slate-700 p-1 text-slate-200">' + esc(src.connection_id || '-') + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">抽取时间</td><td class="border border-slate-700 p-1 text-slate-200">' + esc(src.fetched_at || '-') + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">原始引用</td><td class="border border-slate-700 p-1 text-slate-200">' + esc(src.raw_ref || '-') + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">候选 ID</td><td class="border border-slate-700 p-1 text-slate-200">' + esc(inst.candidate_id || '-') + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">入库时间</td><td class="border border-slate-700 p-1 text-slate-200">' + esc(inst.imported_at || '-') + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">更新时间</td><td class="border border-slate-700 p-1 text-slate-200">' + esc(inst.updated_at || '-') + '</td></tr>';
  html += '</table></div>';
  return html;
}

async function saveInstanceEdit() {
  const inst = state.selectedInstance;
  if (!inst) return;
  const inputs = document.querySelectorAll('#instanceEditTable input[data-attr]');
  const newInstance = { ...(inst.instance || inst.props || inst) };
  inputs.forEach(input => {
    const attrId = input.dataset.attr;
    const val = input.value.trim();
    if (val === '') delete newInstance[attrId];
    else newInstance[attrId] = val;
  });
  try {
    const j = await api('/api/ingest/instances/' + (inst.id || inst.node_id), {
      method: 'PUT',
      body: JSON.stringify({ instance: newInstance })
    });
    state.selectedInstance = j.data;
    renderInstanceDetail(j.data);
    loadInstances();
    toast('实例已更新', 'ok');
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function deleteInstance() {
  const inst = state.selectedInstance;
  if (!inst) return;
  if (!confirm('确定删除该实例？')) return;
  try {
    await api('/api/ingest/instances/' + (inst.id || inst.node_id), { method: 'DELETE' });
    closeInstanceDetail();
    loadInstances();
    toast('实例已删除', 'ok');
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

function _displayValue(v) {
  if (v === undefined || v === null) return '-';
  if (typeof v === 'object') return JSON.stringify(v).slice(0, 200);
  return String(v).slice(0, 200);
}

function renderLineage(inst, objDef, ontology) {
  const data = inst.instance || inst.props || inst;
  const rels = objDef.relations || [];
  if (!rels.length && !state.instances.length) return '';

  let html = '<div><label>血缘 / 关系</label>';
  let rows = '';

  // 出向关系
  for (const r of rels) {
    const raw = data[r.id];
    if (raw === undefined || raw === null || raw === '') continue;
    const targets = Array.isArray(raw) ? raw : [raw];
    for (const targetId of targets) {
      const target = state.instances.find(it => it.id === targetId);
      const targetLabel = target ? _instanceLabel(target) : '未知实例';
      const targetType = target ? (target.object_type || '') : r.target_object || '';
      rows += '<tr><td class="border border-slate-700 p-1 text-emerald-400">出向</td><td class="border border-slate-700 p-1 text-slate-400">' + esc(r.name || r.id) + '</td><td class="border border-slate-700 p-1 text-slate-300">' + esc(targetType) + '</td><td class="border border-slate-700 p-1 text-slate-300">' + esc(targetLabel) + ' <span class="text-slate-500">' + esc(String(targetId).slice(0, 12)) + '</span></td></tr>';
    }
  }

  // 入向关系
  for (const other of state.instances) {
    if (other.id === inst.id) continue;
    const otherData = other.instance || other.props || other;
    const otherObj = (ontology.objects || []).find(o => o.id === other.object_type) || {};
    for (const r of (otherObj.relations || [])) {
      if (r.target_object !== inst.object_type) continue;
      const raw = otherData[r.id];
      if (raw === undefined || raw === null || raw === '') continue;
      const targets = Array.isArray(raw) ? raw : [raw];
      if (targets.some(t => t === inst.id)) {
        rows += '<tr><td class="border border-slate-700 p-1 text-amber-400">入向</td><td class="border border-slate-700 p-1 text-slate-400">' + esc(r.name || r.id) + '</td><td class="border border-slate-700 p-1 text-slate-300">' + esc(other.object_type || '') + '</td><td class="border border-slate-700 p-1 text-slate-300">' + esc(_instanceLabel(other)) + ' <span class="text-slate-500">' + esc(String(other.id).slice(0, 12)) + '</span></td></tr>';
      }
    }
  }

  if (!rows) {
    html += '<div class="text-xs text-slate-500">暂无关系数据</div></div>';
    return html;
  }
  html += '<table class="w-full text-xs border-collapse"><thead><tr><th class="border border-slate-700 p-1 text-left">方向</th><th class="border border-slate-700 p-1 text-left">关系</th><th class="border border-slate-700 p-1 text-left">来源/目标类型</th><th class="border border-slate-700 p-1 text-left">来源/目标实例</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  return html;
}

function closeInstanceDetail() {
  state.selectedInstance = null;
  const panel = $('instanceDetailPanel');
  if (panel) panel.classList.add('hidden');
  renderIngestTarget();
}

function showModal(title, content, opts) {
  let modal = $('ingestModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'ingestModal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60';
    modal.innerHTML = '<div class="bg-slate-900 border border-slate-700 rounded-xl p-5 w-[720px] max-w-[95vw] max-h-[90vh] overflow-auto shadow-2xl"><h3 id="ingestModalTitle" class="font-medium mb-3"></h3><div id="ingestModalBody"></div></div>';
    document.body.appendChild(modal);
    modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
  }
  $('ingestModalTitle').textContent = title;
  $('ingestModalBody').innerHTML = content;
  if (opts && opts.readOnly) {
    document.querySelectorAll('#ingestModalBody input, #ingestModalBody textarea, #ingestModalBody select').forEach(i => { i.disabled = true; });
  }
  modal.classList.remove('hidden');
}

function closeModal() {
  const modal = $('ingestModal');
  if (modal) modal.classList.add('hidden');
}

// ---- MCP ----
const MCP_PAGE_SIZE = 10;
let mcpList = { page: 1, total: 0, items: [], meta: {} };

// ---- MCP 页：一键复制与接入配置 ----
// 通用复制：优先 Clipboard API（127.0.0.1/localhost 属安全上下文可用），
// 加 1.5s 超时竞速（个别环境下写入会挂起），失败回退 execCommand（用户手势下可用）
async function copyText(text, label) {
  label = label || '内容';
  let ok = false;
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await Promise.race([
        navigator.clipboard.writeText(text),
        new Promise((_, rej) => setTimeout(() => rej(new Error('clipboard timeout')), 1500)),
      ]);
      ok = true;
    } catch (e) { /* 回退 execCommand */ }
  }
  if (!ok) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      ok = document.execCommand('copy');
      ta.remove();
    } catch (e2) { ok = false; }
  }
  toast(ok ? label + '已复制' : '复制失败，请手动选择复制', ok ? 'ok' : 'err');
}

// 复制小图标：悬停提示「点击复制X」，点击复制并 toast
// 注意：参数注入 onclick 属性（双引号定界），JSON.stringify 的引号必须转成 &quot;
function copyIcon(text, label) {
  const q = s => JSON.stringify(s).replace(/"/g, '&quot;');
  return '<span class="ml-1 cursor-pointer text-slate-500 hover:text-sky-400" title="点击复制' + label + '" ' +
    'onclick="event.stopPropagation();copyText(' + q(text) + ', ' + q(label) + ')">⧉</span>';
}

// MCP 对外连接基址：后端固定 uvicorn --host 0.0.0.0 --port 8000。
// 注意不能取 location.host：前端可能跑在 8123 等静态端口，那不是后端。
function mcpBaseUrl() {
  if (location.protocol.startsWith('http') && location.port === '8000') return location.origin;
  return 'http://127.0.0.1:8000';
}

// 当前项目行（项目 ID + 一键复制）
function renderMcpProjectLine() {
  const el = $('mcpProjectLine');
  if (!el) return;
  const p = state.currentProject || {};
  el.innerHTML = p.id
    ? '当前项目 <b class="text-slate-200">' + esc(p.name || p.id) + '</b> · 项目 ID <code class="text-slate-300">' + esc(p.id) + '</code>' + copyIcon(p.id, '项目ID')
    : '';
}

// 完整 MCP Server 配置（当前项目），一键复制到 AI 平台
function renderMcpServerConfig() {
  const pre = $('mcpServerConfig');
  if (!pre) return;
  const project = state.currentProject || {};
  const projectId = project.id || '<project_id>';
  const url = mcpBaseUrl() + '/mcp/' + projectId;
  const serverName = (project.name || 'ontology').replace(/[^A-Za-z0-9_\u4e00-\u9fa5-]/g, '').slice(0, 24) || 'ontology';
  const text = JSON.stringify({ mcpServers: { [serverName]: { type: 'sse', url } } }, null, 2);
  pre.textContent = text;
  const btn = $('mcpCopyConfigBtn');
  if (btn) btn.onclick = () => copyText(text, 'MCP Server 配置');
  const hint = $('mcpConfigHint');
  if (hint) {
    const meta = (mcpList && mcpList.meta) || {};
    hint.textContent = meta.server_published
      ? '端点已在线：' + url + '（已发布 ' + (meta.published_count || 0) + ' 个本体）'
      : '当前项目尚未发布本体，端点 ' + url + ' 暂不可用——先在上方清单发布任一本体即可生效，本配置无需改动。';
  }
}

async function loadMcpOntologies(page) {
  const p = page || 1;
  try {
    const j = await api('/api/mcp/ontologies?page=' + p + '&page_size=' + MCP_PAGE_SIZE);
    const d = j.data || {};
    mcpList = { page: d.page || p, total: d.total || 0, items: d.items || [], meta: d };
    renderMcpList();
    renderMcpProjectLine();
    renderMcpServerConfig();
  } catch (e) { toast('加载本体发布清单失败: ' + e.message, 'err'); }
}

function renderMcpList() {
  const tb = $('mcpOntoBody');
  const { items, page, total } = mcpList;
  const meta = mcpList.meta || {};
  if (!items.length) {
    tb.innerHTML = '<tr><td colspan="7" class="p-3 text-center text-slate-500">当前项目暂无本体，请先在「建模」页创建</td></tr>';
  } else {
    tb.innerHTML = items.map((o, i) => {
      const seq = (page - 1) * MCP_PAGE_SIZE + i + 1;
      const act = o.published
        ? '<span class="cursor-pointer text-amber-400" title="取消该本体的发布；项目内无已发布本体时端点自动下线" onclick="mcpTogglePublish(\'' + o.ontology_id + '\', false)">取消发布</span>'
        : '<span class="cursor-pointer text-emerald-400" title="发布到 /mcp/{项目id}" onclick="mcpTogglePublish(\'' + o.ontology_id + '\', true)">发布</span>';
      return '<tr class="border-b border-slate-800 hover:bg-slate-800/30">' +
        '<td class="p-1 text-slate-400">' + seq + '</td>' +
        '<td class="p-1 font-mono text-slate-500 whitespace-nowrap" title="' + esc(o.ontology_id) + '">' + o.ontology_id.slice(0, 8) + '…' + copyIcon(o.ontology_id, '本体ID') + '</td>' +
        '<td class="p-1">' + esc(o.name) + '</td>' +
        '<td class="p-1 text-slate-400 max-w-[280px] truncate" title="' + esc(o.description || '') + '">' + esc(o.description || '-') + '</td>' +
        '<td class="p-1 text-center">' + (o.entity_count ?? 0) + '</td>' +
        '<td class="p-1 text-center">' + (o.instance_count ?? 0) + '</td>' +
        '<td class="p-1 whitespace-nowrap">' +
          '<span class="mr-2 ' + (o.published ? 'text-emerald-400' : 'text-slate-500') + '">' + (o.published ? '已发布' : '未发布') + '</span>' +
          '<span class="mr-2 text-sky-400 cursor-pointer" title="在建模页打开编辑" onclick="mcpEditOntology(\'' + o.ontology_id + '\')">修改</span>' +
          '<span class="mr-2 text-red-400 cursor-pointer" title="删除本体及其实例/候选数据" onclick="mcpDeleteOntology(\'' + o.ontology_id + '\')">删除</span>' +
          act +
        '</td>' +
        '</tr>';
    }).join('');
  }
  const pages = Math.max(1, Math.ceil(total / MCP_PAGE_SIZE));
  const statusEl = $('mcpStatusLine');
  if (statusEl) {
    statusEl.innerHTML = meta.server_published
      ? '<span class="text-emerald-400">● 端点在线</span> <code class="text-slate-300">' + esc(meta.endpoint || '') + '</code>（已发布 ' + (meta.published_count || 0) + ' 个本体，共享该端点）'
      : '<span class="text-slate-500">○ 端点未上线</span> 当前项目尚无已发布本体；发布任一本体后端点 <code class="text-slate-400">' + esc(meta.endpoint || '') + '</code> 自动上线';
  }
  const info = $('mcpPageInfo');
  if (info) info.textContent = '第 ' + page + ' / ' + pages + ' 页 · 共 ' + total + ' 个本体';
  const prev = $('mcpPrevBtn'), next = $('mcpNextBtn');
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= pages;
}

// 按本体发布/取消发布（项目内清单清空后 Server 自动下线）
async function mcpTogglePublish(ontologyId, publish) {
  try {
    const url = (publish ? '/api/mcp/server/publish?ontology_id=' : '/api/mcp/server/unpublish?ontology_id=') + encodeURIComponent(ontologyId);
    await api(url, { method: 'POST', body: '{}' });
    toast(publish ? '已发布: ' + ontologyId : '已取消发布: ' + ontologyId, 'ok');
    loadMcpOntologies();
  } catch (e) { toast((publish ? '发布失败: ' : '取消发布失败: ') + e.message, 'err'); }
}

// MCP 页：跳转到建模页打开该本体的编辑表单
async function mcpEditOntology(oid) {
  try {
    await loadOntologies();
    const o = state.ontologies.find(x => x.id === oid);
    if (!o) return toast('本体不存在（可能已被删除）', 'err');
    switchTab('modeling');
    state.currentOntology = o;
    selectOntology(o);
    editCurrentOntology();
  } catch (e) { toast('打开编辑失败: ' + e.message, 'err'); }
}

// MCP 页：删除本体（复用建模页级联删除逻辑，含确认与实例二次确认）
function mcpDeleteOntology(oid) { deleteOntologyById(oid); }

async function loadMcpTools() {
  try {
    const j = await api('/api/mcp/server/tools');
    const tools = j.data || [];
    const el = $('mcpToolsList');
    if (!tools.length) { el.innerHTML = '<div class="text-sm text-slate-500">暂无工具</div>'; return; }
    el.innerHTML = tools.map(t => '<div class="p-2 rounded-lg border border-slate-700/50 cursor-pointer hover:bg-slate-800/40" onclick="showMcpExample(' + JSON.stringify(t).replace(/"/g, '&quot;') + ')">' +
      '<div class="flex justify-between"><span class="text-sm font-medium">' + esc(t.name) + '</span><span class="text-xs ' + (t.side_effect ? 'text-red-400' : 'text-emerald-400') + '">' + (t.side_effect ? '有副作用' : '只读') + '</span></div>' +
      '<div class="text-xs text-slate-400">' + esc(t.description || '') + '</div>' +
      '<div class="text-xs text-slate-500 mt-1">读: ' + (t.reads || []).join(', ') + ' · 写: ' + (t.writes || []).join(', ') + '</div>' +
    '</div>').join('');
    if (tools[0]) showMcpExample(tools[0]);
  } catch (e) { toast('加载 MCP 工具失败: ' + e.message, 'err'); }
}

function showMcpExample(tool) {
  const example = tool.example || {};
  const base = mcpBaseUrl();
  const projectId = state.currentProject?.id || '<project_id>';
  const text = 'SSE 端点（GET，连接入口，AI 平台配置只填这个）: ' + base + '/mcp/' + projectId +
    '\n消息端点（POST，SSE 握手后客户端自动使用，无需手填）: ' + base + '/mcp/' + projectId + '/messages/' +
    '\n\n' + JSON.stringify(example, null, 2);
  $('mcpExample').textContent = text;
}

// ---- 图谱 ----
function setGraphTab(t) {
  state.graphTab = t;
  $('tabSchema').classList.toggle('bg-slate-700', t === 'schema');
  $('tabKnowledge').classList.toggle('bg-slate-700', t === 'knowledge');
  loadGraph();
}

async function loadGraph() {
  const id = $('graphOntology').value;
  if (!id) {
    $('graphContainer').innerHTML = '<div class="p-8 text-center text-slate-500">当前项目下暂无本体，请先在「建模」页创建或切换到包含本体的项目。</div>';
    return;
  }
  try {
    const j = await api('/api/graph/' + id + '?mode=' + state.graphTab);
    const o = state.ontologies.find(x => x.id === id);
    if (o) state.currentOntology = o;
    drawGraph(j.data);
  } catch (e) { toast('加载图谱失败: ' + e.message, 'err'); }
}

function _graphEmptyHint(data) {
  const mode = data.mode || state.graphTab;
  const hasInstances = (data.nodes || []).some(n => n.kind === 'instance' || n.type === 'instance');
  if (mode === 'knowledge' && !hasInstances) {
    return '<div class="absolute inset-0 flex items-center justify-center pointer-events-none"><div class="text-center"><div class="text-sm text-slate-400">知识图谱视图当前无实例数据</div><div class="text-xs text-slate-500 mt-1">请在「实例导入」页导入数据后再查看</div></div></div>';
  }
  return '';
}

async function runNlQuery() {
  const id = $('graphOntology').value;
  const question = $('nlQueryInput').value.trim();
  if (!id) { toast('请先选择本体', 'err'); return; }
  if (!question) { toast('请输入查询问题', 'err'); return; }
  $('nlQueryResult').innerHTML = '<span class="text-slate-400">生成 SPARQL 并执行中…</span>';
  try {
    const j = await api('/api/graph/' + id + '/nl-query', { method: 'POST', body: JSON.stringify({ question }) });
    const d = j.data;
    $('nlQuerySparql').textContent = d.sparql;
    $('nlQuerySparql').classList.remove('hidden');
    // 如果 LLM 不可达，后端会返回确定性 fallback SPARQL，给用户友好提示
    const fallbackHint = (d.sparql || '').includes('http://ontology.local/') && !d.sparql.toLowerCase().startsWith('select ?s where { ?s a <http://ontology.local/thing>') ? '' : '<div class="text-xs text-amber-400 mb-1">当前 LLM 未正确返回 SPARQL，已使用本地规则生成兜底查询。</div>';
    if (!d.rows || !d.rows.length) {
      $('nlQueryResult').innerHTML = fallbackHint + '<span class="text-slate-400">查询结果为空（共 0 条）</span>';
      return;
    }
    const keys = Object.keys(d.rows[0]);
    let html = '<div class="text-xs text-slate-400 mb-1">共 ' + d.count + ' 条</div>';
    html += fallbackHint;
    html += '<table class="w-full text-xs border-collapse"><thead><tr>' + keys.map(k => '<th class="border border-slate-700 p-1 text-left">' + esc(k) + '</th>').join('') + '</tr></thead><tbody>';
    html += d.rows.map(r => '<tr>' + keys.map(k => '<td class="border border-slate-700 p-1">' + esc(String(r[k] ?? '')) + '</td>').join('') + '</tr>').join('');
    html += '</tbody></table>';
    $('nlQueryResult').innerHTML = html;
  } catch (e) { toast('自然语言查询失败: ' + e.message, 'err'); $('nlQueryResult').innerHTML = '<span class="text-red-400">失败</span>'; }
}

function drawGraph(data) {
  const container = $('graphContainer');
  const width = container.clientWidth;
  const height = container.clientHeight || 500;
  container.innerHTML = '<div class="graph-tooltip" id="graphTooltip"></div>' + _graphEmptyHint(data);
  const svg = d3.select('#graphContainer').append('svg').attr('width', width).attr('height', height);
  const nodes = data.nodes || [];
  const nodeIds = new Set(nodes.map(n => n.id));
  // 过滤掉引用缺失节点的边，避免 D3 报 "node not found"
  const links = (data.edges || [])
    .filter(e => nodeIds.has(e.source) && nodeIds.has(e.target))
    .map(e => ({ source: e.source, target: e.target, label: e.relation || e.label, origin: e.origin }));
  const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(110))
    .force('charge', d3.forceManyBody().strength(-280))
    .force('center', d3.forceCenter(width / 2, height / 2));

  const g = svg.append('g');
  svg.call(d3.zoom().on('zoom', e => g.attr('transform', e.transform)));

  const link = g.append('g').selectAll('line').data(links).enter().append('line')
    .attr('stroke', '#64748b').attr('stroke-width', 1.5).attr('stroke-opacity', 0.6);
  const linkLabel = g.append('g').selectAll('text').data(links).enter().append('text')
    .text(d => d.label).attr('font-size', 10).attr('fill', '#94a3b8').attr('text-anchor', 'middle');
  const node = g.append('g').selectAll('g').data(nodes).enter().append('g').call(d3.drag()
    .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }));
  // 优先用 kind 字段，兼容旧 type 字段
  const nodeKind = d => d.kind || d.type || 'object';
  node.append('circle').attr('r', d => nodeKind(d) === 'object' ? 22 : 14)
    .attr('fill', d => {
      const k = nodeKind(d);
      return k === 'object' ? '#4f8cff' : k === 'attribute' ? '#34d399' : '#f59e0b';
    });
  node.append('text').attr('dy', 4).attr('text-anchor', 'middle').text(d => d.label || d.name || d.id).attr('font-size', 11).attr('fill', '#fff');

  const tip = $('graphTooltip');
  node.on('mouseover', (e, d) => {
    tip.style.opacity = 1;
    const k = nodeKind(d);
    tip.innerHTML = '<strong>' + esc(d.label || d.name || d.id) + '</strong><br/>type: ' + k + (d.source ? '<br/>source: ' + d.source : '') + (d.object_type ? '<br/>object_type: ' + esc(d.object_type) : '');
    tip.style.left = (e.pageX + 10) + 'px';
    tip.style.top = (e.pageY + 10) + 'px';
  }).on('mouseout', () => tip.style.opacity = 0);

  simulation.on('tick', () => {
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    linkLabel.attr('x', d => (d.source.x + d.target.x) / 2).attr('y', d => (d.source.y + d.target.y) / 2);
    node.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
  });
}

// ---- 两行式下拉（业务友好）：主行=语义 label，次行小字=id ----
// 原生 select 的 option 不支持 HTML（无法两行/小字），故自绘；值仍存同名 hidden input，
// 既有 $('impactInstance').value 读取点无需改动。
function bindLabeledSelect(cfg) {
  const trigger = $(cfg.triggerId), panel = $(cfg.panelId), hidden = $(cfg.hiddenId);
  const emptyText = cfg.emptyText || '-- 请选择 --';
  let options = [];
  function renderTrigger() {
    const sel = options.find(o => o.value === hidden.value);
    trigger.innerHTML = sel
      ? '<span class="ls-main">' + esc(sel.main) + '</span><span class="ls-sub">' + esc(sel.sub || '') + '</span>'
      : '<span class="ls-main ls-empty">' + esc(emptyText) + '</span>';
  }
  function renderPanel() {
    panel.innerHTML = options.length
      ? options.map(o => '<div class="ls-opt' + (o.value === hidden.value ? ' selected' : '') + '" data-v="' + esc(o.value).replace(/"/g, '&quot;') + '">' +
          '<span class="ls-main">' + esc(o.main) + '</span><span class="ls-sub">' + esc(o.sub || '') + '</span></div>').join('')
      : '<div class="ls-opt"><span class="ls-main ls-empty">' + esc(emptyText) + '</span></div>';
    panel.querySelectorAll('.ls-opt[data-v]').forEach(el => {
      el.addEventListener('click', () => {
        hidden.value = el.dataset.v;
        panel.classList.add('hidden');
        renderTrigger();
        if (cfg.onChange) cfg.onChange(hidden.value);
      });
    });
  }
  trigger.addEventListener('click', e => {
    e.stopPropagation();
    panel.classList.toggle('hidden');
    if (!panel.classList.contains('hidden')) renderPanel();
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('#' + cfg.boxId)) panel.classList.add('hidden');
  });
  renderTrigger();
  return {
    setOptions(list) { options = list || []; renderTrigger(); renderPanel(); },
    setValue(v) { hidden.value = v || ''; renderTrigger(); },
    get value() { return hidden.value; },
  };
}

// 推理页 label 解析：对象/属性 id → 中文语义名（来自当前选中本体的完整定义）
function getInferLabelMaps() {
  const ont = state.ontologies.find(o => o.id === $('inferOntology').value) || {};
  const objById = new Map((ont.objects || []).map(o => [o.id, o]));
  return {
    ont,
    objName: ot => ((objById.get(ot) || {}).name) || ot,
    attrName: (ot, aid) => ((objById.get(ot) || {}).attributes || []).find(a => a.id === aid)?.name || aid,
  };
}

// 实例展示名：优先业务名称属性（名称/name/title/label）→ stable_key → 首个非空属性 → 实例 id
function instanceDisplayLabel(inst, maps) {
  const d = inst.instance || inst.props || inst;
  const obj = (maps.ont.objects || []).find(o => o.id === inst.object_type) || {};
  const attrs = obj.attributes || [];
  const has = a => a && d[a.id] !== undefined && d[a.id] !== null && String(d[a.id]) !== '';
  const byName = attrs.find(a => /名称|名字|简称|name|title|label/i.test(a.name || '') || /name|title|label/i.test(a.id || ''));
  const byStable = obj.stable_key ? attrs.find(a => a.id === obj.stable_key) : null;
  const first = attrs.find(has);
  const label = has(byName) ? d[byName.id] : has(byStable) ? d[byStable.id] : has(first) ? d[first.id] : null;
  return label !== null && label !== undefined ? String(label) : String(inst.id || '').slice(0, 8);
}

// ---- 推理 ----
async function loadInference() {
  const id = $('inferOntology').value;
  if (!id) return;
  try {
    const j = await api('/api/ontologies/' + id + '/inference');
    const steps = j.data.steps || [];
    const el = $('inferenceSteps');
    el.innerHTML = steps.length ? steps.map((s, i) =>
      '<div class="p-2 rounded-lg border border-slate-700/50 text-sm">' +
      '<div><span class="text-slate-500">#' + (i + 1) + '</span> ' + esc(s.rule) + '</div>' +
      '<div class="text-xs text-slate-400 mt-1">前提: ' + esc((s.premises || []).join('；')) + '</div>' +
      '<div class="text-xs text-emerald-400">结论: ' + esc(s.conclusion || '') + '</div>' +
      '</div>'
    ).join('') : '<div class="text-sm text-slate-500">无推理结论（该本体未发现可推导的隐含公理）</div>';
  } catch (e) { toast('加载推理失败: ' + e.message, 'err'); }
  loadImpactInstances(id);
}

// ---- 3.1 业务影响推理（实例值变化 → 下游业务影响） ----
let impactInstances = [];
let impactInstanceLS = null;
let impactAttributeLS = null;

async function loadImpactInstances(ontologyId) {
  const id = ontologyId || $('inferOntology').value;
  if (!id) return;
  try {
    const ji = await api('/api/ingest/instances?ontology_id=' + encodeURIComponent(id) + '&pageSize=200');
    impactInstances = (ji.data && ji.data.items) || [];
    if (!impactInstanceLS) return;
    const maps = getInferLabelMaps();
    // 两行选项：主行=对象中文名·实例展示名；次行小字=对象id·实例id
    impactInstanceLS.setOptions(impactInstances.map(inst => ({
      value: inst.id,
      main: (maps.objName(inst.object_type) || inst.object_type) + ' · ' + instanceDisplayLabel(inst, maps),
      sub: (inst.object_type || '') + ' · ' + (inst.id || ''),
    })));
    impactAttributeLS.setValue('');
    onImpactInstanceChange();
  } catch (e) { /* 实例加载失败不阻断模式层推理展示 */ }
}

function onImpactInstanceChange() {
  const inst = impactInstances.find(x => x.id === $('impactInstance').value);
  const newVal = $('impactNewValue');
  if (!impactAttributeLS) return;
  if (!inst) {
    impactAttributeLS.setOptions([]);
    newVal.value = '';
    newVal.dataset.attr = '';
    return;
  }
  const maps = getInferLabelMaps();
  const d = inst.instance || inst.props || inst;
  // 排除系统字段：_ 前缀（_source 等）与 object_type（实例类型标记，非业务属性）
  const entries = Object.entries(d).filter(([k, v]) => !String(k).startsWith('_') && k !== 'object_type' && v !== null && v !== '');
  // 两行选项：主行=属性中文名（当前值）；次行小字=属性id
  impactAttributeLS.setOptions(entries.map(([k, v]) => ({
    value: k,
    main: maps.attrName(inst.object_type, k) + '（当前: ' + String(typeof v === 'object' ? JSON.stringify(v) : v).slice(0, 24) + '）',
    sub: k,
  })));
  newVal.dataset.attr = '';
  loadImpactPreview();
}

function loadImpactPreview() {
  const inst = impactInstances.find(x => x.id === $('impactInstance').value);
  const attr = $('impactAttribute').value;
  const newVal = $('impactNewValue');
  if (!inst || !attr) { newVal.placeholder = '如 2026-11-01'; newVal.dataset.attr = ''; return; }
  const d = inst.instance || inst.props || inst;
  const cur = d[attr];
  newVal.placeholder = '如 2026-11-01';
  if (!newVal.dataset.attr) { newVal.dataset.attr = attr; }
  if (newVal.dataset.attr === attr && !newVal.value) {
    // 日期类属性默认给出 +3 天示例
    const m = String(cur).match(/^(\d{4}-\d{2}-\d{2})/);
    if (m) {
      const dt = new Date(m[1] + 'T00:00:00');
      dt.setDate(dt.getDate() + 3);
      newVal.value = dt.toISOString().slice(0, 10);
    }
  }
}

async function runImpactInference() {
  const ontologyId = $('inferOntology').value;
  const instanceId = $('impactInstance').value;
  const attribute = $('impactAttribute').value;
  const newValue = $('impactNewValue').value.trim();
  const out = $('impactResult');
  if (!ontologyId || !instanceId || !attribute || !newValue) {
    return toast('请选择本体、实例、变化属性并填写新值', 'info');
  }
  out.innerHTML = '<div class="text-slate-400">推理中…</div>';
  try {
    const j = await api('/api/inference/impact', {
      method: 'POST',
      body: JSON.stringify({ ontology_id: ontologyId, instance_id: instanceId, attribute, new_value: newValue }),
    });
    const maps = getInferLabelMaps();
    const r = j.data || {};
    const src = r.source || {};
    const rows = (r.impacted || []);
    let html = '<div class="p-2 rounded-lg bg-slate-800/50 text-xs mb-3">' +
      '<div class="ls-main text-[13px]"><b>变化源</b>：' + esc((maps.objName(src.object_type) || src.object_type || '') + ' · ' + (src.label || src.instance_id || '')) +
      ' 的 <b>' + esc(maps.attrName(src.object_type, attribute)) + '</b>：' +
      esc(String(src.old_value ?? '')) + ' → <b>' + esc(newValue) + '</b>' +
      (src.delta_days !== null && src.delta_days !== undefined ? '（' + (src.delta_days > 0 ? '推迟' : '提前') + ' ' + Math.abs(src.delta_days) + ' 天）' : '') + '</div>' +
      '<div class="ls-sub">' + esc(src.object_type || '') + ' · ' + esc(src.instance_id || '') + ' · ' + esc(attribute) + '</div>' +
      '</div>';
    html += '<div class="text-xs text-slate-400 mb-3">' + esc(r.explanation || '') + '</div>';
    if (!rows.length) {
      html += '<div class="text-sm text-slate-500">未发现受影响的下游实例。</div>';
    } else {
      html += '<table class="w-full text-xs border-collapse"><thead><tr class="text-slate-400">' +
        '<th class="border-b border-slate-700 p-1 text-left">受影响实例</th>' +
        '<th class="border-b border-slate-700 p-1 text-left">推断变化（需人工确认）</th>' +
        '<th class="border-b border-slate-700 p-1 text-left">推理依据</th>' +
        '</tr></thead><tbody>';
      rows.forEach(item => {
        const changes = (item.inferred_changes || []).map(c =>
          '<div class="mb-1.5">' +
          '<div>' + esc(maps.attrName(item.object_type, c.attribute)) + ': <span class="text-slate-400">' + esc(String(c.current)) + '</span> → <span class="text-amber-300">' + esc(c.inferred) + '</span>' +
          ' <span class="badge ' + (c.confidence === 'high' ? 'draft' : 'active') + '">' + (c.confidence === 'high' ? '高置信' : '中置信') + '</span></div>' +
          '<div class="ls-sub">' + esc(c.attribute) + '</div>' +
          '</div>'
        ).join('') || '<span class="text-slate-500">仅直接引用，无值级推断</span>';
        html += '<tr class="border-b border-slate-800 align-top">' +
          '<td class="p-1">' +
          '<div class="ls-main">' + esc((maps.objName(item.object_type) || item.object_type) + ' · ' + (item.label || item.instance_id)) + '</div>' +
          '<div class="ls-sub">' + esc(item.object_type || '') + ' · ' + esc(item.instance_id || '') + '</div>' +
          '</td>' +
          '<td class="p-1 text-xs">' + changes + '</td>' +
          '<td class="p-1 text-slate-400">' + esc(item.reason || '') + '</td>' +
          '</tr>';
      });
      html += '</tbody></table>';
      html += '<div class="text-xs text-slate-500 mt-2">规则：' + esc((r.rules || []).join('；')) + '</div>';
    }
    out.innerHTML = html;
  } catch (e) {
    out.innerHTML = '<div class="text-red-400 text-sm">推理失败：' + esc(e.message) + '</div>';
    toast('影响推理失败: ' + e.message, 'err');
  }
}

// ---- LLM 设置 ----
async function loadLLMSettings() {
  try {
    const [settingsRes, envRes] = await Promise.all([
      api('/api/llm/settings', { projectId: null }),
      api('/api/llm/env-hints', { projectId: null })
    ]);
    const s = settingsRes.data || {};
    const env = envRes.data || {};
    state.llmSettings = s;
    $('llmProvider').value = s.provider || 'openai_compatible';
    $('llmModel').value = s.model || '';
    $('llmTemperature').value = s.temperature ?? 0.2;
    $('llmTimeout').value = s.timeout ?? 120;
    $('llmMaxTokens').value = s.max_tokens ?? 4096;
    $('llmBaseUrl').value = s.base_url || env.LLM_BASE_URL || env.LLM_ENDPOINT || '';
    $('llmApiKey').value = '';
    const keyStatus = $('llmKeyStatus');
    const source = s.api_key_source || 'none';
    if (source === 'env' || env.LLM_API_KEY) {
      keyStatus.textContent = '已配置（环境变量）';
      keyStatus.className = 'text-xs badge active';
    } else if (source === 'file' || s.api_key_configured) {
      keyStatus.textContent = '已配置（本地文件）';
      keyStatus.className = 'text-xs badge active';
    } else {
      keyStatus.textContent = '未配置';
      keyStatus.className = 'text-xs badge draft';
    }
    const prompts = s.prompts || {};
    $('promptOntologyGeneration').value = prompts.ontology_generation || '';
    $('promptInstanceExtraction').value = prompts.instance_extraction || '';
    $('promptReverseEngineering').value = prompts.reverse_engineering || '';
    $('promptKgConstruction').value = prompts.kg_construction || '';
    if ($('ingestLlmEndpoint')) $('ingestLlmEndpoint').value = s.base_url || env.LLM_BASE_URL || env.LLM_ENDPOINT || '';
    if ($('ingestLlmModel')) $('ingestLlmModel').value = s.model || '';
  } catch (e) { toast('加载 LLM 设置失败: ' + e.message, 'err'); }
}

function toggleApiKeyVisibility() {
  const input = $('llmApiKey');
  const eye = $('apiKeyEye');
  if (input.type === 'password') {
    input.type = 'text';
    eye.textContent = '🙈';
  } else {
    input.type = 'password';
    eye.textContent = '👁';
  }
}

async function saveLLMSettings() {
  const payload = {
    provider: $('llmProvider').value,
    model: $('llmModel').value.trim(),
    base_url: $('llmBaseUrl').value.trim(),
    temperature: parseFloat($('llmTemperature').value),
    timeout: parseInt($('llmTimeout').value, 10),
    max_tokens: parseInt($('llmMaxTokens').value, 10),
    api_key: $('llmApiKey').value,
    prompts: {
      ontology_generation: $('promptOntologyGeneration').value,
      instance_extraction: $('promptInstanceExtraction').value,
      reverse_engineering: $('promptReverseEngineering').value,
      kg_construction: $('promptKgConstruction').value,
    }
  };
  try {
    await api('/api/llm/settings', { method: 'PUT', body: JSON.stringify(payload), projectId: null });
    $('llmApiKey').value = '';
    state.llmSettings.base_url = payload.base_url;
    state.llmSettings.model = payload.model;
    await loadLLMSettings();
    toast('LLM 设置已保存', 'ok');
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function testLLMConnection() {
  const el = $('llmTestResult');
  el.classList.remove('hidden');
  el.textContent = '测试中…';
  try {
    const payload = {
      base_url: $('llmBaseUrl').value.trim() || undefined,
      model: $('llmModel').value.trim() || undefined,
      timeout: 30
    };
    const j = await api('/api/llm/test', { method: 'POST', body: JSON.stringify(payload), projectId: null });
    const d = j.data || {};
    el.textContent = d.ok
      ? '连通成功\n模型: ' + d.model + '\n延迟: ' + d.latency_ms + 'ms\nURL: ' + d.url + '\n预览: ' + (d.content_preview || '')
      : '连通失败\n' + (d.error || '');
    el.className = d.ok ? 'text-xs ok' : 'text-xs bad';
  } catch (e) {
    el.textContent = '请求失败: ' + e.message;
    el.className = 'text-xs bad';
  }
}

// ---- Action ----
const ACTION_TEMPLATES = [
  {
    name: '同步采购订单到 ERP', category: '数据同步', description: '将审批通过的采购订单推送到 ERP 系统生成正式订单。',
    target_connection: 'erp_system', invoke: { kind: 'webhook', url: 'https://erp.example.com/api/purchase-orders', method: 'POST' },
    input_map: { 'poNumber': '$.PurchaseOrder.poNumber', 'supplierCode': '$.PurchaseOrder.supplierCode', 'poQty': '$.PurchaseOrder.poQty', 'originalRequiredDate': '$.PurchaseOrder.originalRequiredDate' },
    idempotency_key: 'po::{poNumber}', confirmation: 'human_required', dry_run_supported: true
  },
  {
    name: '发送审批通知', category: '通知推送', description: '当采购计划提交审批时，向钉钉/企业微信发送待办通知。',
    target_connection: 'notify_bot', invoke: { kind: 'mcp_tool', tool: 'send_approval_notice' },
    input_map: { 'planCode': '$.PurchasePlan.planCode', 'planName': '$.PurchasePlan.planName', 'approver': '$.PurchasePlan.approver' },
    idempotency_key: 'notice::{planCode}', confirmation: 'auto', dry_run_supported: true
  },
  {
    name: '新建客户到 CRM', category: '客户管理', description: '将潜在供应商或客户信息同步到 CRM，避免销售重复录入。',
    target_connection: 'crm_saas', invoke: { kind: 'rest', url: 'https://crm.example.com/api/customers', method: 'POST' },
    input_map: { 'customerName': '$.Supplier.supplierName', 'contactPhone': '$.Supplier.contactPhone', 'certStatus': '$.Supplier.certStatus' },
    idempotency_key: 'customer::{customerName}', confirmation: 'human_required', dry_run_supported: true
  },
  {
    name: '更新库存状态', category: '库存更新', description: '根据入库单同步仓库库存数量。',
    target_connection: 'wms_system', invoke: { kind: 'webhook', url: 'https://wms.example.com/api/inventory', method: 'PUT' },
    input_map: { 'materialCode': '$.Material.materialCode', 'warehouse': '$.Inventory.warehouseCode', 'qty': '$.Inventory.qty' },
    idempotency_key: 'inv::{materialCode}::{warehouse}', confirmation: 'human_required', dry_run_supported: true
  }
];

function _actionKindLabel(kind) {
  return { mcp_tool: 'MCP Tool', webhook: 'Webhook', rest: 'REST' }[kind] || kind;
}

function _actionCategoryBadge(category) {
  const cat = category || '未分类';
  const colors = {
    '数据同步': 'text-blue-400 border-blue-500/30 bg-blue-500/10',
    '审批触发': 'text-amber-400 border-amber-500/30 bg-amber-500/10',
    '通知推送': 'text-emerald-400 border-emerald-500/30 bg-emerald-500/10',
    '客户管理': 'text-purple-400 border-purple-500/30 bg-purple-500/10',
    '库存更新': 'text-cyan-400 border-cyan-500/30 bg-cyan-500/10'
  };
  return '<span class="badge ' + (colors[cat] || '') + '">' + esc(cat) + '</span>';
}

function _actionIcon(category) {
  const map = { '数据同步': '↔', '审批触发': '✓', '通知推送': '🔔', '客户管理': '👤', '库存更新': '📦' };
  return map[category] || '⚡';
}

async function loadActions() {
  try {
    const j = await api('/api/actions?page=1&pageSize=100');
    state.actions = j.data.items || [];
    renderActionCategoryFilter();
    renderActionList();
    renderActionTemplateCards();
    if (state.currentAction) selectAction(state.currentAction.id);
  } catch (e) { toast('加载业务动作失败: ' + e.message, 'err'); }
}

function renderActionCategoryFilter() {
  const sel = $('actionCategoryFilter');
  if (!sel) return;
  const cats = Array.from(new Set(state.actions.map(a => a.category).filter(Boolean))).sort();
  const current = sel.value;
  sel.innerHTML = '<option value="">全部分类</option>' + cats.map(c => '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('');
  if (cats.includes(current)) sel.value = current;
}

function renderActionList() {
  const el = $('actionList');
  const filter = $('actionCategoryFilter')?.value || '';
  const items = filter ? state.actions.filter(a => a.category === filter) : state.actions;
  if (!items.length) {
    el.innerHTML = '<div class="text-sm text-slate-500">' + (filter ? '该分类下暂无动作' : '暂无业务动作，可从右侧模板快速创建') + '</div>';
    return;
  }
  el.innerHTML = items.map(a => {
    const active = state.currentAction && state.currentAction.id === a.id;
    return '<div class="object-card cursor-pointer ' + (active ? 'border-blue-500/60' : '') + '" onclick="selectAction(\'' + a.id + '\')">' +
      '<div class="flex justify-between items-start">' +
        '<div class="flex gap-3">' +
          '<div class="w-9 h-9 rounded-lg bg-slate-800 border border-slate-700 flex items-center justify-center text-lg">' + _actionIcon(a.category) + '</div>' +
          '<div>' +
            '<div class="font-medium">' + esc(a.name || a.action_id) + '</div>' +
            '<div class="text-xs text-slate-400 mt-1">' + _actionCategoryBadge(a.category) + ' · ' + esc(_actionKindLabel(a.invoke?.kind)) + ' · ' + esc(a.target_connection || '无连接') + '</div>' +
            (a.description ? '<div class="text-xs text-slate-500 mt-1">' + esc(a.description) + '</div>' : '') +
          '</div>' +
        '</div>' +
        '<div class="flex gap-2"><button class="text-xs text-blue-400" onclick="event.stopPropagation();editAction(\'' + a.id + '\')">编辑</button>' +
        '<button class="text-xs text-red-400" onclick="event.stopPropagation();deleteAction(\'' + a.id + '\')">删除</button></div>' +
      '</div></div>';
  }).join('');
}

function renderActionTemplateCards() {
  const el = $('actionTemplateCards');
  if (!el) return;
  el.innerHTML = ACTION_TEMPLATES.map((t, i) =>
    '<div class="card p-4 text-left hover:border-blue-500/50 transition-colors">' +
      '<div class="flex items-center gap-2 mb-2"><span class="text-xl">' + _actionIcon(t.category) + '</span><span class="font-medium">' + esc(t.name) + '</span></div>' +
      '<div class="text-xs text-slate-400 mb-3">' + esc(t.description) + '</div>' +
      '<div class="flex justify-between items-center">' + _actionCategoryBadge(t.category) +
        '<button class="btn text-xs" onclick="createActionFromTemplate(' + i + ')">使用模板</button>' +
      '</div>' +
    '</div>'
  ).join('');
}

function showActionTemplates() {
  state.currentAction = null;
  state.actionToken = null;
  $('actionEditor').classList.add('hidden');
  $('actionRunner').classList.add('hidden');
  $('actionEmpty').classList.remove('hidden');
  renderActionTemplateCards();
}

function createActionFromTemplate(i) {
  const t = ACTION_TEMPLATES[i];
  if (!t) return;
  state.currentAction = null;
  state.actionToken = null;
  $('actionEditorTitle').textContent = '从模板创建业务动作';
  $('actionName').value = t.name || '';
  $('actionCategory').value = t.category || '';
  $('actionDescription').value = t.description || '';
  $('actionTarget').value = t.target_connection || '';
  $('actionInvokeKind').value = t.invoke?.kind || 'mcp_tool';
  $('actionTool').value = t.invoke?.tool || '';
  $('actionUrl').value = t.invoke?.url || '';
  $('actionMethod').value = t.invoke?.method || 'POST';
  renderActionInputMapList(t.input_map || {});
  $('actionIdempotency').value = t.idempotency_key || '';
  $('actionConfirmation').value = t.confirmation || 'human_required';
  $('actionDryRun').checked = t.dry_run_supported !== false;
  onActionKindChange();
  $('actionEditor').classList.remove('hidden');
  $('actionRunner').classList.add('hidden');
  $('actionEmpty').classList.add('hidden');
  resetActionRunner();
}

function renderActionList() {
  const el = $('actionList');
  const filter = $('actionCategoryFilter')?.value || '';
  const items = filter ? state.actions.filter(a => a.category === filter) : state.actions;
  if (!items.length) {
    el.innerHTML = '<div class="text-sm text-slate-500">' + (filter ? '该分类下暂无动作' : '暂无业务动作，可从右侧模板快速创建') + '</div>';
    return;
  }
  el.innerHTML = items.map(a => {
    const active = state.currentAction && state.currentAction.id === a.id;
    return '<div class="object-card cursor-pointer ' + (active ? 'border-blue-500/60' : '') + '" onclick="selectAction(\'' + a.id + '\')">' +
      '<div class="flex justify-between items-start">' +
        '<div class="flex gap-3">' +
          '<div class="w-9 h-9 rounded-lg bg-slate-800 border border-slate-700 flex items-center justify-center text-lg">' + _actionIcon(a.category) + '</div>' +
          '<div>' +
            '<div class="font-medium">' + esc(a.name || a.action_id) + '</div>' +
            '<div class="text-xs text-slate-400 mt-1">' + _actionCategoryBadge(a.category) + ' · ' + esc(_actionKindLabel(a.invoke?.kind)) + ' · ' + esc(a.target_connection || '无连接') + '</div>' +
            (a.description ? '<div class="text-xs text-slate-500 mt-1">' + esc(a.description) + '</div>' : '') +
          '</div>' +
        '</div>' +
        '<div class="flex gap-2"><button class="text-xs text-blue-400" onclick="event.stopPropagation();editAction(\'' + a.id + '\')">编辑</button>' +
        '<button class="text-xs text-red-400" onclick="event.stopPropagation();deleteAction(\'' + a.id + '\')">删除</button></div>' +
      '</div></div>';
  }).join('');
}

function onActionKindChange() {
  const kind = $('actionInvokeKind').value;
  $('actionToolWrap').classList.toggle('hidden', kind !== 'mcp_tool');
  $('actionUrlWrap').classList.toggle('hidden', kind !== 'webhook' && kind !== 'rest');
  $('actionMethodWrap').classList.toggle('hidden', kind !== 'webhook' && kind !== 'rest');
}

function startCreateAction() {
  state.currentAction = null;
  state.actionToken = null;
  $('actionEditorTitle').textContent = '新建业务动作';
  $('actionName').value = '';
  $('actionCategory').value = '';
  $('actionDescription').value = '';
  $('actionTarget').value = '';
  $('actionInvokeKind').value = 'mcp_tool';
  $('actionTool').value = '';
  $('actionUrl').value = '';
  $('actionMethod').value = 'POST';
  renderActionInputMapList({});
  $('actionIdempotency').value = '';
  $('actionConfirmation').value = 'human_required';
  $('actionDryRun').checked = true;
  onActionKindChange();
  $('actionEditor').classList.remove('hidden');
  $('actionRunner').classList.add('hidden');
  $('actionEmpty').classList.add('hidden');
  resetActionRunner();
}

function closeActionEditor() {
  $('actionEditor').classList.add('hidden');
  if (!state.currentAction) $('actionEmpty').classList.remove('hidden');
  else selectAction(state.currentAction.id);
}

function resetActionRunner() {
  state.actionToken = null;
  $('actionPendingBox').classList.add('hidden');
  $('actionResultBox').classList.add('hidden');
  $('actionLogsBox').classList.add('hidden');
  $('actionPlanned').textContent = '';
  $('actionToken').textContent = '';
  if ($('actionResult')) $('actionResult').innerHTML = '';
}

function selectAction(id) {
  const a = state.actions.find(x => x.id === id);
  if (!a) return;
  state.currentAction = a;
  state.actionToken = null;
  renderActionList();
  $('actionEditor').classList.add('hidden');
  $('actionRunner').classList.remove('hidden');
  $('actionEmpty').classList.add('hidden');
  renderActionRunnerHeader(a);
  resetActionRunner();
  renderActionForm(a);
  loadActionLogs();
}

function renderActionRunnerHeader(a) {
  const el = $('actionRunnerHeader');
  el.innerHTML =
    '<div class="flex items-start justify-between">' +
      '<div class="flex gap-3">' +
        '<div class="w-10 h-10 rounded-lg bg-slate-800 border border-slate-700 flex items-center justify-center text-xl">' + _actionIcon(a.category) + '</div>' +
        '<div>' +
          '<div class="font-medium text-base">' + esc(a.name || a.action_id) + '</div>' +
          '<div class="text-xs text-slate-400 mt-1">' + _actionCategoryBadge(a.category) + ' · ' + esc(_actionKindLabel(a.invoke?.kind)) + ' · ' + esc(a.target_connection || '无连接') + '</div>' +
          (a.description ? '<div class="text-xs text-slate-500 mt-1">' + esc(a.description) + '</div>' : '') +
        '</div>' +
      '</div>' +
      '<button class="text-xs text-blue-400" onclick="editAction(\'' + a.id + '\')">编辑</button>' +
    '</div>';
}

function editAction(id) {
  const a = state.actions.find(x => x.id === id);
  if (!a) return;
  state.currentAction = a;
  $('actionEditorTitle').textContent = '编辑业务动作';
  $('actionName').value = a.name || '';
  $('actionCategory').value = a.category || '';
  $('actionDescription').value = a.description || '';
  $('actionTarget').value = a.target_connection || '';
  $('actionInvokeKind').value = a.invoke?.kind || 'mcp_tool';
  $('actionTool').value = a.invoke?.tool || '';
  $('actionUrl').value = a.invoke?.url || '';
  $('actionMethod').value = a.invoke?.method || 'POST';
  renderActionInputMapList(a.input_map || {});
  $('actionIdempotency').value = a.idempotency_key || '';
  $('actionConfirmation').value = a.confirmation || 'human_required';
  $('actionDryRun').checked = a.dry_run_supported !== false;
  onActionKindChange();
  $('actionEditor').classList.remove('hidden');
  $('actionRunner').classList.add('hidden');
  $('actionEmpty').classList.add('hidden');
}

function renderActionInputMapList(inputMap) {
  const container = $('actionInputMapList');
  container.innerHTML = '';
  const entries = Object.entries(inputMap || {});
  if (!entries.length) entries.push(['', '']);
  entries.forEach(([k, v], i) => {
    const row = document.createElement('div');
    row.className = 'flex gap-2 items-center';
    row.innerHTML =
      '<input class="flex-1" placeholder="外部字段名，如 customerName" value="' + esc(k) + '" data-idx="' + i + '" data-role="key" />' +
      '<input class="flex-1 font-mono text-xs" placeholder="$.对象.属性，如 $.Customer.name" value="' + esc(v) + '" data-idx="' + i + '" data-role="val" />' +
      '<button class="text-xs text-red-400" onclick="removeActionInputMapRow(this)">删除</button>';
    container.appendChild(row);
  });
}

function addActionInputMapRow() {
  const container = $('actionInputMapList');
  const i = container.children.length;
  const row = document.createElement('div');
  row.className = 'flex gap-2 items-center';
  row.innerHTML =
    '<input class="flex-1" placeholder="外部字段名" data-idx="' + i + '" data-role="key" />' +
    '<input class="flex-1 font-mono text-xs" placeholder="$.对象.属性" data-idx="' + i + '" data-role="val" />' +
    '<button class="text-xs text-red-400" onclick="removeActionInputMapRow(this)">删除</button>';
  container.appendChild(row);
}

function removeActionInputMapRow(btn) {
  const container = $('actionInputMapList');
  btn.parentElement.remove();
  Array.from(container.children).forEach((row, i) => {
    row.querySelectorAll('input').forEach(inp => inp.dataset.idx = String(i));
  });
}

function getActionInputMap() {
  const map = {};
  const container = $('actionInputMapList');
  Array.from(container.children).forEach(row => {
    const key = row.querySelector('[data-role="key"]').value.trim();
    const val = row.querySelector('[data-role="val"]').value.trim();
    if (key) map[key] = val;
  });
  return map;
}

async function saveAction() {
  const kind = $('actionInvokeKind').value;
  const invoke = { kind };
  if (kind === 'mcp_tool') invoke.tool = $('actionTool').value.trim();
  else { invoke.url = $('actionUrl').value.trim(); invoke.method = $('actionMethod').value; }
  const name = $('actionName').value.trim();
  if (!name) { toast('请填写动作名称', 'err'); return; }
  const payload = {
    name,
    category: $('actionCategory').value || undefined,
    description: $('actionDescription').value.trim() || undefined,
    target_connection: $('actionTarget').value.trim(),
    invoke,
    input_map: getActionInputMap(),
    idempotency_key: $('actionIdempotency').value.trim(),
    confirmation: $('actionConfirmation').value,
    dry_run_supported: $('actionDryRun').checked,
  };
  try {
    if (state.currentAction) {
      await api('/api/actions/' + state.currentAction.id, { method: 'PUT', body: JSON.stringify(payload) });
      toast('业务动作已更新', 'ok');
    } else {
      await api('/api/actions', { method: 'POST', body: JSON.stringify(payload) });
      toast('业务动作已创建', 'ok');
    }
    await loadActions();
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function deleteAction(id) {
  if (!confirm('确认删除该业务动作？')) return;
  try {
    await api('/api/actions/' + id, { method: 'DELETE' });
    if (state.currentAction && state.currentAction.id === id) {
      state.currentAction = null;
      $('actionEditor').classList.add('hidden');
      $('actionRunner').classList.add('hidden');
      $('actionEmpty').classList.remove('hidden');
    }
    await loadActions();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

function renderActionForm(a) {
  const container = $('actionForm');
  container.innerHTML = '';
  const groups = {};
  Object.entries(a.input_map || {}).forEach(([key, path]) => {
    const parts = path.replace(/^\$\./, '').split('.');
    const obj = parts.length > 1 ? parts[0] : '';
    const attr = parts.length > 1 ? parts.slice(1).join('.') : parts[0];
    if (!groups[obj]) groups[obj] = [];
    groups[obj].push({ key, obj, attr, path });
  });

  Object.keys(groups).sort().forEach(objName => {
    const fields = groups[objName];
    const wrap = document.createElement('div');
    wrap.className = 'p-3 rounded-lg border border-slate-700/50 bg-slate-800/30';
    const title = objName || '通用参数';
    let html = '<div class="text-xs font-medium text-slate-300 mb-2">' + esc(title) + '</div><div class="grid grid-cols-1 md:grid-cols-2 gap-3">';
    html += fields.map(f =>
      '<div><label class="text-xs text-slate-400">' + esc(f.key) + '</label>' +
      '<input class="w-full action-form-input" data-path="' + esc(f.path) + '" placeholder="' + esc(f.path) + '" /></div>'
    ).join('');
    html += '</div>';
    wrap.innerHTML = html;
    container.appendChild(wrap);
  });

  if (!Object.keys(groups).length) {
    container.innerHTML = '<div class="text-sm text-slate-500">该动作没有定义输入字段，可直接执行。</div>';
  }
}

function gatherActionFormInput() {
  const result = {};
  document.querySelectorAll('.action-form-input').forEach(inp => {
    const path = inp.dataset.path;
    const parts = path.replace(/^\$\./, '').split('.');
    let cur = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!cur[parts[i]]) cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = inp.value;
  });
  return result;
}

function renderActionResult(d) {
  const status = d.executed ? 'success' : (d.dry_run ? 'info' : 'warning');
  const statusText = { success: '执行成功', info: 'Dry Run 预览', warning: '未执行' }[status];
  let html = '<div class="p-3 rounded-lg border ' + (status === 'success' ? 'border-emerald-600/40 bg-emerald-900/20' : 'border-slate-700/50 bg-slate-800/30') + '">';
  html += '<div class="flex items-center gap-2 mb-2"><span class="badge ' + (status === 'success' ? 'ok' : 'text-slate-400') + '">' + statusText + '</span>';
  if (d.idempotency_key) html += '<span class="text-xs text-slate-500">幂等键: ' + esc(d.idempotency_key) + '</span>';
  html += '</div>';
  if (d.planned_payload) {
    html += '<div class="text-xs text-slate-400 mb-1">计划发送给外部系统的数据</div>';
    html += '<pre class="text-xs mb-3">' + esc(JSON.stringify(d.planned_payload, null, 2)) + '</pre>';
  }
  if (d.external_response) {
    html += '<div class="text-xs text-slate-400 mb-1">外部系统响应</div>';
    html += '<pre class="text-xs">' + esc(JSON.stringify(d.external_response, null, 2)) + '</pre>';
  }
  html += '</div>';
  return html;
}

async function runActionDry() {
  if (!state.currentAction) return;
  const input = gatherActionFormInput();
  try {
    const j = await api('/api/actions/' + state.currentAction.id + '/execute', {
      method: 'POST', body: JSON.stringify({ input, dry_run: true })
    });
    const d = j.data || {};
    $('actionPendingBox').classList.remove('hidden');
    $('actionResultBox').classList.remove('hidden');
    $('actionToken').textContent = '';
    $('actionPlanned').textContent = JSON.stringify(d.planned_payload || {}, null, 2);
    $('actionResult').innerHTML = renderActionResult(d);
  } catch (e) { toast('Dry Run 失败: ' + e.message, 'err'); }
}

async function runActionExecute() {
  if (!state.currentAction) return;
  const input = gatherActionFormInput();
  try {
    const j = await api('/api/actions/' + state.currentAction.id + '/execute', {
      method: 'POST', body: JSON.stringify({ input, dry_run: false })
    });
    const d = j.data || {};
    state.actionToken = d.confirmation_token || null;
    $('actionPendingBox').classList.remove('hidden');
    $('actionResultBox').classList.remove('hidden');
    if (state.actionToken) {
      $('actionToken').textContent = '令牌: ' + state.actionToken.slice(0, 16) + '…';
      $('actionPlanned').textContent = JSON.stringify(d.planned_payload || {}, null, 2);
      toast('执行计划已生成，请确认后回写', 'ok');
    } else {
      $('actionToken').textContent = '已直接返回（dry-run 或 confirmation=auto）';
      $('actionPlanned').textContent = JSON.stringify(d.planned_payload || {}, null, 2);
    }
    $('actionResult').innerHTML = renderActionResult(d);
  } catch (e) { toast('触发失败: ' + e.message, 'err'); }
}

async function runActionConfirm() {
  if (!state.currentAction || !state.actionToken) {
    toast('没有待确认的执行令牌', 'err'); return;
  }
  try {
    const j = await api('/api/actions/' + state.currentAction.id + '/confirm', {
      method: 'POST', body: JSON.stringify({ confirmation_token: state.actionToken })
    });
    const d = j.data || {};
    state.actionToken = null;
    $('actionPendingBox').classList.add('hidden');
    $('actionResultBox').classList.remove('hidden');
    $('actionResult').innerHTML = renderActionResult(d);
    toast(d.executed ? '回写成功' : '回写未完成', d.executed ? 'ok' : 'err');
    loadActionLogs();
  } catch (e) { toast('确认失败: ' + e.message, 'err'); }
}

async function loadActionLogs() {
  if (!state.currentAction) return;
  try {
    const j = await api('/api/actions/' + state.currentAction.id + '/logs?page=1&pageSize=20');
    const items = j.data.items || [];
    $('actionLogsBox').classList.toggle('hidden', items.length === 0);
    $('actionLogs').innerHTML = items.length ? items.map(l => {
      const ok = l.executed !== false;
      return '<div class="p-2 rounded border border-slate-700/50 text-xs">' +
        '<div class="flex justify-between"><span class="' + (ok ? 'ok' : 'text-red-400') + '">' + (ok ? '成功' : '失败') + '</span>' +
        '<span class="text-slate-500">' + esc(l.executed_at || l.created_at || '') + '</span></div>' +
        '<div class="text-slate-400 mt-1">' + esc(JSON.stringify(l.external_response || {}).slice(0, 240)) + '</div></div>';
    }).join('') : '<div class="text-xs text-slate-500">暂无日志</div>';
  } catch (e) { /* logs 接口不存在时静默 */ }
}

// ---- global error capture ----
window.addEventListener('error', e => {
  clientLog('error', 'window error', { message: e.message, filename: e.filename, lineno: e.lineno, colno: e.colno, stack: e.error?.stack });
});
window.addEventListener('unhandledrejection', e => {
  clientLog('error', 'unhandled rejection', { message: e.reason?.message || String(e.reason), stack: e.reason?.stack });
});

// ---- init ----
(async function init() {
  clientLog('info', 'app init');
  // 推理页业务影响推理：两行式下拉（主行语义 label，次行小字 id）
  impactInstanceLS = bindLabeledSelect({
    boxId: 'impactInstanceBox', triggerId: 'impactInstanceTrigger', panelId: 'impactInstancePanel',
    hiddenId: 'impactInstance', emptyText: '-- 选择实例 --',
    onChange: () => onImpactInstanceChange(),
  });
  impactAttributeLS = bindLabeledSelect({
    boxId: 'impactAttributeBox', triggerId: 'impactAttributeTrigger', panelId: 'impactAttributePanel',
    hiddenId: 'impactAttribute', emptyText: '-- 选择属性 --',
    onChange: () => loadImpactPreview(),
  });
  checkHealth();
  await loadProjects();
  if (!state.projects.length) {
    // 自动创建默认项目，保证向后兼容
    try { await createProject('默认项目'); } catch (e) { /* ignore */ }
    await loadProjects();
  }
  switchTab('home');
  setInterval(checkHealth, 10000);
})();
