/**
 * Local API server for the browser-side knowledge base.
 *
 * The browser performs the embedding and sends plain vectors; this process only
 * writes them to seekdb and asks seekdb to run the retrieval.
 */
import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { KnowledgeBase, connectionFromEnv } from './db.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.API_PORT || 8787);
const serveDist = process.argv.includes('--serve-dist');

const app = express();
app.use(express.json({ limit: '64mb' }));

// ONNX Runtime fetches its wasm glue files from this origin; serving them here
// keeps the browser from having to reach a CDN.
app.use('/ort', express.static(path.join(__dirname, '..', 'public', 'ort'), {
  setHeaders: (res, filePath) => {
    if (filePath.endsWith('.mjs') || filePath.endsWith('.js')) {
      res.setHeader('content-type', 'text/javascript');
    } else if (filePath.endsWith('.wasm')) {
      res.setHeader('content-type', 'application/wasm');
    }
  },
}));

let kb = null;
let opening = null;

async function getKb() {
  if (kb) return kb;
  if (!opening) {
    opening = new KnowledgeBase(connectionFromEnv()).open().then((instance) => {
      kb = instance;
      return instance;
    }).finally(() => {
      opening = null;
    });
  }
  return opening;
}

function fail(res, error, status = 500) {
  console.error('[api]', error);
  res.status(status).json({ ok: false, error: String(error?.message || error) });
}

app.get('/api/health', async (req, res) => {
  try {
    const instance = await getKb();
    const stats = await instance.stats();
    res.json({ ok: true, documents: stats.documents, connection: instance.config, table: await instance.tableName() });
  } catch (error) {
    fail(res, error);
  }
});

app.post('/api/ingest', async (req, res) => {
  try {
    const rows = Array.isArray(req.body?.rows) ? req.body.rows : [];
    if (rows.length === 0) {
      return res.status(400).json({ ok: false, error: 'rows is empty' });
    }
    for (const row of rows) {
      if (!row?.id || !Array.isArray(row?.embedding) || row.embedding.length === 0) {
        return res.status(400).json({ ok: false, error: 'each row needs id and embedding' });
      }
    }
    const instance = await getKb();
    const result = await instance.insert(rows);
    const stats = await instance.stats();
    res.json({ ok: true, ...result, documents: stats.documents });
  } catch (error) {
    fail(res, error);
  }
});

app.post('/api/search', async (req, res) => {
  try {
    const mode = req.body?.mode || 'hybrid';
    const queryText = String(req.body?.query ?? '');
    const embedding = req.body?.embedding;
    const topK = Math.min(Math.max(Number(req.body?.topK) || 10, 1), 100);

    if (!['vector', 'keyword', 'hybrid'].includes(mode)) {
      return res.status(400).json({ ok: false, error: `unsupported mode: ${mode}` });
    }
    if (mode !== 'keyword' && (!Array.isArray(embedding) || embedding.length === 0)) {
      return res.status(400).json({ ok: false, error: 'embedding is required for this mode' });
    }
    if (mode !== 'vector' && queryText.length === 0) {
      return res.status(400).json({ ok: false, error: 'query text is required for this mode' });
    }

    const instance = await getKb();
    const { rows, queryMs, table } = await instance.search({ mode, embedding, queryText, topK });
    res.json({ ok: true, mode, results: rows, queryMs, table });
  } catch (error) {
    fail(res, error);
  }
});

app.post('/api/reset', async (req, res) => {
  try {
    const instance = await getKb();
    await instance.reset();
    res.json({ ok: true });
  } catch (error) {
    fail(res, error);
  }
});

if (serveDist) {
  const dist = path.join(__dirname, '..', 'dist');
  app.use(express.static(dist));
  app.get('*', (req, res) => res.sendFile(path.join(dist, 'index.html')));
}

app.listen(PORT, () => {
  console.log(`[api] listening on http://127.0.0.1:${PORT}`);
  console.log('[api] seekdb:', connectionFromEnv());
});
