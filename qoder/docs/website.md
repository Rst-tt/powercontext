# 知新 / Recall Coach — Qoder Sites 站点文案

> 把下面每个 `##` 小节作为站点的一个区块发布。占位符已替换为线上地址（2026-10-29 发布）。
> 站点可见范围务必设为「任何人都可访问」。

## 首屏

# 知新 / Recall Coach

**一个会用「记忆」复习你的学习教练。**
资料检索交给 seekdb，学习者画像与复习上下文交给 PowerContext——每一轮复习都带着上一轮的记忆开始。

[立即体验](https://recall-coach-uatkigmv89n.qoder.website) · [代码仓库](https://github.com/Rst-tt/powercontext/tree/2026-competition-ecosystem) · [3 分钟演示视频](https://recall-coach-uatkigmv89n.qoder.website/assets/demo.mp4)

## 解决的问题

复习的痛点不在「没有资料」，而在资料与学习者之间缺少一条连续的线：

- 笔记、文献、错题散落在不同文件夹，复习时找不到相关原文；
- 每次打开学习工具都像第一次见你：不知道你哪一章薄弱、上次为什么错；
- 复习完就结束，没有东西沉淀到下一轮，同一个知识点反复「重新认识」。

知新把「导入资料 → 带记忆开始 → 出题作答 → 写回记忆 → 下一轮更懂你」做成一个闭环。

## 核心功能

1. **资料导入与切段**：粘贴或上传 `.txt/.md/.json`，自动按课程切分，写入 seekdb（`VECTOR(1024)` + 全文列 + HNSW 索引）。
2. **混合检索**：向量通道 `ORDER BY l2_distance(...) APPROXIMATE` 与全文通道 `MATCH(...) AGAINST(...)` 各取候选，用 RRF 融合；界面逐条展示 SQL、参数、耗时。
3. **带记忆开始**：开始复习前先向 PowerContext `context/prepare` 取上下文——薄弱点、偏好、进度——再用它影响检索与提示。
4. **出题与评分**：从召回原文生成选择题，作答后给出正确答案、相关原文、掌握度变化；连续答错的卡片在之后几轮优先出现。
5. **记忆沉淀**：答错/「有点难」写入 weakness/misconception，偏好写入 preference，里程碑写入 progress；「修正」会先失效旧条目再写新内容，「失效」把条目标记为 `forgotten`，历史仍可审计。
6. **可核对的执行记录**：右栏「执行记录」分 seekdb（SQL/行数/耗时）与 PowerContext（HTTP/请求/响应）两栏，评审可直接对照。

## 技术分工

| 技术 | 分工 | 可核对证据 |
| --- | --- | --- |
| seekdb | 唯一资料存储 + 向量/全文混合检索；掌握度与复习次数回写 | `/api/evidence` 的 `[seekdb]` 记录、界面 SQL 面板 |
| PowerContext | 学习者记忆：`memory/remember`、`memory/search`、`context/prepare`、`entries/list`；修正/失效走 `atomic-memory/lifecycle`（1.4.x） | `/api/evidence` 的 `[powercontext]` 记录、记忆面板 |

两者不互相替代：检索结果只有在作答之后才可能变成记忆，而记忆只影响下一轮检索与提示。

工程质量：核心逻辑带 30 个离线单元测试（pytest），不依赖 seekdb / 网络 / PowerContext 即可回归，Windows 与 Linux 实测均 `30 passed`。

## 演示

- **截图**：`docs/screenshots/01-home.png`（首页与资料库）、`docs/screenshots/02-session.png`（复习卡与上下文）、`docs/screenshots/03-graded.png`（作答与证据）、`docs/screenshots/04-memory.png`（记忆面板与读写记录）。仓库内 `qoder/docs/screenshots/` 已附 4 张 1500×1150 实测截图（首页 / 复习 / 作答后 / 记忆面板），可直接上传。
- **录屏**：`https://recall-coach-uatkigmv89n.qoder.website/assets/demo.mp4`（8 秒：首页 → 开始复习 → 答错写入记忆 → 切换 PowerContext 读写记录）。
- **本地体验**：见仓库 `README.md` 第 5 节；一条命令启动后在浏览器打开 `http://127.0.0.1:8848`。

## 使用效果

> 填写真实使用数据，附来源（访问统计截图、使用日志、问卷导出）。没有数据时可删除本节。

- 使用人数：`{{USERS}}`；累计复习轮次：`{{ROUNDS}}`；持续使用：`{{DAYS}}`
- 提效对比：整理一轮课程复习从 `{{BEFORE}}` 缩短到 `{{AFTER}}`
- 用户反馈：`{{FEEDBACK}}`（已隐去个人信息）

## 开源与复用

- 代码仓库：https://github.com/Rst-tt/powercontext/tree/2026-competition-ecosystem，主目录 `qoder/`
- 嵌入式 PowerContext Server 启动方式参考该仓库 `examples/jupyter/_tutorial.py`
- seekdb 混合检索写法参考 `src/powercontext/builtin/persistence/seekdb/`
- 示例资料为本项目撰写，仅用于演示
