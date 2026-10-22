const API_BASE = (location.hostname === 'localhost' && location.port === '8000') ? '' : 'http://localhost:8000';
const $ = id => document.getElementById(id);
let state = {
  ontologies: [], candidates: [], graphTab: 'schema',
  currentOntology: null, filter: 'all', editorMode: 'form', formObjects: [],
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
    state.projects = j.data.items || [];
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

let _lastTab = '';

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(s => s.classList.add('hidden'));
  $(name).classList.remove('hidden');
  clientLog('info', 'switch tab', { tab: name });
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
  if (name === 'mcp') { loadOntologies().then(() => { renderOntologySelects(); loadMcpStatus(); loadMcpTools(); }); }
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
    return '<div class="p-2 rounded-lg hover:bg-slate-800/60 cursor-pointer border border-transparent hover:border-slate-700 ' + (state.currentOntology?.id === o.id ? 'bg-slate-800 border-slate-700' : '') + '" onclick="selectOntology(' + JSON.stringify(o).replace(/"/g, '&quot;') + ')">' +
      '<div class="flex justify-between items-center"><span class="font-medium text-sm">' + esc(o.name) + '</span><span class="badge ' + status + '">' + (status === 'draft' ? '草稿' : '已发布') + '</span></div>' +
      '<div class="text-xs text-slate-500">' + o.id + ' · ' + (o.objects?.length || 0) + ' 对象</div>' +
    '</div>';
  }).join('');
}

function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function renderOntologySelects() {
  const active = state.ontologies.filter(o => (o.status || 'active') === 'active');
  const opts = active.map(o => '<option value="' + o.id + '">' + esc(o.name) + '</option>').join('');
  ['ingestOntology', 'graphOntology', 'inferOntology', 'mcpOntology'].forEach(id => {
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

function closeEditor() { $('editorCard').classList.add('hidden'); }

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
  try {
    const j = await api('/api/ontologies', { method: 'POST', body: JSON.stringify(payload) });
    toast('本体已保存: ' + j.data.name, 'ok');
    closeEditor();
    loadOntologies();
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

async function publishMcp() {
  if (!state.currentOntology) return;
  try {
    const j = await api('/api/mcp/server/publish?ontology_id=' + state.currentOntology.id, { method: 'POST', body: '{}' });
    const d = j.data;
    const msg = d.published
      ? `MCP Server 已发布：${d.endpoint}`
      : '发布失败：' + (d.error || '未知错误');
    toast(msg, d.published ? 'ok' : 'err');
  } catch (e) {
    toast('MCP 发布失败: ' + e.message, 'err');
  }
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
    renderOntologyDetail(o);
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
async function loadMcpStatus() {
  const box = $('mcpStatusBox');
  let globalData = null;
  try {
    // 5.2：多项目共存视图（不带 project_id 的全局状态）
    try {
      const g = await api('/api/mcp/server/status', { projectId: null, namespace: null });
      globalData = g.data || {};
    } catch (e) { /* 全局状态不可用不阻断 */ }
    const j = await api('/api/mcp/server/status');
    const d = j.data || {};
    if (d.published) {
      const ontList = (d.published_ontologies || []).map(o =>
        '<div class="flex justify-between items-center p-1.5 rounded border border-slate-700/50 text-xs">' +
        '<span>' + esc(o.name || o.ontology_id) + '</span>' +
        '<span class="cursor-pointer text-amber-400" title="仅下线该本体" onclick="unpublishMcpOntology(\'' + o.ontology_id + '\')">下线</span>' +
        '</div>'
      ).join('') || '<div class="text-xs text-slate-500">（未记录本体清单）</div>';
      box.innerHTML = '<div class="text-emerald-400 font-medium mb-1">已发布</div>' +
        '<div class="text-xs text-slate-400 mb-1">SSE 端点: <code class="text-slate-300">' + esc(d.endpoint || '') + '</code></div>' +
        '<div class="text-xs text-slate-400 mb-2">消息端点: <code class="text-slate-300">' + esc(d.messages_endpoint || '') + '</code></div>' +
        '<div class="text-xs text-slate-500 mb-1">已发布本体（' + (d.published_ontologies || []).length + ' 个，共享同一端点与工具）:</div>' +
        '<div class="space-y-1 max-h-[160px] overflow-auto">' + ontList + '</div>' +
        '<div class="text-xs text-slate-500 mt-2">工具: ' + (d.tools || []).join(', ') + '</div>';
    } else {
      box.innerHTML = '<div class="text-slate-400">当前项目未发布 MCP Server。选择上方本体后点击「发布 / 更新」。</div>';
    }
    // 5.2：多项目共存状态
    const projects = (globalData && globalData.published_projects) || [];
    const coexEl = $('mcpCoexist');
    if (coexEl) {
      coexEl.innerHTML = projects.length
        ? projects.map(p => '<div class="flex justify-between text-xs p-1.5 rounded border border-slate-700/50">' +
            '<span>' + esc(p.project_id.slice(0, 8)) + '…</span>' +
            '<span class="text-slate-400">' + esc(p.endpoint) + ' · ' + (p.published_ontologies || []).length + ' 本体</span></div>').join('')
        : '<div class="text-xs text-slate-500">当前无项目处于发布状态</div>';
    }
  } catch (e) { toast('加载 MCP 状态失败: ' + e.message, 'err'); }
}

// 5.1：按本体取消发布（清单清空后项目 Server 自动下线）
async function unpublishMcpOntology(ontologyId) {
  try {
    await api('/api/mcp/server/unpublish?ontology_id=' + encodeURIComponent(ontologyId), { method: 'POST', body: '{}' });
    toast('已下线该本体的 MCP 发布', 'ok');
    loadMcpStatus();
  } catch (e) { toast('下线失败: ' + e.message, 'err'); }
}

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
  const base = location.protocol + '//' + location.host;
  const projectId = state.currentProject?.id || '<project_id>';
  const text = 'SSE endpoint: ' + base + '/mcp/' + projectId + '\n\n' + JSON.stringify(example, null, 2);
  $('mcpExample').textContent = text;
}

async function publishMcpServer() {
  const ontologyId = $('mcpOntology').value;
  if (!ontologyId) return toast('请选择要发布的本体', 'info');
  try {
    const j = await api('/api/mcp/server/publish?ontology_id=' + encodeURIComponent(ontologyId), { method: 'POST', body: '{}' });
    toast('MCP Server 已发布: ' + j.data.endpoint, 'ok');
    loadMcpStatus();
  } catch (e) { toast('发布失败: ' + e.message, 'err'); }
}

async function unpublishMcpServer() {
  try {
    await api('/api/mcp/server/unpublish', { method: 'POST', body: '{}' });
    toast('已取消发布', 'ok');
    loadMcpStatus();
  } catch (e) { toast('取消发布失败: ' + e.message, 'err'); }
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

async function loadImpactInstances(ontologyId) {
  const id = ontologyId || $('inferOntology').value;
  if (!id) return;
  try {
    const ji = await api('/api/ingest/instances?ontology_id=' + encodeURIComponent(id) + '&pageSize=200');
    impactInstances = (ji.data && ji.data.items) || [];
    const sel = $('impactInstance');
    if (!sel) return;
    sel.innerHTML = impactInstances.length
      ? impactInstances.map(inst => {
          const d = inst.instance || inst.props || inst;
          const label = d.name || d.title || d.code || (inst.id || '').slice(0, 8);
          return '<option value="' + esc(inst.id) + '">' + esc(inst.object_type) + ' · ' + esc(label) + '</option>';
        }).join('')
      : '<option value="">-- 暂无实例 --</option>';
    onImpactInstanceChange();
  } catch (e) { /* 实例加载失败不阻断模式层推理展示 */ }
}

function onImpactInstanceChange() {
  const inst = impactInstances.find(x => x.id === $('impactInstance').value);
  const attrSel = $('impactAttribute');
  const newVal = $('impactNewValue');
  if (!inst || !attrSel) return;
  const d = inst.instance || inst.props || inst;
  const entries = Object.entries(d).filter(([k, v]) => !String(k).startsWith('_') && v !== null && v !== '');
  attrSel.innerHTML = entries.map(([k, v]) =>
    '<option value="' + esc(k) + '"' + (newVal && newVal.dataset.attr === k ? '' : '') + '>' + esc(k) + '（当前: ' + esc(String(typeof v === 'object' ? JSON.stringify(v) : v).slice(0, 24)) + '）</option>'
  ).join('') || '<option value="">-- 无可用属性 --</option>';
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
    const r = j.data || {};
    const src = r.source || {};
    const rows = (r.impacted || []);
    let html = '<div class="p-2 rounded-lg bg-slate-800/50 text-xs mb-3">' +
      '<b>变化源</b>：' + esc(src.object_type || '') + '（' + esc(src.label || src.instance_id || '') + '）的 <b>' + esc(attribute) + '</b>：' +
      esc(String(src.old_value ?? '')) + ' → <b>' + esc(newValue) + '</b>' +
      (src.delta_days !== null && src.delta_days !== undefined ? '（' + (src.delta_days > 0 ? '推迟' : '提前') + ' ' + Math.abs(src.delta_days) + ' 天）' : '') +
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
          '<div class="mb-1">' + esc(c.attribute) + ': <span class="text-slate-400">' + esc(String(c.current)) + '</span> → <span class="text-amber-300">' + esc(c.inferred) + '</span>' +
          ' <span class="badge ' + (c.confidence === 'high' ? 'draft' : 'active') + '">' + (c.confidence === 'high' ? '高置信' : '中置信') + '</span></div>'
        ).join('') || '<span class="text-slate-500">仅直接引用，无值级推断</span>';
        html += '<tr class="border-b border-slate-800 align-top">' +
          '<td class="p-1">' + esc(item.object_type) + '<div class="text-slate-500">' + esc(item.label || item.instance_id) + '</div></td>' +
          '<td class="p-1">' + changes + '</td>' +
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
  checkHealth();
  await loadProjects();
  if (!state.projects.length) {
    // 自动创建默认项目，保证向后兼容
    try { await createProject('默认项目'); } catch (e) { /* ignore */ }
    await loadProjects();
  }
  switchTab('modeling');
  setInterval(checkHealth, 10000);
})();
