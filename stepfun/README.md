# 实验知识库

导入实验笔记、读取命中的原文和文件名。浏览器运行 MiniLM 生成文档向量和查询向量，seekdb 保存向量及原文并执行检索。

## 启动

在本目录用 Node.js 22 或更新版本运行以下命令。打开 [实验知识库](http://127.0.0.1:4173)，推荐使用 Chrome 或 Edge。

```sh
npm ci --ignore-scripts --legacy-peer-deps
npm run build
npm start
```

默认使用 seekdb-js 1.4.0 的嵌入式模式，数据库保存在 `data/`。SDK 首次运行会自动下载与系统匹配的原生组件。嵌入式组件支持 macOS arm64、Linux x64/arm64 和 Windows x64。浏览器首次使用时从 Hugging Face 加载固定版本的模型文件。浏览器本地缓存后可复用文件，模型计算始终在本机进行。 [seekdb-js 模式与平台支持](https://github.com/oceanbase/seekdb-js)

已有 seekdb 1.3 或更新版本的服务时，可设置 `SEEKDB_HOST`、`SEEKDB_PORT`、`SEEKDB_USER`、`SEEKDB_DATABASE` 和 `SEEKDB_PASSWORD` 后再启动。指定数据库应已存在，并允许应用创建集合和刷新索引。嵌入式模式可用 `SEEKDB_PATH` 更改数据目录，`PORT` 用于更改网页端口。应用只监听回环地址。

如果评测网络限制 Hugging Face，可预先缓存下列公开文件。应用会优先读取 `public/models/` 中的模型，缓存目录无需随源码提交。

```sh
model='Xenova/all-MiniLM-L6-v2'
revision='751bff37182d3f1213fa05d7196b954e230abad9'
mkdir -p "public/models/$model/onnx"
for asset in config.json tokenizer.json tokenizer_config.json onnx/model.onnx onnx/model_fp16.onnx onnx/model_quantized.onnx; do
  curl -fL "https://huggingface.co/$model/resolve/$revision/$asset" -o "public/models/$model/$asset" || exit 1
done
```

## 复现

1. 保持 `lab-notes`，点「导入示例笔记」。首次成功会显示新增 12 个片段和索引刷新结果。
2. 输入 `sensor calibration`，保留「全文与向量混合」。搜索结果应包含 `Thermal probe calibration` 及对应原文。
3. 再次导入相同示例，应显示已存在 12 个片段。也可将新的 UTF-8 `.txt` 或 `.md` 文件导入同一个库，刷新后搜索其中的词。
4. 切换到一个新库名并重查，结果为空。切回 `lab-notes` 后重新搜索，原结果仍可获取。
5. 打开 [WASM 模式](http://127.0.0.1:4173/?backend=wasm) 复现禁用 WebGPU 的情况。再次搜索时界面应显示 `WASM · INT8`。
6. 展开「复现模型与检索对比实验」，点击「运行对比」。需要测初次初始化时，请在新标签页打开应用后直接运行对比。报告可下载为 JSON。

样例为自编英文实验笔记，模型适合英文检索。界面为中文。单次导入最多 24 个文件、384 KiB、512 个片段。每条结果显示原文片段和对应文件名。

实测条件、对比结果和报告见 [技术说明](TECHNICAL.md)。
