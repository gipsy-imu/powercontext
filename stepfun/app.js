import './style.css';
import { benchmark } from './benchmark.js';
import { documents } from './fixtures.js';
import { ingest, listLibraries, prepareDocuments, search } from './knowledge.js';

const get = id => document.getElementById(id);
const library = get('library');
const results = get('results');
const actions = [...document.querySelectorAll('button, input, select')];
let report;
library.value = localStorage.getItem('stepfun-library') || library.value;

function status(id, message, error = false) {
  const element = get(id);
  element.textContent = message;
  element.dataset.error = String(error);
}

function optionsFor(id) {
  return {
    onStatus(message, backend, dtype) {
      status(id, message);
      const precision = dtype === 'q8' ? 'INT8' : dtype?.toUpperCase();
      get('backend').textContent = `当前后端 ${backend === 'webgpu' ? 'WebGPU' : 'WASM'}${precision ? ` · ${precision}` : ''}`;
    },
  };
}

function activeLibrary() {
  const name = library.value.trim();
  if (!/^[a-z][a-z0-9-]{0,29}$/.test(name)) throw new Error('请输入有效的知识库名称。');
  localStorage.setItem('stepfun-library', name);
  return name;
}

async function refreshLibraries() {
  const { names } = await listLibraries();
  get('libraries').replaceChildren(...names.map(name => {
    const option = document.createElement('option');
    option.value = name;
    return option;
  }));
}

async function busy(id, operation) {
  actions.forEach(element => { element.disabled = true; });
  status(id, '处理中…');
  try {
    await operation();
  } catch (error) {
    status(id, error.message, true);
  } finally {
    actions.forEach(element => { element.disabled = false; });
    get('export').disabled = !report;
  }
}

async function importDocuments(notes, name) {
  const records = await prepareDocuments(notes);
  const result = await ingest(name, records, optionsFor('import-status'));
  status('import-status', `${name} · 新增 ${result.inserted} 个片段 · 已存在 ${result.count - result.inserted} 个 · 索引刷新完成。`);
  await refreshLibraries();
}

library.addEventListener('input', () => {
  results.replaceChildren();
  status('search-status', '');
});

get('import-form').addEventListener('submit', event => {
  event.preventDefault();
  busy('import-status', async () => {
    const name = activeLibrary();
    const files = [...get('files').files];
    if (!files.length || files.length > 24 || files.some(file => file.size > 256 * 1024)
      || files.reduce((sum, file) => sum + file.size, 0) > 384 * 1024) {
      throw new Error('请选择 1 至 24 个文本文件，每个不超过 256 KiB，合计不超过 384 KiB。');
    }
    const decoder = new TextDecoder('utf-8', { fatal: true });
    const notes = await Promise.all(files.map(async file => ({
      title: file.name, text: decoder.decode(await file.arrayBuffer()),
    })));
    await importDocuments(notes, name);
  });
});

get('demo').addEventListener('click', () => {
  busy('import-status', () => importDocuments(documents, activeLibrary()));
});

get('search-form').addEventListener('submit', event => {
  event.preventDefault();
  busy('search-status', async () => {
    results.replaceChildren();
    const name = activeLibrary();
    const text = get('query').value.trim();
    if (!text) throw new Error('请输入查询内容。');
    const result = await search(name, text, get('mode').value, optionsFor('search-status'));
    status('search-status', result.hits.length
      ? `${name} · ${result.hits.length} 条结果 · seekdb 检索 ${result.searchMs.toFixed(1)} ms`
      : `${name} 中没有可显示的结果。可先导入笔记。`);
    for (const hit of result.hits) {
      const row = document.createElement('li');
      const title = document.createElement('strong');
      const text = document.createElement('p');
      title.textContent = hit.title;
      text.textContent = hit.text;
      row.append(title, text);
      results.append(row);
    }
  });
});

get('benchmark').addEventListener('click', () => {
  busy('benchmark-status', async () => {
    report = await benchmark(optionsFor('benchmark-status').onStatus);
    get('report').hidden = false;
    get('report').textContent = JSON.stringify(report, null, 2);
    status('benchmark-status', '对比完成。报告包含量化前后的质量与各阶段耗时。');
  });
});

get('export').addEventListener('click', () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'stepfun-benchmark.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

refreshLibraries().catch(error => status('import-status', error.message, true));
