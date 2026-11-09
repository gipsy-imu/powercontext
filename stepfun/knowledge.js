import { embed } from './embedding.js';

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error ?? `HTTP ${response.status}`);
  return result;
}

export function listLibraries() {
  return api('/api/libraries');
}

function splitDocument(text) {
  const chunks = [];
  let start = 0;
  while (start < text.length) {
    let end = Math.min(start + 760, text.length);
    if (end < text.length) {
      const boundary = Math.max(text.lastIndexOf(' ', end), text.lastIndexOf('\n', end));
      if (boundary > start + 550) end = boundary + 1;
      // Keep a UTF-16 surrogate pair intact at a hard split.
      if (/[\uD800-\uDBFF]/.test(text[end - 1])) end--;
    }
    const chunk = text.slice(start, end);
    if (chunk.trim()) chunks.push(chunk);
    start = end;
  }
  return chunks;
}

export async function prepareDocuments(documents) {
  const encoder = new TextEncoder();
  const unique = new Map();
  for (const { title, text } of documents) {
    const digest = await crypto.subtle.digest('SHA-256', encoder.encode(JSON.stringify([title, text])));
    const documentId = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
    for (const [index, chunk] of splitDocument(text).entries()) {
      const id = `${documentId}:${index}`;
      unique.set(id, { id, title, text: chunk });
    }
  }
  if (!unique.size) throw new Error('文件里没有可导入的正文。');
  if (unique.size > 512) throw new Error('一次最多导入 512 个文本片段，请拆分文件后重试。');
  return [...unique.values()];
}

export async function ingest(library, records, options) {
  const embedded = await embed(records.map(record => record.text), options);
  options.onStatus?.('正在写入 seekdb 并刷新索引', embedded.backend, embedded.dtype);
  const stored = await api(`/api/libraries/${library}/import`, {
    precision: options.precision ?? 'compact',
    records: records.map((record, index) => ({ ...record, embedding: embedded.vectors[index] })),
  });
  const { vectors, ...timings } = embedded;
  return { ...timings, ...stored };
}

export async function search(library, text, mode, options) {
  const started = performance.now();
  const embedded = await embed([text], options);
  const result = await api(`/api/libraries/${library}/search`, {
    precision: options.precision ?? 'compact',
    text, mode, limit: options.limit ?? 5,
    embedding: embedded.vectors[0],
  });
  return { ...result, queryEmbeddingMs: embedded.inferenceMs, endToEndMs: performance.now() - started };
}
