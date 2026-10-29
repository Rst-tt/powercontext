/**
 * seekdb access layer.
 *
 * The embedding vectors are produced in the browser, so the collection is created
 * with `embeddingFunction: null` and every write passes the vectors explicitly.
 * The collection carries both a dense vector index (HNSW, cosine) and a full-text
 * index, which lets seekdb answer vector, keyword and fused (RRF) queries.
 */
import {
  SeekdbClient,
  Schema,
  VectorIndexConfig,
  FulltextIndexConfig,
  registerEmbeddingFunction,
  registerSparseEmbeddingFunction,
} from 'seekdb';

export const VECTOR_DIMENSION = 384;
export const COLLECTION_NAME = process.env.KB_COLLECTION || 'kb_docs';

/**
 * The SDK expects an embedding function for a collection that stores vectors. This
 * application never lets the database embed anything - the vectors arrive from the
 * browser - so a stub is registered under the name the SDK looks for.
 */
class BrowserSideEmbeddingFunction {
  constructor() {
    this.name = 'default-embed';
    this.dimension = VECTOR_DIMENSION;
  }

  async generate() {
    throw new Error('embeddings are computed in the browser and passed explicitly');
  }

  getConfig() {
    return { model: 'browser-side', dimension: VECTOR_DIMENSION };
  }

  async dispose() {}
}

let registered = false;
function registerStubs() {
  if (registered) return;
  try {
    registerEmbeddingFunction('default-embed', BrowserSideEmbeddingFunction);
  } catch (error) {
    // already registered by a previous module instance
  }
  try {
    registerSparseEmbeddingFunction?.('default-sparse', BrowserSideEmbeddingFunction);
  } catch (error) {
    // optional
  }
  registered = true;
}

export function connectionFromEnv() {
  return {
    host: process.env.SEEPDB_HOST || '127.0.0.1',
    port: Number(process.env.SEEPDB_PORT || 2881),
    user: process.env.SEEPDB_USER || 'root',
    password: process.env.SEEPDB_PASSWORD || '',
    database: process.env.SEEPDB_DATABASE || 'test',
  };
}

function schema() {
  return new Schema({
    vectorIndex: new VectorIndexConfig({
      hnsw: { dimension: VECTOR_DIMENSION, distance: 'cosine' },
      embeddingFunction: null,
    }),
    // "space" tokenises on whitespace: good for English corpora. Set
    // KB_FT_ANALYZER=ngram (or ik) for Chinese text on a build that supports it.
    fulltextIndex: new FulltextIndexConfig(process.env.KB_FT_ANALYZER || 'space'),
  });
}

export class KnowledgeBase {
  constructor(config) {
    this.config = config;
    this.client = null;
    this.collection = null;
    this.table = null;
  }

  async open() {
    registerStubs();
    this.client = new SeekdbClient(this.config);
    try {
      this.collection = await this.client.getCollection({ name: COLLECTION_NAME });
    } catch (error) {
      this.collection = await this.client.createCollection({
        name: COLLECTION_NAME,
        schema: schema(),
      });
    }
    return this;
  }

  async reset() {
    try {
      await this.client.deleteCollection(COLLECTION_NAME);
    } catch (error) {
      // missing collection is fine
    }
    this.table = null;
    this.collection = await this.client.createCollection({
      name: COLLECTION_NAME,
      schema: schema(),
    });
  }

  async stats() {
    const table = await this.tableName();
    const rows = await this.client.execute(
      `SELECT COUNT(*) AS cnt FROM ${quoteTable(table)}`,
    );
    const count = Number(rows?.[0]?.cnt ?? rows?.[0]?.CNT ?? 0);
    return { documents: count };
  }

  /**
   * The SDK stores each collection in a table named c$<version>$<collection_id>
   * and keeps the mapping in sdk_collections.
   */
  async tableName() {
    if (this.table) return this.table;
    const id = this.collection?.collectionId;
    if (id) {
      const rows = await this.client.execute(
        'SELECT settings FROM sdk_collections WHERE collection_id = ?',
        [id],
      );
      const settings = rows?.[0]?.settings;
      const version = parseVersion(settings) || 'v2';
      this.table = `c$${version}$${id}`;
      return this.table;
    }
    const rows = await this.client.execute(
      'SELECT collection_id, settings FROM sdk_collections WHERE collection_name = ?',
      [COLLECTION_NAME],
    );
    if (!rows?.length) throw new Error('collection metadata not found');
    const version = parseVersion(rows[0].settings) || 'v2';
    this.table = `c$${version}$${rows[0].collection_id}`;
    return this.table;
  }

  /**
   * Insert chunks. `rows` is [{id, document, embedding, metadata}].
   */
  async insert(rows) {
    if (!rows.length) return { inserted: 0 };
    const started = process.hrtime.bigint();
    await this.collection.add({
      ids: rows.map((r) => r.id),
      documents: rows.map((r) => r.document),
      embeddings: rows.map((r) => r.embedding),
      metadatas: rows.map((r) => r.metadata || {}),
    });
    const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;
    return { inserted: rows.length, writeMs: elapsedMs };
  }

  /**
   * One retrieval call. `mode` is vector | keyword | hybrid.
   */
  async search({ mode, embedding, queryText, topK }) {
    const started = process.hrtime.bigint();
    const include = ['documents', 'metadatas', 'distances'];
    let result;

    if (mode === 'keyword') {
      result = await this.collection.hybridSearch({
        query: { whereDocument: { $contains: queryText }, nResults: topK },
        rank: { rrf: { rankWindowSize: topK * 4, rankConstant: 60 } },
        nResults: topK,
        include,
      });
    } else if (mode === 'vector') {
      result = await this.collection.query({
        queryEmbeddings: [embedding],
        nResults: topK,
        include,
      });
    } else {
      result = await this.collection.hybridSearch({
        query: { whereDocument: { $contains: queryText }, nResults: topK * 4 },
        knn: { queryEmbeddings: [embedding], nResults: topK * 4 },
        rank: { rrf: { rankWindowSize: topK * 4, rankConstant: 60 } },
        nResults: topK,
        include,
      });
    }

    const elapsedMs = Number(process.hrtime.bigint() - started) / 1e6;
    return { rows: normalize(result, mode), queryMs: elapsedMs, table: await this.tableName() };
  }
}

function quoteTable(name) {
  return '`' + String(name).replace(/`/g, '') + '`';
}

function parseVersion(settings) {
  if (!settings) return null;
  try {
    const parsed = typeof settings === 'string' ? JSON.parse(settings) : settings;
    return parsed?.version || null;
  } catch (error) {
    return null;
  }
}

/**
 * The SDK returns column-oriented arrays; turn them into a row list.
 */
function normalize(result, mode) {
  const ids = result?.ids?.[0] || [];
  const documents = result?.documents?.[0] || [];
  const metadatas = result?.metadatas?.[0] || [];
  const distances = result?.distances?.[0] || [];
  return ids.map((id, index) => ({
    id,
    document: documents[index] ?? '',
    metadata: metadatas[index] ?? {},
    score: distances[index] ?? null,
    mode,
  }));
}
