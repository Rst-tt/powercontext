# 技术说明：端侧向量化与 seekdb 混合检索

## 1. 模型与量化

| 项 | 内容 |
|---|---|
| 模型 | `Xenova/all-MiniLM-L6-v2`（MIT，来自 Hugging Face，ONNX 导出） |
| 维度 / 池化 | 384 维，mean pooling + L2 归一化 |
| 来源与版本 | `Xenova/all-MiniLM-L6-v2` 仓库，`onnx/model.onnx`（FP32）与 `onnx/model_quantized.onnx`（INT8，动态量化） |
| 量化方案 | INT8 权重（ONNX 动态量化），WASM 后端默认加载；WebGPU 使用 FP32 图 |
| 体积 | FP32 86.2 MB → INT8 21.9 MB，**压缩比 3.93x** |
| 质量变化 | 30 篇文档 / 12 条查询、按主题判定相关性：Recall@5 `0.5333 → 0.5333`，MRR `0.9583 → 0.9583`（无下降） |

质量评测口径：同一分词器、同一语料、同一查询，只切换权重精度；相关性按文档主题判定，
指标为 Recall@5 与 MRR（`scripts/eval-quality.mjs`，原始结果 `docs/eval-quality.json`）。

## 2. 端侧运行与后端降级

- 运行时：Transformers.js `@huggingface/transformers`，在浏览器主线程外的推理流程中执行；
  模型文件随应用本地提供（`/models`），ONNX Runtime 的 wasm 也本地化（`/ort`），
  **不依赖 CDN，断网可运行**。
- 后端选择：`navigator.gpu.requestAdapter()` 成功则用 `device: 'webgpu'`，否则用
  `device: 'wasm'`；WebGPU 初始化失败时自动回退 WASM，界面右上角标签实时显示
  `WebGPU` / `WASM`，并在降级时给出提示。
- 对照开关：`?backend=wasm` 强制 CPU 路径，便于评委复现降级行为。

## 3. seekdb 使用

- 接入方式：`seekdb` JS SDK 的 `SeekdbClient`（server 模式），
  `createCollection` 时声明 `VectorIndexConfig(hnsw, cosine, 384)` 与
  `FulltextIndexConfig('space')`，因此同一集合同时具备向量索引与全文索引。
- 写入：`embeddingFunction: null`，向量由浏览器显式计算后随 `add()` 写入，
  数据库侧不做任何 embedding；原文与 `{source, chunk}` 元数据同表存储。
- 混合检索：`hybridSearch({ query: { whereDocument: { $contains } }, knn: { queryEmbeddings }, rank: { rrf } })`
  —— 全文召回与向量召回在数据库内完成，RRF 融合由 SQL 层（`DBMS_HYBRID_SEARCH`）
  执行并返回单一名次列表，客户端不做两路结果的二次拼合。
- 纯向量与纯关键词：分别用 `query()` 与仅 `query`（无 `knn`）的 `hybridSearch()`，
  三条路径共用同一张表。

## 4. 状态、增量与刷新

- 每次导入按文件生成 `source` 元数据与稳定前缀 ID，重复导入同一文件不会覆盖其他文件；
- 批次写入（每 100 行一批）后立即 `COUNT(*)` 复核集合规模；
- 导入是增量的：新文档写入后即可被检索，无需重建集合；
- 「清空知识库」调用 `deleteCollection` 并重建 schema，会话状态与集合状态保持一致。

## 5. 优化点与实测

| 优化 | 做法 | 效果 |
|---|---|---|
| 批处理向量化 | 文档按 8 条一批送入模型，减少逐条调用开销 | 6 个片段 222 ms（WebGPU），27.1 chunk/s |
| 模型加载 | 权重与 ORT 本地提供，浏览器缓存复用 | 二次加载 1.87 s（WebGPU）/ 1.26 s（WASM） |
| 量化 | INT8 权重用于 CPU 路径 | 体积 3.93x 更小，加载更快，质量无变化 |
| 后端选择 | WebGPU 优先，失败回退 WASM | 批量吞吐 27.1 vs 11.5 chunk/s（2.4x） |
| 检索 | 单次 `hybridSearch` 调用完成双路召回 + RRF | seekdb 检索 50–51 ms，端到端 71–79 ms |
| 分块 | 360 字符、40 字符重叠 | 避免句子被截断，提升召回稳定性 |

测量条件：同一浏览器、同一语料（12 段 → 6 个 chunk）、同一批 8 条查询；文档向量化与
查询向量化分开计时；首次请求与预热后请求分开报告；表中取预热后的中位数。

## 6. 复现

```bash
npm install --omit=optional --legacy-peer-deps
npm run setup                 # 下载 FP32/INT8 权重并把 ORT wasm 复制到 public/ort
npm run eval                  # 量化前后质量与体积
npm run dev                   # 打开 http://localhost:5173
# 对照测量：http://localhost:5173/bench.html?backend=wasm
```
