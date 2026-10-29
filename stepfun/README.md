# 本地知识库 · 端侧向量化 + seekdb 混合检索

一个完全在浏览器里完成向量化的迷你知识库：导入文本 → 本地 embedding → 写入 seekdb →
用 seekdb 做向量与全文的混合检索。文本与查询都不会离开用户设备。

- **端侧推理**：`@huggingface/transformers`（Transformers.js）在浏览器内运行
  `all-MiniLM-L6-v2`（384 维），优先 WebGPU，不可用时自动降级 WASM 并在界面标注。
- **量化**：随包提供 FP32 与 INT8 两份权重，压缩比 3.93x，Recall@5 / MRR 无变化。
- **数据层**：`seekdb` JS SDK（server 模式）写入向量、原文与元数据；向量索引与全文索引
  都建在 seekdb 里，混合检索（RRF）由数据库执行，客户端不做二次拼合。
- **增量与状态**：支持分批增量导入、索引刷新后可检索、清空与集合状态重置。

## 快速开始

```bash
# 1. 安装依赖（seekdb 的可选原生绑定只用于嵌入式模式，这里不需要）
npm install --omit=optional --legacy-peer-deps

# 2. 准备模型与推理运行时（下载 FP32/INT8 权重 + 本地化 ORT wasm）
npm run setup

# 3. 启动 seekdb（任选其一）
#    a) 已有 seekdb/OceanBase 实例：直接配置连接参数
#    b) 用仓库根目录的 seekdb 二进制：./seekdb --nodaemon --base-dir=/tmp/kb --port=2881
export SEEPDB_HOST=127.0.0.1 SEEPDB_PORT=2881 SEEPDB_USER=root SEEPDB_DATABASE=test

# 4. 启动 API 与前端
npm run dev          # API :8787 + Vite :5173
# 或者构建后用单一进程托管
npm start            # 构建 dist 并由 :8787 同时提供页面与 API
```

打开 <http://localhost:5173>：

1. 点「加载模型」，等待状态栏显示 `WebGPU` / `WASM` 与精度；
2. 拖入 `.txt/.md` 文件或粘贴文本，点「向量化并写入」；
3. 输入查询，切换「混合 (RRF) / 语义向量 / 关键词全文」三种模式查看结果与耗时。

### 强制后端（用于对照测试）

- `http://localhost:5173/?backend=webgpu`：优先 WebGPU，失败回退 WASM
- `http://localhost:5173/?backend=wasm`：固定 WASM（CPU），用于与 WebGPU 对比
- `http://localhost:5173/bench.html?backend=wasm`：内置测量页，输出分阶段耗时

## 目录结构

```
server/            Express API + seekdb SDK 封装（向量/全文/RRF 检索）
src/embedder.js    端侧推理：后端选择、量化精度、批量向量化、分块
src/main.js        界面逻辑：导入、进度、检索、耗时统计
src/bench.js       对照测量页（WebGPU vs WASM、首次 vs 预热）
scripts/setup-assets.mjs  下载模型权重并本地化 ONNX Runtime
scripts/eval-quality.mjs  量化前后检索质量对比（FP32 vs INT8）
docs/TECHNICAL.md  一页技术说明
docs/eval-quality.json    量化评测原始结果
```

## 实测数据

设备：Windows 11 + Intel i7-13650HX，浏览器 Chrome（WebGPU 可用）。
语料：12 段英文技术文本（分块后 6 个 chunk），查询 8 条；每次测量含首次与后续请求。

| 配置 | 模型体积 | 加载 | 文档向量化 | 查询向量化（预热） | seekdb 检索 | 端到端（预热） |
|---|---|---|---|---|---|---|
| WebGPU + FP32 | 86.2 MB | 1873 ms | 222 ms（37.0 ms/chunk，27.1 chunk/s） | 19 ms | 51 ms | 79 ms |
| WASM + INT8 | 21.9 MB | 1259 ms | 522 ms（87.0 ms/chunk，11.5 chunk/s） | 13 ms | 50 ms | 71 ms |

- 批量向量化 WebGPU 约为 WASM 的 **2.4x**；单条查询两者都进入十几毫秒量级，
  因此默认策略是「WebGPU 优先，不可用时降级 WASM」。
- 首次查询明显更慢（WebGPU 568 ms / WASM 86 ms），来源是推理后端图初始化与预热，
  技术说明里单独区分了首次与预热后的数值。
- 量化：FP32 86.2 MB → INT8 21.9 MB（**3.93x**），Recall@5 0.5333 → 0.5333，
  MRR 0.9583 → 0.9583（30 篇文档、12 条查询、按主题判定相关性）。

## 数据模型

`seekdb` collection `kb_docs`（由 SDK 建表，物理表名为 `c$v2$<collection_id>`）：

| 字段 | 说明 |
|---|---|
| id | 片段 ID（`文件#序号-随机串`） |
| document | 原文片段 |
| embedding | 384 维向量（浏览器计算，写入时显式传入） |
| metadata | `{source, chunk}`，用于展示来源与增量判断 |

索引：HNSW 向量索引（cosine）+ 全文索引（space analyzer）。
检索：`hybridSearch`（全文 + 向量，RRF 融合）、`query`（纯向量）、全文条件（纯关键词），
三者都由 seekdb 执行。

## 复现质量与性能

```bash
npm run eval     # FP32 vs INT8：体积、Recall@5、MRR
npm run bench    # 打印同一语料下的端侧耗时（需浏览器，见 bench.html）
```
