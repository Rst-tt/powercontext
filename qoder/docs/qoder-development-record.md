# Qoder 开发记录

> 本记录只收录 Qoder 中**真实发生**的对话截图。共 7 段：站点发布与修复 3 段（图 1–3），核心功能联调 3 段（图 4–7）。
> 截图原图在 `docs/screenshots/qoder/`，按对话发生时间排序；每条都对应 `qoder/` 目录里的真实文件与提交。

## 0. 账号与仓库信息

| 项 | 值 |
| --- | --- |
| Qoder 账号 UID | `01a185b5-724a-70a3-80d7-37e52ddb31a1` |
| Qoder Sites 站点地址 | https://recall-coach-uatkigmv89n.qoder.website （已设为「任何人都可访问」，未登录可打开） |
| 代码仓库地址 | https://github.com/Rst-tt/powercontext/tree/2026-competition-ecosystem |
| 本次开发使用的分支 | `2026-competition-ecosystem`（赛题要求的基线分支） |
| 主要开发目录 | `qoder/` |
| 开发起止时间 | 2026-10-27 至 2026-10-29 |

## 1. 会话总览

| # | 时间（2026-10-29） | 场景 | 截图 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | 11:36 | 让 Sites 用 `site/index.html` 生成静态站点草稿 | `qoder-sites-01-preview.png` | 草稿生成成功，未发布 |
| 2 | 11:46 | 修「代码仓库」按钮卡死 | `qoder-sites-02-link-fix.png` | 外链加 `target="_blank" rel="noopener noreferrer"` |
| 3 | 11:46 | 定位按钮“卡住”的根因 | `qoder-sites-03-root-cause.png` | 链接本身有效；本机 `github.com` TCP 超时所致 |
| 4 | 12:05 | 走查找真实缺陷：挖空卡把答案泄漏在题干 | `qoder-core-01-card-leak-fix.png` | 修 `app/coach.py`，补测试；`pytest -q` 30 passed |
| 5 | 12:07 | 核对检索证据链中的 SQL | `qoder-core-02-retrieval-sql.png` | 向量/全文通道 SQL 均在位，无需改代码 |
| 6 | 12:12 | 修 375px 横向溢出 | `qoder-core-03-mobile-overflow.png` | `site/index.html` 表格加 `.table-wrap`，溢出 164px→0 |
| 7 | 12:13 | 前端失败提示中文化 | `qoder-core-04-fetch-errors.png` | `web/app.js` 网络失败/HTTP 错误改为可读中文 |

## 2. 站点发布（Qoder Sites）

发布方式：

1. 在 Qoder 中打开 Sites，选择 `qoder/site/` 作为静态站点根（`index.html` + `assets/`，全部相对路径）；
2. 可见范围设为「任何人都可访问」；
3. 发布后用无痕窗口打开 `https://recall-coach-uatkigmv89n.qoder.website` 确认；
4. 对话截图见上表 1–3（图 1 生成草稿、图 2 修外链、图 3 根因说明）。

> 提交前注意：§3-C 又改了 `site/index.html`，Sites 里的旧草稿已过期，
> 需要在 Sites 中重新生成草稿并点一次「发布」，让线上站点带上移动端修复。

## 3. 核心功能联调

### A. 挖空卡把答案泄漏在题干（图 4）

- **现象**：真实样例里 2/9 张填空卡把答案泄漏在题干里（如「进程」在原文出现 6 次，只挖空第一处，其余仍可见）。
- **Qoder 定位**：`app/coach.py:433` 的 `_answer_candidates` 只把「只出现一次」的词排到前面，重复词仍是候选；
  `app/coach.py:458` 的 `_build_card` 无条件 `body.replace(answer, "____", 1)`，只挖掉第一处。
- **修复**：只有恰好出现一次的候选词才允许挖空，否则走已有的「复述」回退分支：
  `words = [word for word in keywords if cls._term_like(word, body) and body.count(word) == 1]`。
- **回归测试**：`tests/test_coach.py::test_only_unique_terms_are_blanked`（重复词不挖空且回退标题；唯一词挖空且题干不含答案）。
- **验证**：`pytest -q` → `30 passed`。

### B. 检索证据链的 SQL 核对（图 5）

- 8849 端口当时没有服务在监听，因此**没有**跑 `scripts/smoke.py`（如实记录，不写成已跑）。
- 改为静态核对 `app/store/seekdb.py`：向量通道 `ORDER BY l2_distance(...) APPROXIMATE`（`seekdb.py:557`）、
  全文通道 `MATCH(search_text) AGAINST(:query_text)`（`seekdb.py:619`、`seekdb.py:620`）均在位，无需修改。
- 补充说明：Windows 上 `pylibseekdb` 无 wheel，`/api/evidence` 显示的是 mirror 后端的
  `python: cosine_similarity …` / `python: BM25 …`，属预期降级，不是 seekdb 代码缺陷。

### C. 静态站点与前端质量（图 6–7）

- **375px 横向溢出 164px**：唯一溢出源是「技术分工」表格（最小内容宽 500px，来自不可断行 token）。
  修复：`site/index.html:107` 新增 `.table-wrap { overflow-x: auto; }`，`site/index.html:226` 给表格包一层 `div.table-wrap`（纯容器，不改样式规则和文字）。
  复测：320px / 375px 溢出归零，768px / 1200px 桌面布局不变。
- **失败提示中文化**：`web/app.js:46` 把 `fetch` 网络异常转成「网络连接失败，请确认后端服务已启动」；
  `web/app.js:62` 兜底文案改为「请求失败（HTTP n）」。所有 toast / 面板提示都走这两处。

## 4. 调试对照表（工程侧修复，供交叉核对）

| # | 现象 | 修复 | 验证方式 |
| --- | --- | --- | --- |
| 1 | seekdb 启动报 `Unknown database` | 先连默认 `test` 库再 `CREATE DATABASE IF NOT EXISTS` | `/api/status` 显示 `seekdb/embedded` |
| 2 | 全文索引不存在导致检索降级 | 读取 `SHOW CREATE TABLE` 复用已有索引 parser，并按支持的 parser 依次重试 | `/api/status` 的 `fts_parser` 为 `ngram`，执行记录出现词法通道 |
| 3 | 两通道命中同一资料却重复展示 | RRF 融合改为按资料 id 归一 | `/api/search` 每条结果同时带 `vector` 与 `lexical` 名次 |
| 4 | `AVG(CAST(...) AS DOUBLE)` 别名丢失 | 先子查询再聚合 | `/api/status` 的课程统计正常 |
| 5 | Windows 无 seekdb / PowerContext | 引入 mirror 后端与记忆离线降级 | `[SKIP]` 与 `[PASS]` 判定符合预期 |

## 5. 可选补录的会话脚本（本次未在 Qoder 中单独执行）

核心功能的真实开发证据来自 §3 A/B/C；下面三段是更早的设计对话，**没有**在 Qoder 中单独跑过，
仅作为如需补录时的可复用 prompt，评审请勿按“已执行”理解：

> 1) 我要做「知新 / Recall Coach」：资料存 seekdb 做向量+全文混合检索，学习者薄弱点/偏好/进度存 PowerContext 并在下一轮复习复用。给一份 MVP 拆解：模块、接口、数据表、首次演示路径，不要额外重型依赖。
>
> 2) 用 pyobvector 嵌入式模式接 seekdb：建 `coach_materials` 表（`VECTOR(1024)` + 全文列 + HNSW 索引），检索同时走向量通道和全文通道，再用 RRF 融合，并把每条 SQL 记录下来给界面展示。
>
> 3) 用 PowerContext HTTP API 管学习记忆：`memory/remember` 写薄弱点/偏好/进度，`memory/search` 召回，`context/prepare` 组装下一轮复习上下文；修正/失效用 1.4.x 的 `atomic-memory/lifecycle`（`state=forgotten`）失效旧条目再写新条目。

## 6. 提交清单勾选

- [x] 项目网站 URL（公开可访问）
- [x] 代码仓库地址（公开或评审可访问）
- [x] 运行说明（`README.md` 第 5 节）
- [x] 验证样例（`scripts/smoke.py` + README 第 6.3 节实测记录）
- [x] Qoder 账号 UID 与本文档中的截图
- [x] 站点发布对话截图 3 张（`docs/screenshots/qoder/qoder-sites-0*.png`）
- [x] 核心功能联调截图 4 张（`docs/screenshots/qoder/qoder-core-0*.png`）
- [ ] 使用效果材料（如有）
