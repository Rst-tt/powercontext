/**
 * Quantization report: retrieval quality and size of FP32 versus INT8 weights.
 *
 * Runs both ONNX graphs through the same tokenizer and the same document set, so the
 * only variable is the weight precision. Relevance is defined by topic: a document is
 * relevant for a query when it belongs to the query's topic.
 *
 *   node scripts/eval-quality.mjs
 */
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { env, pipeline } from '@huggingface/transformers';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const MODEL_ID = 'Xenova/all-MiniLM-L6-v2';

env.allowRemoteModels = false;
env.allowLocalModels = true;
env.localModelPath = path.join(ROOT, 'public', 'models').replace(/\\/g, '/') + '/';
env.useBrowserCache = false;
process.env.ORT_LOG_SEVERITY_LEVEL = '3';

const TOPICS = {
  storage: [
    'seekdb stores vectors, full text and relational rows in a single storage engine.',
    'The storage layer keeps a heap table organised by a primary key and row store.',
    'Data files are preallocated and can grow while the instance is running.',
    'A block manager hands out fixed size blocks to the storage engine.',
    'Compaction merges the in-memory memtable with older on-disk sorted data.',
  ],
  vector: [
    'A vector index is created with the CREATE VECTOR INDEX statement and the hnsw type.',
    'HNSW builds a layered proximity graph; m bounds the number of links per node.',
    'ef_construction trades build time for graph quality, ef_search trades latency for recall.',
    'The l2 and cosine distance metrics must match between the index and the query.',
    'Approximate nearest neighbour search walks the graph instead of scanning every row.',
  ],
  hybrid: [
    'Hybrid search fuses a vector ranking with a keyword ranking inside the database.',
    'Reciprocal rank fusion adds one over rank plus a constant for every candidate list.',
    'A full-text index answers MATCH AGAINST predicates with a keyword score.',
    'Fusing two retrievals in SQL avoids fetching two result sets into the client.',
    'The rank window size bounds how deep each candidate list is considered.',
  ],
  browser: [
    'Transformers.js runs an ONNX embedding model inside the browser with WebGPU or WASM.',
    'A worker keeps tokenisation and inference off the UI thread.',
    'The model file is cached after the first load, so later loads are near instant.',
    'WebGPU gives the GPU access from JavaScript; WASM is the portable fallback.',
    'Text never leaves the device when the embedding runs locally.',
  ],
  quantization: [
    'Quantization stores weights in eight bits instead of thirty two.',
    'An INT8 MiniLM model occupies about twenty two megabytes, the FP32 graph about eighty seven.',
    'Compression ratio is the FP32 size divided by the quantized size.',
    'Quantization can cost a little recall, so the retrieval metric must be compared.',
    'Smaller weights load faster and use less memory on a laptop or phone.',
  ],
  sql: [
    'EXPLAIN shows whether a scan or an index range answers the statement.',
    'A dense OR chain is evaluated once per branch for every row.',
    'Rewriting a disjunction into an IN list lets the engine search the value list once.',
    'A predicate that keeps most rows can make a full scan cheaper than an index.',
    'Statistics are collected with ANALYZE TABLE before tuning an execution plan.',
  ],
};

const QUERIES = [
  ['where do vectors and text live together', 'storage'],
  ['what bounds the number of graph links', 'vector'],
  ['how are two rankings combined', 'hybrid'],
  ['does the model run without a server', 'browser'],
  ['how much smaller is the eight bit model', 'quantization'],
  ['what does explain tell me about a scan', 'sql'],
  ['which distance must match the index', 'vector'],
  ['what is ef construction for', 'vector'],
  ['what is the win of fusing inside the database', 'hybrid'],
  ['what happens to text when embedding is local', 'browser'],
  ['how is the compression ratio defined', 'quantization'],
  ['why does a long or chain cost more', 'sql'],
];

function cosine(a, b) {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i += 1) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return dot / (Math.sqrt(na) * Math.sqrt(nb) || 1);
}

function evaluate(docVectors, queryVectors, docs, k = 5) {
  let recallHits = 0;
  let reciprocalRankSum = 0;
  let relevantTotal = 0;
  let hitsTotal = 0;

  QUERIES.forEach(([text, topic], index) => {
    const scored = docs.map((doc, docIndex) => ({
      topic: doc.topic,
      score: cosine(queryVectors[index], docVectors[docIndex]),
    })).sort((a, b) => b.score - a.score);

    const relevant = docs.filter((doc) => doc.topic === topic).length;
    relevantTotal += relevant;
    const topK = scored.slice(0, k).filter((row) => row.topic === topic).length;
    recallHits += topK;
    hitsTotal += topK;

    const firstRelevant = scored.findIndex((row) => row.topic === topic);
    reciprocalRankSum += firstRelevant >= 0 ? 1 / (firstRelevant + 1) : 0;
  });

  return {
    recallAt5: recallHits / relevantTotal,
    mrr: reciprocalRankSum / QUERIES.length,
    hits: hitsTotal,
  };
}

async function embedAll(pipe, texts) {
  const vectors = [];
  for (let offset = 0; offset < texts.length; offset += 8) {
    const batch = texts.slice(offset, offset + 8);
    const output = await pipe(batch, { pooling: 'mean', normalize: true });
    vectors.push(...output.tolist());
  }
  return vectors;
}

async function run(dtype) {
  const started = Date.now();
  const pipe = await pipeline('feature-extraction', MODEL_ID, { dtype, device: 'cpu' });
  const loadMs = Date.now() - started;
  const docs = Object.entries(TOPICS).flatMap(([topic, sentences]) =>
    sentences.map((text) => ({ topic, text })),
  );
  const docVectors = await embedAll(pipe, docs.map((doc) => doc.text));
  const queryVectors = await embedAll(pipe, QUERIES.map(([text]) => text));
  const metrics = evaluate(docVectors, queryVectors, docs, 5);
  return { dtype, loadMs, ...metrics };
}

function sizeOf(relative) {
  const full = path.join(ROOT, 'public', 'models', MODEL_ID, relative);
  return fs.existsSync(full) ? fs.statSync(full).size : null;
}

async function main() {
  console.log(`model: ${MODEL_ID}`);
  const fp32 = await run('fp32');
  const int8 = await run('q8');

  const fp32Bytes = sizeOf('onnx/model.onnx');
  const int8Bytes = sizeOf('onnx/model_quantized.onnx');
  const ratio = fp32Bytes && int8Bytes ? fp32Bytes / int8Bytes : null;

  const round = (value) => Number(value.toFixed(4));
  const report = {
    model: MODEL_ID,
    documents: Object.values(TOPICS).flat().length,
    queries: QUERIES.length,
    fp32Bytes,
    int8Bytes,
    compressionRatio: ratio ? Number(ratio.toFixed(2)) : null,
    fp32: { loadMs: fp32.loadMs, recallAt5: round(fp32.recallAt5), mrr: round(fp32.mrr) },
    int8: { loadMs: int8.loadMs, recallAt5: round(int8.recallAt5), mrr: round(int8.mrr) },
  };

  console.log('\nconfig        size      load      Recall@5   MRR');
  console.log(`FP32          ${(fp32Bytes / 1048576).toFixed(1)} MB   ${String(fp32.loadMs).padStart(4)} ms   ${report.fp32.recallAt5.toFixed(4)}     ${report.fp32.mrr.toFixed(4)}`);
  console.log(`INT8 (q8)     ${(int8Bytes / 1048576).toFixed(1)} MB   ${String(int8.loadMs).padStart(4)} ms   ${report.int8.recallAt5.toFixed(4)}     ${report.int8.mrr.toFixed(4)}`);
  console.log(`compression   ${ratio ? ratio.toFixed(2) + 'x' : 'n/a'}`);
  console.log(`recall delta  ${(report.int8.recallAt5 - report.fp32.recallAt5).toFixed(4)}`);

  const outDir = path.join(ROOT, 'docs');
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, 'eval-quality.json'), JSON.stringify(report, null, 2));
  console.log('\nwritten to docs/eval-quality.json');
}

main().catch((error) => {
  console.error('FAILED:', error);
  process.exit(1);
});
