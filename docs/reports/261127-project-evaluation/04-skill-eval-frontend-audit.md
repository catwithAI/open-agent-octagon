# 04 — Skill Eval 与前端（React / TypeScript）深度审计

> 审计日期：2026-11-27 · 审计基线：`9c4cfaf` · **状态更新**：2026-11-28（PR #36、PR #37、PR #38 已合入 `main`；Phase 4 待合入）
>
> 状态图例：`✅ 已合入 (PR #36/#37/#38)` · `🟡 本 PR 待合入 (Phase 4)` · `🟡 部分完成` · `⬜ 未开始`
>
> 审计范围：`backend/skill_eval/` 全量模块与 `web/src/` 全量前端代码及测试基线。

---

## 一、具体 Bug 清单

### 后端：`backend/skill_eval/`

1. **[P0 数据错乱] 多轮对话历史回复 SQL 缺上界，前面所有轮次串成最后一轮回复** — `✅ 已合入 (PR #36: T1.4)`
   - **位置**：`backend/skill_eval/runs.py:57-69`（已补 `AND r.seq < e.seq`）。

2. **[P1 状态机损坏] 生成进行中允许 `send_message()` 强插消息** — `✅ 已合入 (PR #36: T1.4)`
   - **位置**：`backend/skill_eval/runs.py:168-170, 203-204`（`active` / 生成进行中返回 409）。

3. **[P1 稳定性] Worker 遇单次锁冲突永久死亡 + 每 200ms 同步写库风暴 + 停机误判失败** — `✅ 大部已合入 (PR #36/#37)`
   - **状态拆解**：
     - `run_forever` 异常捕获退避、删 200ms 冗余写库、清理 `self.signals`、超时 `stop` 保护：`✅ 已合入 (PR #36: T1.4)`。
     - `Repository` once-WAL 与 `busy_timeout`/`synchronous=NORMAL`：`✅ 已合入 (PR #37: T2.1)`。
     - 优雅停机不误判 `failed`（保留可恢复态）：`⬜ 待后续补充`。

4. **[P1 恢复缺陷] 崩溃恢复缺异常保护且漏更新 `generations` / `interactions` 表** — `✅ 已合入 (PR #36: T1.4)`
   - **位置**：`backend/skill_eval/recovery.py:74-99`。

5. **[P1 边界与错误处理] `display_only` 双向漏洞、缺失 blob 500 崩溃与磁盘无 GC** — `✅ 大部已合入 (PR #36/#37)`
   - **状态拆解**：
     - `previews.py` `display_only` 只读保护、缺失 blob 统一 410、防御 `Content-Length`：`✅ 已合入 (PR #36: T1.4)`。
     - `collect_unreferenced` 纳入 `previews/` 清理：`✅ 已合入 (PR #37: T2.3)`。
     - `POST comparisons/query` 只读放行与定时 GC 调度：`⬜ 待后续补充`。

### 前端：`web/src/`

6. **[P0 死循环] `RunGroupLive` 无限刷接口循环 & 未捕获 Reducer 崩溃** — `✅ 已合入 (PR #36: T1.5)`
   - **位置**：`web/src/pages/RunGroupLive.tsx`、`web/src/api/groupStream.ts`。

7. **[P1 泄漏与状态错误] Skill Eval 终态 SSE 永不关闭、过期闭包与粘滞失败 Promise** — `✅ 已合入 (PR #36/#37/#38)`
   - **状态拆解**：
     - 终态关闭 SSE（前后端）、清理 `ComparePage`/`Workspace` 失败 Promise ref：`✅ 已合入 (PR #36: T1.4 / T1.5)`。
     - `EventStore` 推送唤醒 SSE 与 `/process` LRU 投影缓存：`✅ 已合入 (PR #37: T2.3)`。
     - `ProcessView` `following` 依赖数组过期闭包与切 `"events"` 停拉 `/process`：`✅ 已合入 (PR #38: T3.3)`。

8. **[P1 泄漏/竞态/测试断裂] `RunDetail` 定时器泄漏、跨 Run 脏状态、缺失 i18n Key 与 88 个失败前端测试** — `✅ 已合入 (PR #36/#38)`
   - **状态拆解**：
     - 14 个前端测试与全量 CI 门禁、`RunDetail` `setInterval` 清理、跨 `runId` 状态重置、缺失 key `runDetail.hop.callLink`：`✅ 已合入 (PR #36: T0.1 / T1.5)`。
     - `ExperimentBuilder` 切场景竞态保护、`RunGroupLive` 语言 locale、`RunList` 页面隐藏暂停轮询：`✅ 已合入 (PR #38: T3.3)`。

---

## 二、架构问题

1. **`backend/skill_eval/` 作为平行宇宙重复造轮子** — `✅ 部分缓解 (PR #37: T2.1 / T2.3)`（完整收编待 Phase 5）
2. **`web/src/pages/RunDetail.tsx`（3,751 → 1,552 LOC）巨石组件与反转依赖** — `✅ 已合入 (PR #38: T3.3)`（已拆出 4 个独立模块并解除 `skillEval → RunDetail` 反转依赖）
3. **缺少前端代码分割** — `✅ 已合入 (PR #38: T3.3)`（`main.tsx` 接入路由级 `React.lazy`，主包 640KB → 405KB）；统一数据缓存层与类型化 i18n `⬜ 待后续`
4. **割裂的无类型 i18n** — `⬜ 待后续`

---

## 三、优先优化建议

1. **终结 `ProcessView` $O(N^2)$ 投影与 SSE 扇出风暴** — `✅ 已合入 (PR #37/#38)`（后端推送 SSE + `/process` LRU `✅ PR #37`；前端 SSE 健康时暂停 3s 冗余轮询 `✅ PR #38: T3.3`）
2. **核查 `ArtifactCollector` 双倍下载与同步阻塞** — `✅ 已核实保留 two_matching_reads 契约并优化哈希/内存 (PR #36: T1.4)`
3. **消除浏览器主线程卡死（分块 Base64）与上传缺失校验** — `✅ 已合入 (PR #36: T1.5)`
4. **拆分 `RunDetail.tsx`、路由级懒加载与优化实时体验** — `✅ 已合入 (PR #38: T3.3)`（`ArtifactsPanel` 展开按 `attemptId` 重置不折叠目录；站内 Modal 替换 `window.prompt` 待 Phase 5）

---

## 四、高价值功能机会

1. **Skill Eval ↔ Core Octagon 双向打通**：支持从 `ExperimentBuilder` 直接选择 `skill_eval_versions` 不可变快照发起批量多模型/多重复矩阵评测，并在 Skill Eval 运行详情中直接复用主站 `RunCostCard`、`TokenCurve`、自动 Rubric/Pairwise 评分与 ATIF 归因。
2. **统一产物预览器与富比较工作台**：将 `skillEval/ComparePage.tsx` 从纯 `<pre>` unified diff 升级为语法高亮并排 diff + 渲染产物并排对比 + 对比页一键发布胜出快照。
3. **升级 Skill 编辑器闭环**：把 `SkillPackageEditor` 的裸 `<textarea>` 升级为带语法高亮、行号、inline diff 与 `SKILL.md` frontmatter 实时校验的轻量代码编辑器。
