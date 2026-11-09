import { mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { performance } from 'node:perf_hooks';
import express from 'express';
import { FulltextIndexConfig, Schema, SeekdbClient, VectorIndexConfig } from 'seekdb';

const app = express();
const dataPath = process.env.SEEKDB_PATH ?? fileURLToPath(new URL('./data/', import.meta.url));
const host = process.env.SEEKDB_HOST;
const database = process.env.SEEKDB_DATABASE ?? 'test';
const dimension = 384;
const libraryPattern = /^[a-z][a-z0-9-]{0,29}$/;
const precisions = new Set(['compact', 'fp32']);
const schema = new Schema({
  fulltextIndex: new FulltextIndexConfig('beng'),
  vectorIndex: new VectorIndexConfig({ hnsw: { dimension, distance: 'cosine' }, embeddingFunction: null }),
});

if (!host) await mkdir(dataPath, { recursive: true });
const db = new SeekdbClient(host ? {
  host,
  port: Number(process.env.SEEKDB_PORT ?? 2881),
  user: process.env.SEEKDB_USER ?? 'root',
  password: process.env.SEEKDB_PASSWORD ?? '',
  database,
} : { path: dataPath, database });

// The SDK's hybrid search uses a seekdb session variable across SQL statements.
let dbQueue = Promise.resolve();
function runInSession(operation) {
  const result = dbQueue.then(operation);
  dbQueue = result.catch(() => {});
  return result;
}

function badInput(message) {
  return Object.assign(new Error(message), { status: 400 });
}

function collectionName(library, precision) {
  if (!libraryPattern.test(library) || !precisions.has(precision)) {
    throw badInput('库名须用小写字母开头，只含小写字母、数字和短横线，最多 30 个字符。');
  }
  return `stepfun_${precision}_${library.replaceAll('-', '_')}`;
}

function isVector(value) {
  return Array.isArray(value) && value.length === dimension && value.every(Number.isFinite)
    && value.some(component => component !== 0);
}

app.disable('x-powered-by');
app.use((request, response, next) => {
  if (request.hostname === '127.0.0.1' || request.hostname === 'localhost') return next();
  response.sendStatus(403);
});
app.use(express.json({ limit: '10mb' }));

app.get('/api/libraries', async (_request, response) => {
  const names = await runInSession(async () => {
    const collections = await db.listCollections({ withEmbeddingFunction: false });
    return collections.map(({ name }) => /^stepfun_(?:compact|fp32)_(.+)$/.exec(name)?.[1]?.replaceAll('_', '-'))
      .filter(name => name && !name.startsWith('benchmark-'));
  });
  response.json({ names: [...new Set(names)].sort() });
});

app.post('/api/libraries/:library/import', async (request, response) => {
  const { precision, records } = request.body ?? {};
  const name = collectionName(request.params.library, precision);
  if (!Array.isArray(records) || records.length < 1 || records.length > 512 || !records.every(record => (
    record && typeof record.id === 'string' && /^[a-f0-9]{64}:\d{1,4}$/.test(record.id)
    && typeof record.title === 'string' && record.title.length > 0 && record.title.length <= 512
    && typeof record.text === 'string' && record.text.trim() && record.text.length <= 1200
    && isVector(record.embedding)
  ))) throw badInput('导入批次无效，需包含有效文档片段及 384 维有限数值向量。');

  const metrics = await runInSession(async () => {
    const collection = await db.getOrCreateCollection({ name, schema, embeddingFunction: null });
    const writeStart = performance.now();
    const unique = [...new Map(records.map(record => [record.id, record])).values()];
    let inserted = 0;
    await db.execute('START TRANSACTION');
    try {
      for (let offset = 0; offset < unique.length; offset += 32) {
        const batch = unique.slice(offset, offset + 32);
        const existing = await collection.get({ ids: batch.map(record => record.id), include: [] });
        const known = new Set(existing.ids);
        const fresh = batch.filter(record => !known.has(record.id));
        if (!fresh.length) continue;
        await collection.add({
          ids: fresh.map(record => record.id),
          documents: fresh.map(record => record.text),
          metadatas: fresh.map(record => ({ title: record.title, document_id: record.id.split(':')[0] })),
          embeddings: fresh.map(record => record.embedding),
        });
        inserted += fresh.length;
      }
      await db.execute('COMMIT');
    } catch (error) {
      await db.execute('ROLLBACK');
      throw error;
    }
    const refreshStart = performance.now();
    await collection.refresh_index();
    return { inserted, writeMs: refreshStart - writeStart, refreshMs: performance.now() - refreshStart };
  });
  response.json({ count: records.length, ...metrics });
});

app.post('/api/libraries/:library/search', async (request, response) => {
  const { precision, text, embedding, mode = 'hybrid', limit = 5 } = request.body ?? {};
  const name = collectionName(request.params.library, precision);
  if (typeof text !== 'string' || !text.trim() || text.length > 500 || !isVector(embedding)
    || !['hybrid', 'vector'].includes(mode) || !Number.isInteger(limit) || limit < 1 || limit > 20) {
    throw badInput('查询无效，需包含查询正文、有效向量和检索模式。');
  }

  const result = await runInSession(async () => {
    if (!await db.hasCollection(name)) return { hits: [], searchMs: 0 };
    const collection = await db.getCollection({ name, embeddingFunction: null });
    const start = performance.now();
    const include = ['documents', 'metadatas', 'distances'];
    const knn = { queryEmbeddings: [embedding], nResults: Math.max(20, limit) };
    const rows = mode === 'vector'
      ? await collection.query({ queryEmbeddings: [embedding], nResults: limit, include })
      : await collection.hybridSearch({
        query: { whereDocument: { $contains: text.trim() }, nResults: Math.max(20, limit) },
        knn,
        rank: { rrf: {} },
        nResults: limit,
        include,
      });
    const searchMs = performance.now() - start;
    return {
      hits: rows.ids[0].map((id, index) => ({
        id,
        text: rows.documents[0][index],
        ...rows.metadatas[0][index],
      })),
      searchMs,
    };
  });
  response.json(result);
});

const models = fileURLToPath(new URL('./public/models/', import.meta.url));
const dist = fileURLToPath(new URL('./dist/', import.meta.url));
app.use('/models', express.static(models));
app.use(express.static(dist));
app.use((error, _request, response, _next) => {
  const status = error.status >= 400 && error.status < 500 ? error.status : 500;
  if (status === 500) console.error(error);
  response.status(status).json({ error: status === 500 ? 'seekdb 操作失败，请查看服务端日志。' : error.message });
});

const port = Number(process.env.PORT ?? 4173);
app.listen(port, '127.0.0.1', () => console.log(`实验知识库 http://127.0.0.1:${port}`));
