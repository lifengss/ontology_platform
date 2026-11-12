const API_BASE = (location.hostname === 'localhost' && location.port === '8000') ? '' : 'http://localhost:8000';
const $ = id => document.getElementById(id);
let state = {
  ontologies: [], candidates: [], graphTab: 'schema',
  currentOntology: null, editingId: null, filter: 'all', editorMode: 'form', formObjects: [],
  projects: [], currentProject: null,
  actions: [], currentAction: null, actionToken: null,
  logs: [], logSource: 'server',
  selectedIngestObject: null,
  instances: [], selectedInstance: null,
  ingestTab: 'object', allInstanceQ: '', allInstanceObjectFilter: '', instanceEditMode: false,
  expandedTreeNodes: new Set(['__root__']),
  llmSettings: {}, healthDetails: null,
  graphRefit: null, // 当前图谱视图的"适应画布"回调（drawGraph/drawTree 绘制时挂载，(animate)=>…）
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

async function api(path, opts = {}) {
  const start = performance.now();
  const isForm = opts.body instanceof FormData;
  const headers = isForm ? {} : { 'Content-Type': 'application/json' };
  const sep = path.includes('?') ? '&' : '?';
  const projectId = opts.projectId ?? _currentProjectId();
  // 默认作用域 = 项目级（不注入 namespace）；如需项目内空间过滤由调用方显式传 namespace
  const namespace = opts.namespace ?? null;
  let url = API_BASE + path + sep + 'project_id=' + encodeURIComponent(projectId);
  if (namespace) url += '&namespace=' + encodeURIComponent(namespace);
  clientLog('info', `API ${opts.method || 'GET'} ${path}`, { project_id: projectId, namespace });
  try {
    // V2-05：httpOnly Cookie 会话（跨域须 credentials:include）；Bearer 由后端兼容 API 客户端
    const r = await fetch(url, { headers, credentials: 'include', ...opts });
    const j = await r.json().catch(() => ({}));
    const elapsed = Math.round(performance.now() - start);
    if (!r.ok) {
      clientLog('error', `API ${opts.method || 'GET'} ${path} failed`, { status: r.status, message: j.error?.message, elapsed_ms: elapsed });
      if (r.status === 401 && !opts.skipAuthRedirect) showLogin('登录状态已失效，请重新登录');
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

// ---- 所属部门（顶层隔离维度：项目归属部门，顶栏按部门筛选项目空间） ----
const DEPARTMENT_DEFAULT = '默认部门';

function departmentOf(p) {
  return (p && p.department && String(p.department).trim()) || DEPARTMENT_DEFAULT;
}

function _currentDepartment() {
  return localStorage.getItem('op_currentDepartment') || '';  // 空 = 全部部门
}

function filteredProjects() {
  const dept = _currentDepartment();
  const list = state.projects || [];
  return dept ? list.filter(p => departmentOf(p) === dept) : list;
}

function allDepartments() {
  const set = new Set([DEPARTMENT_DEFAULT]);
  (state.projects || []).forEach(p => set.add(departmentOf(p)));
  (homeProjects || []).forEach(p => set.add(departmentOf(p)));  // 含归档项目
  const arr = [...set];
  arr.sort((a, b) => (a === DEPARTMENT_DEFAULT ? -1 : b === DEPARTMENT_DEFAULT ? 1 : a.localeCompare(b, 'zh')));
  return arr;
}

function renderDepartmentSelector() {
  const sel = $('departmentSelect');
  if (!sel) return;
  const current = _currentDepartment();
  sel.innerHTML = '<option value="">全部部门</option>' +
    allDepartments().map(d => '<option value="' + esc(d) + '" ' + (d === current ? 'selected' : '') + '>' + esc(d) + '</option>').join('');
}

// 部门筛选生效：当前项目不属于所选部门时自动切到该部门第一个项目
async function applyDepartmentFilter() {
  const dept = _currentDepartment();
  const list = filteredProjects();
  if (dept && list.length) {
    const currentId = state.currentProject ? state.currentProject.id : null;
    if (!currentId || !list.some(p => p.id === currentId)) {
      await setProject(list[0].id);
      return;
    }
  }
  renderProjectSelector();
}

async function setDepartment(dept) {
  localStorage.setItem('op_currentDepartment', dept || '');
  renderDepartmentSelector();
  await applyDepartmentFilter();
  renderContextLabel();
  // 首页可见时同步刷新总览（项目表/统计按部门过滤）
  const homeEl = $('home');
  if (homeEl && !homeEl.classList.contains('hidden')) await loadHome();
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
    renderDepartmentSelector();
    await applyDepartmentFilter();
  } catch (e) { toast('加载项目失败: ' + e.message, 'err'); }
}

function renderProjectSelector() {
  const sel = $('projectSelect');
  if (!sel) return;
  const current = _currentProjectId();
  const items = filteredProjects();
  sel.innerHTML = '<option value="">选择项目</option>' +
    items.map(p => '<option value="' + p.id + '" ' + (p.id === current ? 'selected' : '') + '>' + esc(p.name) + '</option>').join('');
  renderContextLabel();
}

function renderContextLabel() {
  const label = $('currentProjectLabel');
  if (!label) return;
  const current = _currentProjectId();
  const p = state.projects.find(x => x.id === current);
  const projectName = p ? p.name : (current === 'default' ? '默认项目' : current);
  const dept = _currentDepartment();
  label.textContent = (dept ? dept + ' / ' : '') + projectName;
  label.title = (dept ? '部门: ' + dept + '\n' : '') + '项目: ' + projectName;
}

async function setProject(id) {
  if (!id) return;
  const p = state.projects.find(x => x.id === id);
  if (!p) return;
  state.currentProject = p;
  localStorage.setItem('op_currentProject', id);
  renderProjectSelector();
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
  await loadOntologies();
  await loadActions();
  await refreshActiveTabAfterScopeChange();
  toast('已切换到项目：' + p.name, 'ok');
}

// 切换项目/部门后，按当前活动页签同步刷新数据，
// 避免顶栏作用域已变、页面内容仍是旧项目数据（如实例列表/图谱/统计）
async function refreshActiveTabAfterScopeChange() {
  const tab = _lastTab || 'home';
  try {
    if (tab === 'ingest') {
      renderIngestTarget();
      await Promise.all([loadInstances(), loadCandidates()]);
    } else if (tab === 'graph') {
      await loadGraph();
    } else if (tab === 'inference') {
      await loadInference();
    } else if (tab === 'home') {
      await loadHome();
    } else if (tab === 'orchestration') {
      await loadOrchestration();
    }
    // modeling 已由 setProject 内的 loadOntologies() 刷新
  } catch (e) {
    // 各 loader 自身已有 toast 报错；这里兜底防止切换流程中断
  }
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
    const dept = _currentDepartment();
    const qs = dept ? '&department=' + encodeURIComponent(dept) : '';
    const j = await api('/api/projects?with_stats=true&pageSize=100' + qs, { projectId: null, namespace: null });
    homeProjects = j.data.items || [];
    renderHomeStats();
    renderProjects();
  } catch (e) { toast('加载项目列表失败: ' + e.message, 'err'); }
}

async function saveProjectDepartment(id, value) {
  const dept = (value || '').trim() || DEPARTMENT_DEFAULT;
  try {
    await api('/api/projects/' + id, { method: 'PUT', body: JSON.stringify({ department: dept }), projectId: null, namespace: null });
    toast('项目部门已更新为「' + dept + '」', 'ok');
    await loadProjects();   // 内部按当前部门筛选，必要时自动切换项目
    await loadHome();
  } catch (e) {
    toast('更新部门失败: ' + e.message, 'err');
    await loadHome();
  }
}

function renderHomeStats() {
  const active = homeProjects.filter(p => (p.status || 'active') !== 'archived');
  const archived = homeProjects.length - active.length;
  $('homeStatProjects').textContent = homeProjects.length;
  $('homeStatProjectsSub').textContent = active.length + ' 进行中 · ' + archived + ' 已归档';
  // 项目级统计（已按部门筛选）：汇总所选部门范围内各项目
  $('homeStatOntologies').textContent = homeProjects.reduce((s, p) => s + ((p.stats && p.stats.ontology_count) || 0), 0);
  $('homeStatInstances').textContent = homeProjects.reduce((s, p) => s + ((p.stats && p.stats.instance_count) || 0), 0);
  $('homeStatMcp').textContent = homeProjects.filter(p => p.mcp_published).length;
  const ontoSub = $('homeStatOntologiesSub');
  const instSub = $('homeStatInstancesSub');
  if (ontoSub) ontoSub.textContent = '项目内本体模型数量';
  if (instSub) instSub.textContent = '归化入库后的实例';
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
  if (!items.length) { tb.innerHTML = '<tr><td colspan="10" class="p-3 text-center text-slate-500">暂无项目</td></tr>'; return; }
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
      '<td class="p-2"><input class="bg-transparent border border-slate-700/60 rounded px-1 py-0.5 text-xs text-slate-300 w-24 focus:border-blue-500/60 focus:outline-none" value="' + esc(departmentOf(p)) + '" title="所属部门（点击修改）" onchange="saveProjectDepartment(\'' + p.id + '\', this.value)" /></td>' +
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
  const dept = ($('newProjectDepartment') ? $('newProjectDepartment').value : '').trim();
  try {
    const j = await api('/api/projects', { method: 'POST', body: JSON.stringify({ name, description: desc, department: dept || undefined }), projectId: null, namespace: null });
    $('newProjectName').value = '';
    $('newProjectDesc').value = '';
    if ($('newProjectDepartment')) $('newProjectDepartment').value = '';
    toggleProjectForm(false);
    toast('项目「' + name + '」已创建', 'ok');
    // 若新项目部门与当前筛选不一致，同步筛选器，避免新项目在下拉中“消失”
    if (departmentOf(j.data) !== DEPARTMENT_DEFAULT && _currentDepartment() !== departmentOf(j.data)) {
      localStorage.setItem('op_currentDepartment', departmentOf(j.data));
      renderDepartmentSelector();
    }
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

function connHealthHtml(c) {
  // V2-11：schema 检查健康摘要（上次检查/探测失败/超阈值/停更/待处理漂移数）
  const sh = c.schema_health || {};
  let html;
  if (!sh.last_schema_check_at) {
    html = '<span class="text-slate-500">未检查</span>';
  } else if (sh.last_schema_check_ok === false) {
    html = '<span class="text-rose-400">探测失败</span><div class="text-slate-500 max-w-[160px] break-all" title="' + esc(sh.last_schema_check_error || '') + '">' + esc((sh.last_schema_check_error || '').slice(0, 60)) + '</div>';
  } else if (sh.stale) {
    html = '<span class="text-amber-300">超阈值未检查</span><div class="text-slate-500">' + esc(String(sh.last_schema_check_at).slice(0, 19)) + '</div>';
  } else {
    html = '<span class="text-emerald-400">正常</span><div class="text-slate-500">' + esc(String(sh.last_schema_check_at).slice(0, 19)) + '</div>';
  }
  if (sh.snapshot_auto_update === 'stopped') {
    html += '<div class="text-rose-400 font-medium">快照自动更新已停止</div>';
  } else if (sh.open_drifts > 0) {
    html += '<div class="text-amber-300">漂移 ' + sh.open_drifts + ' 条待处理</div>';
  }
  return html;
}

const CDC_STATUS_LABELS = {
  running: ['text-emerald-400', '运行中'],
  not_supported: ['text-slate-400', 'CDC 未支持'],
  disabled: ['text-slate-500', '未启用'],
  stopped_drift: ['text-rose-400', '漂移停更'],
  circuit_open: ['text-rose-400', '熔断中'],
};
function connCdcHtml(c) {
  // V2-12：CDC 状态摘要（运行中/未支持/未启用/漂移停更/熔断中 + 通道 + 最近事件）
  const st = (c.schema_health || {}).cdc_status || (c.cdc_status) || 'disabled';
  const cdc = (c.schema_health || {}).cdc || {};
  const [cls, label] = CDC_STATUS_LABELS[st] || ['text-slate-500', st];
  let html = '<span class="' + cls + '">' + label + '</span>';
  if (cdc.channel) html += ' · ' + esc(cdc.channel);
  if (st === 'not_supported') html += '<div class="text-slate-500">降级：周期拉取＋联邦读</div>';
  if (st === 'circuit_open') html += '<div class="text-rose-400">冷却后自动半开探测</div>';
  if (cdc.last_event_at) html += '<div class="text-slate-500">事件 ' + esc(String(cdc.last_event_at).slice(5, 16)) + ' · lsn ' + esc(String(cdc.last_event_lsn ?? 0)) + '</div>';
  return html;
}

function renderConnections() {
  const tb = $('connTableBody');
  if (!tb) return;
  const banner = $('connDriftBanner');
  if (banner) {
    // V2-11：键属性漂移停更 → 顶部告警横幅（含明细连接名）
    const stopped = connList.filter(c => (c.schema_health || {}).snapshot_auto_update === 'stopped' || c.snapshot_auto_update === 'stopped');
    if (stopped.length) {
      banner.innerHTML = '<b>Schema 漂移告警（键属性）</b>：' + stopped.map(c => esc(c.display_name)).join('、') +
        ' 的快照自动更新已停止（读取→候选/CDC 被拒）。请修正源端或映射后，点该连接「漂移」→「人工确认恢复」。';
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
      banner.innerHTML = '';
    }
  }
  if (!connList.length) {
    tb.innerHTML = '<tr><td colspan="9" class="p-3 text-center text-slate-500">暂无连接。点右上「＋ 新建连接」配置业务系统 MCP Server。</td></tr>';
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
      '<td class="p-1.5 text-xs">' + connHealthHtml(c) + '</td>' +
      '<td class="p-1.5 text-xs">' + connCdcHtml(c) + '</td>' +
      '<td class="p-1.5 font-mono text-xs text-slate-400">' + esc(allow || '（全部）') + '</td>' +
      '<td class="p-1.5">' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="testConnection(\'' + c.connection_id + '\')">测试</button> ' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="readConnection(\'' + c.connection_id + '\')">读取→候选</button> ' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="schemaCheckConn(\'' + c.connection_id + '\')">检查schema</button> ' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="openConnHealth(\'' + c.connection_id + '\')">健康</button> ' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="openDriftPanel(\'' + c.connection_id + '\')">漂移</button> ' +
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
      '<div class="col-span-12 border-t border-slate-700/50 pt-2 grid grid-cols-12 gap-2">' +
      '<div class="col-span-4 flex items-end gap-2"><label class="flex items-center gap-1.5 text-xs text-slate-400"><input id="connCdcEnabled" type="checkbox"' + ((c && c.cdc && c.cdc.enabled) ? ' checked' : '') + '> 启用 CDC（流式变更 → 关系快照）</label></div>' +
      '<div class="col-span-4"><label class="text-xs text-slate-400">变更通道</label><select id="connCdcChannel" class="w-full">' +
        ['auto', 'binlog', 'mq', 'cursor', 'none'].map(ch => '<option value="' + ch + '"' + (((c && c.cdc && c.cdc.channel) || 'auto') === ch ? ' selected' : '') + '>' + ch + (ch === 'auto' ? '（自动探测源端支持）' : ch === 'none' ? '（无变更通道 → 周期拉取＋联邦读）' : '') + '</option>').join('') +
      '</select></div>' +
      '<div class="col-span-4 self-end text-xs text-slate-500 pb-1.5">CDC 为数据面独立通道（不经 MCP 协议）；只更新关系快照（键/关系边），不同步内容值。</div>' +
      '</div>' +
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
  let savedId = connEditId;
  try {
    if (connEditId) {
      await api('/api/mcp/connections/' + connEditId, { method: 'PUT', body: JSON.stringify(body) });
    } else {
      const cj = await api('/api/mcp/connections', { method: 'POST', body: JSON.stringify(body) });
      savedId = (cj.data && (cj.data.connection_id || cj.data.id)) || null;
    }
    // V2-12（M3-D）：CDC 通道配置（独立端点，配置后 supported 重新探测）
    if (savedId) {
      const cdcOn = $('connCdcEnabled') ? $('connCdcEnabled').checked : false;
      const cdcCh = $('connCdcChannel') ? $('connCdcChannel').value : 'auto';
      await api('/api/cdc/channels/' + savedId + '/config',
        { method: 'POST', body: JSON.stringify({ enabled: cdcOn, channel: cdcCh }) });
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
    closeDriftPanel();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

// ---- V2-11（M3-A）：schema 漂移检查 + 漂移面板 ----
async function schemaCheckConn(id) {
  toast('正在检查 schema（只拉字段结构，不拉数据值）…', 'info');
  try {
    const j = await api('/api/mcp/connections/' + id + '/schema-check', { method: 'POST', body: '{}' });
    const d = j.data || {};
    if (d.status === 'probe_failed') toast('schema 探测失败（不判定漂移，源不可达/鉴权失败）：' + (d.error || ''), 'err', 8000);
    else if (d.severity === 'key') toast('检测到键属性漂移（' + (d.items || []).length + ' 项）：该连接快照自动更新已停止，请修正后在「漂移」面板人工确认恢复', 'err', 10000);
    else if (d.severity === 'attribute') toast('检测到非键属性漂移（' + (d.items || []).length + ' 项）：联邦读将显式标记该字段，快照更新可继续', 'info', 8000);
    else if (d.severity === 'unmapped') toast('发现未映射新字段 ' + (d.items || []).length + ' 个（仅提示补映射，不阻断）', 'info', 8000);
    else toast('schema 检查通过，无漂移', 'ok');
    await loadConnections();
    if (driftConnId === id) await openDriftPanel(id);
  } catch (e) { toast('schema 检查失败: ' + e.message, 'err', 6000); }
}

let driftConnId = null;
const DRIFT_KIND_LABELS = {
  key_missing: '键字段缺失', key_type_changed: '键字段类型变化',
  attr_missing: '属性字段缺失', attr_type_changed: '属性字段类型变化',
  new_unmapped: '未映射新字段',
};

async function openDriftPanel(id) {
  driftConnId = id;
  const box = $('connDriftPanel');
  if (!box) return;
  box.classList.remove('hidden');
  box.innerHTML = '<div class="text-xs text-slate-500">加载漂移记录…</div>';
  try {
    const j = await api('/api/mcp/connections/' + id + '/drifts?status=all');
    renderDriftPanel(j.data || {});
  } catch (e) {
    box.innerHTML = '<div class="text-xs text-rose-400">加载失败: ' + esc(e.message) + '</div>';
  }
}

function renderDriftPanel(d) {
  const box = $('connDriftPanel');
  if (!box) return;
  const recs = d.items || [];
  const connName = (connList.find(c => c.connection_id === driftConnId) || {}).display_name || driftConnId;
  if (!recs.length) {
    box.innerHTML = '<div class="text-xs text-slate-400 mb-2">连接「' + esc(connName) + '」暂无 schema 漂移记录。' +
      '可点「检查schema」手动触发一次探测（与周期探测同一管道）。</div>' +
      '<button class="btn secondary" type="button" style="padding:2px 10px;font-size:12px" onclick="closeDriftPanel()">关闭</button>';
    return;
  }
  box.innerHTML = '<div class="text-xs text-slate-400 mb-2">连接「' + esc(connName) + '」的 schema 漂移记录（恢复仅允许人工确认，不允许自动恢复）</div>' +
    recs.map(r => {
      const rows = (r.items || []).map(i =>
        '<tr class="border-b border-slate-700/40">' +
        '<td class="p-1 font-mono">' + esc(i.field || '-') + '</td>' +
        '<td class="p-1 font-mono">' + esc(i.property || '-') + '</td>' +
        '<td class="p-1">' + esc(DRIFT_KIND_LABELS[i.kind] || i.kind) +
          (i.severity === 'key' ? ' <span class="text-rose-400">（键）</span>' : '') + '</td>' +
        '<td class="p-1 text-slate-400">' + esc(i.expected || '-') + ' → ' + esc(i.actual || '-') + '</td></tr>').join('');
      const head = r.status === 'open'
        ? '<span class="text-rose-400 font-medium">待处理</span>' +
          '<button class="btn" type="button" style="padding:2px 10px;font-size:12px;margin-left:auto" onclick="resolveDrift(\'' + esc(r.connection_id) + '\',\'' + esc(r.id) + '\')">人工确认恢复</button>'
        : '<span class="text-emerald-400">已确认</span><span class="text-slate-500" style="margin-left:auto">确认人 ' + esc(r.resolved_by || '-') + ' · ' + esc((r.resolved_at || '').slice(0, 19)) + '</span>';
      return '<div class="mb-2 p-2 rounded border border-slate-700/60">' +
        '<div class="flex items-center gap-2 mb-1 text-xs">' +
        '<span class="text-slate-500">检测于 ' + esc((r.detected_at || '').slice(0, 19)) + '（通道 ' + esc(r.last_detected_source || r.source || '-') + '）</span>' +
        head + '</div>' +
        '<table class="w-full text-xs border-collapse"><thead><tr class="text-slate-500">' +
        '<th class="border-b border-slate-700 p-1 text-left">源字段</th><th class="border-b border-slate-700 p-1 text-left">本体属性</th>' +
        '<th class="border-b border-slate-700 p-1 text-left">漂移类型</th><th class="border-b border-slate-700 p-1 text-left">期望 → 实际</th></tr></thead><tbody>' +
        rows + '</tbody></table></div>';
    }).join('') +
    '<button class="btn secondary mt-1" type="button" style="padding:2px 10px;font-size:12px" onclick="closeDriftPanel()">关闭</button>';
}

async function resolveDrift(connId, driftId) {
  if (!confirm('人工确认该漂移已处理（源端已恢复/映射已修正）？\n确认后清除漂移标记；若该连接已无其他待处理漂移，快照自动更新将恢复。')) return;
  try {
    const j = await api('/api/mcp/connections/' + connId + '/drifts/' + driftId + '/resolve', { method: 'POST', body: '{}' });
    const d = j.data || {};
    toast(d.resumed_snapshot_auto_update
      ? '漂移已人工确认，快照自动更新已恢复'
      : '漂移已人工确认（该连接仍有其他待处理漂移，快照更新保持停止）', 'ok', 6000);
    await loadConnections();
    await openDriftPanel(connId);
  } catch (e) { toast('确认恢复失败: ' + e.message, 'err', 6000); }
}

function closeDriftPanel() {
  driftConnId = null;
  const box = $('connDriftPanel');
  if (!box) return;
  box.classList.add('hidden');
  box.innerHTML = '';
}

// ---- V2-12（M3-D）：连接健康视图（schema 检查 + CDC + 三类调用统计） ----
let connHealthId = null;
async function openConnHealth(id) {
  connHealthId = id;
  const box = $('connHealthPanel');
  box.classList.remove('hidden');
  box.innerHTML = '<div class="text-xs text-slate-500">加载连接健康视图…</div>';
  try {
    const j = await api('/api/mcp/connections/' + id + '/health');
    renderConnHealth(j.data || {});
  } catch (e) {
    box.innerHTML = '<div class="text-xs text-rose-400">加载失败: ' + esc(e.message) + '</div>';
  }
}

function _statLine(label, s) {
  if (!s || (!s.calls && s.calls !== 0)) return label + '：-';
  const rate = s.success_rate == null ? '-' : (Math.round(s.success_rate * 100) + '%');
  const ms = s.last_ms == null ? '-' : s.last_ms + 'ms';
  const at = s.last_at ? String(s.last_at).slice(5, 16) : '-';
  return label + '：' + rate + '（最近 ' + s.window_calls + ' 次）· 时延 ' + ms + ' · 最近 ' + at;
}

function renderConnHealth(d) {
  const box = $('connHealthPanel');
  if (!box || connHealthId === null) return;
  const cdc = d.cdc || {};
  const cdcSt = d.cdc_status || 'disabled';
  const [cls, label] = CDC_STATUS_LABELS[cdcSt] || ['text-slate-500', cdcSt];
  const circuit = cdc.circuit || {};
  const cdcBlock =
    '<div class="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">' +
    '<div>CDC 状态：<span class="' + cls + ' font-medium">' + label + '</span>' +
      (cdc.channel ? ' · 通道 <b>' + esc(cdc.channel) + '</b>' : '') + '</div>' +
    '<div>最近成功消费：' + (cdc.last_success_at ? esc(String(cdc.last_success_at).slice(0, 19)) : '无') +
      (cdc.last_success_at ? '（' + _humanAgeLocal(cdc.last_success_at) + '）' : '') + '</div>' +
    '<div>最近事件：' + (cdc.last_event_at ? esc(String(cdc.last_event_at).slice(5, 16)) + ' · lsn ' + esc(String(cdc.last_event_lsn ?? 0)) : '无') + '</div>' +
    '<div>游标 last_lsn：' + esc(String(cdc.last_lsn ?? 0)) + ' · 累计应用 ' + esc(String(cdc.applied_total ?? 0)) +
      ' · 去重跳过 ' + esc(String((cdc.skipped_dup_total ?? 0) + (cdc.skipped_stale_total ?? 0))) +
      ' · 转人工 ' + esc(String(cdc.review_total ?? 0)) + '</div>' +
    '<div>熔断：<span class="' + (circuit.state === 'open' ? 'text-rose-400' : circuit.state === 'half_open' ? 'text-amber-300' : 'text-emerald-400') + '">' + esc(circuit.state || 'closed') + '</span>' +
      ' · 连续失败 ' + esc(String(circuit.fail_count ?? 0)) + (circuit.cooldown_seconds ? ' · 冷却 ' + circuit.cooldown_seconds + 's' : '') + '</div>' +
    (cdc.last_fallback_pull_at ? '<div>降级周期拉取：' + esc(String(cdc.last_fallback_pull_at).slice(5, 16)) + '（CDC 未支持 → 周期拉取＋联邦读）</div>' : '') +
    '</div>';
  const alerts = (cdc.alerts || []).slice(-5).reverse();
  const alertHtml = alerts.length
    ? '<div class="text-xs text-slate-400 mt-1">' + alerts.map(a =>
        '<span class="text-amber-300">' + esc(a.type) + '</span> ' + esc(a.detail) + ' <span class="text-slate-500">' + esc(String(a.ts).slice(5, 16)) + '</span>').join('<br>') + '</div>'
    : '<div class="text-xs text-slate-500 mt-1">无最近告警</div>';
  const statsBlock = '<div class="text-xs space-y-0.5 mt-1">' +
    '<div>' + _statLine('手工拉取/读取', (d.stats || {}).read) + '</div>' +
    '<div>' + _statLine('CDC 消费', (d.stats || {}).cdc) + '</div>' +
    '<div>' + _statLine('联邦查询', (d.stats || {}).federation) + '</div>' +
    '</div>';
  box.innerHTML =
    '<div class="flex justify-between items-center mb-2">' +
    '<b class="text-sm">连接健康视图</b>' +
    '<div>' +
    '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="schemaCheckConn(\'' + esc(connHealthId) + '\'); openConnHealth(\'' + esc(connHealthId) + '\')">手动 schema 检查</button> ' +
    '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="api(\'/api/cdc/channels/' + esc(connHealthId) + '/poll\', {method:\'POST\', body:\'{}\'}).then(j => { toast(\'poll: \' + (j.data.status) + \' applied=\' + (j.data.applied ?? 0), \'info\', 5000); openConnHealth(\'' + esc(connHealthId) + '\'); }).catch(e => toast(\'poll 失败: \' + e.message, \'err\'))">手动 poll</button> ' +
    '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="closeConnHealth()">关闭</button></div></div>' +
    '<div class="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">' +
    '<div>最近 schema 检查：' + (d.last_schema_check_at ? esc(String(d.last_schema_check_at).slice(0, 19)) : '未检查') +
      (d.last_schema_check_at ? '（距上次 ' + esc(d.schema_check_age_human || '-') + '）' : '') +
      (d.last_schema_check_ok === false ? ' · <span class="text-rose-400">探测失败</span>' : '') +
      (d.stale ? ' · <span class="text-amber-300">超阈值未检查</span>' : '') + '</div>' +
    '<div>快照自动更新：' + (d.snapshot_auto_update === 'stopped' ? '<span class="text-rose-400 font-medium">已停止（键漂移）</span>' : 'active') +
      ' · 待处理漂移 ' + esc(String(d.open_drifts ?? 0)) + ' 条</div>' +
    '</div>' +
    '<div class="mt-2 p-2 rounded border border-slate-700/60 bg-slate-950/30">' + cdcBlock + alertHtml + '</div>' +
    '<div class="mt-2 p-2 rounded border border-slate-700/60 bg-slate-950/30">' + statsBlock + '</div>';
}

function _humanAgeLocal(isoStr) {
  const t = Date.parse(isoStr);
  if (isNaN(t)) return '';
  let s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  const h = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); s %= 60;
  return h + '小时' + m + '分' + s + '秒';
}

function closeConnHealth() {
  connHealthId = null;
  const box = $('connHealthPanel');
  if (!box) return;
  box.classList.add('hidden');
  box.innerHTML = '';
}

let _lastTab = '';

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(s => s.classList.add('hidden'));
  $(name).classList.remove('hidden');
  clientLog('info', 'switch tab', { tab: name });
  if (name === 'home') loadHome();
  if (name === 'credentials') loadCredentials();
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
  if (name === 'orchestration') { loadOrchestration(); }
  if (name === 'dictionary') { loadDictionary(); }
  if (name === 'capability') { loadCapabilities(); }
  if (name === 'quality') {
    loadOntologies().then(() => {
      renderQualityOntologySelect();
      loadQuality();
      loadQualityTrend();
    });
  }
  if (name === 'mcp') { loadMcpOntologies(1); loadMcpTools(); loadMcpToken(); }
  if (name === 'logs') { loadServerLogs(); }
  if (name === 'settings') { loadLLMSettings(); }
  if (name === 'users') { loadAuthUsers(); loadAuthAudit(); }
  if (name === 'approvals') loadChangesets();
  if (name === 'audit') loadAuditLog();
  if (name === 'tutorial') loadTutorial();
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
      '<div class="flex justify-between items-center"><span class="font-medium text-sm">' + esc(o.name) + '</span><span class="flex gap-1">' + (o.generation_mode === 'fallback' ? '<span class="badge draft" title="LLM 不可用，结果为确定性关键词抽取兜底，非 AI 产出">模板兜底</span>' : '') + '<span class="badge ' + status + '">' + (status === 'draft' ? '草稿' : '已发布') + '</span></span></div>' +
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
  const genMode = o.generation_mode === 'fallback' ? ' · 模板兜底(非AI)' : '';
  $('detailMeta').textContent = o.id + ' · ' + (o.modeling_type || 'manual') + genMode + ' · ' + (o.objects?.length || 0) + ' 对象 · v' + o.version;
  $('detailBody').textContent = JSON.stringify(o, null, 2);
  $('validateResult').classList.add('hidden');
  $('btnPublish').classList.toggle('hidden', status !== 'draft');
  renderOntologySharing(o); // V2-06：共享等级（M2-C）
}

// ---- V2-06 / M2-C：本体共享等级（schema 只读共享，实例永不共享）----
const SHARING_LABELS = { private: '私有（仅本人）', project: '项目组共享', department: '部门共享', public: '公开' };

function _meUser() {
  return state.session?.user || null;
}

function _parseVer(v) {
  const m = String(v || '1.0').split('.');
  return { major: parseInt(m[0] || '1', 10) || 1, minor: parseInt(m[1] || '0', 10) || 0 };
}

function _nextMajorVersion(o) {
  const p = _parseVer(o?.published_version || o?.version);
  return (p.major + 1) + '.0';
}

function _isOntologyOwner(o) {
  const me = _meUser();
  if (!me) return true; // dev/未启用鉴权：不限制
  return me.role === 'admin' || (o.owner || '') === me.username || !o.owner;
}

function renderOntologySharing(o) {
  const el = $('detailSharing');
  if (!el) return;
  const sharing = o.sharing || 'department';
  const me = _meUser();
  const canManage = !me || me.role === 'admin' || (o.owner || '') === me.username || !o.owner;
  const members = (o.shared_with || []).join(', ');
  let html = '<div class="flex items-center gap-2 flex-wrap text-xs">'
    + '<span class="text-slate-400">共享等级：</span>'
    + '<span class="badge">' + esc(SHARING_LABELS[sharing] || sharing) + '</span>';
  if (sharing === 'project' && members) html += '<span class="text-slate-500">成员：' + esc(members) + '</span>';
  if (o.owner) html += '<span class="text-slate-500">建模人：' + esc(o.owner) + '</span>';
  if (o.status === 'active') {
    html += '<span class="text-slate-500">已发布版本：' + esc(o.published_version || o.version || '-') + '</span>';
  }
  html += '<span class="text-slate-600" title="共享仅授 schema 只读；实例永不共享（INV-2）；上传型实例内容默认仅上传者可读">仅 schema 只读</span>';
  if (canManage) {
    html += '<button class="btn secondary text-xs" onclick="showSharingEditor()">调整</button>';
  }
  if (canManage && o.status === 'active') {
    // V2-07：进入表单编辑；已发布本体直编被后端 409 拦截，修改经表单内「提交变更」走变更提案
    html += '<button class="btn secondary text-xs" onclick="editCurrentOntology()">提交变更（V2-07）</button>';
  }
  html += '</div>';
  html += '<div id="sharingEditor" class="hidden mt-2 p-2 rounded border border-slate-700 bg-slate-900/60 space-y-2">'
    + '<div class="flex items-center gap-2 text-xs"><span class="text-slate-400">等级</span>'
    + '<select id="sharingLevel" class="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs" onchange="onSharingLevelChange()">'
    + Object.keys(SHARING_LABELS).map(k => '<option value="' + k + '">' + SHARING_LABELS[k] + '</option>').join('')
    + '</select></div>'
    + '<div id="sharingMembersRow" class="flex items-center gap-2 text-xs hidden"><span class="text-slate-400">成员</span>'
    + '<input id="sharingMembers" class="flex-1 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs" placeholder="用户名，逗号分隔（可跨部门）" /></div>'
    + '<div class="flex gap-2"><button class="btn text-xs" onclick="saveOntologySharing()">保存</button>'
    + '<button class="btn secondary text-xs" onclick="hideSharingEditor()">取消</button></div>'
    + '<p class="text-[11px] text-slate-500">私有=仅本人可见；项目组=指定成员（可跨部门）；部门=本部门全员；公开=全部用户。变更即时生效并留审计。</p>'
    + '</div>';
  html += '<div id="changesetEditor" class="hidden mt-2 p-2 rounded border border-slate-700 bg-slate-900/60 space-y-2">'
    + '<p class="text-[11px] text-slate-400">将当前加载的模型提交为变更提案（V2-07）：兼容变更（新增可选属性/放宽取值范围/新增对象等）自动直接升小版本发布；不兼容变更（删属性/改类型/主键变化/收窄范围/删对象等）须填写目标大版本并进入审批，通过前对外仍是旧版本。</p>'
    + '<div class="flex items-center gap-2 text-xs"><span class="text-slate-400">目标版本</span>'
    + '<input id="csTargetVersion" class="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs" placeholder="不兼容变更必填，如 ' + esc(_nextMajorVersion(o)) + '" />'
    + '<span class="text-slate-500">当前版本 ' + esc(o.version || '-') + '</span></div>'
    + '<div class="flex items-center gap-2 text-xs"><span class="text-slate-400">备注</span>'
    + '<input id="csNote" class="flex-1 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs" placeholder="变更说明（可选）" /></div>'
    + '<div class="flex gap-2"><button class="btn text-xs" onclick="submitChangeset()">提交</button>'
    + '<button class="btn secondary text-xs" onclick="showChangesetEditor()">取消</button></div>'
    + '</div>';
  el.innerHTML = html;
  $('sharingLevel').value = sharing;
  if (members) $('sharingMembers').value = members;
  onSharingLevelChange();
}

function showSharingEditor() { $('sharingEditor').classList.remove('hidden'); }
function hideSharingEditor() { $('sharingEditor').classList.add('hidden'); }
function onSharingLevelChange() {
  const row = $('sharingMembersRow');
  if (row) row.classList.toggle('hidden', $('sharingLevel').value !== 'project');
}

async function saveOntologySharing() {
  const o = state.currentOntology;
  if (!o) return;
  const sharing = $('sharingLevel').value;
  const body = { sharing };
  if (sharing === 'project') {
    body.shared_with = ($('sharingMembers').value || '').split(/[,，]/).map(s => s.trim()).filter(Boolean);
    if (!body.shared_with.length) { toast('项目组共享必须指定至少一个成员', 'warn'); return; }
  }
  try {
    await api('/api/ontologies/' + o.id + '/sharing', { method: 'PATCH', body: JSON.stringify(body) });
    const j = await api('/api/ontologies/' + o.id);
    state.currentOntology = j.data;
    const idx = state.ontologies.findIndex(x => x.id === o.id);
    if (idx >= 0) state.ontologies[idx] = j.data;
    renderOntologyList();
    selectOntology(j.data);
    toast('共享等级已更新：' + (SHARING_LABELS[sharing] || sharing), 'ok');
  } catch (e) {
    const inv = e.details?.invalid_users;
    toast('共享调整失败: ' + e.message + (inv ? '（无效账号: ' + inv.join(', ') + '）' : ''), 'err', 6000);
  }
}

// ---- V2-07 / M2-E：变更审批（changeset）----
const CS_STATUS_LABELS = { pending: '待审批', approved: '已通过', rejected: '已拒绝', withdrawn: '已撤回', abandoned: '已放弃', archived: '已归档' };
const CS_KIND_LABELS = {
  object_added: '新增对象', object_removed: '删除对象', stable_key_changed: '主键(stable_key)变化',
  parent_changed: '父对象变化', attribute_added: '新增属性', attribute_removed: '删除属性',
  attribute_type_changed: '属性类型变化', attribute_required_changed: '必填性变化',
  constraint_widened: '取值范围放宽', constraint_narrowed: '取值范围收窄',
  relation_added: '新增关系', relation_removed: '删除关系', relation_changed: '关系变化',
  constraint_added: '新增业务约束', constraint_removed: '删除业务约束', constraint_modified: '业务约束修改',
  function_added: '新增函数', function_removed: '删除函数',
};

function _csKindLabel(kind) { return CS_KIND_LABELS[kind] || kind; }

async function loadChangesets() {
  const el = $('csList');
  if (!el) return;
  const status = $('csFilterStatus') ? $('csFilterStatus').value : '';
  try {
    const j = await api('/api/changesets' + (status ? '?status=' + encodeURIComponent(status) : ''));
    const items = (j.data && j.data.items) || [];
    $('csDetail').classList.add('hidden');
    if (!items.length) { el.innerHTML = '<p class="text-xs text-slate-500">暂无变更审批记录</p>'; return; }
    let html = '<table class="w-full text-xs"><thead><tr class="text-slate-400 border-b border-slate-800">'
      + '<th class="text-left py-1 pr-2">ID</th><th class="text-left pr-2">本体</th><th class="text-left pr-2">目标版本</th>'
      + '<th class="text-left pr-2">兼容性</th><th class="text-left pr-2">状态</th><th class="text-left pr-2">提交人</th>'
      + '<th class="text-left pr-2">提交时间</th><th class="text-left pr-2">处理人</th><th class="text-left pr-2">备注/拒绝原因</th><th></th></tr></thead><tbody>';
    for (const c of items) {
      const verdict = c.diff?.compatible ? '<span class="text-emerald-400">兼容</span>' : '<span class="text-rose-400">不兼容</span>';
      const who = c.status === 'approved' ? (c.approved_by || '-') : c.status === 'rejected' ? (c.rejected_by || '-') : '-';
      const why = c.reject_reason ? '<span class="text-rose-300">' + esc(c.reject_reason) + '</span>' : esc(c.note || '');
      html += '<tr class="border-b border-slate-800/60 hover:bg-slate-900/40">'
        + '<td class="py-1 pr-2 font-mono">' + esc(c.id) + '</td>'
        + '<td class="pr-2">' + esc(c.ontology_name || c.ontology_id) + '</td>'
        + '<td class="pr-2">' + esc(c.target_version || '-') + '</td>'
        + '<td class="pr-2">' + verdict + '（' + (c.diff?.changes?.length || 0) + ' 项变更）</td>'
        + '<td class="pr-2"><span class="badge">' + esc(CS_STATUS_LABELS[c.status] || c.status) + '</span></td>'
        + '<td class="pr-2">' + esc(c.submitted_by) + '</td>'
        + '<td class="pr-2">' + esc(c.submitted_at || '') + '</td>'
        + '<td class="pr-2">' + esc(who) + '</td>'
        + '<td class="pr-2 max-w-[220px] truncate" title="' + esc(c.reject_reason || c.note || '') + '">' + why + '</td>'
        + '<td class="whitespace-nowrap"><button class="btn secondary text-[11px]" onclick="showChangeset(\'' + c.id + '\')">详情</button></td>'
        + '</tr>';
    }
    el.innerHTML = html + '</tbody></table>';
  } catch (e) {
    toast('加载变更审批失败: ' + e.message, 'err');
  }
}

let _csOpenId = null;

async function showChangeset(cid) {
  const box = $('csDetail');
  if (!box) return;
  _csOpenId = cid;
  box.classList.remove('hidden');
  box.innerHTML = '<p class="text-xs text-slate-500">加载中…</p>';
  let c;
  try {
    const j = await api('/api/changesets/' + cid);
    c = j.data;
  } catch (e) { box.innerHTML = '<p class="text-xs text-rose-400">' + esc(e.message) + '</p>'; return; }
  const ch = (c.diff?.changes || []);
  let rows = '';
  for (const x of ch) {
    const cls = x.compatible ? 'text-emerald-400' : 'text-rose-400';
    rows += '<tr class="border-b border-slate-800/60"><td class="py-1 pr-2"><span class="' + cls + '">'
      + (x.compatible ? '兼容' : '不兼容') + '</span></td><td class="pr-2">' + esc(_csKindLabel(x.kind)) + '</td>'
      + '<td class="pr-2 font-mono">' + esc(x.object_id || '') + '</td><td class="pr-2 font-mono">' + esc(x.attribute_id || '') + '</td>'
      + '<td class="pr-2 text-slate-400">' + esc(x.detail || '') + '</td></tr>';
  }
  const me = _meUser() || {};
  const canReview = me.role === 'admin' || me.role === 'reviewer';
  const isSubmitter = me.username === c.submitted_by;
  let acts = '';
  if (c.status === 'pending') {
    if (canReview && me.username !== c.submitted_by) {
      acts += '<button class="btn text-xs" onclick="csAct(\'' + c.id + '\',\'approve\')">通过</button> '
        + '<button class="btn secondary text-xs" onclick="csAskReject(\'' + c.id + '\')">拒绝（须理由）</button>';
    }
    if (isSubmitter) {
      acts += ' <button class="btn secondary text-xs" onclick="csAct(\'' + c.id + '\',\'withdraw\')">撤回</button> '
        + '<button class="btn secondary text-xs" onclick="csAct(\'' + c.id + '\',\'abandon\')">放弃</button>';
    }
  }
  const proc = c.status === 'approved' ? ('审批通过：' + (c.approved_by || '-') + ' @ ' + (c.approved_at || '-'))
    : c.status === 'rejected' ? ('审批拒绝：' + (c.rejected_by || '-') + ' @ ' + (c.rejected_at || '-') + '，理由：' + (c.reject_reason || '-'))
    : c.status === 'archived' ? ('已归档：' + (c.archived_reason || '')) : '';
  box.innerHTML = '<div class="p-3 rounded border border-slate-700 bg-slate-900/60 text-xs space-y-2">'
    + '<div class="flex items-center gap-2 flex-wrap"><span class="badge">' + esc(CS_STATUS_LABELS[c.status] || c.status) + '</span>'
    + '<span>本体：<b>' + esc(c.ontology_name || c.ontology_id) + '</b></span>'
    + '<span>基线版本：' + esc(c.base_version || '-') + ' → 目标版本：<b>' + esc(c.target_version || '-') + '</b></span>'
    + '<span>提交：' + esc(c.submitted_by) + ' @ ' + esc(c.submitted_at || '') + '</span>'
    + (proc ? '<span>' + esc(proc) + '</span>' : '')
    + (c.note ? '<span>备注：' + esc(c.note) + '</span>' : '')
    + '</div>'
    + '<div class="overflow-x-auto"><table class="w-full text-xs"><thead><tr class="text-slate-400 border-b border-slate-800">'
    + '<th class="text-left py-1 pr-2">兼容性</th><th class="text-left pr-2">变更</th><th class="text-left pr-2">对象</th><th class="text-left pr-2">属性</th><th class="text-left pr-2">说明</th></tr></thead><tbody>'
    + (rows || '<tr><td colspan="5" class="py-1 text-slate-500">无变更项</td></tr>') + '</tbody></table></div>'
    + (acts ? '<div class="flex gap-2 pt-1">' + acts + '</div>' : '')
    + '</div>';
}

function csAskReject(cid) {
  const reason = prompt('拒绝理由（必填，TC-07-02）：');
  if (reason === null) return;
  csAct(cid, 'reject', reason.trim());
}

async function csAct(cid, act, reason) {
  try {
    const body = {};
    if (act === 'reject') body.reason = reason;
    await api('/api/changesets/' + cid + '/' + act, { method: 'POST', body: JSON.stringify(body) });
    toast(act === 'approve' ? '审批通过，已发布大版本' : act === 'reject' ? '已拒绝（整组驳回）' : act === 'withdraw' ? '已撤回' : '已放弃', 'ok');
    await loadChangesets();
    if (_csOpenId === cid) showChangeset(cid).catch(() => {});
  } catch (e) {
    toast('操作失败: ' + e.message, 'err', 6000);
  }
}

// 建模页：提交变更（V2-07）——以当前加载的本体模型为提案
function showChangesetEditor() {
  const el = $('changesetEditor');
  if (!el) return;
  el.classList.toggle('hidden');
}
// 表单内打开变更提案编辑器（editCurrentOntology 对已发布本体已把编辑器移入表单卡片；
// 提案模型由 submitChangeset 从表单编辑收集，而非已加载模型）
function openChangesetFromForm() {
  const el = $('changesetEditor');
  if (!el) return;
  el.classList.remove('hidden');
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function submitChangeset() {
  const o = state.currentOntology;
  if (!o) return;
  const note = ($('csNote') || {}).value ? $('csNote').value.trim() : '';
  const targetVersion = ($('csTargetVersion') || {}).value ? $('csTargetVersion').value.trim() : '';
  let model;
  if (!$('editorCard').classList.contains('hidden')) {
    // V2-07：表单内提交（已发布本体直编被 409 拦截，唯一修改通道）——
    // 提案模型取表单编辑结果；否则不兼容变更永远无法进入变更单（TCE2E-06 联测缺口修复）
    model = Object.assign(collectFormOntology(), { functions: o.functions || [] });
  } else {
    model = {
      name: o.name, description: o.description || '',
      objects: o.objects || [], constraints: o.constraints || [], functions: o.functions || [],
    };
  }
  try {
    const j = await api('/api/ontologies/' + o.id + '/changesets', {
      method: 'POST', body: JSON.stringify({ model, note, target_version: targetVersion || null }),
    });
    const r = j.data || {};
    if (r.status === 'published') {
      toast('兼容变更已直接发布：v' + r.version + '（' + (r.diff?.changes?.length || 0) + ' 项变更）', 'ok', 6000);
      const fresh = await api('/api/ontologies/' + o.id);
      state.currentOntology = fresh.data;
      const idx = state.ontologies.findIndex(x => x.id === o.id);
      if (idx >= 0) state.ontologies[idx] = fresh.data;
      renderOntologyList();
      selectOntology(fresh.data);
    } else {
      toast('不兼容变更：已提交 v' + (r.target_version || '?') + ' 大版本审批（' + (r.diff?.changes?.length || 0) + ' 项变更），通过前对外仍是旧版本', 'warn', 8000);
      if (!$('editorCard').classList.contains('hidden')) {
        // 表单内提交后回到详情面板：展示当前对外仍是旧版本，变更单待审批
        $('changesetEditor').classList.add('hidden');
        closeEditor();
        const idx = state.ontologies.findIndex(x => x.id === o.id);
        if (idx >= 0) selectOntology(state.ontologies[idx]);
      }
    }
  } catch (e) {
    const d = e.details || {};
    const br = (d.breaking_changes || []).map(x => _csKindLabel(x.kind) + (x.attribute_id ? ':' + x.attribute_id : '')).join('，');
    toast('提交失败: ' + e.message + (br ? '（不兼容项：' + br + '）' : ''), 'err', 8000);
  }
}

// ---- V2-09 / M2-D：审计日志页（admin）----
let auditPage = 1;
let auditPageSize = 30;

function _auditQuery(withPage) {
  const q = new URLSearchParams();
  const g = id => { const el = $(id); return el ? el.value.trim() : ''; };
  const actor = g('auditActor'); if (actor) q.set('actor', actor);
  const action = g('auditAction'); if (action) q.set('action', action);
  const target = g('auditTarget'); if (target) q.set('target', target);
  const start = g('auditStart'); if (start) q.set('start', start + 'T00:00:00');
  const end = g('auditEnd'); if (end) q.set('end', end + 'T23:59:59');
  if (withPage) { q.set('page', String(auditPage)); q.set('page_size', String(auditPageSize)); }
  return q;
}

async function loadAuditLog(page) {
  if (page) auditPage = page;
  const table = $('auditTable');
  if (!table) return;
  try {
    const j = await api('/api/audit?' + _auditQuery(true).toString());
    const d = j.data || {};
    const items = d.items || [];
    $('auditSummary').textContent = '共 ' + (d.total || 0) + ' 条（第 ' + (d.page || 1) + ' 页）· 点击行查看前值/后值';
    if (!items.length) { table.innerHTML = '<p class="text-xs text-slate-500">无匹配记录</p>'; $('auditPager').innerHTML = ''; $('auditDetail').classList.add('hidden'); return; }
    let html = '<table class="w-full text-xs"><thead><tr class="text-slate-400 border-b border-slate-800">'
      + '<th class="text-left py-1 pr-2">时间</th><th class="text-left pr-2">操作者</th><th class="text-left pr-2">角色</th>'
      + '<th class="text-left pr-2">动作</th><th class="text-left pr-2">对象</th><th class="text-left pr-2">项目</th><th class="text-left pr-2">前值/后值</th></tr></thead><tbody>';
    for (const r of items) {
      const hasBA = (r.before !== null && r.before !== undefined) || (r.after !== null && r.after !== undefined);
      html += '<tr class="border-b border-slate-800/60 hover:bg-slate-900/40 cursor-pointer" onclick="showAuditRow(\'' + r.id + '\')">'
        + '<td class="py-1 pr-2 whitespace-nowrap">' + esc(r.ts || '') + '</td>'
        + '<td class="pr-2">' + esc(r.actor) + '</td>'
        + '<td class="pr-2">' + esc(r.actor_role || '-') + '</td>'
        + '<td class="pr-2 font-mono">' + esc(r.action) + '</td>'
        + '<td class="pr-2">' + esc(r.target_type ? r.target_type + '/' : '') + esc(r.target_id || r.target_name || '') + '</td>'
        + '<td class="pr-2">' + esc(r.project_id || '-') + '</td>'
        + '<td class="pr-2">' + (hasBA ? '<span class="text-sky-400">查看 diff</span>' : '-') + '</td>'
        + '</tr>';
    }
    table.innerHTML = html + '</tbody></table>';
    _auditItemsCache = items;
    const pages = Math.max(1, Math.ceil((d.total || 0) / auditPageSize));
    let pg = '';
    if (pages > 1) {
      pg += '<button class="btn secondary text-[11px]" ' + (auditPage <= 1 ? 'disabled' : '') + ' onclick="loadAuditLog(' + (auditPage - 1) + ')">上一页</button>';
      pg += '<span>第 ' + auditPage + ' / ' + pages + ' 页</span>';
      pg += '<button class="btn secondary text-[11px]" ' + (auditPage >= pages ? 'disabled' : '') + ' onclick="loadAuditLog(' + (auditPage + 1) + ')">下一页</button>';
    }
    $('auditPager').innerHTML = pg;
  } catch (e) {
    table.innerHTML = '<p class="text-xs text-rose-400">加载审计日志失败: ' + esc(e.message) + '</p>';
  }
}

let _auditItemsCache = [];

async function showAuditRow(id) {
  const box = $('auditDetail');
  if (!box) return;
  let r = (_auditItemsCache || []).find(x => x.id === id);
  if (!r) {
    try {
      const j = await api('/api/audit?target_id=' + encodeURIComponent(id) + '&page_size=1');
      r = ((j.data && j.data.items) || [])[0];
    } catch (e) { /* ignore */ }
  }
  if (!r) { box.innerHTML = '<p class="text-xs text-rose-400">未找到该记录</p>'; return; }
  box.classList.remove('hidden');
  const pre = v => v === null || v === undefined ? '<i class="text-slate-600">（空）</i>'
    : '<pre class="text-[11px] overflow-x-auto p-2 rounded bg-slate-950/70 max-h-64 overflow-y-auto">' + esc(JSON.stringify(v, null, 2)) + '</pre>';
  box.innerHTML = '<div class="p-3 rounded border border-slate-700 bg-slate-900/60 text-xs space-y-2">'
    + '<div class="flex items-center gap-3 flex-wrap text-slate-400">'
    + '<span>' + esc(r.ts) + '</span><span>操作者：<b class="text-slate-200">' + esc(r.actor) + '</b>（' + esc(r.actor_role || '-') + '）</span>'
    + '<span>动作：<span class="font-mono text-sky-300">' + esc(r.action) + '</span></span>'
    + '<span>对象：' + esc(r.target_type || '-') + '/' + esc(r.target_id || r.target_name || '') + '</span>'
    + '<span>项目：' + esc(r.project_id || '-') + '</span>'
    + (r.tenant_id ? '<span>租户：' + esc(r.tenant_id) + '</span>' : '')
    + '</div>'
    + '<div class="grid grid-cols-2 gap-2"><div><div class="text-slate-400 mb-1">前值（before）</div>' + pre(r.before) + '</div>'
    + '<div><div class="text-slate-400 mb-1">后值（after）</div>' + pre(r.after) + '</div></div>'
    + (r.detail && Object.keys(r.detail).length ? '<div class="text-slate-400 mb-1">detail</div>' + pre(r.detail) : '')
    + '</div>';
}

async function exportAudit(fmt) {
  try {
    const url = API_BASE + '/api/audit/export?' + _auditQuery(false).toString() + '&format=' + fmt;
    const r = await fetch(url, { credentials: 'include' });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error?.message || ('HTTP ' + r.status));
    }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'audit-export.' + (fmt === 'jsonl' ? 'jsonl' : 'csv');
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast('已导出 ' + (fmt === 'jsonl' ? 'JSONL' : 'CSV') + '（与检索页同条件）', 'ok');
  } catch (e) {
    toast('导出失败: ' + e.message, 'err');
  }
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
    id: 'obj_customer', name: '客户', description: '', parent: null,
    attributes: [{ id: 'attr_name', name: '名称', data_type: 'string', required: true, constraints: {} }],
    relations: []
  }];
  globalConstraintRows = [];
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
  const total = state.formObjects.length;
  container.innerHTML = state.formObjects.map((obj, idx) => {
    const attrs = obj.attributes || [];
    const attrHtml = attrs.map((a, aidx) => {
      const hasC = a.constraints && Object.keys(a.constraints).length > 0;
      const op = '<div class="col-span-2 flex gap-1 items-center justify-end">' +
        '<button class="pk-move" title="上移" onclick="moveAttr(' + idx + ',' + aidx + ',-1)"' + (aidx === 0 ? ' disabled' : '') + '>↑</button>' +
        '<button class="pk-move" title="下移" onclick="moveAttr(' + idx + ',' + aidx + ',1)"' + (aidx === attrs.length - 1 ? ' disabled' : '') + '>↓</button>' +
        '<span class="remove-btn" onclick="removeAttr(' + idx + ',' + aidx + ')">删除</span>' +
      '</div>';
      return '<div class="attr-row">' +
        '<div class="grid grid-cols-12 gap-2 items-end">' +
          '<div class="col-span-3 flex items-center gap-1"><span class="pk-badge shrink-0" data-pkbadge="' + idx + ':' + aidx + '" style="display:none" title="该属性将自动设为业务主键 stable_key">🔑 主键</span><input class="w-full attr-id" data-oidx="' + idx + '" data-aidx="' + aidx + '" value="' + esc(a.id) + '" placeholder="attr_name" list="attrIdList" /></div>' +
          '<div class="col-span-3"><input class="w-full attr-name" data-oidx="' + idx + '" data-aidx="' + aidx + '" value="' + esc(a.name) + '" placeholder="名称" list="attrNameList" /></div>' +
          '<div class="col-span-2">' + typeSelect('attr-type', idx, aidx, a.data_type) + '</div>' +
          '<div class="col-span-1"><label class="inline-flex items-center gap-1"><input type="checkbox" class="attr-required" data-oidx="' + idx + '" data-aidx="' + aidx + '" ' + (a.required ? 'checked' : '') + '/> 必填</label></div>' +
          '<div class="col-span-1"><span class="cursor-pointer text-sky-400 text-xs hover:underline" onclick="toggleAttrConstraints(' + idx + ', ' + aidx + ')">约束' + (hasC ? ' <span class="text-amber-400">●</span>' : '') + '</span></div>' +
          op +
        '</div>' +
        attrConstraintRowHtml(a, idx, aidx) +
      '</div>';
    }).join('');
    const relHtml = (obj.relations || []).map((r, ridx) =>
      '<div class="grid grid-cols-12 gap-2 items-end">' +
        '<div class="col-span-3"><label class="text-[10px] text-slate-500">关系 ID（英文/下划线）</label><input class="w-full rel-id" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.id) + '" placeholder="belongs_to" list="relIdList" /></div>' +
        '<div class="col-span-3"><label class="text-[10px] text-slate-500">显示名称（中文）</label><input class="w-full rel-name" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.name) + '" placeholder="所属" list="relNameList" /></div>' +
        '<div class="col-span-2"><label class="text-[10px] text-slate-500">目标对象 ID（自动补全）</label><input class="w-full rel-target" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.target_object) + '" placeholder="obj_department" list="relTargetList" /></div>' +
        '<div class="col-span-2">' + cardSelect('rel-card', idx, ridx, r.cardinality) + '</div>' +
        '<div class="col-span-2 flex gap-1 items-center justify-end">' +
          '<button class="pk-move" title="上移" onclick="moveRel(' + idx + ',' + ridx + ',-1)"' + (ridx === 0 ? ' disabled' : '') + '>↑</button>' +
          '<button class="pk-move" title="下移" onclick="moveRel(' + idx + ',' + ridx + ',1)"' + (ridx === (obj.relations || []).length - 1 ? ' disabled' : '') + '>↓</button>' +
          '<span class="remove-btn" onclick="removeRel(' + idx + ',' + ridx + ')">删除</span>' +
        '</div>' +
      '</div>'
    ).join('');
    return '<div class="object-card" data-oidx="' + idx + '">' +
      '<div class="flex justify-between items-center mb-2"><span class="font-medium text-sm">对象 #' + (idx + 1) + '</span><span class="flex items-center gap-2">' +
        '<button class="pk-move" title="上移对象" onclick="moveObject(' + idx + ',-1)"' + (idx === 0 ? ' disabled' : '') + '>↑</button>' +
        '<button class="pk-move" title="下移对象" onclick="moveObject(' + idx + ',1)"' + (idx === total - 1 ? ' disabled' : '') + '>↓</button>' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" title="新建一个父级为本对象的子对象（parent 预填）" onclick="addChildObject(' + idx + ')">+ 子对象</button><span class="remove-btn" onclick="removeObject(' + idx + ')">删除对象</span></span></div>' +
      '<div class="grid grid-cols-3 gap-3 mb-3">' +
        '<div><label>对象 ID</label><input class="w-full obj-id" data-idx="' + idx + '" value="' + esc(obj.id) + '" placeholder="obj_customer" /></div>' +
        '<div><label>对象名称</label><input class="w-full obj-name" data-idx="' + idx + '" value="' + esc(obj.name) + '" placeholder="客户" /></div>' +
        '<div><label title="上级对象（父类）：编译为 RDFS.subClassOf；父对象必须在本表单内定义">上级对象（父类）</label>' + parentSelectHtml(idx, obj) + '</div>' +
      '</div>' +
      '<div class="mb-2"><label>描述</label><input class="w-full obj-desc" data-idx="' + idx + '" value="' + esc(obj.description || '') + '" /></div>' +
      '<div class="mb-2 flex items-center gap-2 text-xs text-slate-400"><label class="shrink-0" title="V2-03：可显式指定业务主键，选项仅为本对象的 string 属性">业务主键</label>' +
        '<select class="pk-select w-64" data-pkselect="' + idx + '" data-pkvalue="' + esc(obj.stable_key || '') + '" data-pkmanual="' + (obj.stable_key_explicit ? '1' : '0') + '" title="显式指定业务主键（仅 string 属性可选）"></select>' +
        '<span class="pk-origin text-[10px] text-slate-500" data-pkorigin="' + idx + '"></span></div>' +
      '<div class="form-section-title"><span>属性</span><button class="btn" onclick="addAttr(' + idx + ')">+ 属性</button></div>' +
      '<div class="pk-rule text-[10px] text-slate-500 mb-1">主键规则：未显式指定时，第一个「必填 + string」属性将自动设为业务主键（stable_key），属性顺序有意义，可用行内 ↑↓ 调整；也可在上方下拉显式指定（仅 string 属性可选）。<span class="pk-hint" data-pkhint="' + idx + '"></span></div>' +
      // 属性较多时列表底部也有新增入口（与对象列表底部「+ 添加对象」风格一致），无需回滚到区标题
      '<div class="space-y-2 mb-3">' + attrHtml + (attrs.length ? '<button class="btn secondary w-full" onclick="addAttr(' + idx + ')">+ 添加属性</button>' : '') + '</div>' +
      '<div class="form-section-title"><span>关系</span><button class="btn" onclick="addRel(' + idx + ')">+ 关系</button></div>' +
      '<div class="space-y-2">' + relHtml + ((obj.relations || []).length ? '<button class="btn secondary w-full" onclick="addRel(' + idx + ')">+ 添加关系</button>' : '') + '</div>' +
    '</div>';
  }).join('') + relTargetDatalist();
  bindObjectFormsDelegation();
  refreshFieldDatalists();
  updatePkBadgesAll();
  renderGlobalConstraints();
}

// ===== 业务主键（stable_key）实时提示 =====
// 规则与后端 _backfill_object_fields 三级回退一致：required+string → 第一个 string → 第一个属性
function pkHitIndex(attrs) {
  if (!attrs.length) return -1;
  let i = attrs.findIndex(a => a.required && (a.data_type || 'string') === 'string');
  if (i >= 0) return i;
  i = attrs.findIndex(a => (a.data_type || 'string') === 'string');
  if (i >= 0) return i;
  return 0;
}

// 从对象卡片 DOM 读当前属性定义（未 blur 的输入也算数）
function pkAttrsFromDom(card) {
  return Array.from(card.querySelectorAll('.attr-row')).map(row => ({
    id: (row.querySelector('.attr-id') || {}).value || '',
    data_type: (row.querySelector('.attr-type') || {}).value || 'string',
    required: (row.querySelector('.attr-required') || {}).checked || false,
  }));
}

// 增量更新单卡片的 🔑 徽标 / 提示行 / 业务主键下拉（不整卡重渲染，避免输入焦点丢失）
// V2-03：下拉选项只列本对象的 string 属性；未手动选择时按 pkHitIndex 自动规则回显。
function updatePkBadges(idx) {
  const card = document.querySelector('#objectForms .object-card[data-oidx="' + idx + '"]');
  if (!card) return;
  const attrs = pkAttrsFromDom(card);
  const autoHit = pkHitIndex(attrs);
  const stringAttrs = attrs.filter(a => (a.data_type || 'string') === 'string' && a.id);
  const sel = card.querySelector('.pk-select');
  let chosen = '';
  if (sel) {
    const manual = sel.dataset.pkmanual === '1';
    const prev = sel.value || sel.dataset.pkvalue || '';
    sel.innerHTML = ['<option value="">（按自动规则）</option>'].concat(
      stringAttrs.map(a => '<option value="' + esc(a.id) + '">' + esc(a.id) + '</option>')
    ).join('');
    const autoValue = autoHit >= 0 && attrs[autoHit].data_type === 'string' ? (attrs[autoHit].id || '') : '';
    const wanted = prev || (manual ? '' : autoValue);
    if (wanted && stringAttrs.some(a => a.id === wanted)) chosen = wanted;
    else if (!manual) chosen = autoValue;
    sel.value = chosen;
    sel.dataset.pkvalue = chosen;
    sel.dataset.pkmanual = manual && chosen ? '1' : '0';
    sel.disabled = !stringAttrs.length;
  }
  const manual = sel ? sel.dataset.pkmanual === '1' : false;
  const chosenIdx = chosen ? attrs.findIndex(a => a.id === chosen) : autoHit;
  card.querySelectorAll('.pk-badge').forEach(b => {
    const aidx = parseInt(String(b.dataset.pkbadge).split(':')[1], 10);
    b.style.display = aidx === chosenIdx && chosenIdx >= 0 ? 'inline-block' : 'none';
    b.title = manual ? '用户显式指定的业务主键 stable_key' : '按自动规则推断的业务主键 stable_key';
  });
  const hint = card.querySelector('.pk-hint');
  if (hint) {
    const name = chosenIdx >= 0 ? (attrs[chosenIdx].id || '（未填 ID）') : '';
    if (!attrs.length) hint.textContent = '｜当前无属性，保存时无法生成业务主键';
    else if (manual && chosen) hint.textContent = '｜显式指定主键 → ' + name;
    else if (attrs[chosenIdx] && attrs[chosenIdx].required && attrs[chosenIdx].data_type === 'string') hint.textContent = '｜当前自动主键 → ' + name;
    else if (attrs[chosenIdx] && attrs[chosenIdx].data_type === 'string') hint.textContent = '｜无「必填 + string」属性，退化为第一个 string 属性 → ' + name;
    else hint.textContent = '｜无 string 属性，建议补一个字符串主键属性';
  }
  const origin = card.querySelector('.pk-origin');
  if (origin) origin.textContent = manual && chosen ? '显式指定' : (stringAttrs.length ? '自动推断' : '无 string 属性');
}

function updatePkBadgesAll() {
  document.querySelectorAll('#objectForms .object-card').forEach(c => updatePkBadges(parseInt(c.dataset.oidx, 10)));
}

// 表单内任意输入（勾选必填/切换类型/改 ID/上下移动后）实时刷新主键标记；容器静态，只绑一次
function bindObjectFormsDelegation() {
  const of = $('objectForms');
  if (!of || of.dataset.pkBound) return;
  of.dataset.pkBound = '1';
  const handler = e => {
    // V2-03：用户主动改动主键下拉即视为显式指定；选回空项则恢复自动规则
    if (e.target && e.target.classList && e.target.classList.contains('pk-select')) {
      e.target.dataset.pkmanual = e.target.value ? '1' : '0';
    }
    const card = e.target && e.target.closest ? e.target.closest('.object-card') : null;
    if (card && card.dataset.oidx != null) updatePkBadges(parseInt(card.dataset.oidx, 10));
    refreshFieldDatalists();
  };
  of.addEventListener('input', handler);
  of.addEventListener('change', handler);
  of.addEventListener('keydown', handleTabComplete);
}

// ===== 字段联想（datalist）+ Tab 补全 =====
// 同类字段（属性 ID/名称、关系 ID/名称）联想本表单内所有对象已填的值；
// 目标对象 ID 另有 relTargetList。输入时浏览器原生下拉展示候选，
// 按 Tab 自动补全联想第一位（前缀匹配、区分大小写不敏感），随后正常移动焦点。
const FIELD_LISTS = {
  'attr-id': 'attrIdList',
  'attr-name': 'attrNameList',
  'rel-id': 'relIdList',
  'rel-name': 'relNameList',
};

// 从表单 DOM 收集各字段已填值重建 datalist（按出现顺序去重；跳过正在输入的框，避免自身占第一位）
function refreshFieldDatalists() {
  const container = $('objectForms');
  if (!container) return;
  for (const [cls, listId] of Object.entries(FIELD_LISTS)) {
    const seen = new Set();
    container.querySelectorAll('input.' + cls).forEach(inp => {
      if (inp === document.activeElement) return;
      const v = (inp.value || '').trim();
      if (v) seen.add(v);
    });
    let el = document.getElementById(listId);
    if (!el) {
      el = document.createElement('datalist');
      el.id = listId;
      container.appendChild(el);
    }
    el.innerHTML = Array.from(seen).map(v => '<option value="' + esc(v) + '"></option>').join('');
  }
}

// Tab 补全：取 datalist 中前缀匹配且不同于当前值的第一个候选回填；不阻止默认行为（焦点照常后移）
function handleTabComplete(e) {
  const t = e.target;
  if (e.key !== 'Tab' || e.shiftKey || !t.matches || !t.matches('input[list]')) return;
  const list = document.getElementById(t.getAttribute('list'));
  if (!list) return;
  const cur = (t.value || '').trim();
  if (!cur) return;
  const cands = Array.from(list.querySelectorAll('option'))
    .map(o => o.value)
    .filter(v => v && v !== t.value && v.toLowerCase().startsWith(cur.toLowerCase()));
  if (cands.length) t.value = cands[0];
}

// 属性/关系/对象上移下移：先 sync 再把 state 里交换，立即重渲染（约束值经 sync 已回写，不丢）
function moveAttr(idx, aidx, dir) {
  syncFormObjectsFromDOM();
  const list = state.formObjects[idx].attributes || [];
  const to = aidx + dir;
  if (to < 0 || to >= list.length) return;
  [list[aidx], list[to]] = [list[to], list[aidx]];
  renderObjectForms();
}
function moveRel(idx, ridx, dir) {
  syncFormObjectsFromDOM();
  const list = state.formObjects[idx].relations || [];
  const to = ridx + dir;
  if (to < 0 || to >= list.length) return;
  [list[ridx], list[to]] = [list[to], list[ridx]];
  renderObjectForms();
}
function moveObject(idx, dir) {
  syncFormObjectsFromDOM();
  const list = state.formObjects;
  const to = idx + dir;
  if (to < 0 || to >= list.length) return;
  [list[idx], list[to]] = [list[to], list[idx]];
  renderObjectForms();
}

// 上级对象（父类）下拉：列出本表单内其它已填 ID 的对象
function parentSelectHtml(oidx, obj) {
  const val = obj.parent || '';
  const opts = state.formObjects
    .filter((o, i) => i !== oidx && o.id)
    .map(o => '<option value="' + esc(o.id) + '"' + (val === o.id ? ' selected' : '') + '>' + esc(o.id) + (o.name ? '（' + esc(o.name) + '）' : '') + '</option>')
    .join('');
  return '<select class="w-full obj-parent" data-idx="' + oidx + '"><option value=""' + (!val ? ' selected' : '') + '>(无 — 根对象)</option>' + opts + '</select>';
}

// 属性约束录入行（默认折叠，输入始终在 DOM 中，切换不丢失已填值）
function attrConstraintRowHtml(a, oidx, aidx) {
  const c = a.constraints || {};
  const v = k => esc(c[k] != null ? String(c[k]) : '');
  return '<div class="attr-constraints mt-1 p-2 rounded bg-slate-900/50 border border-slate-700/40" data-oidx="' + oidx + '" data-aidx="' + aidx + '" style="display:none">' +
    '<div class="text-[10px] text-slate-500 mb-1">属性约束（可选）：长度/正则用于 string；最小/最大值用于 integer/float；枚举值须与数据类型匹配，逗号分隔</div>' +
    '<div class="grid grid-cols-12 gap-2">' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">最小长度</label><input class="w-full c-minlen" data-oidx="' + oidx + '" data-aidx="' + aidx + '" value="' + v('min_length') + '" placeholder="如 3" /></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">最大长度</label><input class="w-full c-maxlen" data-oidx="' + oidx + '" data-aidx="' + aidx + '" value="' + v('max_length') + '" placeholder="如 20" /></div>' +
      '<div class="col-span-4"><label class="text-[10px] text-slate-500">正则 pattern</label><input class="w-full c-pattern" data-oidx="' + oidx + '" data-aidx="' + aidx + '" value="' + v('pattern') + '" placeholder="如 ^[0-9]+$" /></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">最小值</label><input class="w-full c-min" data-oidx="' + oidx + '" data-aidx="' + aidx + '" value="' + v('minimum') + '" placeholder="如 0" /></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">最大值</label><input class="w-full c-max" data-oidx="' + oidx + '" data-aidx="' + aidx + '" value="' + v('maximum') + '" placeholder="如 9999" /></div>' +
    '</div>' +
    '<div class="mt-2"><label class="text-[10px] text-slate-500">枚举值（逗号分隔）</label><input class="w-full c-enum" data-oidx="' + oidx + '" data-aidx="' + aidx + '" value="' + (c.enum ? esc(c.enum.join(', ')) : '') + '" placeholder="如 pending, paid, shipped" /></div>' +
  '</div>';
}

function toggleAttrConstraints(oidx, aidx) {
  const card = $('objectForms').children[oidx];
  if (!card) return;
  const row = card.querySelector('.attr-constraints[data-aidx="' + aidx + '"]');
  if (row) row.style.display = (row.style.display === 'none' || !row.style.display) ? 'block' : 'none';
}

// 从属性行 DOM 收集 constraints（空输入 → 空对象）
function collectAttrConstraints(card, aidx) {
  const q = cls => { const el = card.querySelector('.' + cls + '[data-aidx="' + aidx + '"]'); return el ? el.value.trim() : ''; };
  const c = {};
  const mn = parseInt(q('c-minlen'), 10); if (!isNaN(mn)) c.min_length = mn;
  const mx = parseInt(q('c-maxlen'), 10); if (!isNaN(mx)) c.max_length = mx;
  const pat = q('c-pattern'); if (pat) c.pattern = pat;
  const vmin = parseFloat(q('c-min')); if (!isNaN(vmin)) c.minimum = vmin;
  const vmax = parseFloat(q('c-max')); if (!isNaN(vmax)) c.maximum = vmax;
  const en = q('c-enum');
  if (en) {
    const dtEl = card.querySelector('.attr-type[data-aidx="' + aidx + '"]');
    const dt = dtEl ? dtEl.value : 'string';
    c.enum = en.split(/[,，]/).map(s => s.trim()).filter(Boolean).map(s =>
      (dt === 'integer' && /^-?\d+$/.test(s)) ? parseInt(s, 10) :
      (dt === 'float' && /^-?\d*\.?\d+$/.test(s)) ? parseFloat(s) : s);
  }
  return c;
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
  syncGlobalConstraintsFromDOM();
  const cards = $('objectForms').children;
  for (let i = 0; i < cards.length; i++) {
    const card = cards[i];
    if (!card.dataset || card.dataset.oidx == null) continue;
    const idx = parseInt(card.dataset.oidx);
    const obj = state.formObjects[idx];
    if (!obj) continue;
    obj.id = card.querySelector('.obj-id').value;
    obj.name = card.querySelector('.obj-name').value;
    obj.description = card.querySelector('.obj-desc').value;
    obj.parent = (card.querySelector('.obj-parent') || {}).value || null;
    const pkSel = card.querySelector('.pk-select');
    if (pkSel) {
      obj.stable_key = pkSel.value || null;
      obj.stable_key_explicit = pkSel.dataset.pkmanual === '1' && !!pkSel.value;
    }
    const oldAttrs = obj.attributes || [];
    obj.attributes = [];
    card.querySelectorAll('.attr-id').forEach(el => {
      const aidx = parseInt(el.dataset.aidx);
      obj.attributes.push({
        id: el.value,
        name: card.querySelector('.attr-name[data-aidx="' + aidx + '"]').value,
        data_type: card.querySelector('.attr-type[data-aidx="' + aidx + '"]').value,
        required: card.querySelector('.attr-required[data-aidx="' + aidx + '"]').checked,
        // 约束输入行始终在 DOM（折叠只是 display:none），统一读回
        constraints: collectAttrConstraints(card, aidx)
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
  state.formObjects.push({ id: '', name: '', description: '', parent: null, attributes: [], relations: [] });
  renderObjectForms();
  // 新增卡片滚动入视（头/尾按钮通用；nearest 避免顶部按钮场景过度滚动；容器尾部还有共享 datalist，须锁定 object-card）
  const cards = document.querySelectorAll('#objectForms .object-card');
  if (cards.length) cards[cards.length - 1].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
// 父对象卡片「+ 子对象」：新建一个 parent 预填为该对象 id 的对象，滚动定位过去
function addChildObject(oidx) {
  syncFormObjectsFromDOM();
  const parent = state.formObjects[oidx];
  if (!parent || !parent.id) { toast('请先填写父对象的「对象 ID」', 'err'); return; }
  state.formObjects.push({ id: '', name: '', description: '', parent: parent.id, attributes: [], relations: [] });
  renderObjectForms();
  const cards = $('objectForms').children;
  const card = cards[cards.length - 1];
  if (card) {
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.style.outline = '2px solid #38bdf8';
    setTimeout(() => { card.style.outline = ''; }, 1600);
    const idInput = card.querySelector('.obj-id');
    if (idInput) idInput.focus();
  }
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
      parent: o.parent || null,
      stable_key: o.stable_key || null,
      stable_key_explicit: !!o.stable_key_explicit,
      attributes: o.attributes.map(a => ({ id: a.id, name: a.name, data_type: a.data_type, required: a.required, constraints: a.constraints || {} })),
      relations: o.relations.map(r => ({ id: r.id, name: r.name, target_object: r.target_object, cardinality: r.cardinality }))
    })),
    constraints: (globalConstraintRows || []).filter(c => c.id).map(c => ({
      id: c.id, name: c.name || c.id, scope: c.scope || null, rule: c.rule, params: c.params || {}
    }))
  };
}

// 保存前本地预检（后端仍会做权威校验）：父级存在/重复 ID/约束取值合法性
function validateFormBeforeSave() {
  const objs = state.formObjects;
  const ids = new Set(objs.map(o => o.id).filter(Boolean));
  for (const o of objs) {
    const label = o.id || o.name || '?';
    if (o.parent && !ids.has(o.parent)) return '对象 ' + label + ' 的上级对象 "' + o.parent + '" 在本表单中不存在';
    if (o.id && objs.filter(x => x.id === o.id).length > 1) return '对象 ID 重复: ' + o.id;
  }
  for (const o of objs) {
    for (const a of (o.attributes || [])) {
      const c = a.constraints || {};
      const al = a.id || a.name || '?';
      if (c.min_length != null && c.max_length != null && c.min_length > c.max_length) return '属性 ' + al + ': 最小长度大于最大长度';
      if (c.minimum != null && c.maximum != null && c.minimum > c.maximum) return '属性 ' + al + ': 最小值大于最大值';
      if (c.enum && c.enum.length) {
        if (a.data_type === 'integer' && c.enum.some(v => !/^-?\d+$/.test(v))) return '属性 ' + al + ': integer 的枚举值必须全部是整数（' + c.enum.join(', ') + '）';
        if (a.data_type === 'float' && c.enum.some(v => isNaN(Number(v)))) return '属性 ' + al + ': float 的枚举值必须全部是数字（' + c.enum.join(', ') + '）';
      }
    }
  }
  for (const c of (globalConstraintRows || [])) {
    if (!c.id) continue;
    if (c.rule === 'unique') {
      if (!c.scope) return '全局约束 ' + c.id + ': 缺少作用对象';
      if (!c.params || !c.params.attribute) return '全局约束 ' + c.id + ': 缺少目标属性（unique 规则须指定属性）';
    }
  }
  return null;
}

// ---- 本体级全局约束（unique 等）：作用对象 + 规则 + 参数（params.attribute） ----
let globalConstraintRows = [];

function renderGlobalConstraints() {
  const box = $('constraintForms');
  if (!box) return;
  if (!globalConstraintRows.length) {
    box.innerHTML = '<div class="text-xs text-slate-500">无全局约束。可为某对象的属性定义唯一性（unique）约束。</div>';
    return;
  }
  box.innerHTML = globalConstraintRows.map((c, i) => {
    const scopeObj = state.formObjects.find(o => o.id === c.scope);
    const scopeOpts = state.formObjects.filter(o => o.id)
      .map(o => '<option value="' + esc(o.id) + '"' + (c.scope === o.id ? ' selected' : '') + '>' + esc(o.id) + '</option>').join('');
    const attrOpts = (scopeObj ? (scopeObj.attributes || []) : [])
      .filter(a => a.id)
      .map(a => '<option value="' + esc(a.id) + '"' + ((c.params || {}).attribute === a.id ? ' selected' : '') + '>' + esc(a.id) + '</option>').join('');
    return '<div class="grid grid-cols-12 gap-2 items-end p-2 rounded bg-slate-900/40 border border-slate-700/40">' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">约束 ID</label><input class="w-full gc-id" data-cidx="' + i + '" value="' + esc(c.id) + '" placeholder="cst_sku_unique" /></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">名称</label><input class="w-full gc-name" data-cidx="' + i + '" value="' + esc(c.name || '') + '" placeholder="SKU 唯一" /></div>' +
      '<div class="col-span-3"><label class="text-[10px] text-slate-500">作用对象（scope）</label><select class="w-full gc-scope" data-cidx="' + i + '" onchange="onGcScopeChange(' + i + ')"><option value="">(选择对象)</option>' + scopeOpts + '</select></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">规则</label><select class="w-full gc-rule" data-cidx="' + i + '"><option value="unique"' + (c.rule === 'unique' ? ' selected' : '') + '>unique 唯一</option></select></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">目标属性（attribute）</label><select class="w-full gc-attr" data-cidx="' + i + '"><option value="">(选择属性)</option>' + attrOpts + '</select></div>' +
      '<div class="col-span-1"><span class="remove-btn" onclick="removeGlobalConstraint(' + i + ')">删除</span></div>' +
    '</div>';
  }).join('');
}

function addGlobalConstraint() {
  syncFormObjectsFromDOM();
  const first = state.formObjects.find(o => o.id);
  globalConstraintRows.push({ id: '', name: '', scope: (first && first.id) || '', rule: 'unique', params: {} });
  renderGlobalConstraints();
}

function onGcScopeChange(i) {
  const el = $('constraintForms') && $('constraintForms').querySelector('.gc-scope[data-cidx="' + i + '"]');
  if (el) globalConstraintRows[i].scope = el.value; // 写回新作用对象，否则重渲染会弹回旧值
  globalConstraintRows[i].params = {}; // 目标属性选项随作用对象变化，重置
  renderGlobalConstraints();
}

function removeGlobalConstraint(i) {
  globalConstraintRows.splice(i, 1);
  renderGlobalConstraints();
}

function syncGlobalConstraintsFromDOM() {
  const box = $('constraintForms');
  if (!box) return;
  box.querySelectorAll('.gc-id').forEach(el => {
    const i = parseInt(el.dataset.cidx);
    const row = globalConstraintRows[i];
    if (!row) return;
    row.id = el.value;
    row.name = box.querySelector('.gc-name[data-cidx="' + i + '"]').value;
    row.scope = box.querySelector('.gc-scope[data-cidx="' + i + '"]').value;
    row.rule = box.querySelector('.gc-rule[data-cidx="' + i + '"]').value;
    const attr = box.querySelector('.gc-attr[data-cidx="' + i + '"]').value;
    row.params = (row.rule === 'unique' && attr) ? { attribute: attr } : {};
  });
}

async function saveOntology() {
  const payload = collectFormOntology();
  const preErr = validateFormBeforeSave();
  if (preErr) { toast('保存前检查: ' + preErr, 'err'); return; }
  const isEdit = !!state.editingId;
  try {
    // 编辑既有本体走 PUT（原地更新，不再重复新建）；新建走 POST
    const j = isEdit
      ? await api('/api/ontologies/' + state.editingId, { method: 'PUT', body: JSON.stringify(payload) })
      : await api('/api/ontologies', { method: 'POST', body: JSON.stringify(payload) });
    toast((isEdit ? '本体已更新: ' : '本体已保存: ') + j.data.name, 'ok');
    // V2-03 / EP-03-01：主键变更时明确提示存量实例影响（不静默改写）
    const pkImpact = j.data && j.data.primary_key_change;
    if (pkImpact && pkImpact.existing_instances > 0) {
      setTimeout(() => toast('主键变更影响：' + pkImpact.hint, 'info'), 400);
    }
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
  toast('AI 正在生成本体…（思考型模型可能需要 1~3 分钟）', 'info', 5000);
  try {
    const j = await api('/api/ontologies/from-text', { method: 'POST', body: JSON.stringify({ text }) });
    const draft = j.data;
    if (draft.generation_mode === 'fallback') {
      toast('LLM 生成失败，已回退到确定性模板（仅关键词抽取，非 AI 产出）。请检查「设置 → LLM」后重新生成: ' + draft.name, 'err', 10000);
    } else {
      toast('已生成本体草稿: ' + draft.name, 'ok');
    }
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
        $('btnDocParse').disabled = false;
        await loadOntologies();
        const draft = state.ontologies.find(o => o.id === j.data.draft_id);
        if (draft && draft.generation_mode === 'fallback') {
          toast('文档解析完成（LLM 不可用，使用模板兜底结果，非 AI 产出）', 'err', 10000);
        } else {
          toast('文档解析完成', 'ok');
        }
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
  // V2-07：已发布本体直编会被后端 409 PUBLISHED_IMMUTABLE 拒绝——
  // 表单内显示「提交变更」入口，并把变更提案编辑器移入表单卡片（模型取自表单编辑）
  const publishedEditing = (o.status || 'active') === 'active';
  $('btnFormChangeset').style.display = publishedEditing ? '' : 'none';
  const ce = $('changesetEditor');
  ce.classList.add('hidden');
  if (publishedEditing) {
    $('editorCard').appendChild(ce);
  } else if (ce.parentElement === $('editorCard')) {
    $('detailSharing').insertAdjacentElement('afterend', ce);
  }
  $('editorCard').classList.remove('hidden');
  $('detailPanel').classList.add('hidden');
  $('editorTitle').textContent = '编辑本体';
  $('formName').value = o.name || '';
  $('formVersion').value = o.version || '1';
  $('formModelingType').value = o.modeling_type || 'manual';
  $('formDesc').value = o.description || '';
  state.formObjects = (o.objects || []).map(x => ({
    id: x.id, name: x.name, description: x.description || '', parent: x.parent || null,
    stable_key: x.stable_key || null, stable_key_explicit: !!x.stable_key_explicit,
    attributes: (x.attributes || []).map(a => ({ id: a.id, name: a.name, data_type: a.data_type, required: !!a.required, constraints: a.constraints || {} })),
    relations: (x.relations || []).map(r => ({ id: r.id, name: r.name, target_object: r.target_object, cardinality: r.cardinality }))
  }));
  // 本体级约束回显（unique 等）
  globalConstraintRows = (o.constraints || []).map(c => ({ id: c.id, name: c.name, scope: c.scope || '', rule: c.rule, params: c.params || {} }));
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
  if (activeTab && activeTab.id === 'mcp') { loadMcpOntologies(mcpList.page); loadMcpToken(); }
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
  state.allInstanceQ = '';
  state.allInstanceObjectFilter = '';
  const s = $('allInstanceSearch'); if (s) s.value = '';
  const sel = $('ingestOntology').value;
  const o = state.ontologies.find(x => x.id === sel);
  state.expandedTreeNodes = new Set(o ? (o.objects || []).map(x => x.id) : []);
  closeInstanceDetail();
  renderIngestTarget();
  renderIngestObjectMeta(null);
  refreshSingleImportContext();
  renderInstanceList();
  renderAllInstanceList();
  // 切本体必须重拉实例/候选：否则树计数与列表仍用旧本体的全局缓存
  // （新本体各实体显示 0，仅 object_type id 与旧数据相同的实体残留旧值）
  loadInstances();
  loadCandidates();
}

// ---- 实例导入页子标签：对象视图 / 导入与审核 / 全部实例 ----
const INGEST_TABS = { object: '对象视图', review: '导入与审核', all: '全部实例' };
function setIngestTab(tab) {
  if (!INGEST_TABS[tab]) tab = 'object';
  state.ingestTab = tab;
  Object.keys(INGEST_TABS).forEach(k => {
    const btn = $('subtab' + k.charAt(0).toUpperCase() + k.slice(1));
    const panel = $('ingestPanel' + k.charAt(0).toUpperCase() + k.slice(1));
    if (btn) btn.classList.toggle('active', k === tab);
    if (panel) { panel.classList.toggle('hidden', k !== tab); panel.classList.toggle('flex', k === tab); }
  });
  if (tab === 'object') renderInstanceList();
  if (tab === 'all') renderAllInstanceList();
}
function updateIngestTabBadges() {
  const rb = $('ingestBadgeReview');
  if (rb) {
    const pending = (state.candidates || []).filter(c => c.status === 'pending').length;
    rb.textContent = pending;
    rb.classList.toggle('hidden', pending <= 0);
  }
  const ab = $('ingestBadgeAll');
  if (ab) ab.textContent = (state.instances || []).length;
}

// ---- 1.5 左侧实体层级树：按 many_to_one 关系构建父子层级（Protege 风格） ----
// 注意：关系图可能含 many_to_one 环（A→B→A）或菱形共享父节点，直接按边递归渲染会无限爆栈，
// 故这里先把图转成真树——每个节点只挂到第一个到达它的父节点下，环形/重复边丢弃，保证每节点仅渲染一次。
function buildObjectTree(ont) {
  const objs = ont.objects || [];
  const byId = new Map(objs.map(o => [o.id, o]));
  const rawChildren = new Map();
  const hasParent = new Set();
  objs.forEach(o => (o.relations || []).forEach(r => {
    // 自环不入树（节点不能做自己的父级）
    if (r.cardinality === 'many_to_one' && r.target_object !== o.id && byId.has(r.target_object)) {
      if (!rawChildren.has(r.target_object)) rawChildren.set(r.target_object, []);
      rawChildren.get(r.target_object).push(o.id);
      hasParent.add(o.id);
    }
  }));
  const placed = new Set();
  const childrenOf = new Map();
  const walk = id => {
    (rawChildren.get(id) || []).forEach(cid => {
      if (placed.has(cid)) return;
      placed.add(cid);
      if (!childrenOf.has(id)) childrenOf.set(id, []);
      childrenOf.get(id).push(cid);
      walk(cid);
    });
  };
  const roots = [];
  const enterRoot = id => {
    if (placed.has(id)) return;
    placed.add(id);
    roots.push(id);
    walk(id);
  };
  objs.filter(o => !hasParent.has(o.id)).forEach(o => enterRoot(o.id));
  // 仅由环可达的节点（无树根可挂）提升为根节点，确保不丢失
  objs.forEach(o => { if (!placed.has(o.id)) enterRoot(o.id); });
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
  renderInstanceList();
  // 选中对象即切到对象视图，保证用户看到的就是该对象的元数据 / 实例列表 / 导入入口
  setIngestTab('object');
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

// 属性约束摘要（只读视图"约束"列）：必填 + 长度/正则/取值范围/枚举
function constraintSummary(a) {
  const c = a.constraints || {};
  const parts = [];
  if (a.required) parts.push('<span class="text-red-400">必填</span>');
  if (c.min_length != null || c.max_length != null) parts.push(esc('长度 ' + (c.min_length != null ? c.min_length : '?') + '–' + (c.max_length != null ? c.max_length : '?')));
  if (c.pattern) parts.push(esc('正则 ' + c.pattern));
  if (c.minimum != null || c.maximum != null) parts.push(esc('取值 [' + (c.minimum != null ? c.minimum : '−∞') + ', ' + (c.maximum != null ? c.maximum : '+∞') + ']'));
  if (c.enum && c.enum.length) parts.push(esc('枚举 ' + c.enum.join('/')));
  return parts.length ? parts.join(' · ') : '<span class="text-slate-600">—</span>';
}

function renderIngestObjectMeta(obj) {
  setObjectMetaActions(false);
  if (!obj) { $('ingestObjectMeta').innerHTML = '<div class="text-sm text-slate-400">点击左侧任意对象查看属性、关系与约束。</div>'; return; }
  const attrs = (obj.attributes || []).map(a => '<tr><td class="border border-slate-700 p-1">' + esc(a.id) + '</td><td class="border border-slate-700 p-1">' + esc(a.name || '-') + '</td><td class="border border-slate-700 p-1">' + esc(a.data_type || 'string') + '</td><td class="border border-slate-700 p-1">' + constraintSummary(a) + '</td></tr>').join('');
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

// 归化页对象编辑：属性约束录入行（折叠，输入始终在 DOM 中）
function ecConstraintRowHtml(a, i) {
  const c = a.constraints || {};
  const v = k => esc(c[k] != null ? String(c[k]) : '');
  return '<div class="ec-constraints p-1" data-eidx="' + i + '" style="display:none">' +
    '<div class="text-[10px] text-slate-500 mb-1">属性约束（可选）：长度/正则用于 string；最小/最大值用于 integer/float；枚举值须与数据类型匹配，逗号分隔</div>' +
    '<div class="grid grid-cols-12 gap-2">' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">最小长度</label><input class="w-full ec-c-minlen" data-eidx="' + i + '" value="' + v('min_length') + '" /></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">最大长度</label><input class="w-full ec-c-maxlen" data-eidx="' + i + '" value="' + v('max_length') + '" /></div>' +
      '<div class="col-span-4"><label class="text-[10px] text-slate-500">正则 pattern</label><input class="w-full ec-c-pattern" data-eidx="' + i + '" value="' + v('pattern') + '" /></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">最小值</label><input class="w-full ec-c-min" data-eidx="' + i + '" value="' + v('minimum') + '" /></div>' +
      '<div class="col-span-2"><label class="text-[10px] text-slate-500">最大值</label><input class="w-full ec-c-max" data-eidx="' + i + '" value="' + v('maximum') + '" /></div>' +
    '</div>' +
    '<div class="mt-1"><label class="text-[10px] text-slate-500">枚举值（逗号分隔）</label><input class="w-full ec-c-enum" data-eidx="' + i + '" value="' + (c.enum ? esc(c.enum.join(', ')) : '') + '" /></div>' +
  '</div>';
}

function toggleEcConstraints(i) {
  const row = $('ingestObjectMeta').querySelector('.ec-constraints[data-eidx="' + i + '"]');
  if (row) row.style.display = (row.style.display === 'none' || !row.style.display) ? 'block' : 'none';
}

function collectEcConstraints(i) {
  const box = $('ingestObjectMeta');
  const q = cls => { const el = box.querySelector('.' + cls + '[data-eidx="' + i + '"]'); return el ? el.value.trim() : ''; };
  const c = {};
  const mn = parseInt(q('ec-c-minlen'), 10); if (!isNaN(mn)) c.min_length = mn;
  const mx = parseInt(q('ec-c-maxlen'), 10); if (!isNaN(mx)) c.max_length = mx;
  const pat = q('ec-c-pattern'); if (pat) c.pattern = pat;
  const vmin = parseFloat(q('ec-c-min')); if (!isNaN(vmin)) c.minimum = vmin;
  const vmax = parseFloat(q('ec-c-max')); if (!isNaN(vmax)) c.maximum = vmax;
  const en = q('ec-c-enum');
  if (en) {
    const dtEl = $('edit_attr_type_' + i);
    const dt = dtEl ? dtEl.value : 'string';
    c.enum = en.split(/[,，]/).map(s => s.trim()).filter(Boolean).map(s =>
      (dt === 'integer' && /^-?\d+$/.test(s)) ? parseInt(s, 10) :
      (dt === 'float' && /^-?\d*\.?\d+$/.test(s)) ? parseFloat(s) : s);
  }
  return c;
}

function renderIngestObjectEditor(obj) {
  const attrRows = (obj.attributes || []).map((a, i) => {
    const hasC = a.constraints && Object.keys(a.constraints).length > 0;
    return '<tr>' +
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
      '<td class="border border-slate-700 p-1 text-center"><span class="cursor-pointer text-sky-400 text-xs hover:underline" onclick="toggleEcConstraints(' + i + ')">约束' + (hasC ? ' <span class="text-amber-400">●</span>' : '') + '</span></td>' +
      '<td class="border border-slate-700 p-1"><button class="btn secondary text-xs" onclick="removeEditAttr(' + i + ')">删</button></td>' +
    '</tr>' +
    '<tr class="bg-slate-900/30"><td colspan="6" class="border border-slate-700 p-0">' + ecConstraintRowHtml(a, i) + '</td></tr>';
  }).join('');

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
    '<table class="w-full text-xs border-collapse"><thead><tr><th class="border border-slate-700 p-1 text-left">ID</th><th class="border border-slate-700 p-1 text-left">名称</th><th class="border border-slate-700 p-1 text-left">类型</th><th class="border border-slate-700 p-1 text-center">必填</th><th class="border border-slate-700 p-1 text-center">约束</th><th class="border border-slate-700 p-1"></th></tr></thead><tbody>' + attrRows + '</tbody></table>' +
    '<div class="flex justify-between items-center mb-1 mt-3"><strong class="text-sm text-slate-300">关系</strong><button class="btn secondary text-xs" onclick="addEditRel()">+ 关系</button></div>' +
    '<table class="w-full text-xs border-collapse"><thead><tr><th class="border border-slate-700 p-1 text-left">ID</th><th class="border border-slate-700 p-1 text-left">名称</th><th class="border border-slate-700 p-1 text-left">目标对象</th><th class="border border-slate-700 p-1 text-left">基数</th><th class="border border-slate-700 p-1"></th></tr></thead><tbody>' + relRows + '</tbody></table>' +
    // 底部保存入口：属性多/约束折叠区长时，无需回到卡片顶部找「保存」确认
    '<div class="flex gap-2 items-center mt-3"><button class="btn success text-xs" onclick="saveObjectMeta()">保存</button><span class="text-[10px] text-slate-500">修改属性/约束后须点保存才会生效</span></div>';
  $('ingestObjectMeta').innerHTML = html;
}

// 结构操作（增删属性/关系）会整体重渲染编辑器，约束输入框按 clone 旧值回填——
// 若 DOM 中已有未保存的录入值会被静默冲掉（"约束已更新"不名符其实）。
// 故任何重渲染前先把 DOM 约束值回写 clone。
function syncEcConstraintsToClone() {
  if (!editingObjectClone) return;
  (editingObjectClone.attributes || []).forEach((a, i) => {
    a.constraints = collectEcConstraints(i);
  });
}

function addEditAttr() {
  if (!editingObjectClone) return;
  syncEcConstraintsToClone();
  editingObjectClone.attributes = editingObjectClone.attributes || [];
  editingObjectClone.attributes.push({ id: 'attr_new_' + editingObjectClone.attributes.length, name: '', data_type: 'string', required: false, constraints: {} });
  renderIngestObjectEditor(editingObjectClone);
}

function removeEditAttr(i) {
  if (!editingObjectClone) return;
  syncEcConstraintsToClone();
  editingObjectClone.attributes.splice(i, 1);
  renderIngestObjectEditor(editingObjectClone);
}

function addEditRel() {
  if (!editingObjectClone) return;
  syncEcConstraintsToClone();
  editingObjectClone.relations = editingObjectClone.relations || [];
  editingObjectClone.relations.push({ id: 'rel_new_' + editingObjectClone.relations.length, name: '', target_object: '', cardinality: 'N:1' });
  renderIngestObjectEditor(editingObjectClone);
}

function removeEditRel(i) {
  if (!editingObjectClone) return;
  syncEcConstraintsToClone();
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
      // 从约束录入行读回（不再恒透传 clone 旧值），让"对象约束已更新"名副其实
      constraints: collectEcConstraints(i),
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
    updateIngestTabBadges();
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
    // V2-05 RBAC（API-INTERFACE-DOC §21.3）：批准入库/驳回/恢复待审核属 review 动作（admin/reviewer）；
    // 编辑/删除候选属 write 动作（admin/modeler）。与后端 REVIEW_POST_RE 强制一致，
    // 越权操作无入口（TCE2E-06 负向断言）；dev/未启用鉴权（无会话）不限制。
    const me = _meUser();
    const canReview = !me || me.role === 'admin' || me.role === 'reviewer';
    const canWrite = !me || me.role === 'admin' || me.role === 'modeler';
    let actions = '';
    if (isPending) {
      if (canReview) {
        actions += '<button class="btn text-xs" onclick="approveCandidate(\'' + cid + '\')">批准入库</button>';
      }
      if (canWrite) {
        actions += '<button class="btn secondary text-xs" onclick="startReviewCandidate(\'' + cid + '\')">编辑</button>';
      }
      if (canReview) {
        actions += '<button class="btn secondary text-xs" onclick="rejectCandidate(\'' + cid + '\')">驳回</button>';
      }
    } else if (isRejected) {
      actions = '<button class="btn secondary text-xs" onclick="startReviewCandidate(\'' + cid + '\')">查看</button>';
      if (canReview) {
        actions += '<button class="btn secondary text-xs" onclick="revertCandidate(\'' + cid + '\')">恢复待审核</button>';
      }
      if (canWrite) {
        actions += '<button class="btn secondary text-xs" style="background:#991b1b" onclick="deleteCandidateRow(\'' + cid + '\')">删除</button>';
      }
    } else if (isImported) {
      actions = '<button class="btn secondary text-xs" onclick="startReviewCandidate(\'' + cid + '\')">查看</button>';
      if (canWrite) {
        actions += '<button class="btn secondary text-xs" style="background:#991b1b" onclick="deleteCandidateRow(\'' + cid + '\')">删除</button>';
      }
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

const _BATCH_BTN_IDS = ['btnBatchApprove', 'btnBatchReject', 'btnBatchRestore', 'btnBatchDelete'];
const _BATCH_BTN_BY_ACTION = { approve: 'btnBatchApprove', reject: 'btnBatchReject', restore: 'btnBatchRestore', delete: 'btnBatchDelete' };

// 批量操作 busy 态：禁用全部批量按钮（防重复提交），当前按钮显示进度文案；.btn:disabled 自带半透明样式
function _setBatchBusy(action, text) {
  _BATCH_BTN_IDS.forEach(id => { const b = $(id); if (b) b.disabled = true; });
  const b = $(_BATCH_BTN_BY_ACTION[action]);
  if (b) { if (!b.dataset.origText) b.dataset.origText = b.textContent; b.textContent = text + '…'; }
}
function _clearBatchBusy() {
  _BATCH_BTN_IDS.forEach(id => {
    const b = $(id);
    if (!b) return;
    b.disabled = false;
    if (b.dataset.origText) { b.textContent = b.dataset.origText; delete b.dataset.origText; }
  });
}

async function batchCandidates(action) {
  const ids = _checkedCandidateIds();
  if (!ids.length) return toast('请先勾选要操作的候选行', 'info');
  const labels = { approve: '批量批准', reject: '批量驳回', restore: '批量恢复待审核', delete: '批量删除' };
  if (action === 'delete' && !confirm('确认删除选中的 ' + ids.length + ' 组候选？此操作不可恢复。')) return;
  _setBatchBusy(action, labels[action] + '中（共 ' + ids.length + ' 组）');
  toast(labels[action] + '处理中（共 ' + ids.length + ' 组）…', 'info', 2500);
  try {
    if (action === 'approve' || action === 'reject') {
      const j = await api('/api/ingest/candidates/batch-' + action, { method: 'POST', body: JSON.stringify({ candidate_ids: ids }) });
      const d = j.data || {};
      toast(labels[action] + '完成：生效 ' + (d.affected || 0) + ' 组' + (d.skipped ? '，跳过 ' + d.skipped + ' 组（状态不符）' : ''), 'ok');
    } else {
      let done = 0, failed = 0;
      for (const id of ids) {
        _setBatchBusy(action, labels[action] + '（' + (done + failed + 1) + '/' + ids.length + '）');
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
  finally { _clearBatchBusy(); }
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

// V2-21 / M3-C：批次健康卡（入库时点观测指标，人工确认卡展示；不阻断审批）
function renderBatchHealthCard(c) {
  const h = c.health;
  if (!h || h.error) {
    return '<div class="text-xs text-slate-600 mb-2">批次健康卡：' + esc((h && h.error) || '未计算（存量候选可经 GET /candidates/{id}/health 补算）') + '</div>';
  }
  const items = ['批次 ' + h.batch_size + (h.stable_key ? '（键 ' + esc(h.stable_key) + '）' : '（未定义主键）')];
  if (h.key_missing_count) items.push('<span class="text-amber-400">缺键 ' + h.key_missing_count + '</span>');
  if (h.key_dup_within_batch) items.push('<span class="text-rose-400">批内重复键 ' + h.key_dup_within_batch + '</span>'
    + (h.key_dup_groups && Object.keys(h.key_dup_groups).length ? '（' + esc(Object.keys(h.key_dup_groups).join(', ')) + '）' : ''));
  if (h.key_dup_with_existing && h.key_dup_with_existing.length) items.push('<span class="text-amber-400">与存量重复 ' + h.key_dup_with_existing.length + ' 键</span>');
  if (h.enum_violation_total) items.push('<span class="text-rose-400">枚举出域 ' + h.enum_violation_total + ' 处</span>');
  if (h.required_fill_rate !== null && h.required_fill_rate < 1) items.push('<span class="text-amber-400">必填填充率 ' + Math.round(h.required_fill_rate * 100) + '%</span>');
  const fr = h.freshness || {};
  items.push('新鲜度：' + (fr.basis === 'fetched_at' ? (fr.age_days === null ? '未知' : fr.age_days + ' 天') : '刚上传'));
  const bad = h.key_missing_count || h.key_dup_within_batch || (h.key_dup_with_existing && h.key_dup_with_existing.length) || h.enum_violation_total;
  return '<div class="mb-2 p-2 rounded border ' + (bad ? 'border-amber-500/40 bg-amber-500/5' : 'border-emerald-500/30') + ' text-xs space-y-0.5">'
    + '<div class="text-slate-400 font-medium">批次健康卡（V2-21 观测指标，不阻断审批）</div>'
    + '<div class="text-slate-500">' + items.join(' · ') + '</div>'
    + (h.enum_violations && Object.keys(h.enum_violations).length
      ? '<div class="text-rose-400/80">出域明细：' + Object.entries(h.enum_violations).map(([aid, d]) => esc(d.name || aid) + '=' + esc((d.values || []).join('/'))).join('、') + '</div>' : '')
    + '</div>';
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
  const content = readOnlyNote + renderBatchHealthCard(c) + '<div class="space-y-2 max-h-[420px] overflow-auto mb-3">' + rows + '</div>' +
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

// 实例统一取「本体全局缓存」：左侧树各对象实例数、关系血缘、对象视图列表、全部实例列表
// 都从同一份全量数据本地过滤。旧实现按选中对象请求会丢全量数据，导致树计数失真、
// 跨对象血缘查不到实例；现在一次拉全，切换对象/搜索/过滤零请求
// 按 total 循环翻页，把某本体的实例全量拉到本地（修复「单页截断」：单页最多 pageSize 条，
// 实例数超过单页上限时只取第 1 页会让其余对象计数恒为 0、total 失真）。
// 达到 max（默认 5000）条时截断并提示改用筛选，避免超大集一次性拉满阻塞前端。
async function fetchAllInstances(ontologyId, max) {
  if (max == null) max = 5000;
  const PAGE_SIZE = 200;
  const items = [];
  let page = 1;
  let total = Infinity;
  while (items.length < total) {
    const j = await api('/api/ingest/instances?ontology_id=' + encodeURIComponent(ontologyId) +
      '&page=' + page + '&pageSize=' + PAGE_SIZE);
    total = (j.data && j.data.total) || 0;
    const batch = (j.data && j.data.items) || [];
    items.push(...batch);
    page++;
    if (!batch.length) break;                 // 空页兜底，防止异常 total 导致死循环
    if (items.length >= max) {
      if (total > max) toast('实例共 ' + total + ' 条，超出 ' + max + ' 条加载上限，仅加载前 ' + max + ' 条；请用搜索 / 对象过滤缩小范围', 'warn');
      break;
    }
  }
  return items;
}

async function loadInstances() {
  const ontologyId = $('ingestOntology').value;
  if (!ontologyId) {
    state.instances = [];
    renderIngestTarget(); renderInstanceList(); renderAllInstanceList(); updateIngestTabBadges();
    return;
  }
  try {
    state.instances = await fetchAllInstances(ontologyId);
    // 若详情弹层正开着，刷新其指向为最新记录
    if (state.selectedInstance) {
      const cur = state.instances.find(x => x.id === state.selectedInstance.id);
      if (cur) state.selectedInstance = cur;
    }
    renderIngestTarget();
    renderInstanceList();
    renderAllInstanceList();
    updateIngestTabBadges();
  } catch (e) { toast('加载实例失败: ' + e.message, 'err'); }
}

// 对象视图：当前选中对象的实例列表（从全局缓存过滤，切换对象即时刷新）
function renderInstanceList() {
  const el = $('instanceList');
  if (!el) return;
  const countEl = $('instanceCount');
  const obj = state.selectedIngestObject;
  const list = (state.instances || []).filter(it => !obj || it.object_type === obj.id);
  const scope = obj ? '「' + (obj.name || obj.id) + '」' : '全部对象';
  if (countEl) countEl.textContent = scope + ' 共 ' + list.length + ' 条';
  if (!list.length) {
    if (!(state.ontologies || []).length) {
      el.innerHTML = '<div class="text-sm text-slate-500">当前项目暂无本体，因此没有已入库实例。请先在「建模」页创建本体，或切换其他项目。</div>';
    } else if (!obj) {
      el.innerHTML = '<div class="text-sm text-slate-500">请先在左侧树中选择一个对象，此处展示该对象已入库的实例。</div>';
    } else {
      el.innerHTML = '<div class="text-sm text-slate-500">对象「' + esc(obj.name || obj.id) + '」暂无已入库实例。使用下方「导入 / 添加实例」上传文件或粘贴记录，或到「导入与审核」页做多对象批量导入。</div>';
    }
    return;
  }
  el.innerHTML = list.map(inst => _instanceRowHtml(inst, false)).join('');
}

// 全部实例 tab：全局列表 + 关键字搜索 + 对象类型过滤
function renderAllInstanceList() {
  const el = $('allInstanceList');
  if (!el) return;
  const countEl = $('allInstanceCount');
  const sel = $('allInstanceFilter');
  // 对象过滤下拉与当前本体的对象集合同步（本体切换后自动重置为全部）
  if (sel) {
    const ont = state.ontologies.find(o => o.id === $('ingestOntology').value);
    const objs = ont ? (ont.objects || []) : [];
    const cur = sel.value;
    sel.innerHTML = '<option value="">全部对象</option>' +
      objs.map(o => '<option value="' + esc(o.id) + '">' + esc(o.name || o.id) + '</option>').join('');
    if (cur && objs.some(o => o.id === cur)) sel.value = cur;
    else { sel.value = ''; state.allInstanceObjectFilter = ''; }
  }
  const q = (state.allInstanceQ || '').trim().toLowerCase();
  const objF = state.allInstanceObjectFilter || '';
  const list = (state.instances || []).filter(it => {
    if (objF && it.object_type !== objF) return false;
    if (!q) return true;
    const hay = JSON.stringify({ id: it.id || it.node_id, t: it.object_type, d: it.instance || it.props || it }).toLowerCase();
    return hay.includes(q);
  });
  if (countEl) countEl.textContent = '共 ' + list.length + ' / ' + (state.instances || []).length + ' 条';
  if (!list.length) {
    el.innerHTML = '<div class="text-sm text-slate-500">' + ((state.instances || []).length
      ? '没有符合搜索 / 过滤条件的实例。'
      : '当前本体暂无已入库实例，先到「导入与审核」页上传数据。') + '</div>';
    return;
  }
  el.innerHTML = list.map(inst => _instanceRowHtml(inst, true)).join('');
}

// 实例行（对象视图 / 全部实例共用）；global 模式下额外显示对象类型，避免混淆
function _instanceRowHtml(inst, global) {
  const data = inst.instance || inst.props || inst;
  const preview = Object.entries(data)
    .filter(([k]) => !String(k).startsWith('_'))
    .slice(0, 5)
    .map(([k, v]) => esc(k) + '=' + esc(typeof v === 'object' ? JSON.stringify(v) : v))
    .join('　');
  const active = state.selectedInstance?.id === inst.id ? 'border-amber-500/60 bg-slate-800/60' : 'border-slate-700/50 hover:border-slate-600';
  return '<div class="p-2 rounded-lg border ' + active + ' cursor-pointer" onclick="selectInstance(' + JSON.stringify(inst).replace(/"/g, '&quot;') + ')">' +
    '<div class="flex justify-between items-center"><span class="text-sm font-medium">' + esc(data.name || data.title || inst.id || inst.node_id || '') + (global ? ' <span class="badge">' + esc(inst.object_type || '') + '</span>' : '') + '</span>' +
    '<span class="text-xs text-slate-500">' + esc((inst.id || inst.node_id || '').slice(0, 16)) + '</span></div>' +
    '<div class="text-xs text-slate-400 mt-1 truncate">' + preview + '</div></div>';
}

// 实例详情弹层：懒创建一次，供左树节点 / 对象视图列表 / 全部实例列表三处入口共用。
// 旧版是页面底部内联面板 + scrollIntoView，点击实例会把浏览位置带到页面底，
// 且与全局卡片混排；改为弹层后列表上下文保持不动，看完即关
function _ensureInstanceModal() {
  let m = $('instanceModal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'instanceModal';
    m.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/60 hidden';
    m.innerHTML =
      '<div class="bg-slate-900 border border-slate-700 rounded-xl shadow-2xl w-[720px] max-w-[95vw] max-h-[88vh] flex flex-col">' +
      '  <div class="flex justify-between items-start p-4 border-b border-slate-800">' +
      '    <div><h3 id="instanceModalTitle" class="font-medium"></h3>' +
      '    <div id="instanceModalMeta" class="text-xs text-slate-500 mt-1"></div></div>' +
      '    <button class="btn secondary text-xs" onclick="closeInstanceDetail()">关闭 ✕</button>' +
      '  </div>' +
      '  <div id="instanceDetailBody" class="p-4 overflow-y-auto space-y-3"></div>' +
      '  <div class="flex gap-2 p-4 border-t border-slate-800 justify-end">' +
      '    <button class="btn" style="background:#991b1b" onclick="deleteInstance()">删除</button>' +
      '    <button class="btn secondary" onclick="closeInstanceDetail()">关闭</button>' +
      '    <button class="btn success" onclick="saveInstanceEdit()">保存</button>' +
      '  </div>' +
      '</div>';
    document.body.appendChild(m);
    m.addEventListener('click', e => { if (e.target === m) closeInstanceDetail(); });
  }
  return m;
}

function renderInstanceDetail(inst) {
  _ensureInstanceModal().classList.remove('hidden');
  state.selectedInstance = inst;
  const data = inst.instance || inst.props || inst;
  const ontology = state.ontologies.find(o => o.id === inst.ontology_id) || {};
  const objDef = (ontology.objects || []).find(o => o.id === inst.object_type) || {};

  $('instanceModalTitle').innerHTML = esc(data.name || data.title || inst.id || inst.node_id || '实例详情') +
    ' <span class="badge">' + esc(inst.object_type || '') + '</span>';
  $('instanceModalMeta').textContent = (inst.id || inst.node_id || '') + ' · ' + (inst.object_type || '') +
    (inst.imported_at ? ' · 入库 ' + inst.imported_at : '');

  let html = '';

  // V2-06：上传型实例内容默认仅上传者可读——未开放者只见元数据，显式提示（不静默）
  if (inst.content_restricted) {
    html += '<div class="p-2 rounded border border-amber-700/50 bg-amber-900/20 text-xs text-amber-300">'
      + '🔒 内容受限：该上传型实例的内容默认仅上传者可读；当前账号未被开放范围包含，仅展示键/类型等元数据。</div>';
  }
  html += renderContentOpenPanel(inst);

  // 可编辑属性
  const attrRows = (objDef.attributes || []).map(a => {
    const val = data[a.id];
    const inputVal = (val === undefined || val === null) ? '' : String(val);
    const input = '<input class="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200" ' +
      'data-attr="' + esc(a.id) + '" value="' + esc(inputVal) + '" />';
    return '<tr><td class="border border-slate-700 p-1 text-slate-400">' + esc(a.name || a.id) + (a.required ? '*' : '') + '</td><td class="border border-slate-700 p-1">' + input + '</td></tr>';
  }).join('');
  html += '<div><label>属性（点击表格可直接编辑）</label><table class="w-full text-xs border-collapse" id="instanceEditTable">' + attrRows + '</table></div>';

  // 数据血缘（V2-08 细粒度血缘：六要素垂直链＋上游追溯，异步加载）
  html += renderLineagePanel(inst);

  // 关系血缘
  html += renderLineage(inst, objDef, ontology);
  $('instanceDetailBody').innerHTML = html;
  loadInstanceLineage(inst); // V2-08：血缘面板异步填充（失败回退本地 _source 静态展示）
}

// ---- V2-06 / M2-C：上传型实例内容开放管理（仅上传者/admin 可见）----
function renderContentOpenPanel(inst) {
  const me = _meUser();
  const canManage = !me || me.role === 'admin' || (inst.uploaded_by || '') === me.username || !inst.uploaded_by;
  if (!canManage) {
    const opened = (inst.content_open?.users || []).join(', ') || '';
    return opened ? '<p class="text-[11px] text-slate-500">内容已开放给：' + esc(opened) + '</p>' : '';
  }
  if (!inst.uploaded_by && !(inst.instance && Object.keys(inst.instance).length)) return '';
  const co = inst.content_open || {};
  return '<div><label>内容开放管理（V2-06：默认仅上传者可读；上限=本体共享等级可见范围）</label>'
    + '<div class="text-[11px] text-slate-500 mb-1">当前开放：用户 [' + esc((co.users || []).join(', ')) + '] 部门 [' + esc((co.departments || []).join(', ')) + '] · 上传者：' + esc(inst.uploaded_by || '-') + '</div>'
    + '<div class="flex gap-2 items-center">'
    + '<input id="contentOpenUsers" class="flex-1 bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs" placeholder="开放给用户名，逗号分隔" />'
    + '<button class="btn text-xs" onclick="saveContentOpen(false)">开放</button>'
    + '<button class="btn secondary text-xs" onclick="saveContentOpen(true)">收回</button>'
    + '</div></div>';
}

async function saveContentOpen(revoke) {
  const inst = state.selectedInstance;
  if (!inst) return;
  const users = ($('contentOpenUsers').value || '').split(/[,，]/).map(s => s.trim()).filter(Boolean);
  if (!users.length) { toast('请输入至少一个用户名', 'warn'); return; }
  try {
    const path = revoke ? 'content-revoke' : 'content-open';
    await api('/api/ingest/instances/' + inst.id + '/' + path, { method: 'POST', body: JSON.stringify({ users }) });
    toast(revoke ? '已收回内容开放' : '已开放内容给指定用户', 'ok');
    // 重新拉取该实例并刷新弹层
    const j = await api('/api/ingest/instances/' + inst.id);
    const fresh = j.data;
    const idx = (state.instances || []).findIndex(x => x.id === inst.id);
    if (idx >= 0) state.instances[idx] = fresh;
    renderInstanceDetail(fresh);
  } catch (e) {
    const d = e.details || {};
    const extra = (d.out_of_cap_users?.length || d.invalid_users?.length)
      ? '（无效/超范围: ' + [].concat(d.invalid_users || [], d.out_of_cap_users || []).join(', ') + '）' : '';
    toast('操作失败: ' + e.message + extra, 'err', 6000);
  }
}

// ---- V2-08 细粒度血缘（M3-B）：统一血缘面板 ----
// 数据源与 API GET /api/lineage/instances/{id}、MCP lineage_get 同一装配（三通道一致）：
// 来源系统 / 源字段（经字典术语）/ 导入批次 / fetched_at / 转换规则（公式能力版本）/ 上游实例
function renderLineagePanel(inst) {
  return '<div><label>数据血缘 / 来源（V2-08 细粒度血缘）</label>'
    + '<div id="lineagePanel" class="text-xs text-slate-400">血缘加载中…</div></div>';
}

async function loadInstanceLineage(inst) {
  const panel = $('lineagePanel');
  if (!panel) return;
  const id = inst.id || inst.node_id;
  try {
    const j = await api('/api/lineage/instances/' + encodeURIComponent(id));
    panel.innerHTML = renderLineageData(j.data, inst);
  } catch (e) {
    // 血缘 API 不可用（如旧版本后端）回退本地 _source 静态展示，来源信息不静默丢失
    panel.innerHTML = renderSourceLineageFallback(inst);
  }
}

// ---- V2-21 / M3-C 数据健康度（四维 + 钻取 + 趋势；数据源同 MCP quality_health）----
let _qualitySnap = null;

function renderQualityOntologySelect() {
  const sel = $('qualityOntology');
  if (!sel) return;
  const active = (state.ontologies || []).filter(o => (o.status || 'active') === 'active');
  const prev = sel.value;
  sel.innerHTML = active.map(o => '<option value="' + o.id + '">' + esc(o.name) + '</option>').join('');
  if (prev && active.find(o => o.id === prev)) sel.value = prev;
}

function _qRate(v) {
  if (v === null || v === undefined) return '-';
  return (Math.round(v * 10000) / 100) + '%';
}

function _qCard(title, lines, tone) {
  const border = tone === 'bad' ? 'border-rose-500/40' : (tone === 'warn' ? 'border-amber-500/40' : 'border-slate-700/50');
  return '<div class="card p-3 border ' + border + '">'
    + '<div class="text-xs text-slate-400 mb-1">' + title + '</div>'
    + '<div class="text-sm text-slate-200 space-y-0.5">' + lines.join('') + '</div></div>';
}

function _qLine(k, v) { return '<div class="flex justify-between gap-2"><span class="text-slate-500">' + k + '</span><span class="font-mono">' + v + '</span></div>'; }

async function loadQuality() {
  const sel = $('qualityOntology');
  if (!sel || !sel.value) return;
  const box = $('qualitySummary');
  box.innerHTML = '<div class="text-xs text-slate-500 col-span-12">评估中…（源连接型实例会按治理身份采样读源）</div>';
  try {
    const j = await api('/api/quality/ontologies/' + encodeURIComponent(sel.value) + '/health');
    _qualitySnap = j.data;
    renderQualityHealth(j.data);
  } catch (e) {
    _qualitySnap = null;
    box.innerHTML = '<div class="text-xs text-rose-400 col-span-12">评估失败: ' + esc(e.message) + '</div>';
  }
}

function renderQualityHealth(d) {
  const s = d.summary || {};
  const scale = d.scale || {};
  const comp = s.completeness || {};
  const uniq = s.uniqueness || {};
  const conf = s.conformity || {};
  const time = s.timeliness || {};
  const staleRatio = time.stale_ratio;
  const cards = [];
  cards.push(_qCard('完整性', [
    _qLine('键填充率', _qRate(comp.key_fill_rate)),
    _qLine('必填填充率', _qRate(comp.required_fill_rate)),
    _qLine('关系解析率', _qRate(comp.relation_resolve_rate)),
    _qLine('规模', (scale.instance_count ?? 0) + ' 实例'),
  ], (comp.required_fill_rate !== null && comp.required_fill_rate < 1) ? 'warn' : 'ok'));
  cards.push(_qCard('唯一性', [
    _qLine('重复率', _qRate(uniq.dup_rate)),
    _qLine('缺键率', _qRate(uniq.missing_key_rate)),
    _qLine('通道', 'local_snapshot'),
  ], (uniq.dup_rate > 0) ? 'bad' : 'ok'));
  cards.push(_qCard('合规性', [
    _qLine('SHACL 违规率', conf.shacl_violation_rate === null ? '不可用' : _qRate(conf.shacl_violation_rate)),
    _qLine('违规数', conf.shacl_violation_count ?? '-'),
    _qLine('枚举命中率', _qRate(conf.enum_hit_rate)),
  ], (conf.shacl_violation_rate > 0) ? 'bad' : 'ok'));
  cards.push(_qCard('新鲜度', [
    _qLine('陈旧占比', staleRatio === null ? '-' : _qRate(staleRatio)),
    _qLine('陈旧实例', time.stale_instance_count ?? 0),
    _qLine('连接数', (time.connections || []).length),
  ], (staleRatio !== null && staleRatio > 0) ? 'warn' : 'ok'));
  $('qualitySummary').innerHTML = cards.join('');
  renderQualityObjects(d);
  renderQualityTimeliness(d);
  $('qualityDrill').innerHTML = '<span class="text-slate-500">点击对象行钻取属性级</span>';
  const cal = d.caliber || {};
  const note = $('qualityCaliberNote');
  if (note) note.innerHTML = '<div class="text-[11px] text-slate-500 mt-1">口径：constraint=' + esc(cal.constraint_hash || '-')
    + ' · dictionary=' + esc(cal.dictionary_hash || '-') + ' · ' + esc(cal.module_version || '-')
    + (d.is_snapshot === false ? ' · <span class="text-amber-400">即时计算（未记快照）</span>' : '') + '</div>';
}

function renderQualityObjects(d) {
  const objs = d.objects || {};
  const rows = Object.values(objs).map(m => {
    const u = m.uniqueness || {}, c = m.completeness || {}, f = m.conformity || {};
    return '<tr class="border-b border-slate-800 cursor-pointer hover:bg-slate-800/40" onclick="qualityDrill(\'' + esc(m.object_type) + '\')">'
      + '<td class="p-1 font-mono">' + esc(m.object_type) + '</td>'
      + '<td class="p-1">' + m.total + '</td>'
      + '<td class="p-1 ' + ((u.dup_rate || 0) > 0 ? 'text-rose-400' : 'text-slate-400') + '">' + _qRate(u.dup_rate) + '</td>'
      + '<td class="p-1">' + _qRate(c.key_fill_rate) + '</td>'
      + '<td class="p-1">' + _qRate(c.required_fill_rate) + '</td>'
      + '<td class="p-1">' + _qRate(c.relation_resolve_rate) + '</td>'
      + '<td class="p-1 ' + ((f.shacl_violation_rate || 0) > 0 ? 'text-rose-400' : 'text-slate-400') + '">'
      + (f.shacl_violation_rate === null ? '-' : _qRate(f.shacl_violation_rate)) + '</td>'
      + '<td class="p-1">' + _qRate(f.enum_hit_rate) + '</td>'
      + '<td class="p-1 text-[11px] text-slate-500">' + esc(m.value_channel || '-') + (m.coverage < 1 ? ' · 覆盖' + _qRate(m.coverage) : '') + '</td>'
      + '</tr>';
  }).join('');
  $('qualityObjects').innerHTML = '<table class="w-full text-xs">'
    + '<thead><tr class="text-slate-500 text-left">'
    + '<th class="p-1">对象</th><th class="p-1">规模</th><th class="p-1">重复率</th><th class="p-1">键填充</th><th class="p-1">必填填充</th><th class="p-1">关系解析</th><th class="p-1">SHACL违规</th><th class="p-1">枚举命中</th><th class="p-1">值通道</th>'
    + '</tr></thead><tbody>' + (rows || '<tr><td class="p-2 text-slate-500" colspan="9">无对象</td></tr>') + '</tbody></table>';
}

function qualityDrill(otype) {
  if (!_qualitySnap) return;
  const m = (_qualitySnap.objects || {})[otype];
  if (!m) return;
  const c = m.completeness || {}, f = m.conformity || {};
  let html = '<div class="text-xs text-slate-400 space-y-2">';
  html += '<div class="font-mono text-slate-300">' + esc(otype) + '（' + m.total + ' 实例，覆盖 ' + _qRate(m.coverage) + '）</div>';
  html += '<div><div class="text-slate-500 mb-1">必填属性填充率</div>';
  const req = c.required_detail || {};
  html += Object.keys(req).length ? Object.entries(req).map(([aid, d2]) =>
    _qLine(esc(d2.name || aid), _qRate(d2.fill_rate) + '（' + d2.filled + '/' + d2.total + '）')).join('')
    : '<div class="text-slate-600">无必填属性</div>';
  html += '</div>';
  html += '<div><div class="text-slate-500 mb-1">关系指向解析率</div>';
  const rel = c.relation_detail || {};
  html += Object.keys(rel).length ? Object.entries(rel).map(([rid, d2]) =>
    _qLine(esc(d2.name || rid) + ' → ' + esc(d2.target_object || '?'), d2.total ? _qRate(d2.resolve_rate) + '（' + d2.resolved + '/' + d2.total + '）' : '无值')).join('')
    : '<div class="text-slate-600">无关系</div>';
  html += '</div>';
  html += '<div><div class="text-slate-500 mb-1">SHACL 违规（按属性）</div>';
  const byAttr = f.shacl_by_attribute || {};
  html += f.shacl_available === false ? '<div class="text-amber-400">SHACL 引擎不可用（pyshacl 未安装），违规率未计算</div>'
    : (Object.keys(byAttr).length ? Object.entries(byAttr).map(([aid, d2]) =>
      _qLine(esc(aid), d2.count + ' 次')).join('') + '<div class="text-slate-600 mt-1">违规明细见审计/入库门禁（此处仅计数）</div>'
      : '<div class="text-emerald-400">无违规</div>');
  html += '</div>';
  html += '<div><div class="text-slate-500 mb-1">枚举值域命中</div>';
  const en = f.enum_detail || {};
  html += Object.keys(en).length ? Object.entries(en).map(([aid, d2]) =>
    _qLine(esc(d2.name || aid), _qRate(d2.hit_rate) + '（' + d2.hit + '/' + d2.total + (d2.out_of_enum && d2.out_of_enum.length ? '，出域: ' + esc(d2.out_of_enum.join(', ')) : '') + '）')).join('')
    : '<div class="text-slate-600">无枚举约束属性</div>';
  html += '</div>';
  if (m.sample && m.sample.errors && m.sample.errors.length) {
    html += '<div class="text-amber-400">采样错误（源端不可读部分按 source_required 明示，不臆测）：<br>'
      + m.sample.errors.map(esc).join('<br>') + '</div>';
  }
  html += '</div>';
  $('qualityDrill').innerHTML = html;
}

function renderQualityTimeliness(d) {
  const time = (d.summary || {}).timeliness || {};
  const conns = time.connections || [];
  if (!conns.length) {
    $('qualityTimeliness').innerHTML = '<div class="text-xs text-slate-600">无带来源取数时间的实例</div>';
    return;
  }
  const rows = conns.map(c =>
    '<div class="flex justify-between text-xs py-1 border-b border-slate-800">'
    + '<span class="text-slate-400">' + esc(c.connection_id) + (c.stale ? ' <span class="text-rose-400">陈旧</span>' : '') + '（' + c.instance_count + ' 实例）</span>'
    + '<span class="font-mono">' + (c.age_days === null ? '-' : c.age_days + ' 天') + ' / 阈值' + c.stale_threshold_days + '天</span></div>'
  ).join('');
  $('qualityTimeliness').innerHTML = rows;
}

async function qualityEvaluate() {
  const sel = $('qualityOntology');
  if (!sel || !sel.value) return;
  try {
    const j = await api('/api/quality/ontologies/' + encodeURIComponent(sel.value) + '/evaluate', { method: 'POST', body: JSON.stringify({}) });
    toast('已评估并记录快照（规模 ' + j.data.scale.instance_count + '）', 'ok');
    loadQuality();
    loadQualityTrend();
  } catch (e) { toast('评估失败: ' + e.message, 'err'); }
}

async function loadQualityTrend() {
  const sel = $('qualityOntology');
  const box = $('qualityTrend');
  if (!sel || !sel.value) { box.innerHTML = ''; return; }
  try {
    const j = await api('/api/quality/ontologies/' + encodeURIComponent(sel.value) + '/snapshots?limit=30');
    const items = (j.data.items || []).slice().reverse(); // 新→旧展示
    if (!items.length) {
      box.innerHTML = '<div class="text-xs text-slate-500">' + esc(j.data.baseline_hint || '尚无快照') + '</div>';
      return;
    }
    const rows = items.map(s => {
      const sum = s.summary || {}, cal = s.caliber || {};
      const badges = [];
      if (s.is_baseline) badges.push('<span class="text-sky-400">基线</span>');
      if (s.caliber_changed) badges.push('<span class="text-amber-400">口径变化</span>');
      if (s.scale_changed) badges.push('<span class="text-violet-400">规模变化</span>');
      if (s.caliber_changed) badges.push('<span class="text-slate-600">（约束/字典口径已变，不可与上一版直接比质量）</span>');
      return '<tr class="border-b border-slate-800">'
        + '<td class="p-1 font-mono text-slate-500">' + esc((s.computed_at || '').replace('T', ' ').slice(0, 19)) + '</td>'
        + '<td class="p-1">' + (s.scale || {}).instance_count + '</td>'
        + '<td class="p-1">' + _qRate((sum.completeness || {}).required_fill_rate) + '</td>'
        + '<td class="p-1">' + _qRate((sum.uniqueness || {}).dup_rate) + '</td>'
        + '<td class="p-1">' + ((sum.conformity || {}).shacl_violation_rate === null ? '-' : _qRate((sum.conformity || {}).shacl_violation_rate)) + '</td>'
        + '<td class="p-1">' + (((sum.timeliness || {}).stale_ratio === null) ? '-' : _qRate((sum.timeliness || {}).stale_ratio)) + '</td>'
        + '<td class="p-1 font-mono text-[10px] text-slate-600">' + esc((cal.constraint_hash || '').slice(0, 8)) + '/' + esc((cal.dictionary_hash || '').slice(0, 8)) + '</td>'
        + '<td class="p-1 text-[11px]">' + badges.join(' ') + '</td>'
        + '</tr>';
    }).join('');
    box.innerHTML = '<table class="w-full text-xs"><thead><tr class="text-slate-500 text-left">'
      + '<th class="p-1">时间</th><th class="p-1">规模</th><th class="p-1">必填填充</th><th class="p-1">重复率</th><th class="p-1">SHACL违规</th><th class="p-1">陈旧占比</th><th class="p-1">口径</th><th class="p-1">标注</th>'
      + '</tr></thead><tbody>' + rows + '</tbody></table>';
  } catch (e) {
    box.innerHTML = '<div class="text-xs text-rose-400">趋势加载失败: ' + esc(e.message) + '</div>';
  }
}

function renderLineagePanelData(d) {
  const t = (v) => (v === undefined || v === null || v === '') ? '-' : esc(String(v));
  let html = '';
  const s = d.source || {};
  const sys = s.system || {};
  const batch = s.batch;

  // 六要素概览
  html += '<table class="w-full text-xs border-collapse mb-2">';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">来源系统</td><td class="border border-slate-700 p-1 text-slate-200">'
    + (sys.display_name ? esc(sys.display_name) + ' <span class="text-slate-500">(' + esc(sys.connection_id || '') + ')</span>' : esc(sys.kind || '-')) + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">抽取时间</td><td class="border border-slate-700 p-1 text-slate-200">' + t(s.fetched_at) + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">导入批次</td><td class="border border-slate-700 p-1 text-slate-200">'
    + (batch
      ? esc((batch.candidate_id || '').slice(0, 16)) + ' · ' + esc(batch.source_type || '') + ' · ' + t(batch.created_at)
        + (batch.created_by ? ' · ' + esc(batch.created_by) : '') + ' · 状态 ' + esc(batch.status || '') + ' · ' + (batch.record_count || 0) + ' 条'
      : '无候选记录（手工/历史实例）') + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">原始引用</td><td class="border border-slate-700 p-1 text-slate-200">' + t(s.raw_ref) + '</td></tr>';
  html += '<tr><td class="border border-slate-700 p-1 text-slate-400">入库时间</td><td class="border border-slate-700 p-1 text-slate-200">' + t(d.instance && d.instance.imported_at) + '</td></tr>';
  html += '</table>';

  // 字段级血缘
  const fields = d.fields || [];
  if (fields.length) {
    html += '<div class="text-xs text-slate-300 mb-1">字段级血缘（源字段映射'
      + (d.field_map_source ? ' · 映射来源: ' + esc(d.field_map_source) : '') + '）</div>';
    html += '<table class="w-full text-xs border-collapse mb-2"><tr class="text-slate-500">'
      + '<td class="border border-slate-700 p-1">属性</td><td class="border border-slate-700 p-1">源字段</td>'
      + '<td class="border border-slate-700 p-1">来源</td><td class="border border-slate-700 p-1">字典术语（解析路径）</td></tr>';
    for (const f of fields) {
      const cap = f.capability || {};
      const via = f.via === 'capability'
        ? '<span class="text-amber-300">公式</span> ' + esc(cap.capability_name || cap.capability_id || '') + ' v' + esc(cap.capability_version || '')
        : '直接';
      const term = f.term
        ? esc(f.term.canonical || f.term.term_id || '') + (f.term.bound && f.term.system_field ? ' → 系统字段 ' + esc(f.term.system_field) : '')
        : '-';
      html += '<tr><td class="border border-slate-700 p-1 text-slate-200">' + esc(f.name || f.attribute) + '</td>'
        + '<td class="border border-slate-700 p-1 text-slate-200">' + t((f.source_field || []).join(', ')) + '</td>'
        + '<td class="border border-slate-700 p-1">' + via + '</td>'
        + '<td class="border border-slate-700 p-1 text-slate-300">' + term + '</td></tr>';
    }
    html += '</table>';
  }

  // 转换规则（M1-B 四要素溯源）
  const tr = d.transformation;
  if (tr) {
    const inputs = tr.inputs || {};
    const used = (inputs.used_inputs || []).map(i =>
      esc(String(i.attr)) + '=' + (i.value === null || i.value === undefined ? '∅' : esc(String(i.value)))
    ).join(', ');
    const gaps = (inputs.data_gaps || []).length ? ' · 数据缺口: ' + esc(inputs.data_gaps.join(', ')) : '';
    html += '<div class="text-xs text-slate-300 mb-1">转换规则（公式能力 · M1-B 四要素溯源）</div>';
    html += '<div class="text-xs border border-slate-700 rounded p-2 mb-2 bg-slate-800/40">'
      + '<div class="text-slate-200">能力: ' + esc(tr.capability_name || tr.capability_id || '') + ' · 版本 v' + esc(tr.capability_version || '')
      + ' · 状态: ' + esc(tr.execution_status || '') + (tr.stale ? ' · <span class="text-amber-400">stale（基线已变更未重算）</span>' : '') + '</div>'
      + '<div class="text-slate-400 mt-1">表达式: ' + esc(tr.expression || '-') + '</div>'
      + '<div class="text-slate-400 mt-1">输入原始值: ' + (used || '-') + gaps + '</div>'
      + '<div class="text-slate-400 mt-1">执行时间: ' + t(tr.executed_at) + ' · 通道: ' + esc(tr.execution_channel || '-')
      + (inputs.inputs_persisted === false ? ' · 输入未持久化（联邦读/输入覆盖）' : '') + '</div>'
      + '</div>';
  }

  // 上游实例（点击向上追溯）
  const ups = d.upstream || [];
  html += '<div class="text-xs text-slate-300 mb-1">上游实例（公式 lookup 命中，点击向上追溯）</div>';
  if (!ups.length) {
    html += '<div class="text-xs text-slate-500">无上游实例</div>';
  } else {
    html += '<div class="border-l-2 border-slate-600 ml-2 pl-3 space-y-2">';
    html += '<div class="text-xs text-slate-400">└─ 本实例: ' + esc((d.instance && d.instance.name) || d.instance_id)
      + ' <span class="badge">' + esc(d.object_type || '') + '</span></div>';
    for (const u of ups) {
      html += '<div class="text-xs border border-slate-700 rounded p-2 bg-slate-800/40 cursor-pointer hover:border-amber-500/60" onclick="openInstanceById(\'' + esc(u.instance_id) + '\')">'
        + '<div class="text-slate-200">↑ ' + esc(u.name || u.instance_id) + ' <span class="badge">' + esc(u.object_type || '') + '</span></div>'
        + '<div class="text-slate-400 mt-1">提供属性 ' + esc(u.attribute || '-')
        + (u.dims ? ' · 维度 ' + esc(JSON.stringify(u.dims)) : '')
        + ' · 原始值 ' + (u.value === null ? '∅' : esc(String(u.value)))
        + (u.matched_count ? ' · 命中 ' + u.matched_count + ' 条' : '') + '</div>'
        + '</div>';
    }
    html += '</div>';
  }
  return html;
}

// 上游实例点击向上追溯：按实例 ID 拉取最新记录并打开详情弹层
async function openInstanceById(id) {
  try {
    const j = await api('/api/ingest/instances/' + encodeURIComponent(id));
    selectInstance(j.data);
  } catch (e) {
    toast('打开上游实例失败: ' + e.message, 'err');
  }
}

// 回退展示：血缘 API 不可用时用本地 _source 静态信息（保持来源不静默丢失）
function renderSourceLineageFallback(inst) {
  const src = (inst.instance || inst.props || inst)._source || inst._source || {};
  let html = '<table class="w-full text-xs border-collapse">';
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
  const current = { ...(inst.instance || inst.props || inst) };
  const patch = {};
  inputs.forEach(input => {
    const attrId = input.dataset.attr;
    const val = input.value.trim();
    const oldVal = current[attrId];
    const old = (oldVal === undefined || oldVal === null) ? '' : String(oldVal);
    if (val !== old) patch[attrId] = val === '' ? null : val;
  });
  if (!Object.keys(patch).length) { toast('无字段变更', 'ok'); return; }
  try {
    const j = await api('/api/ingest/instances/' + (inst.id || inst.node_id), {
      method: 'PATCH',
      body: JSON.stringify({ patch, reason: '前端字段级编辑' })
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
  const m = $('instanceModal');
  if (m) m.classList.add('hidden');
  renderIngestTarget();
  renderInstanceList();
  renderAllInstanceList();
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
// 默认 Streamable HTTP，type 值按客户端取：默认 "streamableHttp"（WorkBuddy/Cherry Studio/Cline，
// 与 docs/MCP接入配置-方案C静态Token.md §2.1 一致）；Claude Desktop/Cursor 把 type 改成 "http"；
// 只支持旧版 SSE 的客户端改成 "sse"，URL 不变。headers 带接入 token（静态 Bearer，无 OAuth）。
function renderMcpServerConfig() {
  const pre = $('mcpServerConfig');
  if (!pre) return;
  const project = state.currentProject || {};
  const projectId = project.id || '<project_id>';
  const url = mcpBaseUrl() + '/mcp/' + projectId;
  const serverName = (project.name || 'ontology').replace(/[^A-Za-z0-9_\u4e00-\u9fa5-]/g, '').slice(0, 24) || 'ontology';
  const token = (mcpToken && mcpToken.token) || '<在上方生成接入 token 后自动填入>';
  const cfg = { type: 'streamableHttp', url, headers: { Authorization: 'Bearer ' + token } };
  const text = JSON.stringify({ mcpServers: { [serverName]: cfg } }, null, 2);
  pre.textContent = text;
  const btn = $('mcpCopyConfigBtn');
  if (btn) btn.onclick = () => copyText(text, 'MCP Server 配置');
  const hint = $('mcpConfigHint');
  if (hint) {
    const meta = (mcpList && mcpList.meta) || {};
    if (!meta.server_published) {
      hint.textContent = '当前项目尚未发布本体，端点 ' + url + ' 暂不可用（连接返回 404 NOT_PUBLISHED）——先在上方清单发布任一本体即可生效，本配置无需改动。';
    } else if (!(mcpToken && mcpToken.configured)) {
      hint.textContent = '端点已在线（已发布 ' + (meta.published_count || 0) + ' 个本体），但接入 token 未生成——先在上方生成 token，本配置会自动带上。';
    } else {
      hint.textContent = '端点在线：' + url + '（已发布 ' + (meta.published_count || 0) + ' 个本体，token 已内置）';
    }
  }
}

// ---- MCP 接入 token（P0-2：外部客户端连接鉴权） ----
let mcpToken = { configured: false, token: null, created_at: null };

async function loadMcpToken() {
  try {
    const j = await api('/api/mcp/server/token');
    mcpToken = j.data || { configured: false, token: null, created_at: null };
    renderMcpTokenLine();
    renderMcpServerConfig();
  } catch (e) { /* 静默：token 区保持原样 */ }
}

function renderMcpTokenLine() {
  const el = $('mcpTokenLine');
  if (!el) return;
  const p = state.currentProject || {};
  if (!p.id) { el.innerHTML = ''; return; }
  if (!mcpToken.configured) {
    el.innerHTML = '接入 token：<span class="text-amber-400">未生成</span>（未配置 token 时外部客户端连接端点一律 401）' +
      '<button type="button" class="btn secondary text-xs ml-2" onclick="mcpGenToken()">生成 / 轮换 Token</button>';
  } else {
    el.innerHTML = '接入 token（生成于 ' + esc(mcpToken.created_at || '-') + '）' +
      '<code class="mx-1 text-slate-300 break-all">' + esc(mcpToken.token || '') + '</code>' + copyIcon(mcpToken.token || '', 'MCP token') +
      '<button type="button" class="btn secondary text-xs ml-2" onclick="mcpGenToken()">轮换</button>' +
      '<button type="button" class="btn secondary text-xs" onclick="mcpRevokeToken()">吊销</button>' +
      '<span class="ml-1 text-slate-500">轮换/吊销后旧 token 立即失效，已接入的 AI 平台需同步更新</span>';
  }
}

async function mcpGenToken() {
  try {
    await api('/api/mcp/server/token', { method: 'POST', body: '{}' });
    toast('已生成新 token（旧 token 失效）', 'ok');
    loadMcpToken();
  } catch (e) { toast('生成 token 失败: ' + e.message, 'err'); }
}

async function mcpRevokeToken() {
  if (!confirm('吊销接入 token？吊销后外部客户端连接本项目 MCP 端点将全部 401。')) return;
  try {
    await api('/api/mcp/server/token/revoke', { method: 'POST', body: '{}' });
    toast('已吊销 token', 'ok');
    loadMcpToken();
  } catch (e) { toast('吊销失败: ' + e.message, 'err'); }
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
  const tok = (mcpToken && mcpToken.token) || '<token>';
  const text = 'Streamable HTTP 端点（推荐，AI 平台配置的 url 填这个，需带 Authorization: Bearer <token>）: ' + base + '/mcp/' + projectId +
    '\nSSE 兼容端点（仅旧版客户端：GET 此 URL 建立长连接，消息走下方 POST，AI 平台配置 type 用 "sse"）: ' + base + '/mcp/' + projectId +
    '\nSSE 消息端点（握手后客户端自动使用，无需手填）: ' + base + '/mcp/' + projectId + '/messages/' +
    '\n当前项目 token（轮换后失效，以 MCP 接入 token 行为准）: ' + tok +
    '\n\n' + JSON.stringify(example, null, 2);
  $('mcpExample').textContent = text;
}

// ---- 图谱 ----
function setGraphTab(t) {
  state.graphTab = t;
  $('tabSchema').classList.toggle('bg-slate-700', t === 'schema');
  $('tabKnowledge').classList.toggle('bg-slate-700', t === 'knowledge');
  if ($('tabTree')) $('tabTree').classList.toggle('bg-slate-700', t === 'tree');
  loadGraph();
}

async function loadGraph() {
  const id = $('graphOntology').value;
  if (!id) {
    $('graphContainer').innerHTML = '<div class="p-8 text-center text-slate-500">当前项目下暂无本体，请先在「建模」页创建，或切换到包含本体的项目。</div>';
    return;
  }
  try {
    // 类树视图取 schema 数据（节点含 parent 字段），在前端按继承关系构树
    const mode = state.graphTab === 'tree' ? 'schema' : state.graphTab;
    // knowledge 视图按「实例上限」抽样（0=全部）；schema/tree 与实例上限无关
    let qs = '/api/graph/' + id + '?mode=' + mode;
    if (mode === 'knowledge') {
      const lim = $('graphInstLimit') ? $('graphInstLimit').value : '300';
      qs += '&max_instances=' + lim;
      if (lim === '0') toast('已选「全部」实例：节点较多时首屏可能卡顿数秒', 'info', 3000);
    }
    const j = await api(qs);
    const o = state.ontologies.find(x => x.id === id);
    if (o) state.currentOntology = o;
    if (state.graphTab === 'tree') drawTree(j.data);
    else drawGraph(j.data);
  } catch (e) { toast('加载图谱失败: ' + e.message, 'err'); }
}

// 「适应画布」：按当前节点位置重新 fit（drawGraph/drawTree 绘制时挂载 state.graphRefit）
function graphFitAll() {
  if (typeof state.graphRefit === 'function') state.graphRefit(true);
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
    // 后端显式返回 source：fallback 表示 LLM 不可达/返回非法，用的是确定性模板（只能近似列出对象实例）
    const fallbackHint = (d.source === 'fallback')
      ? '<div class="text-xs text-amber-400 mb-1">当前 LLM 未正确返回 SPARQL（不可达或超时），已使用本地规则生成兜底查询，结果仅为对象实例抽样，不包含问题中的条件过滤。</div>'
      : '';
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
    .map(e => ({ source: e.source, target: e.target, label: e.relation || e.label, origin: e.origin, kind: e.kind }));
  // 优先用 kind 字段，兼容旧 type 字段
  const nodeKind = d => d.kind || d.type || 'object';
  // 节点大小由关系数（度数）与实例数（指向它的 instance_of 边）决定，越多越大
  const relDeg = {}, instCnt = {};
  for (const e of links) {
    relDeg[e.source] = (relDeg[e.source] || 0) + 1;
    relDeg[e.target] = (relDeg[e.target] || 0) + 1;
    if (e.kind === 'instance_of') instCnt[e.target] = (instCnt[e.target] || 0) + 1;
  }
  const nodeRadius = d => {
    const rel = relDeg[d.id] || 0, inst = instCnt[d.id] || 0;
    if (nodeKind(d) === 'object') return Math.max(14, Math.min(38, 14 + 4 * Math.sqrt(rel) + 3 * Math.sqrt(inst)));
    return Math.max(6, Math.min(16, 6 + 2.5 * Math.sqrt(rel)));
  };
  // 力参数按图规模分档：孤立节点只有斥力没有吸引力，distanceMax 不限作用半径会被无限推远
  // （17 节点图被撑到 ~2000×1500，fit 后 scale≈0.3、标签全隐藏）；
  // forceX/forceY 提供真实向心弹簧（forceCenter 只整体平移、不收缩）
  const N = nodes.length;
  const chargeStrength = N > 800 ? -35 : N > 300 ? -60 : N > 80 ? -90 : -130;
  const chargeMaxDist = N > 300 ? 220 : 420;
  const simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id)
      .distance(d => d.kind === 'instance_of' ? (N > 300 ? 30 : 55) : 90)
      .strength(d => d.kind === 'instance_of' ? 0.9 : 0.5))
    .force('charge', d3.forceManyBody().strength(chargeStrength).distanceMax(chargeMaxDist))
    .force('x', d3.forceX(width / 2).strength(0.06))
    .force('y', d3.forceY(height / 2).strength(0.06))
    .force('collide', d3.forceCollide(d => nodeRadius(d) + (N > 300 ? 3 : 6)));
  if (N > 800) simulation.alphaDecay(0.05); // 大图提前收敛，减少首屏等待

  const g = svg.append('g');
  // 节点名显示策略：
  // 1) 对象节点（kind=object）始终显示（数量少，可读性优先——旧阈值下 fit 缩放后全部隐藏）；
  // 2) 大节点（基础半径 > R_BASE）默认显示名称；
  // 3) 视图放大后，屏幕半径（基础半径×缩放比）超过 R_ZOOM 的节点逐个显示名称；
  // 4) 其余小节点仅在鼠标悬停时显示名称。
  const R_BASE = 16, R_ZOOM = 9;
  let viewScale = 1;
  let hasFitted = false;
  let nodeLabelSel = null;
  const updateLabelVisibility = () => {
    if (nodeLabelSel) nodeLabelSel.style('opacity', d => nodeKind(d) === 'object' || nodeRadius(d) > R_BASE || nodeRadius(d) * viewScale > R_ZOOM ? 1 : 0);
  };
  const zoomBehavior = d3.zoom().scaleExtent([0.05, 8]).on('zoom', e => {
    g.attr('transform', e.transform);
    viewScale = e.transform.k;
    updateLabelVisibility();
  });
  svg.call(zoomBehavior);
  // 适配全图：放大上限 1.6（收拢后小图适度放大，字号可读）；
  // 首次 fit 直接应用（不走 transition，避免"先炸开再缩回"的观感）；
  // 「适应画布」按钮经 state.graphRefit 复用本函数（animate=true 时 300ms 过渡）
  const applyFit = animate => {
    if (!nodes.length) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of nodes) {
      const r = nodeRadius(n);
      minX = Math.min(minX, n.x - r); maxX = Math.max(maxX, n.x + r);
      minY = Math.min(minY, n.y - r); maxY = Math.max(maxY, n.y + r + 16);
    }
    const pad = 30;
    const scale = Math.max(0.05, Math.min(1.6, width / (maxX - minX + pad * 2), height / (maxY - minY + pad * 2)));
    const tx = width / 2 - scale * (minX + maxX) / 2;
    const ty = height / 2 - scale * (minY + maxY) / 2;
    const t = d3.zoomIdentity.translate(tx, ty).scale(scale);
    if (animate) svg.transition().duration(300).call(zoomBehavior.transform, t);
    else svg.call(zoomBehavior.transform, t);
  };
  // 布局稳定后首次自动适配：默认视图可见全部节点（之后用户缩放/平移不再重置）
  simulation.on('end', () => { if (!hasFitted) { hasFitted = true; applyFit(false); } });
  state.graphRefit = applyFit;
  // 抽样提示（knowledge 视图后端按 max_instances 截断时）
  if (data.mode === 'knowledge' && data.truncated) {
    const hint = document.createElement('div');
    hint.className = 'absolute top-2 left-1/2 -translate-x-1/2 z-10 px-3 py-1 rounded-full text-xs bg-slate-800/90 text-amber-300 border border-amber-500/40 pointer-events-none';
    hint.textContent = '已抽样显示 ' + data.shown + ' / ' + data.total_instances + ' 条实例（右上角「实例上限」可调）';
    container.appendChild(hint);
  }

  const link = g.append('g').selectAll('line').data(links).enter().append('line')
    .attr('stroke', '#64748b').attr('stroke-width', 1.5).attr('stroke-opacity', 0.6);
  const linkLabel = g.append('g').selectAll('text').data(links).enter().append('text')
    .text(d => d.label).attr('font-size', 10).attr('fill', '#94a3b8').attr('text-anchor', 'middle');
  const node = g.append('g').selectAll('g').data(nodes).enter().append('g').call(d3.drag()
    .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }));
  node.append('circle').attr('r', d => nodeRadius(d))
    .attr('fill', d => {
      const k = nodeKind(d);
      return k === 'object' ? '#4f8cff' : k === 'attribute' ? '#34d399' : '#f59e0b';
    });
  // 节点名：位于节点下方（描边光晕保证杂色背景上可读），显隐由 updateLabelVisibility 按阈值控制
  nodeLabelSel = node.append('text').attr('y', d => nodeRadius(d) + 12).attr('text-anchor', 'middle')
    .text(d => d.label || d.name || d.id).attr('font-size', 11).attr('fill', '#e2e8f0')
    .style('pointer-events', 'none')
    .style('paint-order', 'stroke').style('stroke', '#0f172a').style('stroke-width', 3).style('stroke-linejoin', 'round');
  updateLabelVisibility();

  const tip = $('graphTooltip');
  // tooltip 是容器（position:relative）内的 absolute 定位，必须用相对容器坐标；
  // pageX/pageY 是文档级坐标（含页头/标签栏高度），直接写入会偏移几百像素
  const moveTip = e => {
    const rect = container.getBoundingClientRect();
    tip.style.left = (e.clientX - rect.left + 12) + 'px';
    tip.style.top = (e.clientY - rect.top + 12) + 'px';
  };
  node.on('mouseover', (e, d) => {
    // 悬停时临时显示该节点名称（小节点平时隐藏）
    if (nodeLabelSel) nodeLabelSel.filter(l => l === d).style('opacity', 1);
    tip.style.opacity = 1;
    const k = nodeKind(d);
    tip.innerHTML = '<strong>' + esc(d.label || d.name || d.id) + '</strong><br/>type: ' + k + (d.source ? '<br/>source: ' + esc(d.source.id || d.source) : '') + (d.object_type ? '<br/>object_type: ' + esc(d.object_type) : '');
    moveTip(e);
  }).on('mousemove', moveTip)
    .on('mouseout', () => { tip.style.opacity = 0; updateLabelVisibility(); });

  simulation.on('tick', () => {
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
    linkLabel.attr('x', d => (d.source.x + d.target.x) / 2).attr('y', d => (d.source.y + d.target.y) / 2);
    node.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
  });
}

// 类树视图：按对象 parent（RDFS.subClassOf 继承）把对象折叠成树，点击节点展开/折叠。
// 数据来自 schema 图（节点含 parent 字段）；父不在本图谱节点集内（缺失占位/外部引用）的按根处理。
function drawTree(data) {
  const container = $('graphContainer');
  let width = container.clientWidth; // let：rAF 兜底重排时按真实尺寸重赋值
  let height = container.clientHeight || 500;
  container.innerHTML = '<div class="graph-tooltip" id="graphTooltip"></div>';
  const nodes = (data.nodes || []).filter(n => (n.kind || n.type) === 'object');
  if (!nodes.length) {
    container.innerHTML = '<div class="p-8 text-center text-slate-500">该本体暂无对象，无法生成类树。请先在「建模」页创建对象。</div>';
    return;
  }
  const byId = new Map(nodes.map(n => [n.id, n]));
  const children = new Map();
  const roots = [];
  for (const n of nodes) {
    if (n.parent && n.parent !== n.id && byId.has(n.parent)) {
      if (!children.has(n.parent)) children.set(n.parent, []);
      children.get(n.parent).push(n);
    } else {
      roots.push(n);
    }
  }
  // 多棵树挂到虚拟根下，共用一次 d3.tree 布局。
  // d.data 恒为原始节点本身（浅拷贝补 children），渲染统一读 d.data.id/label/kind；
  // children 须【递归】挂载——只挂根层会让深度 >=3 的节点（孙辈）整体丢失。
  const toHier = n => {
    const kids = children.get(n.id) || [];
    return { ...n, children: kids.length ? kids.map(toHier) : undefined };
  };
  const rootData = { id: '__root__', label: '', kind: 'virtual', children: (roots.length ? roots : [nodes[0]]).map(toHier) };
  const root = d3.hierarchy(rootData);
  // 默认展开 2 层，更深层级折叠（点节点展开），避免深树铺满画布
  root.descendants().forEach(d => {
    if (d.depth > 2 && d.children) { d._children = d.children; d.children = null; }
  });

  // 节点间距按内容自适应：纵向 28→32（矩形高 24，避免折叠徽章贴边）；
  // 深度间距按最长标签估算（中文 12px 字号 ≈ 12px/字），避免长对象名重叠
  const maxLabelChars = Math.max(...nodes.map(n => (n.label || n.id || '').length));
  const depthGap = Math.max(180, maxLabelChars * 12 + 80);
  const layout = d3.tree().nodeSize([32, depthGap]); // [垂直间距, 深度间距]（横向树）
  const svg = d3.select('#graphContainer').append('svg').attr('width', width).attr('height', height);
  const g = svg.append('g');
  // 初始 transform 交给 zoomIdentity 托管：d3.tree() 把根节点 x 归零，子节点 x 有正有负，
  // 手写 translate(90,16) 会让负坐标部分（~208px）跑出画布外；且 zoom 事件会整体覆盖
  // 手写值导致首次缩放"跳位"。必须经 zoom.transform 设置，居中量才会记入 d3 内部状态。
  const zoom = d3.zoom().scaleExtent([0.1, 4]).on('zoom', e => g.attr('transform', e.transform));
  svg.call(zoom);
  const tip = $('graphTooltip');

  // 按树包围盒居中（排除虚拟根）；横向树：屏幕 x=d.y，屏幕 y=d.x
  const refit = animate => {
    const desc = root.descendants().filter(d => d.data.kind !== 'virtual');
    if (!desc.length) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const d of desc) {
      minX = Math.min(minX, d.y); maxX = Math.max(maxX, d.y);
      minY = Math.min(minY, d.x); maxY = Math.max(maxY, d.x);
    }
    const pad = 40;
    const s = Math.max(0.1, Math.min(1,
      Math.min((width - pad * 2) / (maxX - minX + 200), // 200 ≈ 标签宽度预留
               (height - pad * 2) / (maxY - minY + 40))));
    const tx = width / 2 - s * (minX + maxX) / 2;
    const ty = height / 2 - s * (minY + maxY) / 2;
    const t = d3.zoomIdentity.translate(tx, ty).scale(s);
    if (animate) svg.transition().duration(300).call(zoom.transform, t);
    else svg.call(zoom.transform, t);
  };

  function update(animateRefit) {
    layout(root);
    const desc = root.descendants();
    // 过滤虚拟根的连线（否则 __root__ 会画出一条多余引线）
    const links = root.links().filter(l => l.source.data.kind !== 'virtual');
    g.selectAll('*').remove();
    g.selectAll('path').data(links).enter().append('path')
      .attr('fill', 'none')
      .attr('stroke', '#475569')
      .attr('stroke-width', 1.5)
      .attr('d', d3.linkHorizontal().x(d => d.y).y(d => d.x));
    const nodeG = g.selectAll('g.tree-node').data(desc, d => d.data.id).enter().append('g')
      .attr('class', 'tree-node')
      .attr('transform', d => 'translate(' + d.y + ',' + d.x + ')')
      .style('cursor', d => d.data.kind === 'virtual' ? 'default' : 'pointer')
      .on('click', (event, d) => {
        if (d.data.kind === 'virtual') return;
        if (d.children) { d._children = d.children; d.children = null; }
        else if (d._children) { d.children = d._children; d._children = null; }
        update(true); // 展开/折叠后自动 refit 回可视区
      })
      .on('mousemove', (event, d) => {
        if (d.data.kind === 'virtual') return;
        const n = d.data;
        const kids = d.children || d._children || [];
        tip.style.opacity = 1;
        tip.innerHTML = '<strong>' + esc(n.label || n.id) + '</strong><br/>' + esc(n.id) +
          (n.missing ? '<br/><span style="color:#fbbf24">被引用但未在当前本体定义</span>' : '') +
          '<br/>子对象: ' + (kids.length ? kids.map(k => esc(k.data.label || k.data.id)).join(', ') : '无') +
          '<br/><span style="color:#64748b">点击展开 / 折叠</span>';
        // 同知识图谱：tooltip 相对容器定位，须用 clientX/Y 减去容器 rect，不能用 pageX/pageY
        const rect = container.getBoundingClientRect();
        tip.style.left = (event.clientX - rect.left + 12) + 'px';
        tip.style.top = (event.clientY - rect.top + 10) + 'px';
      })
      .on('mouseout', () => { tip.style.opacity = 0; });
    nodeG.each(function(d) {
      const el = d3.select(this);
      if (d.data.kind === 'virtual') return;
      const missing = !!d.data.missing;
      el.append('rect')
        .attr('x', -7).attr('y', -12).attr('width', 14).attr('height', 24).attr('rx', 3)
        .attr('fill', missing ? '#1e293b' : '#0f172a')
        .attr('stroke', missing ? '#fbbf24' : '#38bdf8')
        .attr('stroke-dasharray', missing ? '3,2' : null)
        .attr('stroke-width', 1.5);
      if (d._children) el.append('circle').attr('cx', 11).attr('cy', -12).attr('r', 3.5).attr('fill', '#38bdf8');
      el.append('text')
        .attr('x', 18).attr('dy', '0.32em')
        .attr('fill', missing ? '#94a3b8' : '#e2e8f0')
        .attr('font-size', 12)
        .text((d.data.label || d.data.id) + (d._children ? ' +' + d._children.length : ''));
    });
    if (animateRefit) refit(true); else refit(false); // 每次布局后按包围盒居中
  }
  update(false);
  state.graphRefit = () => refit(true);
  // 容器尺寸兜底：刚切页时 clientWidth/Height 可能为 0（上面回退 500），rAF 后按真实尺寸重排一次
  if (container.clientWidth === 0 || container.clientHeight === 0) {
    requestAnimationFrame(() => {
      const w = container.clientWidth || 500, h = container.clientHeight || 500;
      if (w !== width || h !== height) { width = w; height = h; svg.attr('width', w).attr('height', h); }
      refit(false);
    });
  }
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
    // 业务人员可读为主（*_zh 中文推断），OWL2-RL 原始形式折叠在"查看原始规则"里供技术排查
    el.innerHTML = steps.length ? steps.map((s, i) =>
      '<div class="p-2 rounded-lg border border-slate-700/50 text-sm">' +
      '<div><span class="text-slate-500">#' + (i + 1) + '</span> <span class="text-slate-200 font-medium">' + esc(s.rule_zh || s.rule) + '</span></div>' +
      '<div class="text-xs text-slate-400 mt-1">依据: ' + esc((s.premises_zh || s.premises || []).join('；')) + '</div>' +
      '<div class="text-xs text-emerald-400 mt-0.5">推断: ' + esc(s.conclusion_zh || s.conclusion || '') + '</div>' +
      '<details class="mt-1"><summary class="text-[11px] text-slate-500 cursor-pointer select-none hover:text-slate-400">查看原始规则（OWL2-RL）</summary>' +
      '<pre class="text-[11px] text-slate-500 mt-1 whitespace-pre-wrap font-mono">' + esc(
        s.rule + '\n前提: ' + (s.premises || []).join('；') + '\n结论: ' + (s.conclusion || '')
      ) + '</pre></details>' +
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
    impactInstances = await fetchAllInstances(id);
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
    // key 已配置时预填明文到密码框（默认显示 ·······，点眼睛图标显示明文）；
    // 保存时仅当值确实变化才回传，改其他设置不会误触 key
    if (keyStatus.textContent.startsWith('已配置')) {
      try {
        const rev = await api('/api/llm/settings/reveal', { projectId: null });
        state.llmKeyLoaded = (rev.data && rev.data.api_key) || '';
        $('llmApiKey').value = state.llmKeyLoaded;
      } catch (_) { state.llmKeyLoaded = ''; }
    } else {
      state.llmKeyLoaded = '';
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
    // key 仅当用户确实修改过（非空且不同于预填值）才发送；否则 null = 不修改
    api_key: ($('llmApiKey').value.trim() && $('llmApiKey').value.trim() !== state.llmKeyLoaded) ? $('llmApiKey').value.trim() : null,
    prompts: {
      ontology_generation: $('promptOntologyGeneration').value,
      instance_extraction: $('promptInstanceExtraction').value,
      reverse_engineering: $('promptReverseEngineering').value,
      kg_construction: $('promptKgConstruction').value,
    }
  };
  try {
    await api('/api/llm/settings', { method: 'PUT', body: JSON.stringify(payload), projectId: null });
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
    const keyVal = $('llmApiKey').value.trim();
    const payload = {
      base_url: $('llmBaseUrl').value.trim() || undefined,
      model: $('llmModel').value.trim() || undefined,
      // 用户改过 key 时用新值测（否则后端用已保存的旧 key，测的不是要保存的配置）
      api_key: (keyVal && keyVal !== state.llmKeyLoaded) ? keyVal : undefined,
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
            '<div class="font-medium">' + esc(a.name || a.action_id) + ' ' + _actionRiskBadge(a.risk_level || 'high') + '</div>' +
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
            '<div class="font-medium">' + esc(a.name || a.action_id) + ' ' + _actionRiskBadge(a.risk_level || 'high') + '</div>' +
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

// ---- V2-13 回写风险分级（M3-E） ----
const _RISK_META = {
  low:    { label: '低风险', cls: 'ok', desc: '映射门禁通过即自动回写' },
  medium: { label: '中风险', cls: 'warn', desc: 'AI 三条件评估通过自动执行，否则升级人工确认' },
  high:   { label: '高风险', cls: 'err', desc: '必须人工确认（签核标识=身份+时间戳）' },
};
function _actionRiskBadge(level) {
  const m = _RISK_META[level] || _RISK_META.high;
  return '<span class="badge ' + m.cls + '" title="' + esc(m.desc) + '">' + m.label + '</span>';
}
function _isAdminUser() {
  const me = _meUser();
  return !me || me.role === 'admin'; // dev/未启用鉴权：不限制（与后端 bypass 一致）
}
async function adjustActionRisk() {
  const a = state.currentAction;
  if (!a) return;
  if (!_isAdminUser()) { toast('调整风险等级需要平台管理员权限', 'err'); return; }
  const current = a.risk_level || 'high';
  const opts = Object.entries(_RISK_META).map(([k, v]) =>
    k === current ? k + '（当前）' : k + '（' + v.label + '）').join('\n');
  const ans = prompt('将回写风险等级调整为（输入 low / medium / high）：\n' + opts + '\n\n当前: ' + current);
  if (!ans) return;
  const next = ans.trim().toLowerCase();
  if (!['low', 'medium', 'high'].includes(next)) { toast('无效的风险等级', 'err'); return; }
  const reason = prompt('调整原因（留痕审计）：', '') || '';
  try {
    await api('/api/actions/' + a.id + '/risk-level', {
      method: 'PUT', body: JSON.stringify({ risk_level: next, reason })
    });
    toast('风险等级已调整为 ' + next, 'ok');
    await loadActions();
    selectAction(a.id);
  } catch (e) { toast('调整失败: ' + e.message, 'err'); }
}
async function runActionReject() {
  if (!state.currentAction || !state.actionToken) { toast('没有待确认的执行令牌', 'err'); return; }
  const reason = prompt('拒绝原因（将随签核标识留痕）：', '业务方未准备好') || '';
  try {
    const j = await api('/api/actions/' + state.currentAction.id + '/reject', {
      method: 'POST', body: JSON.stringify({ confirmation_token: state.actionToken, reason })
    });
    const d = j.data || {};
    state.actionToken = null;
    $('actionPendingBox').classList.add('hidden');
    $('actionResultBox').classList.remove('hidden');
    $('actionResult').innerHTML = renderActionResult(d);
    toast('回写已拒绝并留痕', 'ok');
    loadActionLogs();
  } catch (e) { toast('拒绝失败: ' + e.message, 'err'); }
}
async function replayActionLog(logId) {
  const a = state.currentAction;
  if (!a) return;
  if (!confirm('以原 payload/幂等键重放该失败场景的回写？')) return;
  try {
    const j = await api('/api/actions/' + a.id + '/logs/' + logId + '/replay', { method: 'POST' });
    const d = j.data || {};
    $('actionResultBox').classList.remove('hidden');
    $('actionResult').innerHTML = renderActionResult(d);
    toast(d.executed ? '重放回写成功' : '重放未完成（outcome=' + (d.outcome || '?') + '）', d.executed ? 'ok' : 'err');
    loadActionLogs();
  } catch (e) { toast('重放失败: ' + e.message, 'err'); }
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
  if ($('actionRiskLevel')) $('actionRiskLevel').value = 'high';
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
  const riskBtn = _isAdminUser()
    ? '<button class="text-xs text-amber-400" onclick="adjustActionRisk()" title="V2-13：风险等级调整仅平台管理员，变更全程审计（防降级绕过）">调整风险等级</button>'
    : '';
  el.innerHTML =
    '<div class="flex items-start justify-between">' +
      '<div class="flex gap-3">' +
        '<div class="w-10 h-10 rounded-lg bg-slate-800 border border-slate-700 flex items-center justify-center text-xl">' + _actionIcon(a.category) + '</div>' +
        '<div>' +
          '<div class="font-medium text-base">' + esc(a.name || a.action_id) + ' ' + _actionRiskBadge(a.risk_level || 'high') + '</div>' +
          '<div class="text-xs text-slate-400 mt-1">' + _actionCategoryBadge(a.category) + ' · ' + esc(_actionKindLabel(a.invoke?.kind)) + ' · ' + esc(a.target_connection || '无连接') + '</div>' +
          (a.description ? '<div class="text-xs text-slate-500 mt-1">' + esc(a.description) + '</div>' : '') +
        '</div>' +
      '</div>' +
      '<div class="flex gap-3">' + riskBtn +
      '<button class="text-xs text-blue-400" onclick="editAction(\'' + a.id + '\')">编辑</button>' +
      '</div>' +
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
  if ($('actionRiskLevel')) $('actionRiskLevel').value = a.risk_level || 'high';
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
    risk_level: $('actionRiskLevel') ? $('actionRiskLevel').value : 'high',
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

const _OUTCOME_META = {
  success: { label: '回写成功', cls: 'ok' },
  simulated: { label: '回写成功（展示模式模拟）', cls: 'ok' },
  idempotency_hit: { label: '幂等命中（已生效，未重复回写）', cls: 'ok' },
  failed: { label: '回写失败（可重放）', cls: 'err' },
  gate_failed: { label: '映射门禁失败（未回写）', cls: 'err' },
  cancelled: { label: '回写已取消', cls: 'warn' },
  circuit_open: { label: '熔断开启（快速失败，未回写）', cls: 'warn' },
  awaiting_confirmation: { label: '待人工确认', cls: 'warn' },
  escalated_to_human: { label: 'AI 评估未通过，已升级人工确认', cls: 'warn' },
};
function renderActionResult(d) {
  const om = _OUTCOME_META[d.outcome] || null;
  const success = om ? om.cls === 'ok' : (d.executed ? true : (d.dry_run ? null : false));
  const statusText = om ? om.label : (d.dry_run ? 'Dry Run 预览' : (d.executed ? '执行成功' : '未执行'));
  const border = success === true ? 'border-emerald-600/40 bg-emerald-900/20' : (success === false ? 'border-red-600/40 bg-red-900/10' : 'border-slate-700/50 bg-slate-800/30');
  let html = '<div class="p-3 rounded-lg border ' + border + '">';
  html += '<div class="flex items-center gap-2 mb-2 flex-wrap">';
  html += '<span class="badge ' + (om ? om.cls : (d.dry_run ? 'text-slate-400' : (success ? 'ok' : 'err'))) + '">' + statusText + '</span>';
  if (d.risk_level) html += _actionRiskBadge(d.risk_level);
  if (d.auto_executed) html += '<span class="badge ok">自动回写</span>';
  if (d.escalated) html += '<span class="badge warn">已升级人工</span>';
  if (d.idempotency_key) html += '<span class="text-xs text-slate-500">幂等键: ' + esc(d.idempotency_key) + '</span>';
  html += '</div>';
  // V2-13：映射门禁预览/失败原因
  if (d.gate && Array.isArray(d.gate.failures)) {
    html += '<div class="text-xs mb-2 ' + (d.gate.passed ? 'text-slate-500' : 'text-red-400') + '">映射门禁：' +
      (d.gate.passed ? '通过' : '未通过 → ' + d.gate.failures.map(esc).join('；')) + '</div>';
  }
  if (d.gate_failures && d.gate_failures.length) {
    html += '<div class="text-xs mb-2 text-red-400">门禁失败原因（未回写，已留痕）：' + d.gate_failures.map(esc).join('；') + '</div>';
  }
  // V2-13：AI 三条件评估报告（中危）
  if (d.evaluation) {
    const ev = d.evaluation;
    const condLabels = { no_unexpected_external_impact: '无预期外外部影响', atomicity: '原子性', completeness: '完整性' };
    const condHtml = Object.entries(condLabels).map(([k, lab]) => {
      const v = ev.conditions ? ev.conditions[k] : null;
      const t = v === true ? '<span class="ok">通过</span>' : (v === false ? '<span class="text-red-400">不通过</span>' : '<span class="text-slate-500">无法判定</span>');
      return '<div>' + esc(lab) + '：' + t + '</div>';
    }).join('');
    const verdict = { pass: '<span class="ok">通过 → 自动执行</span>', fail: '<span class="text-red-400">不通过 → 升级人工</span>', undecidable: '<span class="warn">无法判定 → 升级人工（失败≠通过）</span>' }[ev.verdict] || esc(ev.verdict);
    html += '<div class="text-xs mb-2 p-2 rounded border border-slate-700/50 bg-slate-800/40">' +
      '<div class="mb-1 text-slate-300">AI 三条件评估报告（' + esc(ev.source || '') + (ev.model ? ' / ' + esc(ev.model) : '') + '）：结论 ' + verdict + '</div>' +
      condHtml +
      (ev.rationale ? '<div class="text-slate-500 mt-1">依据：' + esc(ev.rationale) + '</div>' : '') +
      (ev.error ? '<div class="text-amber-400 mt-1">' + esc(ev.error) + '</div>' : '') +
      '</div>';
  }
  // V2-13：签核标识（身份+时间戳）
  if (d.signer) {
    html += '<div class="text-xs text-slate-400 mb-2">签核标识：' + esc(d.signer.username || '?') +
      (d.signer.role ? '（' + esc(d.signer.role) + '）' : '') + ' @ ' + esc(d.signer.signed_at || '') + '</div>';
  }
  if (d.cancel_reason) {
    html += '<div class="text-xs text-amber-400 mb-2">取消原因：' +
      (d.cancel_reason === 'confirmation_timeout' ? '确认超时（TTL 内未完成确认）' : (d.cancel_reason === 'rejected' ? '人工拒绝' : esc(d.cancel_reason))) +
      (d.reject_reason ? '：' + esc(d.reject_reason) : '') + '</div>';
  }
  if (d.identity && d.identity !== 'not_applicable' && d.identity !== 'none') {
    html += '<div class="text-xs text-slate-500 mb-2">回写身份：' + (d.identity === 'mapped' ? '身份联邦映射身份（M2-B）' : esc(d.identity)) + '</div>';
  }
  if (d.circuit_open) {
    html += '<div class="text-xs text-amber-400 mb-2">熔断开启，约 ' + esc(d.retry_after ?? '?') + 's 后自动重试（期间快速失败，已产生熔断告警）</div>';
  }
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
let _booted = false;
async function bootApp() {
  if (_booted) return;
  _booted = true;
  clientLog('info', 'app boot');
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
}

// V2-05：先校验会话再加载业务页面；未登录只呈现登录页
(async function init() {
  clientLog('info', 'app init');
  await loadSession();
})();

// ===================== 数据字典（M1-A / V2-01：词条 + 联邦查询 + 审计） =====================
let dictTerms = [];
let dictFmSrc = [];
let dictEditingId = null;

async function loadDictionary() {
  try { await loadConnections(); } catch (e) { /* 连接下拉可留空 */ }
  try {
    const [termsJ, fmJ] = await Promise.all([
      api('/api/dictionary/terms'),
      api('/api/dictionary/field-map-sources').catch(() => ({ data: { items: [] } }))
    ]);
    dictTerms = termsJ.data.items || [];
    dictFmSrc = fmJ.data.items || [];
    renderDictTerms();
    renderDictFmSources();
    loadDictAudit();
  } catch (e) { console.warn('loadDictionary', e); }
}

function dictConnOpts(selectedId) {
  const conns = (typeof connList !== 'undefined' && connList.length)
    ? connList
    : [];
  return '<option value="">-- 不指定 --</option>' + conns.map(c =>
    '<option value="' + esc(c.connection_id) + '"' + (c.connection_id === selectedId ? ' selected' : '') + '>' +
    esc(c.display_name) + '（' + esc((c.connection_id || '').slice(0, 10)) + '）</option>'
  ).join('');
}

function renderDictTerms() {
  const tb = $('dictTermsBody');
  if (!tb) return;
  if (!dictTerms.length) {
    tb.innerHTML = '<tr><td colspan="6" class="p-3 text-center text-slate-500">暂无词条。点右上「＋ 新建词条」，或到「MCP」页建好连接后在词条表单里绑定各系统字段。</td></tr>';
    return;
  }
  tb.innerHTML = dictTerms.map(t => {
    const bindings = Object.entries(t.system_bindings || {})
      .map(([cid, f]) => '<div>' + esc(dictConnName(cid)) + '：<span class="font-mono">' + esc(f) + '</span></div>').join('')
      || '<span class="text-slate-600">未绑定</span>';
    return '<tr class="border-b border-slate-700/40 hover:bg-slate-800/30 align-top">' +
      '<td class="p-1.5 font-medium">' + esc(t.canonical) + '</td>' +
      '<td class="p-1.5">' + esc(t.object_type || '-') + ' / <span class="font-mono text-slate-400">' + esc(t.property_id || '-') + '</span></td>' +
      '<td class="p-1.5">' + (((t.synonyms || []).map(s => '<span class="badge text-slate-400 mr-1">' + esc(s) + '</span>').join('')) || '<span class="text-slate-600">-</span>') + '</td>' +
      '<td class="p-1.5">' + bindings + '</td>' +
      '<td class="p-1.5">' + (t.authoritative_source ? esc(dictConnName(t.authoritative_source)) : '<span class="text-slate-600">-</span>') + '</td>' +
      '<td class="p-1.5 whitespace-nowrap">' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="openDictTermEditor(\'' + t.id + '\')">编辑</button> ' +
        '<button class="btn danger" style="padding:2px 8px;font-size:12px" onclick="deleteDictTerm(\'' + t.id + '\')">删除</button>' +
      '</td></tr>';
  }).join('');
}

function dictConnName(cid) {
  const c = (typeof connList !== 'undefined' ? connList : []).find(x => x.connection_id === cid);
  return (c && c.display_name) || cid;
}

async function openDictTermEditor(id) {
  dictEditingId = id || null;
  try { await loadConnections(); } catch (e) { /* 连接下拉可留空 */ }
  const t = dictTerms.find(x => x.id === id) || null;
  const box = $('dictTermEditor');
  box.classList.remove('hidden');
  const bindings = (t && t.system_bindings) || {};
  box.innerHTML =
    '<div class="grid grid-cols-12 gap-2 text-sm">' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">规范术语 *</label><input id="dictTCan" class="w-full" value="' + esc((t && t.canonical) || '') + '" placeholder="如：PN码"></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">对应对象</label><input id="dictTObj" class="w-full" value="' + esc((t && t.object_type) || '') + '" placeholder="如：Product"></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">对应属性</label><input id="dictTProp" class="w-full font-mono text-xs" value="' + esc((t && t.property_id) || '') + '" placeholder="如：pn"></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">同义词（逗号分隔）</label><input id="dictTSyn" class="w-full" value="' + esc(((t && t.synonyms) || []).join(', ')) + '" placeholder="如：PN号, PN"></div>' +
      '<div class="col-span-12">' +
        '<label class="text-xs text-slate-400">系统绑定（系统 → 该系统里的键字段名）</label>' +
        '<div id="dictTBindings" class="space-y-1"></div>' +
        '<button class="btn secondary mt-1" style="padding:2px 8px;font-size:12px" type="button" onclick="addDictBindingRow()">＋ 绑定一个系统</button>' +
      '</div>' +
      '<div class="col-span-6"><label class="text-xs text-slate-400">权威来源（冲突时取该系统的值）</label><select id="dictTAuth" class="w-full">' + dictConnOpts((t && t.authoritative_source) || '') + '</select></div>' +
      '<div class="col-span-6 flex items-end gap-2">' +
        '<button class="btn" type="button" onclick="saveDictTerm()">保存</button>' +
        '<button class="btn secondary" type="button" onclick="closeDictTermEditor()">取消</button>' +
      '</div>' +
    '</div>';
  const bindBox = $('dictTBindings');
  if (!Object.keys(bindings).length) {
    bindBox.innerHTML = '<div class="text-xs text-slate-500">尚未绑定任何系统（未绑定的系统查询时会显式列入未覆盖）。</div>';
  } else {
    Object.entries(bindings).forEach(([cid, f]) => addDictBindingRow(cid, f));
  }
}

function addDictBindingRow(cid, field) {
  const box = $('dictTBindings');
  if (!box) return;
  const note = box.querySelector('.text-slate-500');
  if (note) note.remove();
  const row = document.createElement('div');
  row.className = 'dict-bind-row grid grid-cols-12 gap-2 items-center';
  row.innerHTML =
    '<select class="col-span-7 dict-bind-conn w-full">' + dictConnOpts(cid || '') + '</select>' +
    '<input class="col-span-4 dict-bind-field w-full font-mono text-xs" value="' + esc(field || '') + '" placeholder="该系统中的键字段名，如 PN / PN码 / PN_Num">' +
    '<button class="col-span-1 btn danger" style="padding:2px 6px;font-size:12px" type="button" onclick="this.closest(\'.dict-bind-row\').remove()">×</button>';
  box.appendChild(row);
}

function closeDictTermEditor() {
  dictEditingId = null;
  $('dictTermEditor').classList.add('hidden');
  $('dictTermEditor').innerHTML = '';
}

async function saveDictTerm() {
  const bindings = {};
  document.querySelectorAll('.dict-bind-row').forEach(r => {
    const cid = r.querySelector('.dict-bind-conn').value;
    const f = (r.querySelector('.dict-bind-field').value || '').trim();
    if (cid && f) bindings[cid] = f;
  });
  const body = {
    canonical: ($('dictTCan').value || '').trim(),
    object_type: ($('dictTObj').value || '').trim(),
    property_id: ($('dictTProp').value || '').trim(),
    synonyms: ($('dictTSyn').value || '').split(/[,，]/).map(s => s.trim()).filter(Boolean),
    system_bindings: bindings,
    authoritative_source: $('dictTAuth').value || null
  };
  if (!body.canonical) return toast('规范术语不能为空', 'err');
  try {
    if (dictEditingId) {
      await api('/api/dictionary/terms/' + dictEditingId, { method: 'PUT', body: JSON.stringify(body) });
    } else {
      await api('/api/dictionary/terms', { method: 'POST', body: JSON.stringify(body) });
    }
    closeDictTermEditor();
    toast('词条已保存', 'ok');
    loadDictionary();
  } catch (e) { toast('保存失败: ' + e.message, 'err'); }
}

async function deleteDictTerm(id) {
  if (!confirm('确认删除该词条？')) return;
  try {
    await api('/api/dictionary/terms/' + id, { method: 'DELETE' });
    toast('已删除', 'ok');
    loadDictionary();
  } catch (e) { toast('删除失败: ' + e.message, 'err'); }
}

// ---- 联邦查询 ----
async function dictLookup(dryRun, confirmCost) {
  const term = ($('dictLookupTerm').value || '').trim();
  if (!term) return toast('请输入词条或同义词', 'err');
  const body = {
    term,
    value: ($('dictLookupValue').value || '').trim(),
    systems: ($('dictLookupSystems').value || '').trim(),
    limit: parseInt($('dictLookupLimit').value || '50', 10) || 50,
    dry_run: !!dryRun,
    confirm_cost: !!confirmCost
  };
  const box = $('dictResult');
  box.innerHTML = '<div class="text-xs text-slate-400">查询中…</div>';
  $('dictConfirmBtn').classList.add('hidden');
  try {
    const j = await api('/api/dictionary/term-lookup', { method: 'POST', body: JSON.stringify(body) });
    renderDictResult(j.data || {});
  } catch (e) {
    box.innerHTML = '<div class="text-xs text-rose-400">查询失败: ' + esc(e.message) + '</div>';
  }
}

function renderDictResult(res) {
  const box = $('dictResult');
  const statusLabel = {
    ok: '成功', preview: '成本预览（未执行）', needs_disambiguation: '歧义待确认',
    term_not_found: '词条未匹配', cost_confirmation_required: '成本待确认', error: '失败'
  }[res.status] || res.status;
  const statusCls = { ok: 'ok', preview: 'text-amber-300', needs_disambiguation: 'text-amber-300',
    cost_confirmation_required: 'text-amber-300', term_not_found: 'text-rose-400', error: 'text-rose-400' }[res.status] || 'text-slate-400';
  let html = '<div class="flex items-center gap-2 mb-2">' +
    '<span class="badge ' + statusCls + '">' + esc(statusLabel) + '（' + esc(res.status) + '）</span>' +
    (res.cost_ms != null ? '<span class="text-xs text-slate-500">耗时 ' + res.cost_ms + ' ms</span>' : '') +
    '</div>';
  if (res.hint) html += '<div class="text-xs text-amber-300 mb-2">' + esc(res.hint) + '</div>';
  if (res.error) html += '<div class="text-xs text-rose-400 mb-2 whitespace-pre-wrap">' + esc(res.error) + '</div>';

  // 歧义候选
  if (res.status === 'needs_disambiguation' && (res.candidates || []).length) {
    html += '<div class="mb-3"><div class="text-xs text-slate-400 mb-1">候选词条（人工选择，可将所选晋升为同义词后再查）：</div>' +
      res.candidates.map(c =>
        '<div class="mb-1 border border-slate-700/60 rounded px-2 py-1 text-xs">' +
        '<span class="font-medium">' + esc(c.canonical) + '</span> ' +
        '<span class="text-slate-500">置信度 ' + (c.confidence == null ? '-' : c.confidence.toFixed(3)) + '</span>' +
        (c.synonyms && c.synonyms.length ? ' <span class="text-slate-500">同义词: ' + c.synonyms.map(esc).join(', ') + '</span>' : '') +
        '</div>').join('') + '</div>';
  }

  // 成本预览/确认
  if (res.cost_estimate) {
    const ce = res.cost_estimate;
    html += '<div class="mb-3"><div class="text-xs text-slate-400 mb-1">成本估算（needs_confirm=' + (ce.needs_confirm ? '是' : '否') + '）</div>' +
      '<table class="w-full text-xs border-collapse mb-1"><thead><tr class="text-slate-500">' +
      '<th class="border-b border-slate-700 p-1 text-left">系统</th><th class="border-b border-slate-700 p-1 text-left">拉取模式</th><th class="border-b border-slate-700 p-1 text-right">预估行数</th></tr></thead><tbody>' +
      Object.entries(ce.systems || {}).map(([sid, s]) =>
        '<tr class="border-b border-slate-700/40"><td class="p-1">' + esc(dictConnName(sid)) + '</td>' +
        '<td class="p-1">' + esc(s.fetch_mode || 'paged') + (s.fetch_limit ? '（上限 ' + s.fetch_limit + '）' : '') + '</td>' +
        '<td class="p-1 text-right">' + (s.est_rows != null ? s.est_rows : '-') + '</td></tr>').join('') +
      '</tbody></table>' +
      (ce.total_est_rows != null ? '<div class="text-xs text-slate-500">合计预估 ' + ce.total_est_rows + ' 行</div>' : '') +
      '</div>';
    if (res.status === 'cost_confirmation_required') $('dictConfirmBtn').classList.remove('hidden');
  }

  // 未覆盖系统
  if ((res.uncovered || []).length) {
    html += '<div class="text-xs text-amber-300 mb-2">未覆盖系统（显式列出，不隐藏）：' +
      res.uncovered.map(sid => esc(dictConnName(sid))).join('、') + '</div>';
  }

  // 各系统明细
  if (res.per_system && Object.keys(res.per_system).length) {
    html += '<div class="mb-3"><div class="text-xs text-slate-400 mb-1">各系统明细</div>' +
      '<table class="w-full text-xs border-collapse"><thead><tr class="text-slate-500">' +
      '<th class="border-b border-slate-700 p-1 text-left">系统</th><th class="border-b border-slate-700 p-1 text-left">状态</th>' +
      '<th class="border-b border-slate-700 p-1 text-right">行数</th><th class="border-b border-slate-700 p-1 text-left">模式</th>' +
      '<th class="border-b border-slate-700 p-1 text-right">耗时(ms)</th><th class="border-b border-slate-700 p-1 text-left">错误</th></tr></thead><tbody>' +
      Object.entries(res.per_system).map(([sid, s]) =>
        '<tr class="border-b border-slate-700/40"><td class="p-1">' + esc(dictConnName(sid)) + '</td>' +
        '<td class="p-1 ' + (s.status === 'ok' ? 'text-emerald-400' : 'text-rose-400') + '">' + esc(s.status) + (s.code ? '（' + esc(s.code) + '）' : '') + '</td>' +
        '<td class="p-1 text-right">' + (s.row_count != null ? s.row_count : '-') + '</td>' +
        '<td class="p-1">' + esc(s.fetch_mode || '-') + (s.truncated ? '（截断）' : '') + '</td>' +
        '<td class="p-1 text-right">' + (s.latency_ms != null ? s.latency_ms : '-') + '</td>' +
        '<td class="p-1 text-rose-400 break-all">' + esc(s.error || '') + '</td></tr>').join('') +
      '</tbody></table></div>';
  }

  // 合并结果
  if ((res.merged || []).length) {
    html += '<div class="mb-1"><div class="text-xs text-slate-400 mb-1">合并结果（' + res.merged.length + ' 条；同 key 值冲突并存并带来源标记，权威来源取权威值）</div>' +
      '<div class="overflow-x-auto"><table class="w-full text-xs border-collapse"><thead><tr class="text-slate-500">' +
      '<th class="border-b border-slate-700 p-1 text-left">属性</th><th class="border-b border-slate-700 p-1 text-left">值</th>' +
      '<th class="border-b border-slate-700 p-1 text-left">冲突</th><th class="border-b border-slate-700 p-1 text-left">来源</th></tr></thead><tbody>' +
      res.merged.map(m => Object.entries(m.fields || {}).map(([prop, f]) => {
        // V2-11 TP-11-02/EP-11-03：schema 漂移字段显式标记（不静默放行、不当作有效旧值）
        const marks = (f.drift_marks || []).map(mk =>
          '<div class="text-amber-300 text-[11px] mt-0.5" title="' + esc(mk.message || '') + '">[漂移] ' + esc(mk.connection) + ' · ' + esc(mk.field) + '（' + esc(DRIFT_KIND_LABELS[mk.kind] || mk.kind || '') + '）</div>').join('');
        return '<tr class="border-b border-slate-700/40 align-top"><td class="p-1 font-mono">' + esc(prop) + '</td>' +
        '<td class="p-1">' + (f.conflict ? '-' : esc(f.value == null ? '（空）' : String(f.value))) + (f.authoritative ? ' <span class="text-emerald-400 text-[11px]">[权威]</span>' : '') + marks + '</td>' +
        '<td class="p-1 ' + (f.conflict ? 'text-amber-300' : 'text-slate-600') + '">' + (f.conflict ? '是' : '') + '</td>' +
        '<td class="p-1 text-slate-400">' + Object.entries(f.sources || {}).map(([src, v]) =>
          esc(src) + '=' + esc(v == null ? '（空）' : String(v))).join('<br>') + '</td></tr>';
      }).join('')).join('') +
      '</tbody></table></div></div>';
  }
  if (!res.per_system && !res.cost_estimate && !res.candidates) {
    html += '<div class="text-xs text-slate-500">（无更多明细）</div>';
  }
  box.innerHTML = html;
}

function renderDictFmSources() {
  const box = $('dictFmSources');
  if (!box) return;
  if (!dictFmSrc.length) { box.innerHTML = '<div class="text-slate-600">暂无连接。</div>'; return; }
  box.innerHTML = dictFmSrc.map(s =>
    '<div class="flex items-center gap-2">' +
    '<span class="w-32 truncate">' + esc(dictConnName(s.connection_id)) + '</span>' +
    '<span class="badge ' + (s.field_map_source === 'dictionary' ? 'ok' : 'text-slate-400') + '">' + esc(s.field_map_source) + '</span>' +
    '<span class="text-slate-500">' + s.field_map_size + ' 字段映射</span>' +
    '</div>').join('');
}

async function loadDictAudit() {
  const tb = $('dictAuditBody');
  if (!tb) return;
  try {
    const j = await api('/api/dictionary/term-lookup-audit?page=1&pageSize=20');
    const items = j.data.items || [];
    if (!items.length) {
      tb.innerHTML = '<tr><td colspan="8" class="p-3 text-center text-slate-500">暂无审计记录。</td></tr>';
      return;
    }
    tb.innerHTML = items.map(a => {
      const r = a.resolution || {};
      const ps = a.per_system || {};
      const psText = Object.entries(ps).map(([sid, s]) =>
        dictConnName(sid) + ':' + (s.status === 'ok' ? (s.row_count != null ? s.row_count : 'ok') : s.status)).join(', ');
      return '<tr class="border-b border-slate-700/40 align-top">' +
        '<td class="p-1.5 whitespace-nowrap text-slate-400">' + esc((a.created_at || '').replace('T', ' ').slice(0, 19)) + '</td>' +
        '<td class="p-1.5">' + esc(a.input_term || '') + '</td>' +
        '<td class="p-1.5 font-mono">' + esc(a.value || '') + '</td>' +
        '<td class="p-1.5">' + (r.canonical ? esc(r.canonical) + ' <span class="text-slate-500">(' + esc(r.match || '-') + (r.confidence != null ? ', ' + r.confidence.toFixed(2) : '') + ')</span>' : '-') + '</td>' +
        '<td class="p-1.5 ' + (a.status === 'ok' ? 'text-emerald-400' : 'text-amber-300') + '">' + esc(a.status) + '</td>' +
        '<td class="p-1.5 text-slate-400">' + esc(psText) + '</td>' +
        '<td class="p-1.5 text-right">' + (a.cost_ms != null ? a.cost_ms : '-') + '</td>' +
        '<td class="p-1.5">' + esc(a.actor || '') + '</td></tr>';
    }).join('');
  } catch (e) {
    tb.innerHTML = '<tr><td colspan="8" class="p-3 text-center text-rose-400">加载失败: ' + esc(e.message) + '</td></tr>';
  }
}

/* ================= 能力层（M1-B / V2-04 首片：能力注册 + 公式引擎 + 执行 + 溯源） =================
   能力 = 绑定 object_type 的表达式（derive×expression）。
   公式语言：赋值块 / this.<attr> / lookup(对象类型, {维度}, 属性) / ref('能力名'[,'x.y.z']) /
   if(a,b,c) / if_null(a,b) / 三元 ?: / && ||（and or 别名）。null 传播（禁止当 0）。
   版本化：draft→publish 锁定引用版本；active 内容变更自动 minor+1 发新版（旧版可溯源）。 */
const capState = { list: [], editingId: null };

async function loadCapabilities() {
  const st = ($('capFilterStatus') && $('capFilterStatus').value) || '';
  try {
    const j = await api('/api/capabilities' + (st ? '?status=' + encodeURIComponent(st) : ''));
    capState.list = j.data.items || [];
    renderCapList();
  } catch (e) { console.warn('loadCapabilities', e); }
}

function capBtn(label, fn, cls) {
  return '<button class="btn ' + (cls || 'secondary') + '" style="padding:2px 8px;font-size:12px" onclick="' + fn + '">' + label + '</button> ';
}

function renderCapList() {
  const tb = $('capListBody');
  if (!tb) return;
  if (!capState.list.length) {
    tb.innerHTML = '<tr><td colspan="7" class="p-3 text-center text-slate-500">暂无能力。点「＋ 新建能力」创建派生型公式能力（如上门率 D3、人工基线 D）。</td></tr>';
    return;
  }
  const statusCls = { draft: 'text-amber-300', active: 'text-emerald-400', deprecated: 'text-slate-500' };
  const statusCn = { draft: '草稿', active: '已发布', deprecated: '已弃用' };
  tb.innerHTML = capState.list.map(c =>
    '<tr class="border-b border-slate-700/40 hover:bg-slate-800/30 align-top">' +
      '<td class="p-1.5 font-mono">' + esc(c.name) + '</td>' +
      '<td class="p-1.5">' + esc(c.display_name || '') + '</td>' +
      '<td class="p-1.5 font-mono text-slate-400">' + esc(c.object_type || '') + '</td>' +
      '<td class="p-1.5 font-mono text-slate-400">' + (c.output_attribute ? esc(c.output_attribute) : '-') + '</td>' +
      '<td class="p-1.5">' + esc(c.version || '-') + '</td>' +
      '<td class="p-1.5 ' + (statusCls[c.status] || '') + '">' + (statusCn[c.status] || esc(c.status)) + '</td>' +
      '<td class="p-1.5 whitespace-nowrap">' +
        capBtn('编辑', "openCapEditor('" + c.id + "')") +
        (c.status === 'draft' ? capBtn('发布', "capPublish('" + c.id + "')", '') : '') +
        capBtn('版本', "capShowVersions('" + c.id + "')") +
        capBtn('依赖', "capShowDependents('" + c.id + "')") +
        capBtn('删除', "capDelete('" + c.id + "')", 'danger') +
      '</td></tr>'
  ).join('');
}

function openCapEditor(id) {
  capState.editingId = id || null;
  const c = capState.list.find(x => x.id === id) || null;
  const box = $('capEditor');
  box.classList.remove('hidden');
  box.innerHTML =
    '<div class="grid grid-cols-12 gap-2 text-sm">' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">name *（小写下划线，ref 定位用）</label><input id="capFName" class="w-full font-mono text-xs" value="' + esc(c ? c.name : '') + '" placeholder="如 cap_onsite_rate_d3" ' + (c ? 'readonly' : '') + '></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">显示名</label><input id="capFDisplayName" class="w-full" value="' + esc(c ? (c.display_name || '') : '') + '" placeholder="如 上门率 D3"></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">对象类型 *</label><input id="capFObjectType" class="w-full font-mono text-xs" value="' + esc(c ? (c.object_type || '') : '') + '" placeholder="如 obj_maintenance_product"></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">输出属性（写回目标，留空仅记元数据）</label><input id="capFOutAttr" class="w-full font-mono text-xs" value="' + esc(c ? (c.output_attribute || '') : '') + '" placeholder="如 attr_d3_rate"></div>' +
      '<div class="col-span-12"><label class="text-xs text-slate-400">公式（expression）*</label>' +
        '<textarea id="capFExpr" rows="6" class="w-full font-mono text-xs" spellcheck="false" placeholder="lookup(obj_onsite_rate_baseline, {&#10;  &quot;attr_product_line&quot;: this.attr_product_line,&#10;  &quot;attr_customer_segment&quot;: this.attr_customer_segment,&#10;  &quot;attr_domestic&quot;: this.attr_region&#10;}, &quot;attr_onsite_rate&quot;) * if(this.attr_term_years == 3, 1.0, if(this.attr_term_years == 4, 1.4, 1.88))">' + esc(c ? (c.expression || '') : '') + '</textarea>' +
        '<div class="text-xs text-slate-600 mt-1">语法：赋值块（var = expr）/ this.&lt;attr&gt; / lookup(对象类型, {维度}, 属性) / ref(&#39;能力名&#39;[,&#39;x.y.z&#39;]) / if(a,b,c) / if_null(a,b) / ?: / &amp;&amp; ||。lookup 0 命中→NOT_FOUND、多命中→AMBIGUOUS（逐实例错误，批不中断）；算术遇 null→null（用 if_null 显式守卫）。</div>' +
      '</div>' +
      '<div class="col-span-12"><label class="text-xs text-slate-400">说明</label><input id="capFDesc" class="w-full" value="' + esc(c ? (c.description || '') : '') + '" placeholder="口径/来源说明"></div>' +
      '<div class="col-span-12 flex gap-2">' +
        '<button class="btn" type="button" onclick="capSave()">保存</button>' +
        '<button class="btn secondary" type="button" onclick="capValidateDraft()">校验公式（不落库）</button>' +
        '<button class="btn ghost" type="button" onclick="closeCapEditor()">取消</button>' +
      '</div>' +
    '</div>';
}

function closeCapEditor() {
  capState.editingId = null;
  const box = $('capEditor');
  box.classList.add('hidden');
  box.innerHTML = '';
  const vb = $('capValidateBox');
  vb.classList.add('hidden');
  vb.innerHTML = '';
}

function capCollectForm() {
  return {
    name: ($('capFName').value || '').trim(),
    display_name: ($('capFDisplayName').value || '').trim() || null,
    description: ($('capFDesc').value || '').trim() || null,
    object_type: ($('capFObjectType').value || '').trim(),
    output_attribute: ($('capFOutAttr').value || '').trim() || null,
    expression: ($('capFExpr').value || '').trim(),
  };
}

async function capSave() {
  const data = capCollectForm();
  if (!data.name || !data.object_type || !data.expression) {
    toast('name / 对象类型 / 公式 必填', 'err');
    return;
  }
  try {
    if (capState.editingId) {
      const j = await api('/api/capabilities/' + capState.editingId, { method: 'PUT', body: data });
      toast('已保存' + (j.data.version ? '（版本 ' + j.data.version + '）' : ''), 'ok');
    } else {
      const j = await api('/api/capabilities', { method: 'POST', body: data });
      toast('能力已创建（草稿），可「校验」→「发布」', 'ok');
    }
    closeCapEditor();
    loadCapabilities();
  } catch (e) {
    capShowValidate({ valid: false, error_code: e.code, issues: (e.details && (e.details.issues || e.details.dependents)) || [{ message: e.message }] });
    toast('保存失败: ' + e.message, 'err');
  }
}

async function capValidateDraft() {
  const expr = ($('capFExpr').value || '').trim();
  if (!expr) { toast('先填写公式', 'err'); return; }
  try {
    let res;
    if (capState.editingId) {
      res = await api('/api/capabilities/' + capState.editingId + '/validate?draft_expression=' + encodeURIComponent(expr));
    } else {
      // 未落库能力：用临时校验端点不可用，直接提示先保存
      toast('请先保存能力（草稿），再用「校验公式」', 'err');
      return;
    }
    capShowValidate(res.data);
  } catch (e) {
    capShowValidate({ valid: false, error_code: e.code, issues: (e.details && e.details.issues) || [{ message: e.message }] });
  }
}

function capShowValidate(res) {
  const vb = $('capValidateBox');
  vb.classList.remove('hidden');
  if (res.valid) {
    vb.innerHTML = '<div class="text-emerald-400">✓ 校验通过：公式语法/白名单/常量折叠/引用均正常。</div>' +
      '<div class="text-slate-400 mt-1">引用能力：' + (res.refs && res.refs.length ? res.refs.map(r => esc(r.name) + (r.version ? '@' + esc(r.version) : '')).join('、') : '无') +
      '　·　输入属性：' + (res.attr_refs && res.attr_refs.length ? res.attr_refs.map(esc).join('、') : '无') + '</div>';
    return;
  }
  const issues = (res.issues || []).map(i => {
    const msg = typeof i === 'string' ? i : (i.message || JSON.stringify(i));
    return '<div class="text-rose-400">✗ ' + esc(msg) + '</div>';
  }).join('');
  vb.innerHTML = '<div class="text-rose-400 font-medium">校验未通过' + (res.error_code ? '（' + esc(res.error_code) + '）' : '') + '</div>' +
    '<div class="mt-1 space-y-0.5">' + (issues || '<div class="text-rose-400">详见上方 toast</div>') + '</div>';
}

async function capPublish(id) {
  if (!confirm('发布该能力？发布时锁定引用能力的当前版本（后续不自动跟随）。')) return;
  try {
    const j = await api('/api/capabilities/' + id + '/publish', { method: 'POST' });
    toast('已发布 v' + (j.data.version || '?') + '，引用版本已锁定', 'ok');
    loadCapabilities();
  } catch (e) {
    toast('发布失败: ' + e.message + (e.code ? '（' + e.code + '）' : ''), 'err');
  }
}

async function capDelete(id) {
  const c = capState.list.find(x => x.id === id);
  if (!confirm('确认删除能力 ' + (c ? c.name : id) + '？被其他能力引用时禁止删除。')) return;
  try {
    await api('/api/capabilities/' + id, { method: 'DELETE' });
    toast('已删除', 'ok');
    loadCapabilities();
  } catch (e) {
    if (e.code === 'HAS_DEPENDENTS') {
      const deps = (e.details && e.details.dependents) || [];
      capShowPanel('删除被拒：存在 ' + deps.length + ' 个依赖方（引用方）',
        deps.map(d => '<div class="mb-1">· <span class="font-mono">' + esc(d.name) + '</span> v' + esc(d.version || '?') +
          (d.locked_version ? '（锁定 ' + esc(d.locked_version) + '）' : '') + ' — ' + esc(d.display_name || '') + '</div>').join('') ||
        '<div>无依赖清单</div>', 'text-rose-400');
    } else {
      toast('删除失败: ' + e.message, 'err');
    }
  }
}

async function capShowVersions(id) {
  try {
    const j = await api('/api/capabilities/' + id + '/versions');
    const items = j.data.items || [];
    capShowPanel('版本历史（' + items.length + ' 版，最新在前）',
      items.map(v =>
        '<div class="mb-2 border-b border-slate-700/40 pb-2">' +
          '<div><span class="text-slate-200 font-mono">' + esc(v.version) + '</span> ' +
          '<span class="' + (v.status === 'active' ? 'text-emerald-400' : 'text-amber-300') + '">' + esc(v.status) + '</span>' +
          '<span class="text-slate-600 ml-2">' + esc((v.published_at || v.created_at || '').replace('T', ' ').slice(0, 19)) + '</span></div>' +
          '<pre class="font-mono text-slate-400 whitespace-pre-wrap mt-1 mb-0">' + esc(v.expression || '') + '</pre>' +
        '</div>'
      ).join('') || '<div>无历史</div>', 'text-slate-200');
  } catch (e) { toast('加载版本失败: ' + e.message, 'err'); }
}

async function capShowDependents(id) {
  try {
    const j = await api('/api/capabilities/' + id + '/dependents');
    const items = j.data.items || [];
    capShowPanel('依赖方（引用该能力的其他能力，共 ' + items.length + '）',
      items.map(d =>
        '<div class="mb-1">· <span class="font-mono">' + esc(d.name) + '</span> v' + esc(d.version || '?') +
        ' — ' + esc(d.display_name || '') +
        (d.locked_version ? ' <span class="text-slate-500">（当前锁定 ' + esc(d.locked_version) + '）</span>' : ' <span class="text-amber-300">（引用未锁定：未发布或锁定版本不一致）</span>') +
        '</div>'
      ).join('') || '<div class="text-slate-500">无依赖方，可安全删除。</div>', 'text-slate-200');
  } catch (e) { toast('加载依赖失败: ' + e.message, 'err'); }
}

function capShowPanel(title, html, cls) {
  $('capPanelTitle').textContent = title;
  $('capPanel').className = 'text-xs ' + (cls || 'text-slate-400');
  $('capPanel').innerHTML = html;
  $('capPanel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function capFmtValue(v) {
  if (v === null || v === undefined) return '<span class="text-amber-300">null</span>';
  if (typeof v === 'number') return esc(String(v));
  return esc(JSON.stringify(v));
}

function capTraceHtml(trace) {
  const lk = (trace && trace.lookups) || [];
  if (!lk.length) return '<span class="text-slate-600">无 lookup</span>';
  return lk.map(l =>
    '<div class="mb-1"><span class="font-mono text-slate-300">' + esc(l.object_type) + '</span> ' +
    esc(JSON.stringify(l.dims || {})) + ' → ' + esc(l.attr || '(整行)') +
    '　<span class="text-slate-500">命中 ' + esc(String(l.matched_record_id || '?')) + (l.matched_code ? '（' + esc(String(l.matched_code)) + '）' : '') +
    '，原始值 ' + capFmtValue(l.value) + '</span></div>'
  ).join('');
}

async function capResolveName() {
  const raw = ($('capExecName').value || '').trim();
  if (!raw) { toast('填写能力 name / id', 'err'); return null; }
  const hit = capState.list.find(c => c.id === raw || c.name === raw);
  return hit ? hit.id : raw;
}

async function capExecuteSingle() {
  const capId = await capResolveName();
  if (!capId) return;
  const instanceId = ($('capExecInstanceId').value || '').trim();
  if (!instanceId) { toast('填写实例 record_id', 'err'); return; }
  try {
    const j = await api('/api/capabilities/' + capId + '/execute', {
      method: 'POST', body: { instance_id: instanceId, channel: 'ui', trigger_source: 'manual' },
    });
    capRenderExecResult(j.data);
  } catch (e) {
    $('capExecResult').innerHTML = '<div class="text-rose-400">' + esc(e.message) + (e.code ? '（' + esc(e.code) + '）' : '') + '</div>';
  }
}

async function capExecuteBatch() {
  const capId = await capResolveName();
  if (!capId) return;
  const body = {
    limit: parseInt($('capBatchLimit').value, 10) || 100,
    channel: 'ui',
    trigger_source: 'batch',
    dry_run: !!($('capBatchDryRun').checked),
  };
  try {
    const j = await api('/api/capabilities/' + capId + '/execute-batch', { method: 'POST', body });
    capRenderBatch(j.data);
  } catch (e) {
    $('capBatchSummary').textContent = '';
    $('capBatchBody').innerHTML = '<tr><td colspan="6" class="p-3 text-rose-400">' + esc(e.message) + '</td></tr>';
  }
}

function capRenderExecResult(r) {
  const el = $('capExecResult');
  const ok = r.status === 'success';
  el.innerHTML =
    '<div class="mb-1"><span class="text-slate-400">能力：</span><span class="font-mono">' + esc(r.capability_name || '') + '</span> v' + esc(r.capability_version || '?') +
    '　<span class="text-slate-400">实例：</span><span class="font-mono">' + esc(r.instance_id || '') + '</span></div>' +
    '<div class="mb-1 text-lg font-medium ' + (ok ? 'text-emerald-400' : 'text-rose-400') + '">' +
      (ok ? 'value = ' + capFmtValue(r.value) : 'error: ' + esc(r.error_code || '') + ' — ' + esc(r.error_message || '')) + '</div>' +
    '<div class="text-slate-400 mb-1">' + capTraceHtml(r.trace) + '</div>' +
    '<pre class="font-mono text-slate-500 whitespace-pre-wrap max-h-40 overflow-y-auto">' + esc(JSON.stringify(r.trace, null, 1)) + '</pre>';
}

function capRenderBatch(r) {
  $('capBatchSummary').innerHTML =
    'v' + esc(r.capability_version || '?') + '　共 ' + r.total + ' · 成功 ' + r.success +
    ' · 错误 ' + r.error + ' · <span class="text-amber-300">null ' + (r.null != null ? r.null : 0) + '</span>' +
    (r.dry_run ? ' · <span class="text-slate-400">dry_run 未写回</span>' : '') + ' · ' + r.duration_ms + 'ms';
  const tb = $('capBatchBody');
  if (!r.items || !r.items.length) {
    tb.innerHTML = '<tr><td colspan="6" class="p-3 text-center text-slate-500">无实例</td></tr>';
    return;
  }
  tb.innerHTML = r.items.map(it =>
    '<tr class="border-b border-slate-700/40 align-top">' +
      '<td class="p-1.5 font-mono text-slate-400">' + esc(String(it.instance_id || '').slice(0, 12)) + '…</td>' +
      '<td class="p-1.5 ' + (it.status === 'success' ? 'text-emerald-400' : 'text-rose-400') + '">' + esc(it.status) + '</td>' +
      '<td class="p-1.5 font-mono">' + capFmtValue(it.value) + '</td>' +
      '<td class="p-1.5 text-slate-400">' + (it.error_code ? esc(it.error_code) + '<div class="text-slate-600">' + esc(it.error_message || '') + '</div>' : '-') + '</td>' +
      '<td class="p-1.5">' + esc(it.capability_version || '') + '</td>' +
      '<td class="p-1.5 text-slate-400">' + capTraceHtml(it.trace) + '</td></tr>'
  ).join('');
}

/* ================= 使用教程（frontend/tutorial/） =================
   数据结构（版本化规则参考 knowledgeOS）：
   tutorial/manifest.json      { title, systemVersion, minor, version, updatedAt, chapters[] }
   tutorial/chapters/<id>.md   每章独立 Markdown
   tutorial/assets/*.png       页面截图（与正文一一对应）
   版本规则：教程 version = 系统主版本.教程小版本（如系统 1.x → 教程 1.n）；
   系统主版本升级时教程主版本跟随并重置 minor；仅内容更新 minor+1 并重写 version。 */
const tutState = { manifest: null };

async function loadTutorial() {
  try {
    if (!tutState.manifest) {
      const res = await fetch('tutorial/manifest.json', { cache: 'no-cache' });
      if (!res.ok) throw new Error('manifest.json 加载失败 (' + res.status + ')');
      tutState.manifest = await res.json();
    }
    const m = tutState.manifest;
    $('tutVersion').textContent = m.title + ' · 教程 v' + m.version + '（系统 v' + m.systemVersion + '）· 更新 ' + m.updatedAt;
    renderTutToc(m);
    openTutChapter(m.chapters[0].id);
  } catch (e) {
    $('tutContent').innerHTML = '<p class="text-sm text-red-400">教程加载失败：' + esc(e.message) + '</p>';
  }
}

function renderTutToc(m) {
  let html = '';
  let lastPart = null;
  m.chapters.forEach((ch, i) => {
    if (ch.part && ch.part !== lastPart) {
      html += '<div class="tut-part-h">' + esc(ch.part) + '</div>';
      lastPart = ch.part;
    }
    html += '<a data-ch="' + ch.id + '" title="' + esc(ch.title) + '"><span class="n">' + (i + 1) + '</span><span>' + esc(ch.title) + '</span></a>';
  });
  $('tutToc').innerHTML = html;
  document.querySelectorAll('#tutToc a').forEach(a => { a.onclick = () => openTutChapter(a.dataset.ch); });
}

async function openTutChapter(id) {
  const m = tutState.manifest;
  if (!m) return;
  const idx = m.chapters.findIndex(c => c.id === id);
  document.querySelectorAll('#tutToc a').forEach(a => a.classList.toggle('on', a.dataset.ch === id));
  const box = $('tutContent');
  box.innerHTML = '<p class="text-sm text-slate-400">加载中…</p>';
  try {
    const res = await fetch('tutorial/chapters/' + id + '.md', { cache: 'no-cache' });
    if (!res.ok) throw new Error('章节加载失败 (' + res.status + ')');
    const md = await res.text();
    const prev = idx > 0 ? m.chapters[idx - 1] : null;
    const next = idx < m.chapters.length - 1 ? m.chapters[idx + 1] : null;
    box.innerHTML = renderTutorialMarkdown(md)
      + '<div class="tut-nav">'
      + (prev ? '<button class="btn secondary" data-ch="' + prev.id + '">← ' + esc(prev.title) + '</button>' : '<span></span>')
      + (next ? '<button class="btn" data-ch="' + next.id + '">' + esc(next.title) + ' →</button>' : '<span></span>')
      + '</div>'
      + '<div class="tut-ver">教程 v' + m.version + ' · 系统 v' + m.systemVersion + ' · 更新 ' + m.updatedAt + '</div>';
    box.querySelectorAll('[data-ch]').forEach(b => { b.onclick = () => openTutChapter(b.dataset.ch); });
    window.scrollTo({ top: 0 });
  } catch (e) {
    box.innerHTML = '<p class="text-sm text-red-400">' + esc(e.message) + '</p>';
  }
}

/* 教程 Markdown 渲染：覆盖章节用到的子集（标题/表格/代码块/引用/列表/分隔线/图片/行内样式） */
function renderTutorialMarkdown(src) {
  const attr = s => esc(s).replace(/"/g, '&quot;');
  const blocks = [];
  src = src.replace(/```[\s\S]*?```/g, m => {
    blocks.push(m.replace(/^```\w*\n?/, '').replace(/\n?```$/, ''));
    return '\u0000B' + (blocks.length - 1) + '\u0000';
  });
  const inline = s => esc(s)
    .replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)/g,
      (m, alt, url, cap) => {
        const abs = /^(?:[a-z]+:)?\/\//.test(url) || url.startsWith('/') || url.startsWith('data:');
        const src = abs ? url : 'tutorial/' + url;
        return '<img class="tut-img" src="' + attr(src) + '" alt="' + attr(alt) + '">' + (cap ? '<div class="tut-cap">' + attr(cap) + '</div>' : '');
      })
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines = src.split(/\r?\n/);
  let html = '', i = 0, list = null;
  const closeList = () => { if (list) { html += '</' + list + '>'; list = null; } };
  while (i < lines.length) {
    const t = lines[i].trim();
    if (!t) { closeList(); i++; continue; }
    if (/^\u0000B\d+\u0000$/.test(t)) {
      closeList();
      html += '<pre><code>' + esc(blocks[+t.match(/\d+/)[0]]) + '</code></pre>';
      i++; continue;
    }
    const hm = t.match(/^(#{1,4})\s+(.*)$/);
    if (hm) { closeList(); html += '<h' + hm[1].length + '>' + inline(hm[2]) + '</h' + hm[1].length + '>'; i++; continue; }
    if (/^(-{3,}|\*{3,})$/.test(t)) { closeList(); html += '<hr>'; i++; continue; }
    if (t.startsWith('|')) {
      closeList();
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(lines[i].trim()); i++; }
      const cells = r => r.replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
      const isSep = r => /^[\s|:-]+$/.test(r) && r.includes('-');
      const body = rows.filter(r => !isSep(r));
      html += '<table><thead><tr>' + cells(body[0]).map(c => '<th>' + inline(c) + '</th>').join('') + '</tr></thead><tbody>'
        + body.slice(1).map(r => '<tr>' + cells(r).map(c => '<td>' + inline(c) + '</td>').join('') + '</tr>').join('') + '</tbody></table>';
      continue;
    }
    if (t.startsWith('>')) {
      closeList();
      const q = [];
      while (i < lines.length && lines[i].trim().startsWith('>')) { q.push(lines[i].trim().replace(/^>\s?/, '')); i++; }
      const txt = q.join(' ');
      html += '<blockquote' + (/⚠|注意|Warning/i.test(txt) ? ' class="warn"' : '') + '>' + inline(txt) + '</blockquote>';
      continue;
    }
    if (/^[-*]\s+/.test(t)) {
      if (list !== 'ul') { closeList(); html += '<ul>'; list = 'ul'; }
      html += '<li>' + inline(t.replace(/^[-*]\s+/, '')) + '</li>'; i++; continue;
    }
    if (/^\d+[.)]\s+/.test(t)) {
      if (list !== 'ol') { closeList(); html += '<ol>'; list = 'ol'; }
      html += '<li>' + inline(t.replace(/^\d+[.)]\s+/, '')) + '</li>'; i++; continue;
    }
    closeList();
    const p = [t]; i++;
    while (i < lines.length) {
      const nt = lines[i].trim();
      if (!nt || /^#{1,4}\s/.test(nt) || nt.startsWith('|') || nt.startsWith('>') || /^[-*]\s+/.test(nt)
        || /^\d+[.)]\s+/.test(nt) || /^(-{3,}|\*{3,})$/.test(nt) || /^\u0000B\d+\u0000$/.test(nt)) break;
      p.push(nt); i++;
    }
    html += '<p>' + inline(p.join(' ')) + '</p>';
  }
  closeList();
  return html.replace(/\u0000B(\d+)\u0000/g, (m, n) => '<pre><code>' + esc(blocks[+n]) + '</code></pre>');
}

// ===================== V2-05 登录鉴权与 RBAC（M2-A） =====================
// 会话凭据为 httpOnly Cookie：JS 不接触 token；角色/授权由后端每次请求实时判定。
const WRITE_BUTTON_ACTIONS = [
  'toggleProjectForm()', 'submitNewProject()', 'startCreate()', 'saveOntology()',
  'publishCurrent()', 'deleteCurrentOntology()', 'uploadIngestFile()', 'previewIngest()',
  'openDictTermEditor()', 'openCapEditor()', 'openConnEditor()', 'saveLLMSettings()',
  "batchCandidates('delete')", 'saveAction()', 'startCreateAction()',
  // M2-B 遗留收口：凭据映射新建（建/删属 write 动作，viewer 只读）
  'openCredEditor()',
];
const REVIEW_BUTTON_ACTIONS = ["batchCandidates('approve')", "batchCandidates('reject')", "batchCandidates('restore')"];

function showLogin(message) {
  $('loginOverlay')?.classList.remove('op-hidden');
  $('appShell')?.classList.add('op-hidden');
  $('appHeader')?.classList.add('op-hidden');
  if (message) $('loginError').textContent = message;
}

function hideLogin() {
  $('loginOverlay')?.classList.add('op-hidden');
  $('appShell')?.classList.remove('op-hidden');
  $('appHeader')?.classList.remove('op-hidden');
}

async function loadSession() {
  try {
    const j = await api('/api/auth/me', { skipAuthRedirect: true });
    state.session = j.data;
    applyPermissionMatrix(j.data.permissions || {});
    $('loginError').textContent = '';
    hideLogin();
    // 业务页加载失败不应把已认证用户踢回登录页
    try { await bootApp(); } catch (e2) { clientLog('error', 'app boot failed', { message: e2.message }); }
  } catch (e) {
    state.session = null;
    showLogin(e.status === 401 ? '' : (e.message || '无法连接后端'));
  }
}

function applyPermissionMatrix(perms) {
  const tabs = new Set(perms.tabs || []);
  document.querySelectorAll('.tab[data-tab]').forEach(el => {
    el.classList.toggle('op-hidden', tabs.size > 0 && !tabs.has(el.dataset.tab));
  });
  const actions = new Set(perms.actions || []);
  document.querySelectorAll('button[onclick]').forEach(btn => {
    const act = btn.getAttribute('onclick');
    let need = null;
    if (WRITE_BUTTON_ACTIONS.includes(act)) need = 'write';
    else if (REVIEW_BUTTON_ACTIONS.includes(act)) need = 'review';
    if (!need) return;
    btn.dataset.permission = need;
    const allowed = actions.has(need);
    btn.disabled = !allowed;
    btn.title = allowed ? '' : '当前角色无此操作权限';
  });
  const u = state.session?.user;
  $('currentUserLabel').textContent = u ? `${u.username}（${perms.role_label || u.role}）` : '';
}

async function submitLogin() {
  const username = $('loginUsername').value.trim();
  const password = $('loginPassword').value;
  $('loginError').textContent = '';
  if (!username || !password) { $('loginError').textContent = '请输入账号与密码'; return; }
  $('loginSubmitBtn').disabled = true;
  try {
    await api('/api/auth/login', {
      method: 'POST', body: JSON.stringify({ username, password }), skipAuthRedirect: true,
    });
    $('loginPassword').value = '';
    await loadSession();
  } catch (e) {
    $('loginError').textContent = e.message || '登录失败';
  } finally {
    $('loginSubmitBtn').disabled = false;
  }
}

async function logoutUser() {
  try { await api('/api/auth/logout', { method: 'POST', skipAuthRedirect: true }); } catch (e) { /* ignore */ }
  state.session = null;
  showLogin('');
}

async function loadAuthUsers() {
  try {
    const j = await api('/api/auth/users?pageSize=200');
    const rows = j.data.items || [];
    const labels = { viewer: '只读', modeler: '建模人', reviewer: '审核人', admin: '管理员' };
    const levels = { read: '只读', write: '可写', review: '可审核', none: '显式拒绝' };
    $('authUserTableBody').innerHTML = rows.map(u => {
      const g = (u.grants || []).find(x => x.scope === 'project') || {};
      return `<tr class="border-t border-slate-700/40">
        <td class="py-2">${esc(u.username)}</td>
        <td><select id="role_${u.id}" class="bg-[#0f1627] border border-slate-700/60 rounded px-1 py-1">
          ${Object.keys(labels).map(r => `<option value="${r}" ${r === u.role ? 'selected' : ''}>${labels[r]}</option>`).join('')}
        </select></td>
        <td><input id="grantProject_${u.id}" class="bg-[#0f1627] border border-slate-700/60 rounded px-1 py-1 w-28" value="${esc(g.target || '')}" placeholder="项目 ID" /></td>
        <td><select id="grantLevel_${u.id}" class="bg-[#0f1627] border border-slate-700/60 rounded px-1 py-1">
          ${Object.keys(levels).map(l => `<option value="${l}" ${l === (g.level || 'read') ? 'selected' : ''}>${levels[l]}</option>`).join('')}
        </select></td>
        <td><button class="btn secondary" onclick="saveAuthUser('${u.id}')">保存</button></td>
      </tr>`;
    }).join('') || '<tr><td colspan="5" class="text-slate-500 py-3">暂无用户</td></tr>';
  } catch (e) { toast('加载用户失败：' + e.message, 'err'); }
}

async function saveAuthUser(id) {
  const role = $('role_' + id).value;
  const target = $('grantProject_' + id).value.trim();
  const level = $('grantLevel_' + id).value;
  const grants = target ? [{ scope: 'project', target, level }] : [];
  try {
    await api('/api/auth/users/' + id, { method: 'PUT', body: JSON.stringify({ role, grants }) });
    toast('授权已保存，目标用户无需重新登录即生效', 'ok');
    await loadAuthUsers();
    await loadAuthAudit();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
}

async function createAuthUser() {
  const username = $('newAuthUsername').value.trim();
  const password = $('newAuthPassword').value;
  const role = $('newAuthRole').value;
  if (!username || !password) { toast('请填写账号与初始密码', 'err'); return; }
  try {
    await api('/api/auth/users', { method: 'POST', body: JSON.stringify({ username, password, role }) });
    $('newAuthUsername').value = ''; $('newAuthPassword').value = '';
    toast('用户已创建', 'ok');
    await loadAuthUsers();
    await loadAuthAudit();
  } catch (e) { toast('创建失败：' + e.message, 'err'); }
}

async function loadAuthAudit() {
  try {
    const j = await api('/api/auth/audit?pageSize=30');
    $('authAuditBody').innerHTML = (j.data.items || []).map(a =>
      `<div class="border-b border-slate-700/40 py-1"><span class="text-slate-500">${esc((a.ts || '').slice(0, 19))}</span>
       <span class="text-slate-300">${esc(a.event)}</span> · ${esc(a.actor)} · ${esc(a.target || '')}</div>`
    ).join('') || '<div class="text-slate-500">暂无审计记录</div>';
  } catch (e) { /* 非管理员不展示 */ }
}

// ===================== V2-14i 凭据映射管理 UI（M2-B 遗留收口） =====================
// 后端契约见 docs/API-INTERFACE-DOC.md §23：/api/identity/mappings* ＋ /auth-types。
// 安全口径：secret 明文仅提交时传入一次，本地加密存储，页面/接口永不回显；
// 列表只展示 resolve-status 的 has_secret（已加密存储 / 缺失）。
// RBAC：本 tab 各角色可见（账号级能力）；建/删为 write 动作（viewer 只读），
// admin 可额外指定管理其他用户（后端 _check_owner 校验）。
let credAuthTypes = ['bearer_token', 'api_key', 'oauth2_client_credentials', 'basic'];
let credList = [];
let credStatusMap = {};   // mapping_id -> resolve-status 数据

function _credPerms() { return state.session?.permissions?.actions || []; }
function _credCanWrite() { return _credPerms().includes('write'); }
function _credIsAdmin() { return _credPerms().includes('admin'); }
function _credMeId() { return state.session?.user?.id || ''; }
function _credTargetUserId() {
  const sel = $('credUser');
  return (sel && sel.value) ? sel.value : _credMeId();
}

async function loadCredentials() {
  const me = state.session?.user;
  if (!me) return;
  const summary = $('credSummary');
  try {
    // 数据源连接（映射的绑定对象）
    let conns = connList;
    if (!conns.length) {
      try { await loadConnections(); conns = connList; } catch (e) { /* 连接加载失败不阻断映射列表 */ }
    }
    // 凭据类型下拉
    try {
      const tj = await api('/api/identity/auth-types');
      credAuthTypes = (tj.data.items && tj.data.items.length ? tj.data.items : credAuthTypes);
    } catch (e) { /* 回退内置列表 */ }
    // admin：可切换管理目标用户；非 admin 只能管理自己
    const userBox = $('credUserBox');
    if (_credIsAdmin()) {
      userBox.classList.remove('hidden');
      userBox.classList.add('flex');
      const sel = $('credUser');
      const prev = sel.value;
      let users = [];
      try {
        const uj = await api('/api/auth/users?pageSize=200');
        users = uj.data.items || [];
      } catch (e) { users = [me]; }
      sel.innerHTML = users.map(u =>
        `<option value="${esc(u.id)}" ${u.id === me.id ? 'selected' : ''}>${esc(u.username)}${u.id === me.id ? '（我）' : ''}</option>`
      ).join('');
      if (prev) sel.value = prev;
    } else {
      userBox.classList.add('hidden');
      userBox.classList.remove('flex');
    }
    const uid = _credTargetUserId();
    const j = await api('/api/identity/mappings' + (uid === me.id ? '' : '?user_id=' + encodeURIComponent(uid)));
    credList = j.data.items || [];
    // 逐行凭据状态（has_secret，无明文返回）
    const statuses = await Promise.all(credList.map(m =>
      api('/api/identity/mappings/' + m.id + '/resolve-status')
        .then(sj => [m.id, sj.data || {}])
        .catch(() => [m.id, {}])
    ));
    credStatusMap = Object.fromEntries(statuses);
    renderCredentials(conns);
  } catch (e) {
    if (summary) summary.textContent = '加载失败：' + e.message;
  }
}

function renderCredentials(conns) {
  const box = $('credTable');
  if (!box) return;
  const meId = _credMeId();
  const uid = _credTargetUserId();
  const isSelf = uid === meId;
  const summary = $('credSummary');
  if (summary) {
    summary.textContent = '共 ' + credList.length + ' 条映射' + (isSelf ? '（本人）' : '（目标用户）') +
      ' · 凭据列仅显示存储状态，不回显明文';
  }
  if (!credList.length) {
    box.innerHTML = '<div class="p-3 text-center text-slate-500 text-xs">暂无凭据映射。请先在 MCP 页创建数据源连接，再点「＋ 新建映射」。</div>';
    return;
  }
  const canWrite = _credCanWrite();
  const rows = credList.map(m => {
    const c = (conns || connList || []).find(x => x.connection_id === m.connection_id);
    const st = credStatusMap[m.id] || {};
    const badge = st.has_secret === undefined
      ? '<span class="text-slate-500">…（查询中）</span>'
      : (st.has_secret
        ? '<span class="text-emerald-400">● 已加密存储</span>'
        : '<span class="text-rose-400">● 缺失/无法解密（删除重建）</span>');
    return '<tr class="border-b border-slate-700/40 hover:bg-slate-800/30 align-top">' +
      '<td class="p-1.5">' + esc(c ? (c.display_name || c.connection_id) : (m.connection_id || '-')) +
        '<div class="text-slate-500 font-mono text-[11px]">' + esc(String(m.connection_id || '').slice(0, 14)) + '…</div></td>' +
      '<td class="p-1.5 font-mono text-xs text-slate-400">' + esc(m.auth_type || '-') + '</td>' +
      '<td class="p-1.5 text-xs">' + esc(m.principal || '—') + '</td>' +
      '<td class="p-1.5 text-xs whitespace-nowrap">' + badge + '</td>' +
      '<td class="p-1.5 text-xs text-slate-500 whitespace-nowrap">' + esc(String(m.created_at || '').replace('T', ' ').slice(0, 16)) + '</td>' +
      '<td class="p-1.5 whitespace-nowrap">' +
        '<button class="btn secondary" style="padding:2px 8px;font-size:12px" onclick="credCheckStatus(\'' + m.id + '\')">查状态</button> ' +
        (canWrite
          ? '<button class="btn danger" style="padding:2px 8px;font-size:12px" onclick="deleteCredMapping(\'' + m.id + '\')">删除</button>'
          : '<span class="text-slate-600 text-xs" title="只读角色不可删除（需建模及以上）">—</span>') +
      '</td></tr>';
  }).join('');
  box.innerHTML = '<table class="w-full text-left text-xs"><thead class="text-slate-500"><tr>' +
    '<th class="p-1.5">数据源连接</th><th class="p-1.5">凭据类型</th><th class="p-1.5">principal（外部账号）</th>' +
    '<th class="p-1.5">凭据状态</th><th class="p-1.5">创建时间</th><th class="p-1.5">操作</th></tr></thead><tbody>' + rows + '</tbody></table>';
}

function openCredEditor() {
  if (!_credCanWrite()) { toast('当前角色无映射管理权限（需建模及以上）', 'err'); return; }
  const box = $('credEditor');
  if (!box.classList.contains('hidden')) return;  // 已打开：保留用户输入
  let conns = connList;
  const doRender = () => {
    const connOpts = (conns.length ? conns : []).map(c =>
      '<option value="' + esc(c.connection_id) + '">' + esc(c.display_name || c.connection_id) + '</option>').join('');
    const typeOpts = credAuthTypes.map(t => '<option value="' + esc(t) + '">' + esc(t) + '</option>').join('');
    const sel = $('credUser');
    const targetLabel = (sel && sel.selectedOptions && sel.selectedOptions[0]) ? sel.selectedOptions[0].textContent : '本人';
    box.innerHTML = '<div class="border border-slate-700 rounded p-3 grid grid-cols-12 gap-2 text-sm">' +
      '<div class="col-span-4"><label class="text-xs text-slate-400">数据源连接 *</label>' +
        '<select id="credConn" class="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs">' +
        (connOpts || '<option value="">（暂无连接，请先在 MCP 页创建）</option>') + '</select></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">凭据类型 *</label>' +
        '<select id="credAuthType" class="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs">' + typeOpts + '</select></div>' +
      '<div class="col-span-2"><label class="text-xs text-slate-400">principal（外部账号）</label>' +
        '<input id="credPrincipal" class="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs" placeholder="可选，如 alice@crm"></div>' +
      '<div class="col-span-3"><label class="text-xs text-slate-400">凭据（一次性）*</label>' +
        '<input id="credSecret" type="password" class="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs" placeholder="token / api_key / 密码"></div>' +
      '<div class="col-span-12 flex items-center gap-3 flex-wrap">' +
        '<span class="text-[11px] text-slate-500">目标用户：' + esc(targetLabel) + ' · 明文仅本次保存使用，加密存储、永不回显</span>' +
        '<button class="btn text-xs" onclick="saveCredMapping()">保存</button>' +
        '<button class="btn secondary text-xs" onclick="closeCredEditor()">取消</button>' +
      '</div></div>';
    box.classList.remove('hidden');
    $('credSecret')?.focus();
  };
  if (!conns.length) {
    loadConnections().then(() => { conns = connList; doRender(); }).catch(() => doRender());
  } else {
    doRender();
  }
}

function closeCredEditor() {
  const box = $('credEditor');
  box.classList.add('hidden');
  box.innerHTML = '';
}

async function saveCredMapping() {
  const connection_id = $('credConn')?.value;
  const auth_type = $('credAuthType')?.value;
  const principal = ($('credPrincipal')?.value || '').trim();
  const secret = $('credSecret')?.value || '';
  if (!connection_id) { toast('请选择数据源连接（先在 MCP 页创建）', 'err'); return; }
  if (!auth_type) { toast('请选择凭据类型', 'err'); return; }
  if (!secret) { toast('凭据必填（仅提交一次，之后不回显）', 'err'); return; }
  try {
    await api('/api/identity/mappings', {
      method: 'POST',
      body: JSON.stringify({
        user_id: _credTargetUserId(),
        connection_id,
        auth_type,
        principal: principal || null,
        secret,
      }),
    });
    closeCredEditor();
    toast('凭据映射已保存（凭据已本地加密存储）', 'ok');
    await loadCredentials();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
}

async function deleteCredMapping(id) {
  if (!confirm('删除该凭据映射？本地加密凭据将一并删除，此后对应系统的委托代查/联邦接入将被 403 拒绝（缺映射，不回退匿名）。')) return;
  try {
    await api('/api/identity/mappings/' + id, { method: 'DELETE' });
    toast('凭据映射已删除', 'ok');
    await loadCredentials();
  } catch (e) { toast('删除失败：' + e.message, 'err'); }
}

async function credCheckStatus(id) {
  try {
    const sj = await api('/api/identity/mappings/' + id + '/resolve-status');
    credStatusMap[id] = sj.data || {};
    renderCredentials(connList);
    const st = credStatusMap[id] || {};
    toast(st.has_secret ? '凭据状态：已加密存储，可解析' : '凭据状态：缺失/无法解密，请删除后重建', st.has_secret ? 'ok' : 'err');
  } catch (e) { toast('状态查询失败：' + e.message, 'err'); }
}


/* ============================================================
 * V2-14 Action 编排（M3-F）— no-code 三面板编辑器
 * 节点库 / 画布（拖拽＋端口连线）/ 属性面板；干跑；运行/确认/拒绝/恢复；SOP 生成；片段生成
 * 执行侧零 LLM 决策（INV-3）；能力缺口不静默丢弃（EP-14-05）
 * ============================================================ */
const ORCH = {
  workflows: [], actions: [],
  wf: null, nodes: [], edges: [],
  layout: {}, selNode: null, selEdge: null, pendingPort: null,
  runs: [], selRun: null, dirty: false, nodeSeq: 0,
};

const ORCH_NODE_META = {
  start:          { label: '开始',     color: 'border-emerald-500 bg-emerald-600/20' },
  end:            { label: '结束',     color: 'border-slate-500 bg-slate-600/20' },
  action:         { label: '动作',     color: 'border-blue-500 bg-blue-600/20' },
  branch:         { label: '分支',     color: 'border-amber-500 bg-amber-600/20' },
  confirm:        { label: '人工确认', color: 'border-purple-500 bg-purple-600/20' },
  capability_gap: { label: '能力缺口', color: 'border-rose-500 bg-rose-600/20' },
};

const ORCH_OPS = ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'contains', 'not_contains', 'in', 'not_in'];

async function loadOrchestration() {
  try {
    const [wj, aj] = await Promise.all([
      api('/api/orchestration/workflows?page=1&pageSize=100'),
      api('/api/actions?page=1&pageSize=100'),
    ]);
    ORCH.workflows = wj.data.items || [];
    ORCH.actions = aj.data.items || [];
    if (ORCH.wf) {
      const cur = ORCH.workflows.find(w => w.id === ORCH.wf.id);
      if (!cur) ORCH.wf = null; else ORCH.wf = cur;
    }
    if (!ORCH.wf && ORCH.workflows.length) ORCH.wf = ORCH.workflows[0];
    renderOrchWfList();
    renderOrchNodeLib();
    if (ORCH.wf) await orchSelectWorkflow(ORCH.wf.id, true);
    else { ORCH.nodes = []; ORCH.edges = []; ORCH.layout = {}; orchRenderCanvas(); orchRenderProps(); orchRenderWfHead(); orchLoadRuns(); }
  } catch (e) { toast('编排加载失败：' + e.message, 'err'); }
}

function renderOrchWfList() {
  const el = $('orchWfList'); if (!el) return;
  if (!ORCH.workflows.length) { el.innerHTML = '<div class="text-xs text-slate-500">暂无工作流，点「新建」或从 SOP 生成。</div>'; return; }
  el.innerHTML = ORCH.workflows.map(w => {
    const st = w.status || 'draft';
    const stCls = st === 'published' ? 'bg-emerald-500/20 text-emerald-300' : (st === 'archived' ? 'bg-slate-500/20 text-slate-400' : 'bg-blue-500/20 text-blue-300');
    const hasGap = (w.nodes || []).some(n => n.kind === 'capability_gap');
    return `<div onclick="orchSelectWorkflow('${w.id}')" class="cursor-pointer rounded border p-2 text-xs ${w.id === (ORCH.wf && ORCH.wf.id) ? 'border-blue-500 bg-blue-500/10' : 'border-slate-700 hover:border-slate-500'}">
      <div class="flex items-center gap-1"><span class="font-medium truncate">${esc(w.name)}</span>
        <span class="ml-auto px-1.5 py-0.5 rounded text-[10px] ${stCls}">${st === 'published' ? '已发布' : st === 'archived' ? '已归档' : '草稿'}</span></div>
      <div class="text-slate-500 mt-0.5 flex items-center gap-1">${(w.nodes || []).length} 节点
        ${hasGap ? '<span class="text-rose-400">含能力缺口（阻断发布）</span>' : ''}</div>
    </div>`;
  }).join('');
}

function renderOrchNodeLib() {
  const el = $('orchNodeLib'); if (!el) return;
  el.innerHTML = Object.entries(ORCH_NODE_META).map(([kind, m]) =>
    `<div draggable="true" ondragstart="orchLibDragStart(event,'${kind}')" onclick="orchAddNode('${kind}')"
      class="cursor-grab rounded border px-2 py-1.5 text-xs text-center hover:brightness-125 ${m.color}">${m.label}</div>`
  ).join('');
}

function orchLibDragStart(e, kind) { e.dataTransfer.setData('text/orch-kind', kind); e.dataTransfer.effectAllowed = 'copy'; }

async function orchNewWorkflow() {
  try {
    const r = await api('/api/orchestration/workflows', {
      method: 'POST',
      body: JSON.stringify({ name: '工作流 ' + (ORCH.workflows.length + 1), trigger: { type: 'manual' },
        nodes: [{ id: 'start', kind: 'start' }, { id: 'end', kind: 'end' }],
        edges: [{ id: 'e1', from: 'start', to: 'end' }] }),
    });
    toast('已创建：' + (r.data.name || ''), 'ok');
    ORCH.wf = r.data;
    await orchSelectWorkflow(r.data.id, true);
    await loadOrchestration();
  } catch (e) { toast('新建失败：' + e.message, 'err'); }
}

async function orchSelectWorkflow(id, keepCanvas) {
  try {
    const r = await api('/api/orchestration/workflows/' + id);
    ORCH.wf = r.data;
    ORCH.nodes = (ORCH.wf.nodes || []).map(n => JSON.parse(JSON.stringify(n)));
    ORCH.edges = (ORCH.wf.edges || []).map(e => JSON.parse(JSON.stringify(e)));
    ORCH.layout = {}; ORCH.selNode = null; ORCH.selEdge = null; ORCH.pendingPort = null; ORCH.dirty = false;
    ORCH.nodes.forEach((n, i) => { ORCH.layout[n.id] = { x: 60 + (i % 3) * 230, y: 60 + Math.floor(i / 3) * 130 }; });
    orchAutoLayout();
    renderOrchWfList(); orchRenderWfHead(); orchRenderCanvas(); orchRenderProps(); orchLoadRuns();
  } catch (e) { toast('加载工作流失败：' + e.message, 'err'); }
}

function orchRenderWfHead() {
  const name = $('orchWfName'), st = $('orchWfStatus');
  if (!name) return;
  if (!ORCH.wf) { name.textContent = '未选择工作流'; st.innerHTML = ''; return; }
  name.textContent = ORCH.wf.name + (ORCH.dirty ? '（未保存）' : '');
  const s = ORCH.wf.status || 'draft';
  st.innerHTML = `<span class="text-xs px-1.5 py-0.5 rounded ${s === 'published' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-slate-500/20 text-slate-400'}">${s === 'published' ? '已发布' : '草稿'}</span>`;
}

function orchAddNode(kind, x, y) {
  if (!ORCH.wf) { toast('请先新建或选择工作流', 'err'); return; }
  ORCH.nodeSeq += 1;
  let id;
  do { id = (kind === 'start' ? 'start' : kind === 'end' ? 'end' : kind + ORCH.nodeSeq); }
  while (ORCH.nodes.some(n => n.id === id));
  const node = { id, kind, label: ORCH_NODE_META[kind].label };
  if (kind === 'action') Object.assign(node, { action_id: '', params: {}, failure_policy: 'retry', retry_max: 2 });
  if (kind === 'branch') Object.assign(node, { branches: {
    p1: { label: '条件1', condition: { logic: 'AND', rules: [] } },
    d: { label: '默认', is_default: true, condition: null },
  } });
  if (kind === 'confirm') Object.assign(node, { confirm_title: '请确认是否执行' });
  if (kind === 'capability_gap') Object.assign(node, { gap_reason: '该步骤暂无已绑定 Action 可支撑（能力缺口，发布被阻断）' });
  if (kind === 'start') node = { id, kind: 'start', label: '开始' };
  if (kind === 'end') node = { id, kind: 'end', label: '结束' };
  ORCH.nodes.push(node);
  ORCH.layout[id] = { x: x != null ? x : 80 + Math.random() * 300, y: y != null ? y : 80 + Math.random() * 300 };
  ORCH.selNode = id; ORCH.dirty = true;
  orchRenderCanvas(); orchRenderProps(); orchRenderWfHead();
}

function orchDelNode(id) {
  const idx = ORCH.nodes.findIndex(n => n.id === id);
  if (idx < 0) return;
  if ((ORCH.nodes[idx].kind === 'start' || ORCH.nodes[idx].kind === 'end') && ORCH.nodes.filter(n => n.kind === ORCH.nodes[idx].kind).length <= 1) {
    toast('start/end 各须保留一个', 'err'); return;
  }
  ORCH.nodes.splice(idx, 1);
  ORCH.edges = ORCH.edges.filter(e => e.from !== id && e.to !== id);
  delete ORCH.layout[id];
  ORCH.selNode = null; ORCH.dirty = true;
  orchRenderCanvas(); orchRenderProps();
}

function orchPortClick(e, nodeId, port) {
  e.stopPropagation();
  if (ORCH.pendingPort && ORCH.pendingPort.node === nodeId && ORCH.pendingPort.port === port) { ORCH.pendingPort = null; orchRenderCanvas(); return; }
  if (!ORCH.pendingPort) { ORCH.pendingPort = { node: nodeId, port: port || 'default' }; orchRenderCanvas(); return; }
  if (ORCH.pendingPort.node === nodeId) { ORCH.pendingPort = null; orchRenderCanvas(); return; }
  const fromPort = ORCH.pendingPort.port || 'default';
  const dup = ORCH.edges.some(ed => ed.from === ORCH.pendingPort.node && ed.to === nodeId && (ed.from_port || 'default') === fromPort);
  if (dup) { toast('该连线已存在', 'err'); }
  else {
    ORCH.edgeSeq = (ORCH.edgeSeq || 0) + 1;
    ORCH.edges.push({ id: 'e' + Date.now().toString(36) + ORCH.edgeSeq, from: ORCH.pendingPort.node, to: nodeId, from_port: fromPort === 'default' ? undefined : fromPort });
    ORCH.dirty = true;
  }
  ORCH.pendingPort = null;
  orchRenderCanvas();
}

function orchDelEdge(id) {
  ORCH.edges = ORCH.edges.filter(e => e.id !== id);
  ORCH.selEdge = null; ORCH.dirty = true;
  orchRenderCanvas();
}

const ORCH_NODE_W = 170;

function orchNodePorts(node) {
  if (node.kind === 'branch') return Object.keys(node.branches || {});
  return ['default'];
}

function orchRenderCanvas() {
  const box = $('orchNodes'); if (!box) return;
  box.innerHTML = ORCH.nodes.map(n => {
    const pos = ORCH.layout[n.id] || { x: 60, y: 60 };
    const m = ORCH_NODE_META[n.kind] || ORCH_NODE_META.action;
    const ports = orchNodePorts(n);
    const outs = ports.map((p, i) => {
      const active = ORCH.pendingPort && ORCH.pendingPort.node === n.id && (ORCH.pendingPort.port || 'default') === p;
      return `<div class="orch-port absolute -right-1.5 w-3 h-3 rounded-full cursor-pointer border border-slate-300 ${active ? 'bg-emerald-400' : 'bg-slate-400'} hover:bg-emerald-300"
        style="top:${22 + i * 22 - 6}px" title="输出端口 ${esc(p)}（点击→点目标输入端口连线）"
        onclick="orchPortClick(event,${JSON.stringify(n.id).replace(/"/g, '&quot;')},${JSON.stringify(p).replace(/"/g, '&quot;')})"></div>`;
    }).join('');
    let extra = '';
    if (n.kind === 'action') {
      const a = ORCH.actions.find(x => x.action_id === n.action_id);
      extra = a ? esc(a.name) + ' <span class="text-[10px] opacity-70">(' + (a.risk_level || 'high') + ')</span>' : '<span class="text-rose-300">未选择 Action</span>';
    } else if (n.kind === 'branch') {
      extra = ports.map(p => `<div class="text-[10px] opacity-80">${esc(p)}：${esc(((n.branches || {})[p] || {}).label || p)}${(n.branches || {})[p] && (n.branches[p]).is_default ? '（默认）' : ''}</div>`).join('');
    } else if (n.kind === 'capability_gap') {
      extra = '<div class="text-[10px] text-rose-200">发布被阻断（EP-14-05）</div>';
    } else if (n.kind === 'confirm') {
      extra = `<div class="text-[10px] opacity-80">${esc(n.confirm_title || '')}</div>`;
    }
    return `<div data-oid="${n.id}" class="orch-node absolute rounded-md border-2 shadow-lg select-none ${m.color} ${ORCH.selNode === n.id ? 'ring-2 ring-blue-400' : ''}"
      style="left:${pos.x}px;top:${pos.y}px;width:${ORCH_NODE_W}px;cursor:grab">
      <div class="px-2 py-1 rounded-t text-xs font-medium flex items-center gap-1">${m.label}<span class="opacity-70 truncate">· ${esc(n.label || n.id)}</span></div>
      <div class="px-2 py-1.5 text-xs min-h-[28px]">${extra}</div>
      <div class="orch-inport absolute -left-1.5 w-3 h-3 rounded-full bg-slate-300 border border-slate-500 cursor-pointer hover:bg-emerald-300" style="top:50%;transform:translateY(-50%)" title="输入端口"
        onclick="orchPortClick(event,'${n.id}',null)"></div>
      ${outs}
    </div>`;
  }).join('');
  box.querySelectorAll('.orch-node').forEach(el => orchBindNodeDrag(el));
  orchRenderEdges();
  const sel = $('orchFragmentAfter');
  if (sel) sel.innerHTML = '<option value="">锚点：流程末尾/起点</option>' +
    ORCH.nodes.map(n => `<option value="${n.id}">${esc(n.label || n.id)}（${n.kind}）</option>`).join('');
}

function orchBindNodeDrag(el) {
  const id = el.getAttribute('data-oid');
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || e.target.classList.contains('orch-port') || e.target.classList.contains('orch-inport')) return;
    const start = ORCH.layout[id] || { x: 60, y: 60 };
    const sx = e.clientX, sy = e.clientY;
    el.style.cursor = 'grabbing';
    el.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const x = Math.max(0, Math.min(1600, start.x + ev.clientX - sx));
      const y = Math.max(0, Math.min(980, start.y + ev.clientY - sy));
      ORCH.layout[id] = { x, y };
      el.style.left = x + 'px'; el.style.top = y + 'px';
      orchRenderEdges();
    };
    const up = (ev) => {
      el.style.cursor = 'grab';
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      ORCH.dirty = true;
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    ORCH.selNode = id; ORCH.selEdge = null;
    // 仅更新选中态样式，不重渲染画布 DOM（重渲染会使 el 被移除 → 丢失 pointer capture，拖拽失效）
    const box = el.parentElement;
    if (box) box.querySelectorAll('.orch-node').forEach(x => x.classList.remove('ring-2', 'ring-blue-400'));
    el.classList.add('ring-2', 'ring-blue-400');
    orchRenderProps();
  });
}

function orchPortCenter(nodeId, port, isOut) {
  const inner = $('orchCanvasInner');
  const el = isOut
    ? inner.querySelector(`.orch-node[data-oid="${nodeId}"] .orch-port[title*="${port}"]`)
    : inner.querySelector(`.orch-node[data-oid="${nodeId}"] .orch-inport`);
  const pos = ORCH.layout[nodeId] || { x: 60, y: 60 };
  if (el && inner) {
    const r = el.getBoundingClientRect(), ir = inner.getBoundingClientRect();
    return { x: r.left - ir.left + r.width / 2, y: r.top - ir.top + r.height / 2 };
  }
  const h = 64;
  return isOut ? { x: pos.x + ORCH_NODE_W + 6, y: pos.y + h / 2 } : { x: pos.x - 6, y: pos.y + h / 2 };
}

function orchRenderEdges() {
  const svg = $('orchEdgesSvg'); if (!svg) return;
  svg.querySelectorAll('.orch-edge-g').forEach(g => g.remove());
  const NS = 'http://www.w3.org/2000/svg';
  ORCH.edges.forEach(ed => {
    const p1 = orchPortCenter(ed.from, ed.from_port || 'default', true);
    const p2 = orchPortCenter(ed.to, null, false);
    const dx = Math.max(40, Math.abs(p2.x - p1.x) / 2);
    const g = document.createElementNS(NS, 'g');
    g.setAttribute('class', 'orch-edge-g');
    g.style.cursor = 'pointer';
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`);
    path.setAttribute('stroke', ORCH.selEdge === ed.id ? '#38bdf8' : '#64748b');
    path.setAttribute('stroke-width', ORCH.selEdge === ed.id ? 2.5 : 1.5);
    path.setAttribute('marker-end', 'url(#orchArrow)');
    g.appendChild(path);
    if (ed.from_port) {
      const t = document.createElementNS(NS, 'text');
      t.setAttribute('x', (p1.x + p2.x) / 2); t.setAttribute('y', (p1.y + p2.y) / 2 - 6);
      t.setAttribute('fill', '#94a3b8'); t.setAttribute('font-size', '11'); t.setAttribute('text-anchor', 'middle');
      t.textContent = ed.from_port;
      g.appendChild(t);
    }
    g.addEventListener('click', (e) => { e.stopPropagation(); ORCH.selEdge = ORCH.selEdge === ed.id ? null : ed.id; ORCH.selNode = null; orchRenderCanvas(); });
    svg.appendChild(g);
  });
}

document.addEventListener('dragover', (e) => {
  const c = $('orchCanvas');
  if (c && e.dataTransfer && [...e.dataTransfer.types].includes('text/orch-kind')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }
});
document.addEventListener('drop', (e) => {
  const kind = e.dataTransfer && e.dataTransfer.getData('text/orch-kind');
  const c = $('orchCanvas');
  if (!kind || !c) return;
  e.preventDefault();
  const ir = $('orchCanvasInner').getBoundingClientRect();
  orchAddNode(kind, Math.max(0, e.clientX - ir.left - ORCH_NODE_W / 2), Math.max(0, e.clientY - ir.top - 20));
});

document.addEventListener('keydown', (e) => {
  if (_lastTab !== 'orchestration' || (ORCH.selNode === null && ORCH.selEdge === null)) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
  if (e.key === 'Delete' || e.key === 'Backspace') {
    e.preventDefault();
    if (ORCH.selNode) orchDelNode(ORCH.selNode);
    else if (ORCH.selEdge) orchDelEdge(ORCH.selEdge);
  }
});

function orchAutoLayout() {
  if (!ORCH.nodes.length) return;
  const preds = {};
  ORCH.nodes.forEach(n => { preds[n.id] = []; });
  ORCH.edges.forEach(e => { if (preds[e.to]) preds[e.to].push(e.from); });
  const layer = {}, visited = {};
  const calc = (id, depth) => {
    if (visited[id]) return;
    visited[id] = true;
    layer[id] = Math.max(layer[id] || 0, depth);
    (ORCH.edges.filter(e => e.from === id).map(e => e.to)).forEach(t => calc(t, (layer[id] || 0) + 1));
  };
  const starts = ORCH.nodes.filter(n => n.kind === 'start').map(n => n.id);
  (starts.length ? starts : ORCH.nodes.map(n => n.id)).forEach(s => calc(s, 0));
  ORCH.nodes.forEach(n => { if (layer[n.id] == null) layer[n.id] = 0; });
  const byLayer = {};
  ORCH.nodes.forEach(n => { (byLayer[layer[n.id]] = byLayer[layer[n.id]] || []).push(n); });
  Object.entries(byLayer).forEach(([ly, arr]) => arr.forEach((n, i) => {
    ORCH.layout[n.id] = { x: 60 + (+ly) * 230, y: 80 + i * 130 };
  }));
  orchRenderCanvas();
}

/* ---------- 属性面板 ---------- */
function orchRenderProps() {
  const el = $('orchProps'); if (!el) return;
  if (!ORCH.wf) { el.innerHTML = '<div class="text-xs text-slate-500">先选择/新建工作流。</div>'; return; }
  if (!ORCH.selNode) {
    const t = ORCH.wf.trigger || { type: 'manual' };
    el.innerHTML = `
      <div><label class="text-xs text-slate-400">名称</label>
        <input id="orchWfName2" class="w-full text-sm" value="${esc(ORCH.wf.name || '')}" oninput="ORCH.wf.name=this.value;ORCH.dirty=true;orchRenderWfHead()"></div>
      <div><label class="text-xs text-slate-400">描述</label>
        <input id="orchWfDesc" class="w-full text-sm" value="${esc(ORCH.wf.description || '')}" oninput="ORCH.wf.description=this.value;ORCH.dirty=true"></div>
      <div class="pt-1 border-t border-slate-800 mt-2"><label class="text-xs text-slate-400">触发器</label>
        <select class="w-full text-sm mt-1" onchange="orchTriggerTypeChange(this.value)">
          <option value="manual" ${t.type === 'manual' ? 'selected' : ''}>手动</option>
          <option value="event" ${t.type === 'event' ? 'selected' : ''}>事件（业务事件总线）</option>
          <option value="cron" ${t.type === 'cron' ? 'selected' : ''}>定时（cron）</option>
        </select></div>
      <div id="orchTriggerExtra" class="space-y-2">${orchTriggerExtraHtml(t)}</div>
      <div class="text-xs text-slate-500 pt-2 border-t border-slate-800">选中画布节点后可编辑节点属性。</div>`;
    return;
  }
  const n = ORCH.nodes.find(x => x.id === ORCH.selNode);
  if (!n) { el.innerHTML = ''; return; }
  let html = `<div class="flex items-center gap-2 mb-1">
    <span class="text-xs font-medium">${ORCH_NODE_META[n.kind].label}</span>
    <span class="text-xs text-slate-500">${esc(n.id)}</span>
    <button class="ml-auto text-xs text-rose-400" onclick="orchDelNode('${n.id}')">删除</button></div>
    <div><label class="text-xs text-slate-400">标签</label>
      <input class="w-full text-sm" value="${esc(n.label || '')}" oninput="orchNodeField('${n.id}','label',this.value)"></div>`;
  if (n.kind === 'action') {
    html += `
      <div><label class="text-xs text-slate-400">Action（白名单内）</label>
        <select class="w-full text-sm" onchange="orchNodeField('${n.id}','action_id',this.value)">
          <option value="">— 请选择 —</option>
          ${ORCH.actions.map(a => `<option value="${a.action_id}" ${n.action_id === a.action_id ? 'selected' : ''}>${esc(a.name)}（${a.risk_level || 'high'}）</option>`).join('')}
        </select></div>
      <div><label class="text-xs text-slate-400">参数（key + 值来源）</label>
        <div class="space-y-1 mt-1" id="orchParamRows">${orchParamRowsHtml(n)}</div>
        <button class="btn-ghost text-xs mt-1" onclick="orchAddParamRow('${n.id}')">＋ 参数</button></div>
      <div><label class="text-xs text-slate-400">失败策略</label>
        <div class="flex gap-1">
          <select class="flex-1 text-sm" onchange="orchNodeField('${n.id}','failure_policy',this.value)">
            <option value="retry" ${n.failure_policy === 'retry' ? 'selected' : ''}>重试</option>
            <option value="skip" ${n.failure_policy === 'skip' ? 'selected' : ''}>跳过</option>
            <option value="abort" ${n.failure_policy === 'abort' ? 'selected' : ''}>终止</option>
          </select>
          <input type="number" min="0" max="10" class="w-14 text-sm" title="重试次数" value="${n.retry_max != null ? n.retry_max : 2}"
            onchange="orchNodeField('${n.id}','retry_max',parseInt(this.value||'0',10))"></div></div>`;
  }
  if (n.kind === 'branch') {
    const ports = Object.entries(n.branches || {});
    html += `<div><label class="text-xs text-slate-400">分支端口（按序求值，默认端口兜底）</label>
      <div class="space-y-2 mt-1">` + ports.map(([pid, b]) => `
        <div class="rounded border border-slate-700 p-2 space-y-1">
          <div class="flex items-center gap-1">
            <input class="w-14 text-sm" value="${esc(pid)}" title="端口ID（唯一）" onchange="orchBranchPortRename('${n.id}','${pid}',this.value)">
            <input class="flex-1 text-sm" value="${esc(b.label || '')}" placeholder="分支标签" oninput="orchBranchField('${n.id}','${pid}','label',this.value)">
            <label class="text-xs flex items-center gap-1 cursor-pointer"><input type="checkbox" ${b.is_default ? 'checked' : ''}
              onchange="orchBranchDefault('${n.id}','${pid}',this.checked)">默认</label>
            <button class="text-rose-400 text-xs" onclick="orchBranchDelPort('${n.id}','${pid}')">×</button>
          </div>
          ${b.is_default ? '' : `
          <select class="text-xs" onchange="orchBranchField('${n.id}','${pid}','logic',this.value)">
            <option value="AND" ${(!b.condition || b.condition.logic === 'AND') ? 'selected' : ''}>AND</option>
            <option value="OR" ${b.condition && b.condition.logic === 'OR' ? 'selected' : ''}>OR</option>
          </select>
          <div class="space-y-1" id="orchRules_${n.id}_${pid}">${orchRulesHtml(n, pid, b)}</div>
          <button class="btn-ghost text-xs" onclick="orchAddRule('${n.id}','${pid}')">＋ 条件（引用 trigger.x / node:nodeId.body.字段）</button>`}
        </div>`).join('') + `</div>
      <button class="btn-ghost text-xs mt-1" onclick="orchAddPort('${n.id}')">＋ 分支端口</button></div>`;
  }
  if (n.kind === 'confirm') {
    html += `<div><label class="text-xs text-slate-400">确认标题</label>
      <input class="w-full text-sm" value="${esc(n.confirm_title || '')}" oninput="orchNodeField('${n.id}','confirm_title',this.value)"></div>
      <div class="text-xs text-slate-500">高风险 Action 自动在此暂停并等待人工签核（超时 TTL 自动取消留痕）。</div>`;
  }
  if (n.kind === 'capability_gap') {
    html += `<div><label class="text-xs text-slate-400">缺口原因</label>
      <textarea class="w-full text-sm" rows="3" oninput="orchNodeField('${n.id}','gap_reason',this.value)">${esc(n.gap_reason || '')}</textarea></div>
      <div class="text-xs text-rose-400">含能力缺口的工作流发布被阻断（EP-14-05）；绑定对应 Action 后替换为 action 节点。</div>`;
  }
  el.innerHTML = html;
}

function orchTriggerExtraHtml(t) {
  if (t.type === 'event') return `
    <div><label class="text-xs text-slate-400">事件名</label>
      <input class="w-full text-sm" value="${esc(t.event_topic || '')}" oninput="orchTriggerField('event_topic',this.value)"></div>
    <div><label class="text-xs text-slate-400">必需上下文（逗号分隔）</label>
      <input class="w-full text-sm" value="${esc((t.required_ctx || []).join(','))}" oninput="orchTriggerField('required_ctx',this.value)"></div>
    <div class="text-xs text-slate-500">payload 字段经 trigger.字段 注入节点参数。</div>`;
  if (t.type === 'cron') return `
    <div><label class="text-xs text-slate-400">cron 表达式</label>
      <input class="w-full text-sm" value="${esc(t.cron_expr || '')}" oninput="orchTriggerField('cron_expr',this.value)" placeholder="* * * * *"></div>
    <div><label class="text-xs text-slate-400">静态 payload（JSON）</label>
      <input class="w-full text-sm" value="${esc(t.static_payload || '')}" oninput="orchTriggerField('static_payload',this.value)" placeholder='{"k":"v"}'></div>`;
  return '<div class="text-xs text-slate-500">手动运行：右上「运行」按钮（payload 可选）。</div>';
}

function orchTriggerTypeChange(type) {
  const t = Object.assign({}, ORCH.wf.trigger, { type });
  if (type === 'event') Object.assign(t, { event_topic: t.event_topic || '', required_ctx: t.required_ctx || [] });
  if (type === 'cron') Object.assign(t, { cron_expr: t.cron_expr || '', static_payload: t.static_payload || '' });
  ORCH.wf.trigger = t; ORCH.dirty = true; orchRenderProps();
}
function orchTriggerField(k, v) {
  const t = ORCH.wf.trigger = Object.assign({}, ORCH.wf.trigger);
  if (k === 'required_ctx') t.required_ctx = v.split(',').map(s => s.trim()).filter(Boolean);
  else if (k === 'static_payload') {
    const s = v.trim();
    if (!s) t.static_payload = '';
    else { try { t.static_payload = JSON.parse(s); } catch (e) { t.static_payload = s; } }
  } else t[k] = v;
  ORCH.dirty = true;
}

function orchNodeField(id, k, v) {
  const n = ORCH.nodes.find(x => x.id === id); if (!n) return;
  n[k] = v; ORCH.dirty = true; orchRenderWfHead();
  if (n.kind === 'branch' || k === 'label') orchRenderCanvas();
}

function orchParamRowsHtml(n) {
  const rows = Object.entries(n.params || {});
  if (!rows.length) return '<div class="text-xs text-slate-500">无参数</div>';
  return rows.map(([k, v]) => `
    <div class="flex gap-1">
      <input class="w-20 text-xs" value="${esc(k)}" title="参数名" onchange="orchParamRename('${n.id}','${esc(k)}',this.value)">
      <select class="w-20 text-xs" onchange="orchParamVType('${n.id}','${esc(k)}',this.value)">
        <option value="literal" ${(v.value_type || 'literal') === 'literal' ? 'selected' : ''}>字面量</option>
        <option value="field_ref" ${v.value_type === 'field_ref' ? 'selected' : ''}>上游字段</option>
        <option value="trigger_ctx" ${v.value_type === 'trigger_ctx' ? 'selected' : ''}>触发上下文</option>
      </select>
      <input class="flex-1 text-xs" value="${esc(v.value != null ? v.value : '')}" placeholder="${v.value_type === 'field_ref' ? 'node:节点ID.body.字段' : v.value_type === 'trigger_ctx' ? 'trigger.字段' : '值'}"
        oninput="orchParamValue('${n.id}','${esc(k)}',this.value)">
      <button class="text-rose-400 text-xs" onclick="orchParamDel('${n.id}','${esc(k)}')">×</button>
    </div>`).join('');
}
function _orchNode(id) { const n = ORCH.nodes.find(x => x.id === id); if (!n) return null; n.params = n.params || {}; return n; }
function orchAddParamRow(id) {
  const n = _orchNode(id); if (!n) return;
  let k = 'p' + Object.keys(n.params).length; while (n.params[k]) k = k + 'x';
  n.params[k] = { value_type: 'literal', value: '' };
  ORCH.dirty = true; orchRenderProps();
}
function orchParamRename(id, oldK, newK) {
  const n = _orchNode(id); if (!n || !newK || newK === oldK || n.params[newK]) return;
  n.params[newK] = n.params[oldK]; delete n.params[oldK];
  ORCH.dirty = true; orchRenderProps();
}
function orchParamVType(id, k, vt) { const n = _orchNode(id); if (!n) return; n.params[k] = Object.assign({}, n.params[k], { value_type: vt }); ORCH.dirty = true; }
function orchParamValue(id, k, v) { const n = _orchNode(id); if (!n) return; n.params[k] = Object.assign({}, n.params[k], { value: v }); ORCH.dirty = true; orchRenderWfHead(); }
function orchParamDel(id, k) { const n = _orchNode(id); if (!n) return; delete n.params[k]; ORCH.dirty = true; orchRenderProps(); }

function orchRulesHtml(n, pid, b) {
  const rules = (b.condition && b.condition.rules) || [];
  if (!rules.length) return '<div class="text-xs text-slate-500">无条件（恒真）</div>';
  return rules.map((r, i) => `
    <div class="flex gap-1">
      <input class="flex-1 text-xs" value="${esc(r.field || '')}" placeholder="node:节点ID.body.字段" oninput="orchRuleField('${n.id}','${pid}',${i},'field',this.value)">
      <select class="w-20 text-xs" onchange="orchRuleField('${n.id}','${pid}',${i},'op',this.value)">
        ${ORCH_OPS.map(op => `<option ${r.op === op ? 'selected' : ''}>${op}</option>`).join('')}
      </select>
      <input class="w-16 text-xs" value="${esc(r.value != null ? r.value : '')}" oninput="orchRuleField('${n.id}','${pid}',${i},'value',this.value)">
      <button class="text-rose-400 text-xs" onclick="orchRuleDel('${n.id}','${pid}',${i})">×</button>
    </div>`).join('');
}
function _orchBranch(n, pid) { if (!n) return null; n.branches = n.branches || {}; if (!n.branches[pid]) n.branches[pid] = { label: pid, condition: { logic: 'AND', rules: [] } }; return n.branches[pid]; }
function orchAddRule(id, pid) {
  const n = ORCH.nodes.find(x => x.id === id); const b = _orchBranch(n, pid); if (!b) return;
  b.condition = b.condition || { logic: 'AND', rules: [] };
  b.condition.rules.push({ field: '', op: 'eq', value: '' });
  ORCH.dirty = true; orchRenderProps();
}
function orchRuleField(id, pid, i, k, v) {
  const n = ORCH.nodes.find(x => x.id === id); const b = _orchBranch(n, pid); if (!b) return;
  b.condition = b.condition || { logic: 'AND', rules: [] };
  b.condition.rules[i] = Object.assign({}, b.condition.rules[i], { [k]: v });
  ORCH.dirty = true; orchRenderWfHead();
}
function orchRuleDel(id, pid, i) {
  const n = ORCH.nodes.find(x => x.id === id); const b = _orchBranch(n, pid); if (!b) return;
  b.condition = b.condition || { logic: 'AND', rules: [] };
  b.condition.rules.splice(i, 1);
  ORCH.dirty = true; orchRenderProps();
}
function orchBranchField(id, pid, k, v) {
  const n = ORCH.nodes.find(x => x.id === id); const b = _orchBranch(n, pid); if (!b) return;
  if (k === 'logic') { b.condition = Object.assign({}, b.condition, { logic: v }); }
  else b[k] = v;
  ORCH.dirty = true;
}
function orchBranchDefault(id, pid, isDefault) {
  const n = ORCH.nodes.find(x => x.id === id); if (!n) return;
  if (isDefault) Object.keys(n.branches).forEach(p => { n.branches[p].is_default = false; });
  n.branches[pid].is_default = isDefault;
  ORCH.dirty = true; orchRenderProps(); orchRenderCanvas();
}
function orchBranchPortRename(id, oldPid, newPid) {
  const n = ORCH.nodes.find(x => x.id === id); if (!n || !newPid || newPid === oldPid || n.branches[newPid]) return;
  n.branches[newPid] = n.branches[oldPid]; delete n.branches[oldPid];
  ORCH.edges.forEach(e => { if (e.from === id && e.from_port === oldPid) e.from_port = newPid; });
  ORCH.dirty = true; orchRenderProps(); orchRenderCanvas();
}
function orchBranchDelPort(id, pid) {
  const n = ORCH.nodes.find(x => x.id === id); if (!n) return;
  if (Object.keys(n.branches).length <= 1) { toast('至少保留一个分支端口', 'err'); return; }
  delete n.branches[pid];
  ORCH.edges = ORCH.edges.filter(e => !(e.from === id && e.from_port === pid));
  ORCH.dirty = true; orchRenderProps(); orchRenderCanvas();
}
function orchAddPort(id) {
  const n = ORCH.nodes.find(x => x.id === id); if (!n) return;
  let pid = 'p' + (Object.keys(n.branches).length + 1);
  while (n.branches[pid]) pid = pid + 'x';
  n.branches[pid] = { label: pid, condition: { logic: 'AND', rules: [] } };
  ORCH.dirty = true; orchRenderProps(); orchRenderCanvas();
}

/* ---------- 保存 / 干跑 / 发布 / 运行 ---------- */
function _orchSpec() {
  const t = ORCH.wf.trigger || { type: 'manual' };
  const trigger = { type: t.type };
  if (t.type === 'event') { trigger.event_topic = t.event_topic; trigger.required_ctx = t.required_ctx; }
  if (t.type === 'cron') {
    trigger.cron_expr = t.cron_expr;
    let sp = t.static_payload;
    if (typeof sp === 'string') {
      const s = sp.trim();
      try { sp = s ? JSON.parse(s) : {}; } catch (e) { sp = {}; }
    }
    trigger.static_payload = sp || {};
  }
  return { name: ORCH.wf.name, description: ORCH.wf.description || '', nodes: ORCH.nodes, edges: ORCH.edges, trigger };
}

async function orchSaveWorkflow() {
  if (!ORCH.wf) { toast('未选择工作流', 'err'); return; }
  try {
    const r = await api('/api/orchestration/workflows/' + ORCH.wf.id, { method: 'PUT', body: JSON.stringify(_orchSpec()) });
    ORCH.wf = r.data; ORCH.dirty = false;
    toast('已保存（DAG 校验通过）', 'ok');
    orchRenderWfHead(); renderOrchWfList();
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
}

async function orchDryRun() {
  if (!ORCH.wf) { toast('未选择工作流', 'err'); return; }
  let payload = {};
  const raw = ($('orchRunPayload') || {}).value;
  if (raw && raw.trim()) { try { payload = JSON.parse(raw); } catch (e) { toast('payload 不是合法 JSON', 'err'); return; } }
  try {
    const r = await api('/api/orchestration/workflows/' + ORCH.wf.id + '/dry-run', { method: 'POST', body: JSON.stringify({ payload }) });
    const d = r.data || {};
    const box = $('orchDryRunBox'); box.classList.remove('hidden');
    const stCls = { ok: 'bg-emerald-500/20 text-emerald-300', preview: 'bg-emerald-500/20 text-emerald-300', preview_ok: 'bg-emerald-500/20 text-emerald-300',
      awaiting_confirmation: 'bg-amber-500/20 text-amber-300', gate_failed: 'bg-rose-500/20 text-rose-300',
      error: 'bg-rose-500/20 text-rose-300', capability_gap: 'bg-rose-500/20 text-rose-300', not_reached: 'bg-slate-600/40 text-slate-400' };
    $('orchDryRunResult').innerHTML = (d.nodes || []).map(n => {
      const out = n.output_preview !== undefined ? n.output_preview : (n.output !== undefined ? n.output : (n.ports ? { selected_port: n.selected_port || null } : null));
      const extra = [
        n.error ? '错误：' + n.error : '',
        n.note ? n.note : '',
        (n.unresolved_params || []).length ? '未解析参数：' + n.unresolved_params.join('、') : '',
      ].filter(Boolean).join(' · ');
      return `<div class="flex items-start gap-2 border-b border-slate-800/60 py-1">
        <span class="w-20 truncate text-slate-300">${esc(n.label || n.node_id || '')}</span>
        <span class="px-1.5 py-0.5 rounded text-[10px] shrink-0 ${stCls[n.status] || 'bg-slate-600/40 text-slate-300'}">${esc(n.status || '')}</span>
        <span class="text-slate-400 break-all">${esc(out ? JSON.stringify(out).slice(0, 160) : '-')}${extra ? '<div class="text-rose-300">' + esc(extra) + '</div>' : ''}</span>
      </div>`;
    }).join('') || '<div class="text-slate-500">无节点</div>';
    toast(d.valid === false ? '干跑：工作流校验未通过（见下方错误）' : '干跑完成（零外部调用）', d.valid === false ? 'err' : 'ok');
  } catch (e) { toast('干跑失败：' + e.message, 'err'); }
}

async function orchPublishToggle() {
  if (!ORCH.wf) return;
  const st = ORCH.wf.status || 'draft';
  try {
    const url = st === 'published'
      ? '/api/orchestration/workflows/' + ORCH.wf.id + '/un-publish'
      : '/api/orchestration/workflows/' + ORCH.wf.id + '/publish';
    const r = await api(url, { method: 'POST' });
    toast(((r.data || {}).status || '') === 'published' ? '已发布' : '已撤销发布', 'ok');
    ORCH.wf = Object.assign({}, ORCH.wf, r.data);
    orchRenderWfHead(); renderOrchWfList();
  } catch (e) { toast('操作失败：' + e.message, 'err'); }
}

async function orchRun() {
  if (!ORCH.wf) { toast('未选择工作流', 'err'); return; }
  let payload = {};
  const raw = ($('orchRunPayload') || {}).value;
  if (raw && raw.trim()) { try { payload = JSON.parse(raw); } catch (e) { toast('payload 不是合法 JSON', 'err'); return; } }
  try {
    const r = await api('/api/orchestration/workflows/' + ORCH.wf.id + '/run', { method: 'POST', body: JSON.stringify({ payload }) });
    toast('运行已提交：' + ((r.data || {}).status || ''), 'ok');
    ORCH.selRun = (r.data || {}).run_id || null;
    await orchLoadRuns();
  } catch (e) { toast('运行失败：' + e.message, 'err'); }
}

async function orchLoadRuns() {
  if (!ORCH.wf) { $('orchRunList').innerHTML = ''; return; }
  try {
    const r = await api('/api/orchestration/workflows/' + ORCH.wf.id + '/runs?page=1&pageSize=30');
    ORCH.runs = (r.data && r.data.items) || [];
    if (!ORCH.selRun && ORCH.runs.length) ORCH.selRun = ORCH.runs[0].run_id;
    renderOrchRunList();
    if (ORCH.selRun) orchShowRun(ORCH.selRun);
  } catch (e) { toast('运行记录加载失败：' + e.message, 'err'); }
}

function renderOrchRunList() {
  const el = $('orchRunList'); if (!el) return;
  if (!ORCH.runs.length) { el.innerHTML = '<div class="text-slate-500">暂无运行</div>'; return; }
  el.innerHTML = ORCH.runs.map(r => {
    const c = { succeeded: 'text-emerald-300', failed: 'text-rose-300', awaiting_confirmation: 'text-amber-300', interrupted: 'text-purple-300', cancelled: 'text-slate-400', aborted: 'text-rose-300' }[r.status] || 'text-slate-300';
    return `<div onclick="orchShowRun('${r.run_id}')" class="cursor-pointer rounded border px-2 py-1 ${r.run_id === ORCH.selRun ? 'border-blue-500 bg-blue-500/10' : 'border-slate-700'}">
      <span class="${c}">${esc(r.status)}</span> <span class="text-slate-500">${esc((r.started_at || '').replace('T', ' ').slice(0, 19))}</span>
      <span class="text-slate-500 ml-1">${esc(((r.trigger || {}).source || '') || '')}</span>
    </div>`;
  }).join('');
}

async function orchShowRun(runId) {
  ORCH.selRun = runId; renderOrchRunList();
  try {
    const r = await api('/api/orchestration/runs/' + runId);
    const run = r.data || {};
    $('orchRunDetail').classList.remove('hidden');
    $('orchRunMeta').textContent = `${run.status || ''} · ${((run.trigger || {}).source || '')} · ${run.started_at || ''}${run.finish_reason ? ' · ' + run.finish_reason : ''}`;
    const ns = run.node_states || {};
    const nstCls = { success: 'text-emerald-300', failed: 'text-rose-300', running: 'text-blue-300', awaiting_confirmation: 'text-amber-300' };
    $('orchRunNodes').innerHTML = Object.entries(ns).map(([nid, st]) =>
      `<div class="flex items-start gap-2 border-b border-slate-800/60 py-1">
        <span class="w-20 truncate text-slate-300">${esc(nid)}</span>
        <span class="px-1.5 py-0.5 rounded text-[10px] bg-slate-700/60 ${nstCls[st.status] || 'text-slate-300'}">${esc(st.status || '')}</span>
        <span class="text-slate-500 break-all flex-1">${esc(st.error || st.confirm_title || '').slice(0, 120)}</span>
      </div>`).join('');
    const acts = [];
    if (run.status === 'awaiting_confirmation' && run.pending_node) {
      acts.push(`<button class="btn-primary text-xs" onclick="orchConfirmRun('${run.pending_node}')">签核确认</button>
        <button class="btn-ghost text-xs" onclick="orchRejectRun('${run.pending_node}')">拒绝</button>`);
    }
    if (run.status === 'interrupted') acts.push(`<button class="btn-primary text-xs" onclick="orchResumeRun()">恢复（resume）</button>`);
    $('orchRunActions').innerHTML = acts.join('');
  } catch (e) { toast('运行详情失败：' + e.message, 'err'); }
}

async function orchConfirmRun(nodeId) {
  try {
    const r = await api(`/api/orchestration/runs/${ORCH.selRun}/confirm`, { method: 'POST', body: JSON.stringify({ node_id: nodeId, reason: '' }) });
    toast('已确认：' + ((r.data || {}).status || ''), 'ok'); await orchShowRun(ORCH.selRun);
  } catch (e) { toast('确认失败：' + e.message, 'err'); }
}
async function orchRejectRun(nodeId) {
  const reason = prompt('拒绝原因（留痕）：') || '';
  try {
    const r = await api(`/api/orchestration/runs/${ORCH.selRun}/reject`, { method: 'POST', body: JSON.stringify({ node_id: nodeId, reason }) });
    toast('已拒绝：' + ((r.data || {}).status || ''), 'ok'); await orchShowRun(ORCH.selRun);
  } catch (e) { toast('拒绝失败：' + e.message, 'err'); }
}
async function orchResumeRun() {
  try {
    const r = await api(`/api/orchestration/runs/${ORCH.selRun}/resume`, { method: 'POST' });
    toast('已恢复：' + ((r.data || {}).status || ''), 'ok'); await orchShowRun(ORCH.selRun);
  } catch (e) { toast('恢复失败：' + e.message, 'err'); }
}

/* ---------- SOP 生成 / 片段生成 ---------- */
async function orchGenerate() {
  const text = ($('orchSopText') || {}).value || '';
  if (!text.trim()) { toast('请粘贴 SOP / skill 文本', 'err'); return; }
  const body = { text, source_type: $('orchSopType').value };
  const name = ($('orchSopName') || {}).value.trim();
  if (name) body.name = name;
  try {
    const r = await api('/api/orchestration/workflows/generate', { method: 'POST', body: JSON.stringify(body) });
    const d = r.data || {};
    toast(`草稿已生成：${(d.nodes || []).length} 节点 / 缺口 ${d.gap_count != null ? d.gap_count : '?'}`, 'ok');
    await orchSelectWorkflow(d.id, true);
    await loadOrchestration();
  } catch (e) { toast('生成失败：' + e.message, 'err'); }
}

function orchToggleFragment() {
  const box = $('orchFragmentBox'); box.classList.toggle('hidden');
}

async function orchGenerateFragment() {
  if (!ORCH.wf) { toast('未选择工作流', 'err'); return; }
  const intent = ($('orchFragmentIntent') || {}).value || '';
  if (!intent.trim()) { toast('请输入意图', 'err'); return; }
  const after = $('orchFragmentAfter').value || undefined;
  try {
    const r = await api(`/api/orchestration/workflows/${ORCH.wf.id}/generate-fragment`, { method: 'POST', body: JSON.stringify({ intent, after_node: after }) });
    toast('片段已并入草稿（grounding 校验通过）', 'ok');
    await orchSelectWorkflow(ORCH.wf.id, true);
  } catch (e) { toast('片段生成失败：' + e.message, 'err'); }
}
