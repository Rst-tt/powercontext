# Qoder 开发记录（提交材料模板）

> 本文档是提交给评审的「Qoder 开发记录」。评审关注的是「项目主要开发工作确实由 Qoder 参与完成」，
> 所以每一节都需要**你在 Qoder 中的真实对话截图/录屏**与**账号 UID**，并让每条记录能对应到 `qoder/` 目录里的真实文件与提交。
>
> 当前状态：**第 5 节（站点发布）已有 3 张真实 Qoder 对话截图**（`docs/screenshots/qoder/`）；
> 第 1–4 节是核心功能的会话脚本（可直接粘贴到 Qoder 里执行），**执行后请把对应截图补进各节**，
> 否则开发记录只覆盖“发布站点”，不足以证明核心功能由 Qoder 完成。

## 0. 账号与仓库信息

| 项 | 值 |
| --- | --- |
| Qoder 账号 UID | `01a185b5-724a-70a3-80d7-37e52ddb31a1` |
| Qoder Sites 站点地址 | https://recall-coach-uatkigmv89n.qoder.website （已设为「任何人都可访问」，未登录可打开） |
| 代码仓库地址 | https://github.com/Rst-tt/powercontext/tree/2026-competition-ecosystem |
| 本次开发使用的分支 | `2026-competition-ecosystem`（赛题要求的基线分支） |
| 主要开发目录 | `qoder/` |
| 开发起止时间 | 2026-10-27 至 2026-10-29 |

## 1. 需求拆解（会话脚本 1，截图待补）

**在 Qoder 中输入的起始需求（可直接复用）：**

> 我要参加 Qoder × OceanBase 比赛，做「知新 / Recall Coach」：个人学习复习教练。
> 资料存 seekdb 做向量+全文混合检索，学习者薄弱点/偏好/进度存 PowerContext 并在下一轮复习复用。
> 先给我一份 MVP 拆解：模块、接口、数据表、首次能跑通的演示路径；不要用额外的重型依赖。

**请附：** 该轮对话截图（含 Qoder 给出的拆解结论）与随后创建的关键文件。

**落地产物对照：**

| Qoder 输出 | 对应文件 |
| --- | --- |
| 模块划分（config / text / embedding / store / coach / main） | `qoder/app/*.py`、`qoder/app/store/*.py` |
| 接口清单 | `README.md` 第 8 节 |
| 演示路径 | `README.md` 第 6.2 节 |

## 2. 检索底座：seekdb（会话脚本 2，截图待补）

**在 Qoder 中输入的后续需求：**

> 用 pyobvector 的嵌入式模式接 seekdb：建 `coach_materials` 表（`VECTOR(1024)` + 全文列 + HNSW 索引），
> 检索要同时走向量通道和全文通道，再用 RRF 融合，并把每条 SQL 记录下来给界面展示。

**落地产物：**

- `qoder/app/store/seekdb.py`：表结构、`l2_distance ... APPROXIMATE` 向量通道、`MATCH ... AGAINST` 全文通道、RRF 融合、证据日志；
- `qoder/app/store/base.py`：`MaterialStore` 契约与 `fuse_rrf`；
- `qoder/app/store/mirror.py`：无 seekdb 运行时的同契约镜像实现。

**请附：** Qoder 中生成/修改上述文件的对话截图，以及一次「检索无结果 → 修正 SQL → 检索命中」的调试录屏。

## 3. 记忆层：PowerContext（会话脚本 3，截图待补）

**在 Qoder 中输入的后续需求：**

> 用 PowerContext 的 HTTP API 管学习记忆：`memory/remember` 写薄弱点/偏好/进度，`memory/search` 召回，
> `context/prepare` 组装下一轮复习上下文，`memory/entries/list` 渲染面板；
> 修正/失效用 1.4.x 的 `atomic-memory/lifecycle`（`state=forgotten`）来失效旧条目、再写新条目。
> 同时支持嵌入式（进程内起 Server）和外部 URL 两种模式。

**落地产物：**

- `qoder/app/pc_memory.py`：`LearnerMemory` 客户端、`ensure_modern_sqlite()`、scope 管理、嵌入式 Server 启动；
- `qoder/app/coach.py`：写记忆、召回、`context/prepare` 的编排；
- `qoder/web/app.js`：记忆面板与执行记录。

**请附：** Qoder 中生成/修改上述文件的对话截图，以及一次「记忆写入 → 第二轮复习上下文出现该薄弱点」的录屏。

## 4. 调试记录（会话脚本 4，截图待补）

按时间顺序摘录 Qoder 中真实发生的调试对话，每条都写清「现象 → Qoder 定位 → 改动 → 验证」：

| # | 现象 | Qoder 的定位/改动 | 验证方式 |
| --- | --- | --- | --- |
| 1 | seekdb 启动报 `Unknown database` | 先连默认 `test` 库再 `CREATE DATABASE IF NOT EXISTS` | `/api/status` 显示 `seekdb/embedded` |
| 2 | 全文索引不存在导致检索降级 | 读取 `SHOW CREATE TABLE` 复用已有索引 parser，并按支持的 parser 依次重试 | `/api/status` 的 `fts_parser` 为 `ngram`，执行记录出现词法通道 |
| 3 | 两通道命中同一资料却重复展示 | RRF 融合改为按资料 id 归一 | `/api/search` 每条结果同时带 `vector` 与 `lexical` 名次 |
| 4 | `AVG(CAST(...) AS DOUBLE)` 别名丢失 | 先子查询再聚合 | `/api/status` 的课程统计正常 |
| 5 | Windows 无 seekdb / PowerContext | 引入 mirror 后端与记忆离线降级 | `[SKIP]` 与 `[PASS]` 判定符合预期 |

**请附：** 每条对应的 Qoder 对话截图；编号与上表一致，便于评审核对。

## 5. 站点发布（Qoder Sites）— 已有真实截图

以下 3 张是 Qoder 中真实发生的对话（原图见 `docs/screenshots/qoder/`）：

| 截图 | 文件 | 内容 |
| --- | --- | --- |
| 图 1 | `docs/screenshots/qoder/qoder-sites-01-preview.png` | 让 Sites 用 `site/index.html` 生成静态站点预览（4 张截图 + demo.mp4/demo.gif 作为 assets，草稿未发布） |
| 图 2 | `docs/screenshots/qoder/qoder-sites-02-link-fix.png` | 定位并修复「代码仓库」按钮卡住：外链加 `target="_blank" rel="noopener noreferrer"`，页面不被预览面板带走 |
| 图 3 | `docs/screenshots/qoder/qoder-sites-03-root-cause.png` | 根因确认：链接本身有效（`Rst-tt/powercontext`、分支 `2026-competition-ecosystem`），是本机 `github.com` TCP 超时导致“看起来卡住” |

站点操作步骤：

1. 在 Qoder 中打开 Sites，选择 `site/index.html` 作为静态站点根；
2. 可见范围设为「任何人都可访问」；
3. 发布后用无痕窗口打开 `https://recall-coach-uatkigmv89n.qoder.website` 确认；
4. 已发布版本的 3 张对话截图见上表。

## 6. 核心功能开发会话（提交前补齐，3 段）

> 现状：Qoder 里只有站点发布相关的 3 段对话。赛题基础验收要求「开发记录能够体现项目的主要功能由 Qoder 参与完成」，
> 因此建议在提交前用 Qoder 再跑下面 3 段真实会话（每段 5–10 分钟，均会修改仓库内真实文件），并各截 1–2 张图。
>
> 截图统一放 `docs/screenshots/qoder/`，命名 `qoder-core-01.png` … `qoder-core-03.png`，再更新本文档。

### 会话 A：代码走查 + 修一个真实缺陷（对应 `app/coach.py`、`tests/`）

在 Qoder 中粘贴：

> 阅读 `qoder/app/coach.py` 里 `_build_card`/`_answer_candidates` 与 `qoder/app/store/base.py` 的 `fuse_rrf`。
> 找出至少 1 个真实缺陷或薄弱点（例如答案候选过滤不完整、RRF 去重边界），给出最小修复，
> 并为它补一个 `tests/` 下的 pytest 用例；运行 `pytest -q` 确认通过后，说明改动点。

预期产出：`coach.py` 或 `base.py` 的 diff + 1 个新测试。

### 会话 B：验证脚本与证据链（对应 `scripts/smoke.py`、`/api/evidence`）

在 Qoder 中粘贴：

> 阅读 `qoder/scripts/smoke.py`。如果本机有服务在 `http://127.0.0.1:8849`，运行它并报告失败步骤；
> 若全部通过，再检查 `/api/evidence` 返回的 SQL 是否包含 `APPROXIMATE` 与 `MATCH(...) AGAINST`，
> 缺失则修复 `app/store/seekdb.py` 并重新运行 smoke，最后贴出关键输出。

预期产出：smoke 输出（11/11）或一处检索链路修复。

### 会话 C：静态站点与前端质量（对应 `site/index.html`、`web/app.js`）

在 Qoder 中粘贴：

> 检查 `qoder/site/index.html` 与 `qoder/web/app.js`：所有外链是否都带 `target="_blank"`；
> 375px 宽度下是否有横向溢出；前端 fetch 失败时提示是否为可读中文。
> 修复发现的问题，列出改动前后对比；不要改设计和文案。

预期产出：`index.html` / `app.js` 的小幅 diff。

## 7. 提交清单勾选

- [ ] 项目网站 URL（公开可访问）
- [ ] 代码仓库地址（公开或评审可访问）
- [ ] 运行说明（`README.md` 第 5 节）
- [ ] 验证样例（`scripts/smoke.py` 输出截图 + README 第 6.3 节实测记录）
- [ ] Qoder 账号 UID 与本文档中的截图/录屏
- [x] 站点发布对话截图 3 张（`docs/screenshots/qoder/qoder-sites-0*.png`）
- [ ] 核心功能开发会话截图（第 6 节 A/B/C，提交前补齐）
- [ ] 使用效果材料（如有）
