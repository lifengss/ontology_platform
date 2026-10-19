const API_BASE = (location.hostname === 'localhost' && location.port === '8000') ? '' : 'http://localhost:8000';
const $ = id => document.getElementById(id);
let state = { ontologies: [], candidates: [], graphTab: 'schema', currentOntology: null, filter: 'all', editorMode: 'form', formObjects: [] };

async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const headers = isForm ? {} : { 'Content-Type': 'application/json' };
  const r = await fetch(API_BASE + path, { headers, ...opts });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error?.message || `HTTP ${r.status}`);
  return j;
}

async function checkHealth() {
  try {
    const h = await api('/health');
    $('healthDot').className = 'w-2 h-2 rounded-full ' + (h.data?.status === 'ok' ? 'bg-emerald-400' : 'bg-amber-400');
    $('healthText').textContent = h.data?.status === 'ok' ? '后端健康' : '后端降级';
    $('ver').textContent = 'v' + (h.data?.version || '?');
  } catch (e) {
    $('healthDot').className = 'w-2 h-2 rounded-full bg-red-500';
    $('healthText').textContent = '后端不可达';
  }
}

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-content').forEach(s => s.classList.add('hidden'));
  $(name).classList.remove('hidden');
  if (name === 'modeling') loadOntologies();
  if (name === 'ingest') { loadOntologies().then(() => { renderOntologySelects(); renderIngestTarget(); }); loadCandidates(); }
  if (name === 'graph') { loadOntologies().then(() => { renderOntologySelects(); loadGraph(); }); }
  if (name === 'inference') { loadOntologies().then(() => { renderOntologySelects(); loadInference(); }); }
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
  ['ingestOntology', 'graphOntology', 'inferOntology'].forEach(id => { $(id).innerHTML = opts; });
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
        '<div class="col-span-3"><input class="w-full rel-id" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.id) + '" placeholder="rel_xx" /></div>' +
        '<div class="col-span-3"><input class="w-full rel-name" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.name) + '" placeholder="关系名" /></div>' +
        '<div class="col-span-3"><input class="w-full rel-target" data-oidx="' + idx + '" data-ridx="' + ridx + '" value="' + esc(r.target_object) + '" placeholder="目标对象 id" /></div>' +
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
  }).join('');
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
    toast('保存失败: ' + e.message, 'err');
  }
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
function renderIngestTarget() {
  const id = $('ingestOntology').value;
  const o = state.ontologies.find(x => x.id === id);
  if (!o) return $('ingestTarget').innerHTML = '';
  $('ingestTarget').innerHTML = '<div class="mb-1"><strong>' + esc(o.name) + '</strong></div>' +
    (o.objects || []).map(obj => '<div class="py-1 border-b border-slate-700/50"><span class="text-slate-300">' + esc(obj.name) + '</span> <span class="text-slate-500">(' + obj.id + ')</span></div>').join('');
}

async function previewIngest() {
  const ontologyId = $('ingestOntology').value;
  const objectType = $('ingestObjectType').value.trim();
  const fieldMap = $('ingestFieldMap').value;
  const records = $('ingestRecords').value;
  if (!ontologyId || !objectType || !fieldMap || !records) return toast('请填写完整参数', 'info');
  try {
    const j = await api('/api/ingest/preview', {
      method: 'POST',
      body: JSON.stringify({ ontology_id: ontologyId, source: { kind: 'field_map', records: JSON.parse(records) }, mapping: { object_type: objectType, field_map: JSON.parse(fieldMap) } })
    });
    $('ingestPreviewResult').textContent = JSON.stringify(j.data, null, 2);
    $('ingestPreviewResult').classList.remove('hidden');
  } catch (e) {
    toast('Preview 失败: ' + e.message, 'err');
  }
}

async function loadCandidates() {
  try {
    const j = await api('/api/ingest/candidates?page=1&pageSize=50');
    state.candidates = j.data.items || [];
    const el = $('candidateList');
    if (!state.candidates.length) { el.innerHTML = '<div class="text-sm text-slate-500">暂无候选</div>'; return; }
    el.innerHTML = state.candidates.map(c => {
      return '<div class="p-2 rounded-lg border border-slate-700/50">' +
        '<div class="flex justify-between"><span class="text-sm font-medium">' + c.id + '</span><span class="text-xs text-slate-500">' + (c.object_type || '') + '</span></div>' +
        '<div class="text-xs text-slate-400">instances: ' + (c.instances?.length || 0) + '</div>' +
        '<div class="mt-2 flex gap-2"><button class="btn" onclick="approveCandidate(\'' + c.id + '\')">入库</button></div>' +
      '</div>';
    }).join('');
  } catch (e) { toast('加载候选失败: ' + e.message, 'err'); }
}

async function approveCandidate(cid) {
  try {
    await api('/api/ingest/candidates/' + cid + '/approve', { method: 'POST', body: '{}' });
    toast('候选已入库', 'ok');
    loadCandidates();
  } catch (e) { toast('入库失败: ' + e.message, 'err'); }
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
  if (!id) return;
  try {
    const j = await api('/api/graph/' + id + '?mode=' + state.graphTab);
    drawGraph(j.data);
  } catch (e) { toast('加载图谱失败: ' + e.message, 'err'); }
}

function drawGraph(data) {
  const container = $('graphContainer');
  const width = container.clientWidth;
  const height = container.clientHeight || 500;
  container.innerHTML = '<div class="graph-tooltip" id="graphTooltip"></div>';
  const svg = d3.select('#graphContainer').append('svg').attr('width', width).attr('height', height);
  const nodes = data.nodes || [];
  const links = (data.edges || []).map(e => ({ source: e.source, target: e.target, label: e.relation || e.label, origin: e.origin }));
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
  node.append('circle').attr('r', d => d.type === 'object' ? 22 : 14).attr('fill', d => d.type === 'object' ? '#4f8cff' : d.type === 'attribute' ? '#34d399' : '#f59e0b');
  node.append('text').attr('dy', 4).attr('text-anchor', 'middle').text(d => d.label || d.name || d.id).attr('font-size', 11).attr('fill', '#fff');

  const tip = $('graphTooltip');
  node.on('mouseover', (e, d) => {
    tip.style.opacity = 1;
    tip.innerHTML = '<strong>' + esc(d.label || d.name || d.id) + '</strong><br/>type: ' + d.type + (d.source ? '<br/>source: ' + d.source : '');
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
      '<div class="p-2 rounded-lg border border-slate-700/50 text-sm"><span class="text-slate-500">#' + (i + 1) + '</span> ' + esc(s.rule) + '<br/><span class="text-xs text-slate-400">' + JSON.stringify(s.bindings || {}) + '</span></div>'
    ).join('') : '<div class="text-sm text-slate-500">无推理结论</div>';
  } catch (e) { toast('加载推理失败: ' + e.message, 'err'); }
}

// ---- init ----
checkHealth();
switchTab('modeling');
setInterval(checkHealth, 10000);
