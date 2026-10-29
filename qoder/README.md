# 知新 / Recall Coach

> 一个会用「记忆」复习你的个人学习教练：资料检索交给 **seekdb**，学习者画像与复习上下文交给 **PowerContext**。

## 1. 解决什么问题

复习的痛点通常不在「没有资料」，而在资料与学习者之间缺少一条连续的线：

- 文献、笔记、错题散落在不同文件夹，想复习时只能从头翻；
- 每次打开学习工具，它都像第一次见你：不知道你哪一章薄弱、上次为什么错、喜欢先看例子还是先看定义；
- 复习完就结束了，没有东西沉淀到下一轮，同一个知识点反复「重新认识」。

**知新**把这件事做成一个完整闭环：资料进 seekdb，作答结果进 PowerContext，下一轮复习自动带着上一轮的记忆开始。

## 2. 核心体验

1. **导入资料**：把课堂笔记、论文摘录、错题解析按「课程 + 标题 + 正文」粘贴或上传（`.txt/.md/.json`），系统自动切段并写入 seekdb（向量 + 全文双索引）。
2. **带记忆的开始**：点「开始复习」时，应用先向 PowerContext 取「这个学习者与本次课程相关的上下文」——薄弱点、偏好、进度——再让 seekdb 按这份上下文检索相关原文。
3. **出题与作答**：从 seekdb 召回的原文里生成选择题（正确项来自原文，干扰项同样来自 seekdb 检索结果），作答即评分。
4. **写回记忆**：答错/标记「有点难」时，把「哪一章、错在哪个概念、上次错因」写成一条 memory entry；达到里程碑时记录进度；可随时修正或失效某条记忆。
5. **下一轮更懂你**：再次开始时，PowerContext 的 `context.prepare` 会把最近的高优先级记忆拼成上下文，界面直接显示「PowerContext 记得的你」。

## 3. 指定技术分工（评审可核对）

| 技术 | 在本项目里的职责 | 可以怎么核对 |
| --- | --- | --- |
| **seekdb**（向量 + 全文混合检索） | 学习资料的唯一存储与检索底座：`coach_materials` 表带 `VECTOR(1024)` 列和 HNSW 索引，检索时向量通道 `ORDER BY l2_distance(...) APPROXIMATE` 与全文通道 `MATCH(search_text) AGAINST(...)` 各自取候选，再用 RRF 融合；每次作答的评估也回写掌握度、复习次数 | 界面「执行记录 → seekdb」逐条显示 SQL、参数、行数、耗时；`/api/evidence` 返回同样内容；`/api/search` 返回两通道名次 |
| **PowerContext**（记忆与上下文） | 学习者侧的持久记忆：薄弱点（weakness）、常见误区（misconception）、偏好（preference）、进度（progress）；`memory/remember` 写入、`memory/search` 召回、`context/prepare` 组装下一轮上下文、`memory/entries/list` 渲染记忆面板；「修正」= 先 `atomic-memory/lifecycle` 失效旧条目再写新条目，「失效」= 标记 `forgotten`（PowerContext 1.4.x Atomic Memory API），历史仍可审计 | 界面「执行记录 → PowerContext」逐条显示 HTTP 方法、路径、请求、响应、耗时；「PowerContext 记得的你」直接展示 `context/prepare` 返回的上下文文本 |

两者不互相替代：**检索结果只有在学习者作答之后才可能变成记忆**，而记忆只用于影响下一轮检索与提示。

## 4. 架构

```
┌──────────────────────────── 浏览器（web/，无构建步骤） ────────────────────────────┐
│  导入资料 · 开始复习 · 作答 · 记忆面板（修正/失效） · 执行记录（seekdb / PowerContext） │
└───────────────────────────────────┬───────────────────────────────────────────────┘
                                    │ HTTP JSON
┌───────────────────────────────────▼───────────────────────────────────────────────┐
│ app/main.py（FastAPI）：/api/materials /api/session /api/grade /api/memory ...      │
│ app/coach.py：复习会话、出题、评分、掌握度、记忆读写编排                            │
│        ┌──────────────────────────┴──────────────────────────┐                    │
│        ▼                                                     ▼                    │
│ app/store/seekdb.py（MaterialStore）                app/pc_memory.py（LearnerMemory）│
│  混合检索：向量 HNSW + 全文 FTS → RRF              PowerContext HTTP API 客户端     │
│  嵌入式 pylibseekdb / 远程 MySQL 协议              嵌入式子进程 Server / 外部 URL   │
└───────────────────────────────────────────────────────────────────────────────────┘
```

降级路径：Windows 上没有 seekdb 运行时（`pylibseekdb` 只提供 Linux/macOS wheel），`app/store/mirror.py` 用 SQLite + 纯 Python 余弦相似度 + 字符 n-gram 词法分保持同一 `MaterialStore` 契约，界面会明确标注「SQLite 镜像」。PowerContext 在 Windows 上未安装时同样降级为离线，记忆面板提示不可用，但检索、导入、复习闭环仍然完整。

## 5. 快速开始

### 5.1 Windows（无 seekdb 运行时的镜像模式，5 分钟体验检索闭环）

```powershell
cd qoder
py -3.12 -m venv .venv                      # 或复用已有虚拟环境
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8848
```

打开 <http://127.0.0.1:8848>，点击左栏「载入示例」写入 9 段示例资料。此时检索后端为 `sqlite-mirror`（没有 seekdb 运行时），但 `powercontext[server,client]` 在 Windows 上同样可用，记忆为「在线」，完整 11 步验证也能跑通；只有主动不装 `powercontext` 时才降级为「记忆离线」（7 步通过 + 3 步跳过）。

### 5.2 Linux / macOS（主通道：seekdb 嵌入式 + PowerContext 嵌入式）

```bash
cd qoder
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
export SEEKDB_PATH="$HOME/.recall-coach/seekdb"      # 嵌入式 seekdb 数据目录
export COACH_DATA_DIR="$HOME/.recall-coach"           # PowerContext 工作目录
PYTHONIOENCODING=utf-8 .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8848
```

启动后 `/api/status` 应显示 `backend.backend = "seekdb"`、`backend.mode = "embedded"`、`memory.available = true`。界面右上角三个徽章分别是：检索底座、embedding、PowerContext。

### 5.3 连接外部 PowerContext（HTTP API / Docker）

```bash
export COACH_PC_MODE=external
export COACH_PC_URL=http://127.0.0.1:8080        # PowerContext Server 地址
export COACH_PC_TOKEN=                            # 若服务端开启了 Bearer 认证
```

### 5.4 连接外部 seekdb / OceanBase（MySQL 协议）

```bash
export SEEKDB_URI=127.0.0.1:2881
export SEEKDB_USER=root
export SEEKDB_PASSWORD='******'
export SEEKDB_DB=recall_coach
```

### 5.5 用真实 embedding 模型（可选）

默认使用内置的确定性 hash 向量（1024 维，零依赖、可离线复现）。接入任意 OpenAI 兼容的 `/embeddings` 接口：

```bash
export COACH_EMBEDDING_MODEL=text-embedding-3-small
export COACH_EMBEDDING_API_KEY=sk-****
export COACH_EMBEDDING_DIM=1536
export COACH_EMBEDDING_BASE_URL=https://api.openai.com/v1   # 可省略
```

切换 embedding 维度后需要清空 `COACH_DATA_DIR`（向量列维度在建表时固定）。

## 6. 验证样例

### 6.1 自动脚本（推荐，可复现）

服务启动后，另开终端：

```bash
cd qoder
.venv/bin/python scripts/smoke.py --base-url http://127.0.0.1:8848
```

更完整的分步核对（初始数据、输入、预期结果、证据记录）见 [`docs/verification-sample.md`](docs/verification-sample.md)。

脚本只用标准库，依次执行并打印可核对结果：

1. 读取 `/api/status`，打印后端与记忆状态；
2. `POST /api/seed` 载入 9 段示例资料（4 门课程）；
3. `POST /api/search` 验证混合检索命中；
4. `POST /api/session` 建一轮 3 张卡的复习；
5. `POST /api/session/{id}/grade` 作答并检查掌握度、相关原文回显、记忆写入；
6. `/api/memory` 读取记忆快照，写入一条讲解偏好；
7. `/api/evidence` 核对 seekdb 执行记录与 PowerContext 读写记录。

在 Windows 镜像模式（无 seekdb 运行时）下，同一脚本仍为 `11/11` 通过（`backend=sqlite-mirror`、`memory=embedded`）；只有未安装 `powercontext` 时，记忆相关 3 步才显示 `[SKIP]`，其余 7 步全部 `[PASS]`。Linux 主通道为 `seekdb/embedded` + `memory online`。

### 6.2 手工路径（界面）

1. 首页点「载入示例」，确认「资料库」出现 4 门课程、9 段资料；
2. 「开始复习」选择「操作系统」，3 张卡；
3. 顶部出现「PowerContext 记得的你」（嵌入/外部 PowerContext 下为真实上下文；未安装时为离线提示）；
4. 第一张卡故意选错 → 出现正确答案、掌握度、seekdb 相关原文；
5. 右栏「学习记忆」出现一条 weakness 记录，可点「修正」（失效旧条目并写新内容）或「失效」；
6. 展开「执行记录」，可逐条看到 `l2_distance ... APPROXIMATE` 与 `MATCH ... AGAINST` 的 SQL，以及 `POST /v1/memory/remember` 的请求与响应；
7. 再开一轮，顶部上下文应出现上一轮的薄弱点，且该知识点优先出现。

### 6.3 期望的验证输出（主通道，实测）

```
=== 后端：seekdb / embedded｜embedding builtin-hash-1024｜记忆 在线
[PASS] status 返回后端信息
[PASS] 载入样例资料 | materials=9 reviews=1
[PASS] seekdb 混合检索有结果 | hit0=进程与线程
[PASS] 创建复习 session | cards=3
--- PowerContext 上下文（583 字节）
    ...BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1 ...已注入 1 条薄弱点...
[PASS] 评分并更新掌握度 | verdict=again answer=异常 mastery=0.0
[PASS] seekdb 相关原文回显 | related=2
--- 记忆写入：《操作系统》虚拟内存与页面置换：概念「异常」还没记牢（复习第 1 次仍然答错）。
[PASS] PowerContext 写入学习记忆
[PASS] 记忆快照可读 | active=2 preference=False
[PASS] 保存讲解偏好 | 讲解偏好：先给反例，再给结论
[PASS] seekdb 执行记录可核对 | rows=10
[PASS] PowerContext 读写记录可核对 | rows=10

=== 11/11 步通过
```

镜像模式下同一脚本的输出（Windows，实测，本机已装 `powercontext`）：

```
=== 后端：sqlite-mirror / sqlite｜embedding builtin-hash-1024｜记忆 在线
[PASS] ...（前进 11 步全部通过，后端徽章显示「SQLite 镜像」）
=== 11/11 步通过
```

如果刻意不安装 `powercontext`，则记忆相关 3 步显示 `[SKIP]`，结果为 `7/7 步通过，3 步跳过（仅记忆面板）`。

真实输出会随作答内容变化；行数、上下文字节数以本机实际为准。空库首次运行时 `context/prepare` 还没有记忆可拼，上下文字节数为 0、`active=1`（脚本写入的第一条），第二轮起上下文就会带上上一轮的薄弱点；上面是本机第二次运行的输出（已有上一轮复习，`active=2`、上下文 583 字节）。

### 6.4 界面截图与演示录屏（WSL seekdb 主通道实测）

| 截图 | 说明 |
| --- | --- |
| [`docs/screenshots/01-home.png`](docs/screenshots/01-home.png) | 首页：3 门课程 9 段资料、复习入口、PowerContext 记忆面板与 seekdb 执行记录 |
| [`docs/screenshots/02-session.png`](docs/screenshots/02-session.png) | 开始一轮复习：卡片由混合检索召回，顶部显示本轮 PowerContext 上下文 |
| [`docs/screenshots/03-graded.png`](docs/screenshots/03-graded.png) | 作答后：所选错误项、正确答案、掌握度、seekdb 相关原文回显、已写入 PowerContext 的薄弱点文本 |
| [`docs/screenshots/04-memory.png`](docs/screenshots/04-memory.png) | 记忆面板与「PowerContext 读写」：条目可「修正」「失效」，右侧是真实请求/响应记录 |

[`site/assets/demo.mp4`](site/assets/demo.mp4)（8 秒，另有 [`demo.gif`](site/assets/demo.gif)）是同一套页面上的完整操作录屏：载入首页 → 开始一轮复习 → 答错并看到薄弱点写入 → 切到 PowerContext 读写记录。截图与录屏都由 CDP 驱动无头 Edge 在 1500×1150 视口下抓取，页面与数据来自同一套 `app/` 代码。


### 6.5 离线单元测试（不依赖 seekdb / 网络 / PowerContext）

核心逻辑的单元测试只使用 SQLite 镜像后端与 fake memory，任何机器上都能跑：

```bash
pip install -r qoder/requirements-dev.txt
cd qoder
pytest -q tests
```

覆盖范围：分词/切段、内置 hash 向量、mirror 后端的写入与聚合、向量+全文 RRF 融合去重、出题（填空回溯、选项、无答案回退、公开卡片不泄漏答案）、评分掌握度、薄弱点记忆去重、PowerContext 离线降级、偏好替换与修正。本机 Windows 与 WSL 均为 `30 passed`；受限 Windows 若报 `No usable temporary directory found`，先把 `TEMP`/`TMP` 指向可写目录再运行。

## 7. 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `COACH_HOST` / `COACH_PORT` | `127.0.0.1` / `8848` | HTTP 监听地址 |
| `COACH_DATA_DIR` | `qoder/data` | 运行数据根目录（PowerContext 工作区、镜像库） |
| `COACH_BACKEND` | `auto` | `auto` / `seekdb` / `mirror`；`seekdb` 时不可静默降级 |
| `SEEKDB_PATH` | 空 | 嵌入式 seekdb 数据目录；设置后 `auto` 优先走 seekdb |
| `SEEKDB_URI` | 空 | 远程 seekdb/OB 地址（MySQL 协议），优先于嵌入式 |
| `SEEKDB_USER` / `SEEKDB_PASSWORD` / `SEEKDB_DB` | `root` / 空 / `recall_coach` | 远程连接参数 |
| `COACH_PC_MODE` | `embedded` | `embedded`（进程内自带 Server）/ `external` |
| `COACH_PC_URL` | 空 | `external` 模式的 PowerContext 地址 |
| `COACH_PC_TOKEN` | 空 | Bearer 认证 token |
| `COACH_PC_WORKSPACE` | `$COACH_DATA_DIR/powercontext` | 嵌入式 Server 的工作区与 SQLite 文件 |
| `COACH_EMBEDDING_MODEL` / `COACH_EMBEDDING_API_KEY` / `COACH_EMBEDDING_DIM` | 空 / 空 / `1024` | 真实 embedding；不配置时用内置 hash 向量 |
| `COACH_EMBEDDING_BASE_URL` | 空 | OpenAI 兼容接口地址 |

## 8. HTTP API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/status` | 后端、embedding、记忆状态与统计 |
| GET | `/api/materials` | 资料列表 + 课程聚合 |
| POST | `/api/materials` | 导入一段资料（自动切段） |
| POST | `/api/materials/upload` | 上传 `.txt/.md/.json` |
| POST | `/api/seed` | 载入内置示例资料 |
| POST | `/api/search` | 混合检索（返回两通道名次与融合分） |
| POST | `/api/session` | 开始一轮复习（含 PowerContext 上下文） |
| POST | `/api/session/{id}/grade` | 作答评分、写记忆、回显相关原文 |
| GET | `/api/memory` | 记忆快照（含偏好与上下文预览） |
| POST | `/api/memory/preference` | 保存讲解偏好 |
| POST | `/api/memory/revise` | 修正一条记忆（失效旧条目 + 写入新条目） |
| POST | `/api/memory/retire` | 失效一条记忆（lifecycle `state=forgotten`） |
| GET | `/api/evidence` | seekdb SQL 与 PowerContext HTTP 记录 |

## 9. 目录结构

```
qoder/
├── app/
│   ├── main.py            # FastAPI 路由与静态资源
│   ├── coach.py           # 复习会话、出题、评分、记忆编排
│   ├── pc_memory.py       # PowerContext 客户端 + 嵌入式 Server
│   ├── config.py          # 环境变量配置
│   ├── text.py            # 分词、切段、关键词
│   ├── embedding.py       # 内置 hash 向量 / OpenAI 兼容 embedding
│   ├── samples.py         # 9 段示例资料
│   └── store/
│       ├── base.py        # MaterialStore 契约、RRF、证据日志
│       ├── seekdb.py      # seekdb 后端：向量 HNSW + 全文 FTS
│       └── mirror.py      # SQLite 镜像后端（无 seekdb 时）
├── tests/                 # 离线单元测试（pytest，不依赖 seekdb/网络）
├── web/                   # index.html / style.css / app.js（无构建）
├── scripts/
│   ├── smoke.py           # 验证样例（stdlib）
│   ├── start.ps1          # Windows 启动
│   └── start.sh           # Linux/macOS 启动
├── docs/
│   ├── qoder-development-record.md   # Qoder 开发记录（提交材料）
│   ├── verification-sample.md        # 可复现的验证样例
│   └── website.md                    # Qoder Sites 文案
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

## 10. 数据与隐私

- 所有数据默认落在本机 `COACH_DATA_DIR`，不访问第三方服务；
- 未配置任何 API Key 也能完整运行检索与记忆闭环；
- `.env` 只放占位值，真实密钥通过环境变量注入，未提交到仓库。

## 11. 已知边界

- **Windows 没有 seekdb 运行时**：`pylibseekdb` 只提供 Linux/macOS wheel，因此 Windows 上走 SQLite 镜像后端；功能与界面一致，但 SQL 会标注为镜像实现。
- **PowerContext 需要 Python 服务端**：Windows 上 `pip install 'powercontext[server,client]' pymysql` 后可复用嵌入式 Server（本机实测 11/11）；未安装时记忆离线，属于设计内的降级。
- **内置 hash 向量是演示级语义近似**：它依赖字面重合，适合零配置复现；要获得真实语义检索，按 5.5 配置 embedding 模型。
- 单机演示，无用户体系；`scope` 按学习者一人一份，保存在 `COACH_DATA_DIR/scope.json`，重启后复用同一 scope。

## 12. 评审复现对照

| 评审要求 | 在哪里 |
| --- | --- |
| 项目网站 | https://recall-coach-uatkigmv89n.qoder.website （Qoder Sites，公开访问；文案见 `docs/website.md`） |
| 代码仓库 | 本仓库 `qoder/` 目录 |
| 运行说明 | 本文第 5、6、7 节 |
| 验证样例 | `scripts/smoke.py` + 本文 6.2 手工路径 |
| seekdb 实际作用 | `app/store/seekdb.py` + `/api/evidence` 的 SQL 记录 |
| PowerContext 实际作用 | `app/pc_memory.py` + 记忆面板 + `/api/evidence` 的 HTTP 记录 |
| Qoder 开发记录 | `docs/qoder-development-record.md` |

## 13. 复用与致谢

- 嵌入式 PowerContext Server 的启动方式参考仓库内 `examples/jupyter/_tutorial.py`；
- seekdb 混合检索 SQL 与索引写法参考 `src/powercontext/builtin/persistence/seekdb/`；
- 示例资料由本项目撰写，仅用于演示，不包含受版权保护的原文。
