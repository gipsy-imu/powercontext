import { embed, model, revision } from './embedding.js';
import { documents, queries } from './fixtures.js';
import { ingest, prepareDocuments, search } from './knowledge.js';

const sharedAssetBytes = 712677;
const weightBytes = { q8: 22972370, fp16: 45297825, fp32: 90387606 };

function median(samples) {
  const sorted = [...samples].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function measure(samples) {
  return { samples: samples.map(value => Math.round(value * 100) / 100), median: Math.round(median(samples) * 100) / 100 };
}

export async function benchmark(onStatus) {
  const library = `benchmark-${crypto.randomUUID().slice(0, 8)}`;
  const records = await prepareDocuments(documents);
  const corpus = records.map(record => record.text);
  const report = {
    model, revision,
    browser: navigator.userAgent,
    logicalCpus: navigator.hardwareConcurrency,
    corpusDocuments: documents.length,
    queries: queries.length,
    artifactBytes: Object.fromEntries(Object.entries(weightBytes).map(([dtype, bytes]) => [dtype, bytes + sharedAssetBytes])),
    models: {},
  };

  for (const precision of ['compact', 'fp32']) {
    const options = { precision, limit: 3, onStatus };
    const imported = await ingest(library, records, options);
    const single = [];
    const batch = [];
    for (let repeat = 0; repeat < 3; repeat++) {
      for (const batchSize of repeat % 2 === 0 ? [1, 8] : [8, 1]) {
        const sample = await embed(corpus, { ...options, batchSize });
        (batchSize === 1 ? single : batch).push(sample.inferenceMs);
      }
    }
    const quality = {};
    for (const mode of ['vector', 'hybrid']) {
      const ranks = [];
      const searchMs = [];
      const queryEmbeddingMs = [];
      const endToEndMs = [];
      for (const [query, expectedTitle] of queries) {
        const result = await search(library, query, mode, options);
        ranks.push(result.hits.findIndex(hit => hit.title === expectedTitle) + 1);
        searchMs.push(result.searchMs);
        queryEmbeddingMs.push(result.queryEmbeddingMs);
        endToEndMs.push(result.endToEndMs);
      }
      quality[mode] = {
        recallAt3: ranks.filter(rank => rank > 0).length / ranks.length,
        mrrAt3: ranks.reduce((sum, rank) => sum + (rank ? 1 / rank : 0), 0) / ranks.length,
        ranks, searchMs: measure(searchMs), queryEmbeddingMs: measure(queryEmbeddingMs), endToEndMs: measure(endToEndMs),
      };
    }
    report.models[precision] = {
      ...imported,
      warmSingleMs: measure(single),
      warmBatch8Ms: measure(batch),
      quality,
    };
  }
  report.compressionRatio = report.artifactBytes.fp32 / report.artifactBytes[report.models.compact.dtype];
  return report;
}
