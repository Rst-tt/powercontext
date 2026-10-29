# 验证样例（可按步骤复现）

> 目标：让评审在 10 分钟内确认三件事——项目能跑、seekdb 在检索里真实生效、PowerContext 的记忆真的跨轮复用。
> 所有命令都在 `qoder/` 目录下执行；主通道以 Linux/WSL 为准，Windows 见第 5 节。

## 0. 前置

| 项 | 要求 |
| --- | --- |
| Python | 3.11+（Ubuntu 22.04 的 3.12 实测可用；系统 SQLite 低于 3.38 时按 `requirements.txt` 装 `pysqlite3-binary`） |
| 依赖 | `pip install -r requirements.txt`（会装 `powercontext[server,client]`、`pyobvector[pyseekdb]`、`pymysql`） |
| 网络 | 无需外网；embedding 默认是内置 hash 向量 |
| 初始数据 | 仓库内置 9 段示例资料（4 门课程，写在 `app/samples.py`），首次「载入示例」时写入 seekdb |

## 1. 启动（主通道：seekdb 嵌入式 + PowerContext 嵌入式）

```bash
cd qoder
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export SEEKDB_PATH="$HOME/.recall-coach/seekdb"
export COACH_DATA_DIR="$HOME/.recall-coach"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8848
```

首次启动约 40–60 秒：pylibseekdb 拉起嵌入式引擎、PowerContext 建库并迁移。看到 `GET /api/status` 可访问即就绪。

**预期（输入 `/api/status` 的输出）**：

```json
{"backend":{"backend":"seekdb","mode":"embedded","dim":1024,"fts_parser":"ngram",
            "tables":["coach_materials","coach_reviews"]},
 "memory":{"available":true,"mode":"embedded","sqlite_module":"pysqlite3"},
 "stats":{"backend":"seekdb","materials":0,...}}
```

- `backend.backend == "seekdb"`：检索底座确实是 seekdb；
- `memory.available == true`：PowerContext 在进程内起来了；
- `fts_parser == "ngram"`：全文索引已按 ngram 建好（中文可检索）。

## 2. 一键验证脚本（推荐）

```bash
.venv/bin/python scripts/smoke.py --base-url http://127.0.0.1:8848
```

脚本只用标准库，覆盖 11 个检查点，全部 `[PASS]` 即满足验收（空库首跑上下文为 0 字节，第二轮起才有记忆可拼）：

```
=== 后端：seekdb / embedded｜embedding builtin-hash-1024｜记忆 在线
[PASS] status 返回后端信息
[PASS] 载入样例资料 | materials=9 reviews≥10
[PASS] seekdb 混合检索有结果 | hit0=进程与线程
[PASS] 创建复习 session | cards=3
[PASS] 评分并更新掌握度 | verdict=again answer=不可剥夺 mastery=0.0
[PASS] seekdb 相关原文回显 | related=2
--- 记忆写入：《操作系统》死锁的四个必要条件：概念「不可剥夺」还没记牢（复习第 6 次仍然答错）。
[PASS] PowerContext 写入学习记忆
[PASS] 记忆快照可读 | active=5 preference=True
[PASS] 保存讲解偏好 | 讲解偏好：先给反例，再给结论
[PASS] seekdb 执行记录可核对 | rows=10
[PASS] PowerContext 读写记录可核对 | rows=10
=== 11/11 步通过
```

`active=5` 是「4 条薄弱点 + 1 条偏好」；同一条薄弱点重复答错时先**失效旧条目**再写新条目，所以计数不会随复习次数增长。

在**全新空数据目录**（`COACH_DATA_DIR` 指向空文件夹、端口 8851）冷启动后跑同一脚本，结果同样是 `11/11 步通过`：首轮 `context/prepare` 为 0 字节、记忆 `active=1`（脚本刚写入的第一条），从第二轮起上下文才会带上上一轮的薄弱点。

## 2.1 离线单元测试（补充证据）

不依赖 seekdb、网络和 PowerContext，任何机器可跑，用于核对核心逻辑没被回归：

```bash
pip install -r requirements-dev.txt
pytest -q tests
```

本机实测：Windows 与 WSL 各 `30 passed`（分词/切段、hash 向量、mirror 写入与聚合、RRF 去重、出题与评分、薄弱点去重、离线降级、偏好替换/修正）。

## 3. 手动核对 seekdb（检索结果如何进入流程）

1. `POST /api/search`，体 `{"query":"死锁的四个必要条件","top_k":3}`；
2. 返回的每条 `hit` 都带 `vector`（l2 距离、名次）与 `lexical`（全文得分、名次），`fused` 是 RRF 融合分；
3. 打开 `/api/evidence?channel=vector` 与 `/api/evidence?channel=lexical`，可以看到真实执行的 SQL：
   - 向量：`ORDER BY l2_distance(embedding, :query_vector) APPROXIMATE`
   - 全文：`MATCH(search_text) AGAINST(:query_text)`
4. 在复习里答错后，`coach_materials` 的 `mastery`、`reviews`、`last_verdict` 被回写；`/api/evidence?channel=read` 可看到统计 SQL。

**预期**：同一段资料只出现一次，并且同时带两通道名次（说明真的走了混合检索，而不是只查一路）。

## 4. 手动核对 PowerContext（记忆如何跨轮复用）

1. 第一次复习：对一张卡选「还是不会」；
   - `/api/evidence?channel=powercontext` 里出现 `POST /v1/memory/remember`，请求体含 `kind=weakness`；
2. 第二次复习（同一课程）：`/api/session` 响应里的 `context` 字段非空，且包含上一轮的薄弱点文本；
   - 对应的 `POST /v1/context/prepare` 请求/响应同样在证据栏；
3. 在右侧「学习记忆」点「修正」：先对旧条目调用 `POST /v1/atomic-memory/lifecycle`（`state=forgotten`），
   再 `POST /v1/memory/remember` 写入修正后的文字；点「失效」只发 lifecycle（`state=forgotten`）；
   - 两条请求都出现在证据栏，`/api/memory` 重新拉取快照后旧条目从面板消失，历史仍可在 PowerContext 侧审计；
4. 保存一次讲解偏好：新偏好会先让旧的 preference 失效再写入，因此面板里始终只有一条生效偏好；
5. 再开一次会话，`context/prepare` 返回的偏好条目应包含刚保存的那条。

**预期**：`/api/memory` 的 `entries` 中能看到 `weakness/preference/progress`，每条都带
`artifact`（`artifact_id` + `revision`）与 `state_version`，可追溯；同一条笔记的薄弱点始终只有一条 `active` 记录。

## 5. Windows（无 seekdb 运行时的降级路径）

```powershell
cd qoder
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8848
```

- `backend.backend == "sqlite-mirror"`：SQLite + 纯 Python 余弦 + 字符 n-gram 词法分，契约与 seekdb 后端一致；
- `powercontext[server,client]` 在 Windows 上同样可用，`memory.available == true`，完整 11 步也能通过；只有主动不装 `powercontext` 时才降级为「记忆离线」（`7/7 步通过，3 步跳过`）。

## 6. 常见问题

| 现象 | 处理 |
| --- | --- |
| 启动报 SQLite 版本过低 | `pip install pysqlite3-binary`（`pc_memory.py` 会自动切换；`/api/status` 的 `sqlite_module` 显示 `pysqlite3`） |
| `Unknown database 'recall_coach'` | 确认 `SEEKDB_DB` 与 `SEEKDB_PATH` 匹配；首次启动会自动建库 |
| 检索只有向量通道 | 看 `/api/status` 的 `fts_parser`；若为空说明全文索引未建成功，检查 seekdb 日志 |
| 端口占用 | `COACH_PORT=8849` 换端口，或用 `--port` 参数启动脚本 |
| `pytest` 启动报 `No usable temporary directory found` | Windows 受限环境把 `TEMP`/`TMP` 指向可写目录后再跑，例如 `set TEMP=%CD%\.scratch\tmp` |
