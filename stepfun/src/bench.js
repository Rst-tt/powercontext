/**
 * Measurement page: same corpus, same queries, one fixed backend at a time.
 *
 * Reported numbers
 *   - model load time (cold and cached are distinguished in the note)
 *   - document embedding: total, per chunk, chunks per second
 *   - query embedding: first query and warm queries
 *   - seekdb retrieval latency for the hybrid query
 *   - end to end latency per query
 */
import { Embedder, detectBackend, chunkText } from './embedder.js';

const CORPUS = `OceanBase seekdb is an AI-native search database that stores vectors, full text and relational rows in one engine.

A vector index is created with CREATE VECTOR INDEX and the hnsw type. The m parameter bounds the number of links per node in the graph.

ef_construction trades build time for graph quality, while ef_search trades query latency for recall at search time.

Hybrid search fuses a dense vector ranking with a keyword ranking inside the database using reciprocal rank fusion.

The full-text index answers MATCH AGAINST predicates. The space analyzer splits on whitespace and suits English corpora.

Transformers.js runs an ONNX embedding model inside the browser through WebGPU, falling back to WebAssembly when no GPU adapter exists.

Quantization stores the weights in eight bits, shrinking the MiniLM model from about eighty seven megabytes to about twenty two.

The browser computes the embedding locally, so the text of a private document never leaves the device.

seekdb keeps the vectors, the original text and the metadata, and the retrieval is a single SQL statement executed by the database.

The knowledge base supports incremental import: after new rows are written, the collection returns them in the next search.

A query is embedded with the same model as the documents, which is required for the vector space to be comparable.

Chunking with a small overlap keeps a sentence from being cut in half at a chunk boundary and improves recall.`;

const QUERIES = [
  'how is the vector index created',
  'what does ef construction control',
  'how are two rankings fused',
  'does the model run in the browser',
  'how large is the quantized model',
  'where is the document text stored',
  'can new documents be searched immediately',
  'why use an overlap when chunking',
];

const out = document.getElementById('out');
const status = document.getElementById('status');
const runButton = document.getElementById('run');

function log(lines) {
  out.textContent = lines.join('\n');
}

async function measure() {
  runButton.disabled = true;
  status.textContent = '加载模型…';
  const backend = await detectBackend();

  const embedder = new Embedder({ precision: 'int8' });
  const loadStarted = performance.now();
  const { device, dtype, loadMs, fellBack } = await embedder.load({});
  status.textContent = `模型就绪（${device}/${dtype}），开始向量化…`;

  const chunks = chunkText(CORPUS, { size: 360 });
  const embedStarted = performance.now();
  const { vectors, elapsedMs } = await embedder.embed(chunks, { batchSize: 8 });
  const chunksPerSecond = chunks.length / (elapsedMs / 1000);

  status.textContent = '写入 seekdb…';
  const payload = chunks.map((document, index) => ({
    id: `bench-${Date.now()}-${index}`,
    document,
    embedding: vectors[index],
    metadata: { source: 'bench' },
  }));
  const writeStarted = performance.now();
  await fetch('/api/ingest', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ rows: payload }),
  });
  const writeMs = performance.now() - writeStarted;

  status.textContent = '测量查询…';
  const queryRows = [];
  for (let index = 0; index < QUERIES.length; index += 1) {
    const query = QUERIES[index];
    const started = performance.now();
    const { vectors: queryVector, elapsedMs: embedMs } = await embedder.embed([query], { batchSize: 1 });
    const response = await fetch('/api/search', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ mode: 'hybrid', query, embedding: queryVector[0], topK: 5 }),
    });
    const data = await response.json();
    const totalMs = performance.now() - started;
    queryRows.push({ query, embedMs, searchMs: data.queryMs ?? 0, totalMs, hits: data.results?.length ?? 0 });
  }

  const median = (values) => {
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
  };
  const warm = queryRows.slice(1);
  const lines = [];
  lines.push(`backend            ${device}${fellBack ? ' (fallback)' : ''}`);
  lines.push(`dtype              ${dtype}`);
  lines.push(`model load         ${loadMs.toFixed(0)} ms  (measure wall clock ${(performance.now() - loadStarted).toFixed(0)} ms)`);
  lines.push(`backend forced     ${new URLSearchParams(location.search).get('backend') || 'auto'}`);
  lines.push('');
  lines.push(`chunks             ${chunks.length}`);
  lines.push(`document embedding ${elapsedMs.toFixed(0)} ms total, ${(elapsedMs / chunks.length).toFixed(1)} ms/chunk, ${chunksPerSecond.toFixed(2)} chunks/s`);
  lines.push(`seekdb write       ${writeMs.toFixed(0)} ms (${chunks.length} rows, one request)`);
  lines.push('');
  lines.push(`query embedding    first ${queryRows[0].embedMs.toFixed(0)} ms, warm median ${median(warm.map((r) => r.embedMs)).toFixed(0)} ms`);
  lines.push(`seekdb search      warm median ${median(warm.map((r) => r.searchMs)).toFixed(0)} ms`);
  lines.push(`end to end         first ${queryRows[0].totalMs.toFixed(0)} ms, warm median ${median(warm.map((r) => r.totalMs)).toFixed(0)} ms`);
  lines.push('');
  lines.push('per query (embed / search / total ms)');
  for (const row of queryRows) {
    lines.push(`  ${row.embedMs.toFixed(0).padStart(5)} / ${row.searchMs.toFixed(0).padStart(5)} / ${row.totalMs.toFixed(0).padStart(5)}   ${row.query}`);
  }

  const summary = {
    backend: device,
    fellBack,
    dtype,
    loadMs: Math.round(loadMs),
    chunks: chunks.length,
    embedMs: Math.round(elapsedMs),
    msPerChunk: Number((elapsedMs / chunks.length).toFixed(1)),
    chunksPerSecond: Number(chunksPerSecond.toFixed(2)),
    writeMs: Math.round(writeMs),
    firstQueryMs: Math.round(queryRows[0].totalMs),
    warmQueryMs: Math.round(median(warm.map((r) => r.totalMs))),
    seekdbMs: Math.round(median(warm.map((r) => r.searchMs))),
  };
  window.__bench = summary;
  lines.push('');
  lines.push(JSON.stringify(summary));
  log(lines);
  status.textContent = '完成';
  runButton.disabled = false;
}

runButton.addEventListener('click', () => {
  measure().catch((error) => {
    status.textContent = `失败：${error.message}`;
    log([`FAILED: ${error.message}`]);
    runButton.disabled = false;
  });
});
