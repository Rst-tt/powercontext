/**
 * UI glue: import → chunk → embed in the browser → write to seekdb → search.
 */
import { Embedder, MODEL_ID, detectBackend, chunkText } from './embedder.js';

const $ = (id) => document.getElementById(id);

const els = {
  backend: $('chip-backend'),
  model: $('chip-model'),
  precision: $('precision-select'),
  load: $('chip-load'),
  loadModel: $('btn-load-model'),
  dropzone: $('dropzone'),
  fileInput: $('file-input'),
  paste: $('paste-area'),
  chunkSize: $('chunk-size'),
  batchSize: $('batch-size'),
  importBtn: $('btn-import'),
  importProgress: $('import-progress'),
  importBar: $('import-progress').querySelector('i'),
  importStatus: $('import-status'),
  importStats: $('import-stats'),
  stChunks: $('st-chunks'),
  stEmbed: $('st-embed'),
  stWrite: $('st-write'),
  stThroughput: $('st-throughput'),
  docCount: $('doc-count'),
  reset: $('btn-reset'),
  collectionInfo: $('collection-info'),
  modeTabs: $('mode-tabs'),
  query: $('query-input'),
  topk: $('topk'),
  searchBtn: $('btn-search'),
  searchStatus: $('search-status'),
  searchStats: $('search-stats'),
  stQembed: $('st-qembed'),
  stQuery: $('st-query'),
  stE2e: $('st-e2e'),
  results: $('results'),
  footerTable: $('footer-table'),
};

const state = {
  embedder: null,
  mode: 'hybrid',
  precision: 'int8',
  documents: 0,
  chunks: [],
  busy: false,
};

function fmt(ms) {
  if (ms == null || Number.isNaN(ms)) return '—';
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms.toFixed(0)} ms`;
}

function setBackendChip(device, fellBack) {
  els.backend.textContent = device === 'webgpu' ? 'WebGPU' : 'WASM';
  els.backend.className = `chip ${device === 'webgpu' ? 'ok' : 'warn'}`;
  els.backend.title = fellBack
    ? 'WebGPU 不可用，已自动降级到 WASM'
    : `推理后端：${device === 'webgpu' ? 'WebGPU' : 'WASM（CPU）'}`;
}

async function refreshHealth() {
  try {
    const response = await fetch('/api/health');
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || 'health failed');
    state.documents = data.documents ?? 0;
    els.docCount.textContent = `${state.documents} 个片段`;
    els.collectionInfo.textContent = `collection: ${data.table || '—'}`;
    els.footerTable.textContent = `seekdb table: ${data.table || '—'}`;
  } catch (error) {
    els.collectionInfo.textContent = `seekdb 未连接：${error.message}`;
  }
}

async function ensureModel() {
  if (state.embedder?.pipe) return state.embedder;
  els.loadModel.disabled = true;
  els.loadModel.textContent = '加载中…';
  const embedder = new Embedder({ precision: state.precision });
  const { device, dtype, loadMs, fellBack } = await embedder.load({
    onProgress: (event) => {
      if (event?.status === 'progress' && event?.file) {
        els.model.textContent = `${event.file.split('/').pop()} ${Math.round(event.progress || 0)}%`;
      }
    },
  });
  state.embedder = embedder;
  setBackendChip(device, fellBack);
  els.model.textContent = `${MODEL_ID.split('/').pop()} · ${String(dtype).toUpperCase()}`;
  els.model.className = 'chip ok';
  els.load.textContent = `加载 ${fmt(loadMs)}`;
  els.loadModel.textContent = '重新加载';
  els.loadModel.disabled = false;
  if (fellBack) {
    els.searchStatus.textContent = 'WebGPU 不可用，已降级到 WASM';
  }
  return embedder;
}

function readFiles(files) {
  return Promise.all(
    Array.from(files).map(
      (file) =>
        new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve({ name: file.name, text: String(reader.result || '') });
          reader.onerror = () => reject(reader.error);
          reader.readAsText(file);
        }),
    ),
  );
}

function buildChunks(sources) {
  const size = Number(els.chunkSize.value) || 360;
  const chunks = [];
  sources.forEach((source) => {
    const pieces = chunkText(source.text, { size });
    pieces.forEach((text, index) => {
      chunks.push({
        id: `${source.name}#${index}-${Math.random().toString(36).slice(2, 8)}`,
        document: text,
        metadata: { source: source.name, chunk: index },
      });
    });
  });
  return chunks;
}

function showProgress(done, total, label) {
  els.importProgress.hidden = false;
  const percent = total ? Math.round((done / total) * 100) : 0;
  els.importBar.style.width = `${percent}%`;
  els.importStatus.textContent = `${label} ${done}/${total}（${percent}%）`;
}

async function importChunks() {
  const pasted = els.paste.value.trim();
  const files = els.fileInput.files;
  const sources = [];
  if (files?.length) sources.push(...(await readFiles(files)));
  if (pasted) sources.push({ name: 'pasted-text', text: pasted });
  if (!sources.length) {
    els.searchStatus.textContent = '请先选择文件或粘贴文本';
    return;
  }

  state.busy = true;
  els.importBtn.disabled = true;
  try {
    const embedder = await ensureModel();
    const chunks = buildChunks(sources);
    if (!chunks.length) throw new Error('没有解析出可用的文本片段');

    showProgress(0, chunks.length, '向量化');
    const texts = chunks.map((chunk) => chunk.document);
    const { vectors, elapsedMs } = await embedder.embed(texts, {
      batchSize: Number(els.batchSize.value) || 8,
      onProgress: (done, total) => showProgress(done, total, '向量化'),
    });

    showProgress(chunks.length, chunks.length, '写入 seekdb');
    let writeMs = 0;
    const payload = [];
    for (let index = 0; index < chunks.length; index += 1) {
      payload.push({ ...chunks[index], embedding: vectors[index] });
    }
    for (let offset = 0; offset < payload.length; offset += 100) {
      const slice = payload.slice(offset, offset + 100);
      const response = await fetch('/api/ingest', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ rows: slice }),
      });
      const data = await response.json();
      if (!data.ok) throw new Error(data.error);
      writeMs += data.writeMs || 0;
      showProgress(Math.min(offset + 100, payload.length), payload.length, '写入 seekdb');
    }

    els.importStats.hidden = false;
    els.stChunks.textContent = String(chunks.length);
    els.stEmbed.textContent = fmt(elapsedMs);
    els.stWrite.textContent = fmt(writeMs);
    els.stThroughput.textContent = `${(chunks.length / (elapsedMs / 1000)).toFixed(1)} 段/秒`;
    els.importStatus.textContent = `完成：${chunks.length} 个片段已写入 seekdb`;
    await refreshHealth();
  } catch (error) {
    els.importStatus.textContent = `失败：${error.message}`;
    els.importProgress.hidden = false;
  } finally {
    state.busy = false;
    els.importBtn.disabled = false;
  }
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function highlight(text, terms) {
  let html = escapeHtml(text);
  for (const term of terms) {
    if (!term || term.length < 2) continue;
    const safe = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    html = html.replace(new RegExp(`(${safe})`, 'gi'), '<mark>$1</mark>');
  }
  return html;
}

function renderResults(rows, terms) {
  if (!rows.length) {
    els.results.innerHTML = '<li class="empty">没有命中结果，换个说法或先导入语料</li>';
    return;
  }
  els.results.innerHTML = rows
    .map((row, index) => {
      const source = row.metadata?.source || row.metadata?.doc_id || 'unknown';
      const score = row.score == null ? '—' : Number(row.score).toFixed(4);
      return `<li class="result">
        <div class="meta">
          <span class="rank">${index + 1}</span>
          <span>${escapeHtml(source)}</span>
          <span class="score">score ${score}</span>
        </div>
        <p class="text">${highlight(row.document, terms)}</p>
      </li>`;
    })
    .join('');
}

async function search() {
  const query = els.query.value.trim();
  if (!query) {
    els.searchStatus.textContent = '请输入查询内容';
    return;
  }
  state.busy = true;
  els.searchBtn.disabled = true;
  const started = performance.now();
  try {
    let embedding = null;
    let embedMs = 0;
    if (state.mode !== 'keyword') {
      const embedder = await ensureModel();
      const { vectors, elapsedMs } = await embedder.embed([query], { batchSize: 1 });
      embedding = vectors[0];
      embedMs = elapsedMs;
    }

    const response = await fetch('/api/search', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        mode: state.mode,
        query,
        embedding,
        topK: Number(els.topk.value) || 10,
      }),
    });
    const data = await response.json();
    if (!data.ok) throw new Error(data.error);

    const e2e = performance.now() - started;
    els.searchStats.hidden = false;
    els.stQembed.textContent = embedMs ? fmt(embedMs) : '—';
    els.stQuery.textContent = fmt(data.queryMs);
    els.stE2e.textContent = fmt(e2e);
    els.searchStatus.textContent = `${data.results.length} 条结果 · ${data.mode}`;
    if (data.table) els.footerTable.textContent = `seekdb table: ${data.table}`;
    renderResults(data.results, query.split(/\s+/));
  } catch (error) {
    els.searchStatus.textContent = `检索失败：${error.message}`;
  } finally {
    state.busy = false;
    els.searchBtn.disabled = false;
  }
}

function bind() {
  els.loadModel.addEventListener('click', () => {
    state.embedder = null;
    ensureModel().catch((error) => {
      els.model.textContent = `模型加载失败：${error.message}`;
      els.model.className = 'chip warn';
    });
  });

  els.dropzone.addEventListener('click', () => els.fileInput.click());
  els.fileInput.addEventListener('change', () => {
    const names = Array.from(els.fileInput.files || []).map((f) => f.name);
    if (names.length) els.dropzone.querySelector('strong').textContent = names.join('、');
  });
  ['dragover', 'dragenter'].forEach((type) =>
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.add('dragging');
    }),
  );
  ['dragleave', 'drop'].forEach((type) =>
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.remove('dragging');
    }),
  );
  els.dropzone.addEventListener('drop', (event) => {
    els.fileInput.files = event.dataTransfer.files;
    const names = Array.from(event.dataTransfer.files).map((f) => f.name);
    els.dropzone.querySelector('strong').textContent = names.join('、');
  });

  els.importBtn.addEventListener('click', importChunks);
  els.searchBtn.addEventListener('click', search);
  els.query.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') search();
  });

  els.modeTabs.addEventListener('click', (event) => {
    const button = event.target.closest('.tab');
    if (!button) return;
    state.mode = button.dataset.mode;
    Array.from(els.modeTabs.children).forEach((tab) => tab.classList.toggle('active', tab === button));
  });

  els.reset.addEventListener('click', async () => {
    if (!confirm('清空知识库中的全部片段？')) return;
    await fetch('/api/reset', { method: 'POST' });
    els.results.innerHTML = '<li class="empty">知识库已清空</li>';
    els.importStats.hidden = true;
    await refreshHealth();
  });

  els.precision.value = state.precision;
  els.precision.addEventListener('change', () => {
    state.precision = els.precision.value;
    state.embedder = null;
    els.model.textContent = '模型未加载';
    els.model.className = 'chip';
    els.load.textContent = '—';
  });
  els.precision.title = '选择量化精度后重新加载模型';
}

async function boot() {
  bind();
  const backend = await detectBackend();
  setBackendChip(backend, false);
  if (backend === 'wasm') {
    els.backend.title = '当前浏览器没有可用的 WebGPU，使用 WASM 后端';
  }
  await refreshHealth();
  els.searchStatus.textContent = '就绪';
}

boot();
