import { env, pipeline } from '@huggingface/transformers';

export const model = 'Xenova/all-MiniLM-L6-v2';
export const revision = '751bff37182d3f1213fa05d7196b954e230abad9';
const forceWasm = new URLSearchParams(location.search).get('backend') === 'wasm';
env.localModelPath = '/models/';
env.backends.onnx.wasm.numThreads = 1;

const engines = new Map();
let webGpuAvailable;

async function supportsWebGpu() {
  if (webGpuAvailable === undefined) {
    try {
      webGpuAvailable = !forceWasm && Boolean(await navigator.gpu?.requestAdapter());
    } catch {
      webGpuAvailable = false;
    }
  }
  return webGpuAvailable;
}

async function loadEngine(precision, backend, onStatus) {
  const dtype = precision === 'fp32' ? 'fp32' : backend === 'webgpu' ? 'fp16' : 'q8';
  const loadStart = performance.now();
  const extractor = await pipeline('feature-extraction', model, {
    revision,
    device: backend,
    dtype,
    progress_callback(event) {
      if (event.status === 'progress') onStatus(`正在加载 ${event.file} · ${Math.round(event.progress)}%`, backend, dtype);
    },
  });
  const engine = { extractor, backend, dtype, loadMs: performance.now() - loadStart };
  engines.set(precision, engine);
  return engine;
}

export async function embed(texts, { precision = 'compact', batchSize, onStatus = () => {} } = {}) {
  let engine = engines.get(precision);
  let backend = engine?.backend ?? (await supportsWebGpu() ? 'webgpu' : 'wasm');
  let loadMs = 0;
  for (;;) {
    try {
      if (!engine) {
        onStatus('正在初始化本地模型', backend);
        engine = await loadEngine(precision, backend, onStatus);
        loadMs += engine.loadMs;
      }
      const size = batchSize ?? (backend === 'webgpu' ? 8 : 1);
      const vectors = [];
      let inferenceMs = 0;
      for (let offset = 0; offset < texts.length; offset += size) {
        const batch = texts.slice(offset, offset + size);
        onStatus(`正在向量化 ${offset + 1}–${offset + batch.length} / ${texts.length}`, backend, engine.dtype);
        const inferenceStart = performance.now();
        const tensor = await engine.extractor(batch, { pooling: 'mean', normalize: true });
        inferenceMs += performance.now() - inferenceStart;
        vectors.push(...tensor.tolist());
      }
      return { vectors, backend, dtype: engine.dtype, loadMs, inferenceMs, batchSize: size };
    } catch (error) {
      if (backend !== 'webgpu') throw error;
      engines.delete(precision);
      await engine?.extractor.dispose().catch(() => {});
      engine = undefined;
      backend = 'wasm';
      onStatus('WebGPU 初始化或推理失败，已切换到 WASM', backend);
    }
  }
}
