# 05 — 冲突消解与有序改造总任务清单（Master Task Plan）

> 制定日期：2026-11-27 · 初始基线：`9c4cfaf` · **最近状态更新**：2026-11-28（Phase 0–3 已随 PR #36/#37/#38 合入 `main` `2963dbe`；Phase 4 已完成并准备 PR）
>
> 状态约定：`[x] 已合入 main 且回归通过` · `[~] 已提 PR 待合入（已过全量既有回归）` · `[ ] 未开始` · `[-] 已冻结/跳过`

---

## 一、为什么要先排顺序：7 组核心冲突与覆盖关系

如果不按依赖排序直接并发修 Bug / 做优化，以下 7 处会发生**重复返工、代码互相覆盖或修错方向**：

| 冲突组 | 互相冲突/覆盖的内容 | 正确先后顺序（避免返工） |
|---|---|---|
| **A. DB 连接与异步阻塞** | 修 `db.py` 连接泄漏 / `BEGIN IMMEDIATE` vs `skill_eval/repository.py` 独立连库 vs 全面移出事件循环 | **先统一底层 DB 连接工厂**（一次建表/WAL、自动 close、`BEGIN IMMEDIATE`）→ 再让 `skill_eval`/`wire`/`evaluator` 复用 → 最后统一消除路由/Worker 阻塞 |
| **B. 6 套 Agent 事件解析器** | 改前端 `RunDetail.tsx` 事件解析 vs 改 `wire/normalizers` vs 改 `atif/converters` vs 建 Canonical 单一真源 | 前端 `RunDetail.tsx` **绝不深改其内部 7 家解析逻辑**（只修泄漏/拆文件）；先在后端对齐 Canonical/ATIF 单一解析层，再一次性切前端消费 |
| **C. 四重状态机与评分拆进程** | 零散修 `sweeper`/`runner`/`scoring_queue`/`recovery` 竞态 vs 抽统一状态机 vs 拆独立评分 Worker | 先修局部高危止血（Rejudge 前置校验、幂等释放）→ 再抽单一 `AttemptStateMachine` + `TaskRegistry`（一步根治 Sweeper/重启复活/竞态/SSE 推送）→ 最后拆独立评分进程 |
| **D. 前端巨石 `RunDetail.tsx` 与单测** | 拆 3,734 行巨石组件 vs 改前端功能 vs 修 14 个坏掉的前端单测 | **必须先修前端单测与 CI 门禁建立安全网** → 再修确定性死循环/泄漏 → 再物理拆出共享 Viewer/Wire/Cost → 最后做数据层与 Skill Eval 融合 |
| **E. Research 冗余子系统去留** | 修 `analysis/`、`rubric_evolution/`、`profiles/` 里的 Bug vs 冻结/下线这些子系统 | **先拍板去留**；确定冻结/下线的模块**不做任何 Bug 修复**，只修保留模块并抽公共 LLM/HTTP/JSON 基座 |
| **F. 7 家 Adapter 重复代码** | 逐个在 7 个 Adapter 修 `stderr` 死锁、`ConversationTraceWriter` 泄漏、Turn Header vs 抽 `BaseCliAdapter` | 单点安全/正确性 bug 立刻修（`http_proxy`、`token_usage`、`cost/ledger`、`blade_cli`、`opencode`/`dsh` 沙盒）；共性壳子问题（`stderr`、句柄、Turn Header）留到抽 `BaseCliAdapter` 一次性解决 |
| **G. Skill Eval 收编 vs 独立修复** | 在 `skill_eval/` 内部重做 DB/SSE/预览 vs 复用主站统一基建 | 先修纯业务正确性 bug（多轮 SQL、生成中发消息、Worker 崩溃、双倍下载、缺 blob 410）；DB 工厂、事件总线、产物预览等底座统一等主站改造后直接复用 |

---

## 二、开工前需确认的 4 个决策分岔点

1. **D1 — 冗余 Research 子系统处置**（**已确认**）：`analysis/` 标记废弃待删、`rubric_evolution/` 冻结不再修非关键 bug、`profiles/` 保留现样仅修 `feedback/learning.py` 中性信号误扣分。
2. **D2 — `_copy_uploads` 允许读取的目录白名单**（**已确认：方案 A**）：HTTP 请求侧 `request.context.uploaded_files` 严格限制在 `data_path/uploads`；服务端 `_copy_uploads` 允许 `data_path/uploads`、`env_dir`、`envs_path` 及已配置的 `selected_skills_path`，并用 `resolve().is_relative_to(...)` 防符号链接逃逸。
3. **D3 — `display_only` 模式下 Skill Eval 行为**（**已确认**）：允许只读查看历史与只读对比，禁止启动期写库、禁止落盘 `previews/` 与一切写操作。
4. **D4 — 执行节奏与验证策略**（**已确认**）：按 Phase 0 → Phase 1 → ... 逐阶段推进；每阶段汇总改动并在本文档更新状态，确认后再进入下一阶段。

---

## 三、按依赖排序的总任务清单

### Phase 0：安全网与 CI 门禁（必须最先做，零架构冲突）

> 目标：在动任何核心代码和重构巨石文件前，先把测试门禁补齐。本阶段内 T0.1 / T0.2 可并行。

- [x] **T0.1 修复前端 14 个失败单测并恢复全量 CI 门禁**（已测试通过：`tsc --noEmit` 0 错，`vitest run` 40/40 文件、186/186 用例全绿）
  - **涉及文件**：新增 `web/src/i18n/test-utils.tsx`、14 个缺失 `<I18nProvider>` 的 `web/src/**/*.test.tsx`、`.github/workflows/ci.yml`
  - **改动内容**：补充 `renderWithI18n` 与 jsdom `localStorage` 兼容封装；将 `.github/workflows/ci.yml` 的 `npm test -- src/skillEval` 恢复为全量 `npm test`。
  - **预期结果**：全量前端 40 个测试文件全部纳入 CI 门禁，后续拆 `RunDetail.tsx` 有回归护栏。

- [x] **T0.2 补齐后端可选依赖测试隔离与静态检查**（已测试通过：5 个文件 `py_compile` 通过，无 `blade_agent_kit` 环境下 26 passed / 8 skipped，不再 collection 崩溃）
  - **涉及文件**：`tests/skill_eval/test_sdk_contract.py`、`tests/test_blade_sdk_compat.py`、`tests/test_blade_cli_adapter.py`、`tests/skill_eval/test_verification_script.py`、`tests/skill_eval/test_capabilities.py`
  - **改动内容**：对真正依赖 `blade_agent_kit` 的用例补充 `pytest.importorskip("blade_agent_kit")`；为 `test_capabilities.py` 补全 `importlib.metadata.version` monkeypatch 使其不依赖宿主机安装状态。
  - **预期结果**：本地未安装 `blade` extra 时 `uv run pytest` 不再在 collection 阶段直接中断。

---

### Phase 1：P0 安全止血 + 零架构冲突的纯正确性 Bug（已随 PR #36 合入 `main`）

> 目标：修掉**无论未来架构怎么改都必须如此**的安全漏洞、数据损毁、统计失真与线上死循环。PR #36（`6d5a108`）已通过后端 392 passed + 前端 186 passed + `vite build` 并合入 `main`。

- [x] **T1.1 [P0 安全] 封堵任意宿主机文件读取与明文密钥泄漏**（PR #36 已合入）
  - **涉及文件**：`backend/run_dispatch.py`（`_copy_uploads`）、`backend/process/docker_launcher.py`、`backend/adapters/blade_cli.py`
  - **改动内容**：
    1. `_copy_uploads` 按 D2 方案 A 校验 `resolved != root and resolved.is_relative_to(root)`（允许 `data_path/uploads`、`env_dir`、`envs_path`、`selected_skills_path`），拒绝越界路径与符号链接逃逸；
    2. `docker_launcher.py` 单行变量改为 `0600` 临时文件 `--env-file`（多行值走无值 `-e KEY` + 子进程 env），不再在命令行暴露明文密钥，并加固 `exec()` `finally` 确保 `proc` 必回收；
    3. `blade_cli.py` 用 `is_relative_to` 修复 zip-slip、`0600` 临时文件 `finally` 删除明文 `env_token`、`prompt_delivery_handler` 补 `inspect.isawaitable`、`ConversationTraceWriter` 移入 `finally` 关闭。
  - **测试注意**：`tests/test_docker_launcher.py`（2 处）与 `tests/test_blade_cli_adapter.py`（1 处）已同步更新为校验 `--env-file` 与凭据临时文件退出即删。全量既有回归已绿（后端 392 passed / 12 skipped，前端 186 passed + `vite build` 通过）；各修复项的**新增回归专测**待后续补充。

- [x] **T1.2 [P0 数据保护] 修复 Rejudge 先删后检、Run 幂等键永久卡死与 `display_only` 启动期写库**（PR #36 已合入）
  - **涉及文件**：`backend/scoring_queue.py`（`_prepare_rejudge`）、`backend/run_service.py`（`create_run_plan`）、`backend/main.py`（lifespan `display_only` 守卫）
  - **改动内容**：
    1. `run_service.py` 新增 HTTP 入站上传文件校验（D2 方案 A：仅允许 `data_path/uploads` 内常规文件，且不覆盖服务端预置 `task_context.uploaded_files`），并将 `fail_idempotency` 的 `BaseException` 保护扩展到建单全流程；
    2. `_prepare_rejudge` 将 `attempt_dir.is_dir()`、`state.envs.get(...)` 移至任何 `UPDATE/DELETE/commit` 之前；
    3. `main.py` 将启动期 rubric/process evidence 修复、`sync_blade_skills`、`record_startup` 与停机 `record_shutdown` 全部纳入 `if not display_only:`。

- [x] **T1.3 [P0 统计准确性] 修复 Anthropic SSE Token 归零、Claude Code 2× Token 与不完整成本账本误报**（PR #36 已合入）
  - **涉及文件**：`backend/wire/sources/http_proxy.py`、`backend/adapters/token_usage.py`、`backend/cost/ledger.py`、`backend/adapters/opencode_family.py`、`backend/adapters/dsh.py`
  - **改动内容**：
    1. `http_proxy.py`：`_UsageProbe._parse_sse_line` 改为按非 `None` 字段合并；`_stream_body` 与 `forward` 兜底 `await _REGISTRY.end(entry)` 释放 `inflight`；按 `Content-Type` / SSE 帧判定 `streamed`；
    2. `token_usage.py`：`result_usage_tokens` 在顶层 `usage` 非零时不再累加 `modelUsage`；
    3. `cost/ledger.py`：`priced_missing or missing or judge_missing` 时将 `total_cost_usd` 设为 `None`；
    4. `opencode_family.py` & `dsh.py`：Docker 沙盒下统一使用 `sandbox.path(workspace)` 与 `sandbox.translate_url(...)`；`dsh.iterative_session` 改为 `False`。

- [x] **T1.4 [P0/P1 Skill Eval 业务逻辑] 修复多轮对话串台、生成中插队、Worker 崩溃与 D3 预览只读安全**（PR #36 已合入）
  - **涉及文件**：`backend/skill_eval/runs.py`、`worker.py`、`recovery.py`、`run_api.py`、`api.py`、`previews.py`、`runtime.py`、`artifacts.py`
  - **改动内容**：
    1. `runs.py`：多轮回复子查询补 `AND r.seq < e.seq`；`send_message` 拒绝 `active` 或生成进行中（409 `generation_in_progress`）；
    2. `worker.py`：`run_forever()` 捕获非取消异常加 1s 退避；删除 `controls()` 每 200ms 冗余续租写库；清理 `self.signals`；超时 `stop()` 加 10s 超时与异常保护；
    3. `recovery.py`：`json.loads(arguments)` 异常保护；恢复时同步更新 `skill_eval_generations` 与 `skill_eval_interactions`；
    4. `run_api.py` & `api.py` & `previews.py` & `runtime.py`：终态关闭 SSE；缺 blob 统一返回 410；`display_only` 下 `previews.py` 禁止落盘写入（D3）；防御解析 `Content-Length`；
    5. `artifacts.py` 备注：核实“双次读取”为契约级变更检测（`two_matching_reads` 且有专测验证），故刻意保留双读语义，仅去重了重复哈希计算并及时释放 `second` 内存。

- [x] **T1.5 [P0/P1 Research 与前端止血] 修复 Leader Outbox 永久堵死、Insights 永久 409、Experiments 取消竞态与前端死循环/泄漏**（PR #36 已合入）
  - **涉及文件**：`backend/experiments/leader.py`、`backend/experiments/coordinator.py`、`backend/insights/service.py`、`backend/insights/providers.py`、`backend/feedback/learning.py`、`backend/api.py`、`web/src/pages/RunGroupLive.tsx`、`web/src/api/groupStream.ts`、`web/src/pages/RunDetail.tsx`、`web/src/i18n/messages.runDetail.ts`、`web/src/skillEval/{SkillPackageEditor,RunDetail,ComparePage,Workspace}.tsx`
  - **改动内容**：
    1. `leader.py`：空候选标记 `consumed_at` 继续消费，`_rank` 安全处理 `duration_ms IS NULL`；
    2. `coordinator.py`：检查 `transition_cell` 返回值阻断已取消 Cell 的 `dispatch`，`stop_group` 取消 `group:{id}` 任务；
    3. `insights/service.py` & `providers.py`：`BaseException` 释放 `insight:active`/`insight:generate` 锁；累加多步 token；`delete_session` 进 `finally:` 且去重流式文本；
    4. `feedback/learning.py`：跳过中性信号（如 `"annotation"`）；
    5. `api.py:1473`：SSE 终态补 `"cancelled"`；
    6. 前端：消除 `RunGroupLive` 无限刷接口循环与 `groupStream` 缺 `boundary` 崩溃；修 `RunDetail` 泄漏 `setInterval`、跨 `runId` 状态重置、补 `runDetail.hop.callLink`；`SkillPackageEditor` 改分块 Base64 并补体积校验；Skill Eval 终态不连 SSE；清理失败 Promise ref。

---

### Phase 2：底层底座统一（DB 契约 + 状态机 + 消除事件循环阻塞 — 已随 [PR #37](https://github.com/catwithAI/open-agent-octagon/pull/37) 合入 `main`）

> 目标：先建好统一的 DB、状态机、事件总线和共享 HTTP/LLM 客户端底座，彻底消除冲突组 A、C、E 的重复返工。PR #37（`3641d97`）已合入 `main`。

- [x] **T2.1 统一 SQLite 连接工厂与初始化策略（解冲突组 A）**（PR #37 已合入）
  - **涉及文件**：`backend/db.py`、`backend/evaluator.py`、`backend/wire/finalize.py`、`backend/wire/aggregate.py`、`backend/skill_eval/repository.py`、`backend/env_attempt_server.py`
  - **改动内容**：
    1. `_open_sync` 改为自动 `conn.close()` 的 `@contextmanager`（支持 `immediate=True`）；WAL 按 `(path, dev, ino)` 仅初始化一次，`synchronous=NORMAL` / `busy_timeout=30000` 统一生效（含 `open_db`）；
    2. `_init_db_sync` 按 `(path, dev, ino)` 加 once-latch，移出热路径；修复迁移将历史 `cancelled` 错改 `failed` 的问题；
    3. `attempt_events` 写入与 `claim_idempotency` 改用 `BEGIN IMMEDIATE`；`env.db` 建表加按路径引用计数锁 + WAL + `hmac.compare_digest`；
    4. `evaluator` / `wire/finalize` / `wire/aggregate` 改用 `_open_sync`；有界化 `_event_data_paths`、`_inbound_seq` 与 `_FINALIZE_LOCKS`。

- [x] **T2.2 收口 `AttemptStateMachine` + `TaskRegistry` + 事件驱动 SSE（解冲突组 C）**（PR #37 已合入）
  - **涉及文件**：`backend/runtime_state.py`、`backend/convergence.py`、`backend/sweeper.py`、`backend/scoring_queue.py`、`backend/runner.py`、`backend/run_dispatch.py`、`backend/recovery.py`
  - **改动内容**：
    1. `runtime_state.py` 新增 `scoring_tasks`、`track_background_task`（强引用 + 完成清理 + 异常日志）以及跨线程安全的 `notify_run_update` / `subscribe_run_updates` 推送总线；
    2. `convergence.py` + `sweeper.py` + `scoring_queue.py`：超时评分同步取消卡死 scorer 协程并触发 cancel hook，同时收敛 `scoring_jobs` 终态防重启复活；新增 `enqueue_scoring_job_async` 将快照与写库移出事件循环；
    3. `runner.py` + `run_dispatch.py`：`_finalize_no_score` 加非终态乐观守卫防竞态覆盖；非 blade agent 异常不再误标 `blade_service_unavailable`（改 `cli_error`）；物料准备与状态更新移出事件循环；`recovery.py` 兼容 `judge.backend == "evals"`。

- [x] **T2.3 全面消除异步事件循环上的同步阻塞与无界内存缓存**（PR #37 已合入）
  - **涉及文件**：`backend/api.py`、`backend/research_api.py`、`backend/artifact_preview.py`、`backend/wire/lifecycle.py`、`backend/wire/sources/http_proxy.py`、`backend/model_integrity.py`、`backend/skill_eval/{events,run_api,storage}.py`
  - **改动内容**：
    1. `api.py`：主站 SSE 接入 `subscribe_run_updates` 事件唤醒；读接口与 JSONL 读取全量 `asyncio.to_thread`；存在性检查改轻量 `SELECT 1`；`POST /upload` 改 64KB 流式分块限 200MB；按 event loop 复用共享 `httpx.AsyncClient`；
    2. `research_api.py` 与 `wire/lifecycle.py` 同步 DB/FS/finalize 全量 `to_thread`；
    3. `artifact_preview.py` 为 `cache_key` 增加 `(path, mtime_ns, size)` 有界 LRU 缓存并淘汰瞬态失败 Future；
    4. `http_proxy` 与 `model_integrity` 缓存全面有界化（不缓存 `None`）；`skill_eval` SSE 接入 `EventStore` 推送通知、`/process` 加 `(run_id, head)` 64 项 LRU 缓存、`collect_unreferenced` 纳入 `previews/` 清理。

- [x] **T2.4 归并 Evals 响应校验、统一 JSON 提取器并冻结冗余 Research 模块（解冲突组 E）**（PR #37 已合入）
  - **涉及文件**：`backend/evals_scorer.py`、`backend/comparison_scorer.py`、`backend/attribution_client.py`、`backend/insights/generator.py`、`backend/mutations/preview.py`、`backend/iteration/controller.py`、`backend/profiles/recommend.py`、`backend/analysis/__init__.py`、`backend/rubric_evolution/{__init__,pipeline,judge_executor}.py`
  - **改动内容**：
    1. `evals_scorer.py` 强校验非空/有限/`[0,1]` 分数与维度 ID 匹配；`comparison_scorer` 与 `attribution_client` 优先对齐冻结 `scoring-work` 快照目录；
    2. 全库 3 份脆弱 `_parse_json` 统一替换为 `backend.insights.jsonutil.parse_json_object`；
    3. `mutations/preview.py` 将 `source_hash` 与 `env_name` 纳入 `preview_token`；`iteration/controller.py` 为 reviewer 隔离工作目录、续租遇瞬时锁重试、`asyncio.Future` 替代 100Hz 忙等；`profiles/recommend.py` 改 `ON CONFLICT DO UPDATE` 避免丢弃学习权重；
    4. 按 D1 为 `backend/analysis` 与 `backend/rubric_evolution` 补充明确废弃/冻结标记（并同步更新 `test_evals_scorer.py` / `test_evals_dimensions.py` 两处旧空响应 stub）。

---

### Phase 3：核心架构解耦（Adapter 抽象 + ATIF/Wire 7 家对等 + 前端拆巨石 — 已随 [PR #38](https://github.com/catwithAI/open-agent-octagon/pull/38) 合入 `main`）

> 目标：根治长期维护成本最高的巨石模块、管道死锁、7 家观测不对等与前端反转依赖。PR #38（`2963dbe`）已合入 `main`。

- [x] **T3.1 抽取 CLI Adapter 公共底座 `cli_common.py` 与拆分/加固 `blade_service.py`（解冲突组 F）**（PR #38 已合入）
  - **涉及文件**：`backend/adapters/{cli_common,claude_code,codex,kimi_code,opencode_family,blade_service}.py`、新增 `backend/adapters/blade/{__init__,artifacts,extraction,interactions}.py`
  - **改动内容**：
    1. `cli_common.py` 统一并发抽干 4 个 CLI Adapter 的 `stderr`（根治 64KB 管道死锁并按轮次追加 `stderr.txt`）与 `finally` 安全关闭 `ConversationTraceWriter`；
    2. 将 `blade_service.py` 从 3,555 行精简至 2,672 行（拆出 `artifacts`/`extraction`/`interactions` 3 个子模块并保持全部导出兼容）；修复恢复期每事件 3 次 REST 风暴（按 `progress_poll_interval_seconds` 节流并保留完整 token/model/tool 统计）、非 `BladeChatError` 异常路径清理远端会话与取消监控任务、自动应答循环上限（`_MAX_UNEXPECTED_AUTO_ANSWERS=20`）以及会话级 `httpx.AsyncClient` 复用。

- [x] **T3.2 补齐 7 家 Agent ATIF / Wire 观测对等、来源感知成本语义与遗留修复（解冲突组 B）**（PR #38 已合入）
  - **涉及文件**：`backend/atif/converters/blade_agent.py`、新增 `backend/wire/normalizers/kimi_code.py`、`backend/wire/normalizers/runner.py`、`backend/cost/estimate.py`、`backend/experiments/recovery.py`、`backend/run_service.py`
  - **改动内容**：
    1. `atif/converters/blade_agent.py` 兼容 `blade_service.py` SDK `{kind, raw}` 事件格式，实现 7 家 Agent ATIF-v1.7 全覆盖；
    2. 新增 `KimiCodeNormalizer` 并注册进 `wire/normalizers/runner.py`，实现 7 家 Agent Wire Native 轨迹全覆盖；
    3. `cost/estimate.py` 支持来源感知的 `input_semantics`（避免 Kimi 等经 HTTP 代理回填 inclusive `prompt_tokens` 重复计费）；
    4. `experiments/recovery.py` 活跃状态补 `'scoring'`；`run_service._create_locks` 按 event loop 清理避免内存泄漏。

- [x] **T3.3 拆分前端巨石 `RunDetail.tsx`、解除 `skillEval` 反转依赖与代码分割（解冲突组 D）**（PR #38 已合入）
  - **涉及文件**：新增 `web/src/components/viewers/ArtifactViewers.tsx`、`web/src/components/ArtifactsPanel.tsx`、`web/src/cost/RunCostCard.tsx`、`web/src/wire/WirePanel.tsx`；改造 `web/src/pages/RunDetail.tsx`、`web/src/skillEval/{ArtifactPreview,ProcessView,RunDetail}.tsx`、`web/src/main.tsx`、`web/src/pages/{ExperimentBuilder,RunGroupLive,RunList}.tsx`
  - **改动内容**：
    1. 将 `RunDetail.tsx` 从 3,751 行缩减至 1,552 行（-58.6%），抽离共享预览器、产物树（修复 SSE 刷新折叠已展开目录）、成本卡片与 Wire 面板并保持向后 re-export；
    2. `skillEval/ArtifactPreview.tsx` 改为直接引用 `components/viewers/ArtifactViewers`，彻底消除 `skillEval → pages/RunDetail` 反转依赖；
    3. `main.tsx` 全面接入路由级 `React.lazy` + `<Suspense>`，主包从 639.55 kB 降至 404.81 kB（消除 Vite >500 kB 警告）；
    4. 修复 `ProcessView` `following` 过期闭包、SSE 健康时暂停 3s 冗余轮询、`ExperimentBuilder` 切场景竞态、`RunGroupLive` 语言 locale 与 `RunList` 页面隐藏暂停轮询。

---

### Phase 4：独立进程隔离（水平扩展与安全边界 — 本次 PR 待合入）

> 目标：利用 `scoring_jobs` 租约列将评分/重型预览迁出 API 主进程，并将外部 `envs/*/core.py` 与 `scorer.py` 从持有明文凭据的 API 进程中隔离。已通过后端 410 passed（含 18 项新增隔离专测）+ 前端 187 passed。

- [~] **T4.1 抽取独立的 `scoring-worker`（评分与重型预览）进程**
  - **涉及文件**：`backend/config.py`、`backend/scoring_queue.py`、`backend/artifact_preview.py`、`backend/artifact_worker.py`、新增 `backend/workers/{__init__,scoring_worker}.py`、`tests/test_scoring_worker_isolation.py`
  - **改动内容**：
    1. `scoring_queue.py` 启用 `scoring_jobs.lease_owner` / `lease_expires_at` / `heartbeat_at` 租约与心跳续租（`BEGIN IMMEDIATE` 原子抢单 + 过期租约自动接管），新增 `scoring_worker_mode`（`in_process` / `subprocess` / `external`）三模调度；
    2. 新增独立常驻进程与单任务隔离入口 `python -m backend.workers.scoring_worker`（支持 `--once` / `--concurrency` / `--run-job` / `--render-preview`），每个评分任务运行在独立进程组，超时/取消触发 OS 级 `SIGTERM → SIGKILL` 整组强杀并剥离管理员密钥；
    3. `artifact_preview.py` 将预览超时升级为 `Popen(start_new_session=True)` + `os.killpg` 整组强杀，防止子渲染进程泄漏；`artifact_worker.py` 支持有界资源限额覆写。
  - **前置依赖**：T2.1、T2.2。

- [~] **T4.2 Env Scorer / Core 子进程隔离**
  - **涉及文件**：`backend/config.py`、`backend/main.py`、`backend/env_loader.py`、`backend/env_attempt_server.py`、新增 `backend/workers/env_worker.py`、`tests/test_env_subprocess_isolation.py`（并顺手修复 `tests/test_atif.py` 跨用例 `MonkeyPatch` 泄漏）
  - **改动内容**：
    1. 新增 `backend/workers/env_worker.py`（支持 `inspect-env` / `call-tool` / `run-scorer` 三个子进程动作），对 `core.py` 采用最小白名单环境变量、对 `scorer.py` 强制剥离 `OPENROUTER_MANAGEMENT_KEY` 等管理员密钥并仅透传当前任务自己的 `judge_env_overlay()`，完全隔离 API 主进程内存中的 `runtime_state.run_credentials`；
    2. `EnvLoader.load_all(..., isolate=True)` 不在 API 进程 `exec_module`，通过 `IsolatedRegisteredTool` / `IsolatedScorerModule` 代理透明兼容全部现有调用方；
    3. `env_attempt_server.py` 在 `env_isolation_mode="subprocess"` 下通过隔离子进程执行工具调用，保留 wire inbound 采集、原子 `env.db` 初始化，并按 `env_tool_timeout_seconds` 对超时工具执行进程组强杀（返回 HTTP 504）。
  - **前置依赖**：T4.1。

---

### Phase 5：核心产品护城河功能发力

> 在底座稳定、债务清零后投入，最大化产品差异化。

- [ ] **T5.1 打通「Skill Eval 快照 ↔ Core Octagon 多模型/多Agent 矩阵评测」**
  - 支持在 `ExperimentBuilder` 直接选择不可变 `skill_eval_versions` 快照跑 **Skill v1 vs v2 × 多模型 × 多重复 × 自动评分**，并在 Skill Eval 详情页复用主站 `RunCostCard`、`TokenCurve` 与 ATIF 归因。
  - **前置依赖**：T3.2、T3.3。

- [ ] **T5.2 差异可视化升级：决策分叉对比（Decision Fork Diff）+ 归因证据直达**
  - 基于 Canonical Trajectory 与 `/attribute`、Pairwise 评分，在并排对比页直接高亮多个 Agent 从哪一步开始分叉、各自的成本/耗时增量与失败根因。
  - **前置依赖**：T3.2、T3.3。

- [ ] **T5.3 提交前 Preflight 体检（`POST /api/runs/preflight`）与估算/结算成本漂移对账**
  - 在提交多 Agent 批量任务前一次性校验 env 依赖、Docker 沙盒、磁盘空间、模态匹配与预算上限；在成本视图并排呈现估算费用与真实结算账单偏差，并结合 `X-Octagon-Turn-Id` 定位高成本轮次。
  - **前置依赖**：T1.3、T3.1。

- [ ] **T5.4 Skill Eval 工作台体验升级**
  - 将 `ComparePage` 升级为并排语法高亮 diff + 渲染产物并排预览 + 一键发布胜出版本；将 `SkillPackageEditor` 升级为带 `SKILL.md` frontmatter 校验的代码编辑器；用站内 Modal 替代 `window.prompt`/`window.confirm`。
  - **前置依赖**：T3.3。
