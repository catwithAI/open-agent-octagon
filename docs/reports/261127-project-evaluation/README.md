# OpenAgentOctagon 全项目综合评估报告

> 审计日期：2026-11-27 · 审计基线：`9c4cfaf` (`main`) · **最近状态更新**：2026-11-28（`main` 已合入 PR #36、`PR #37` 至 `3641d97`；Phase 3 已完成并准备 PR）
>
> 范围：全仓库后端（Python / FastAPI / SQLite，约 72,896 LOC）与前端（React / Vite / TypeScript，非测试代码约 13,842 LOC；测试文件 103 个）。

## 当前修复进度总览（2026-11-28）

> 状态图例：`✅ 已合入 (PR #36/#37)` · `🟡 本 PR 待合入 (Phase 3)` · `⏸️ 已冻结/废弃 (D1)` · `⬜ 未开始`

| 阶段 | 任务 | 当前状态 | 关联 PR / 说明 |
|---|---|---|---|
| **Phase 0** | `T0.1`–`T0.2` 前端全量 CI 门禁与后端可选依赖隔离 | ✅ 已合入 | [PR #36](https://github.com/catwithAI/open-agent-octagon/pull/36)（`6d5a108`） |
| **Phase 1** | `T1.1`–`T1.5` 安全止血、数据保护、Token/成本、Skill Eval、Research & 前端 | ✅ 已合入 | [PR #36](https://github.com/catwithAI/open-agent-octagon/pull/36)（`6d5a108`） |
| **Phase 2** | `T2.1`–`T2.4` SQLite 工厂、状态机/Sweeper、非阻塞事件循环、Scorer 加固与 D1 冻结 | ✅ 已合入 | [PR #37](https://github.com/catwithAI/open-agent-octagon/pull/37)（`3641d97`） |
| **Phase 3** | `T3.1A` 抽 `cli_common.py`，并发抽干 4 个 CLI Adapter `stderr` 与句柄安全 | 🟡 待合入 | Phase 3 PR（已过全量 392 后端测试） |
| **Phase 3** | `T3.1B` 拆分 `blade_service.py`（3,555 → 2,672 行）并修恢复风暴/泄漏/自动应答上限 | 🟡 待合入 | Phase 3 PR（抽出 `backend/adapters/blade/` 3 个子模块） |
| **Phase 3** | `T3.2` 补齐 Blade SDK ATIF、`kimi-code` Wire Normalizer、来源感知成本语义与 `experiments/recovery` | 🟡 待合入 | Phase 3 PR（7 家 Agent ATIF/Wire 全面对等） |
| **Phase 3** | `T3.3` 拆分 `RunDetail.tsx`（3,751 → 1,552 行）、解除 `skillEval` 反转依赖、`React.lazy` 代码分割与前端润色 | 🟡 待合入 | Phase 3 PR（前端 187/187 全绿，主包 640KB → 405KB） |
| **Phase 4** | `T4.1`–`T4.2` 独立 `scoring-worker` 进程 / Env 代码子进程隔离 | ⬜ 未开始 | 等 Phase 3 合入后启动 |
| **Phase 5** | `T5.1`–`T5.4` Skill Eval ↔ Core 矩阵融合、分叉对比、Preflight、富编辑器 | ⬜ 未开始 | 排在底座与架构解耦之后 |

## 报告索引

本报告由总览结论与 4 份子系统深度源码审计组成：

1. **综合评估总览**（本文）：项目健康度画像、P0/P1 核心 Bug 清单、关键架构问题、高 ROI 优化项、产品方向取舍与落地节奏。
2. [01 — 核心后端引擎审计](./01-core-backend-audit.md)：`main` / `api` / `db` / `runner` / `run_dispatch` / `run_service` / `scoring_queue` / `sweeper` / `recovery` / `docker_launcher` / `artifact_preview` / `env_attempt_server`。
3. [02 — Adapters、Wire、ATIF、Cost 与 Conversation 审计](./02-adapters-wire-cost-audit.md)：7 家 Agent 适配器、`http_proxy`、`wire` 归一化、ATIF 转换器、成本账本与多轮对话。
4. [03 — Research/Experiments、Insights、Rubric Evolution、Iteration 与 Scorers 审计](./03-research-analytics-audit.md)：实验编排、变异、Leader/鲁棒性、洞察报告、Rubric 自动演进、多轮审阅与外部评分解耦。
5. [04 — Skill Eval 与前端审计](./04-skill-eval-frontend-audit.md)：`backend/skill_eval/` 全套模块与 `web/src/` 全量前端页面与测试基线。
6. [05 — 冲突消解与有序改造总任务清单（Master Task Plan）](./05-master-task-plan.md)：冲突/覆盖依赖分析、待确认决策项，以及 Phase 0 → Phase 5 共 18 项按序追踪任务清单。

---

## 项目整体画像

### 代码规模分布

| 子系统 | 文件数 | 代码行数（LOC） | 现状简评 |
|---|---:|---:|---|
| `backend/core (*.py)` | 43 | 19,799 | 核心调度、评分、恢复逻辑扎实，但 `api.py`/`db.py`/`run_dispatch.py` 已演变为巨石模块 |
| `backend/wire` | 43 | 13,021 | 强差异化基础设施；热路径阻塞事件循环，Anthropic SSE usage 存在覆盖 bug |
| `backend/adapters` | 15 | 10,425 | 7 家并行重复代码严重；`blade_service.py` 达 3,555 行 |
| `backend/rubric_evolution` | 19 | 5,318 | 过度设计且核心路径未接线/不可激活，建议冻结 |
| `backend/cost` | 14 | 3,785 | 真实账单审计有价值，但新旧两套生命周期并存 |
| `backend/skill_eval` | 22 | 3,346 | 产品体验闭环清晰，但以“城中之城”方式重复核心基建并与主系统抢锁 |
| `backend/experiments` | 13 | 3,311 | 统计严谨性核心骨架，存在取消竞态与 Leader outbox 卡死隐患 |
| `backend/atif` | 10 | 2,905 | 统一轨迹基础良好，但 Blade SDK 格式未覆盖 |
| `backend/iteration` | 9 | 2,115 | 研究板块中工程质量最高，适合继续深化 |
| `backend/insights` | 12 | 1,923 | 证据锚定报告有价值，存在永久锁死与 Provider 泄漏缺陷 |
| `backend/security` + `process` | 15 | 2,702 | 离线安全扫描与 Docker 沙盒边界清晰，有少量路径/密钥透传缺陷 |
| `backend/analysis` + 其余研究子包 | 31 | 3,262 | `analysis/`（1,193 LOC）完全未接线；`profiles/`+`feedback/` 过度设计 |
| `web/src`（非测试 / 测试） | 61 / 40 | 13,842 / 3,996 | `RunDetail.tsx` 3,734 行巨石组件；14/40 测试文件失败且被 CI 跳过 |

### 核心判断

OpenAgentOctagon 的核心定位极其清晰——**同任务、多 Agent 的差异可视化对比平台**（保留原生能力、通过 Wire + ATIF + 安全扫描 + 成本核算 + 沙盒建立可信观测底座）。

当前项目已到达**“高速功能堆积后的结构性拐点”**：
- **测试门禁失守**：实测前端 `npm test` **14/40 文件、88/186 用例失败**（引入 i18n 后旧测试未包 `<I18nProvider>`），但 `.github/workflows/ci.yml` 仅执行 `npm test -- src/skillEval`，主站回归风险长期裸奔；后端缺乏 lint / 静态类型门禁，且部分单测强依赖可选 extra `blade_agent_kit`。
- **代码膨胀与多轨并存**：约 6,500 LOC 未接线或不可激活的演进/分析代码持续增加认知与维护负担；同一份 Agent 原始事件被 6 套互不兼容的解析器重复解析。
- **长稳运行隐患集中**：全库存在普遍的异步事件循环同步阻塞、SQLite 连接/FD 泄漏与 `DEFERRED` 锁升级冲突、以及若干可将后台 Worker/Outbox/锁永久卡死的缺陷。

---

## 一、最需要修复的 Bug（按严重度排序）

### P0：安全漏洞、静默数据损毁与统计失真

1. **任意宿主机文件读取漏洞（Critical Security）**
   - `backend/run_service.py:456` 合并调用方 `request.context` 到 `task_context`，`backend/run_dispatch.py:1191-1206`（`_copy_uploads`）对 `uploaded_files[].path` 不做目录边界检查，直接 `shutil.copy2` 到 `skill_workspace`，随后可经 `GET /api/runs/{id}/attempts/{aid}/artifacts/{name}` 下载宿主机任意文件（如 `/etc/passwd`、`octagon.yaml`、`octagon.db`）。
   - 同源安全问题：`backend/process/docker_launcher.py:214-215` 通过 `docker exec -e KEY=VALUE` 在宿主机命令行暴露 API Key；`backend/adapters/blade_cli.py:552-557` 写入明文 `env_token` 临时文件永不删除；`backend/adapters/blade_cli.py:669-670` zip-slip 检查用 `str.startswith` 可被同前缀目录绕过。
2. **Rejudge（重评）先删库提交、后做前置校验导致永久丢分**
   - `backend/scoring_queue.py:225-249`（`_prepare_rejudge`）在检查 `attempt_dir.is_dir()`、`env` 是否加载以及 `create_scoring_snapshot` 之前，就执行并提交了 `DELETE FROM scoring_jobs` 与 `DELETE FROM scores`，任意失败都会永久清空既有评分。
3. **Token 与成本统计系统性失真**
   - **Anthropic SSE 输入/缓存 Token 归零**：`backend/wire/sources/http_proxy.py:960-962` 整对象覆盖 `self.usage`，`message_delta` 抹掉 `message_start` 里的 `input_tokens` 与 cache 字段。
   - **Claude Code Token 2× 重复计数**：`backend/adapters/token_usage.py:154-165` 同时累加顶层 `usage` 与 `modelUsage`。
   - **成本口径失真**：`backend/cost/ledger.py:387-423` 在存在缺失 ledger 的 `incomplete` Run 上仍汇报低估的 `total_cost_usd`；`backend/cost/estimate.py:54-75` 按 `agent_name` 误将 `kimi-code` 代理回填的 inclusive `prompt_tokens` 按 disjoint 计费，重复计收缓存 token。
4. **Skill Eval 多轮对话历史回复串台**
   - `backend/skill_eval/runs.py:57-69` 回退查 `llm:response:done` 漏掉 `AND r.seq < e.seq`，导致所有前面轮次全部显示最后一轮的回复。

### P1：死锁、永久卡死、无限循环与状态机损坏

5. **全库 SQLite 连接/FD 泄漏与 `DEFERRED` 锁死**
   - `backend/db.py:747-754`（`_open_sync`）返回裸 `sqlite3.Connection`，`with _open_sync(...) as conn:` 永不关闭连接且每次执行 `PRAGMA journal_mode=WAL`；`attempt_events`（`db.py:1787-1818`）、`claim_idempotency`（`db.py:1465-1515`）等读后写路径缺少 `BEGIN IMMEDIATE`。
6. **评分 Sweeper 信号量饥饿与过期任务重启复活**
   - `backend/sweeper.py:113-128` 超时收敛时不取消卡死 scorer（永久占满默认容量为 2 的 `_scoring_semaphore`）、不更新 `scoring_jobs.status`（重启后重新入队）、并误将已完成执行写为 `status="scoring_failed"`。
7. **多处后台 Worker / 队列 / 幂等锁永久卡死**
   - Skill Eval `Worker.run_forever()`（`backend/skill_eval/worker.py:390-403`）未捕获瞬时 DB 锁异常，报错一次即永久下线；优雅停机（`worker.py:117-143`）直接将运行中任务标为 `failed` 并远端停止，破坏恢复语义；`send_message()`（`runs.py:168-170,203-204`）允许在生成中插入消息损坏状态机。
   - Experiments Leader Outbox（`backend/experiments/leader.py:101-129`）遇空候选或 `duration_ms IS NULL` 抛异常且不推进游标，永久堵死全局 Leader 消费。
   - Insights 并发锁（`backend/insights/service.py:66-117`）依赖无 TTL 的 `idempotency_keys` 且未捕获 `CancelledError`，断连或重启即永久 409。
   - Experiments 取消竞态（`backend/experiments/coordinator.py:161-169`）忽略 `transition_cell` 返回值，已停止 Group 仍拉起新 Run。
   - Run 创建幂等键（`backend/run_service.py:438-551`）中段异常留 `in_progress` 永不释放；崩溃对账（`backend/db.py:1569`）因 `result_id` 在 claim 阶段为 `NULL` 恒判不存在。
8. **Adapter / 沙盒 / 代理 / ATIF 对等缺陷**
   - `opencode_family.py:398` 与 `dsh.py:415-475` 在 Docker 沙盒中透传宿主机路径与 `127.0.0.1` URL。
   - `atif/converters/blade_agent.py:6-14,74-80` 不兼容 `blade_service.py` 的事件格式，SDK 模式 Blade 恒无 ATIF 轨迹。
   - 4 个 CLI Adapter 在 `wait()` 后才读 `stderr`，超过 64KB 管道缓冲即死锁。
   - `http_proxy.py:802-809,1048-1092` 客户端提前断连泄漏上游连接与 `inflight` 计数。
   - `blade_service.py:2270-2317` 恢复期间每条 Socket 事件触发 3 次 REST 轮询，异常路径泄漏远端会话。
9. **前端死循环、SSE 泄漏与 `display_only` 边界穿透**
   - `web/src/pages/RunGroupLive.tsx:88-113` 存在 `useEffect` 无限刷接口死循环。
   - 主站 `api.py:1473`（漏判 `cancelled`）与 Skill Eval `run_api.py:170-180`（终态不关流）导致历史页面 SSE 永久轮询 SQLite。
   - `web/src/pages/RunDetail.tsx:3278-3284` 泄漏 `setInterval`、切换 `runId` 残留旧状态、2040 行缺失 i18n key `runDetail.hop.callLink`。
   - `display_only` 模式下 `main.py:105-151` 仍先执行了写库与文件同步，Skill Eval 预览路由（GET）绕过只读边界落盘，而对比查询（POST）被误拦 403。

---

## 二、最需要调整的架构问题

1. **同一份 Agent 原始事件存在 6 套互不兼容的解析器**
   - 分布在 `adapters/`、`wire/normalizers/`、`atif/converters/`、`security/toolcalls.py`、前端 `RunDetail.tsx`、`skill_eval/process.py`。任何一家 Agent 升级协议都需同步修改 5–6 处，极易引发采集口径漂移。
   - **目标架构**：后端确立唯一 **Canonical Trajectory（基于 ATIF-v1.7 / 统一 Event Envelope）** 作为单一派生真源，每家 Agent 只维护一套解析器；前端、安全扫描、过程视图与归因只消费 Canonical 数据。
2. **单进程大泥球 + 进程内全局状态 + Skill Eval 同库竞争**
   - 单 Uvicorn 进程混跑 API、SSE、HTTP 代理、Agent 调度、LLM Judge、LibreOffice 渲染、10+ 后台循环，外加 `skill_eval` 独立维护同步 `sqlite3` 引擎抢占同一 `octagon.db`；关键租约与凭据全在 `runtime_state.py` 内存中。
   - **目标架构**：优先把 `scoring_jobs`（已有 `lease_owner`/`snapshot_ref` 基础）与 Office 渲染拆为独立 worker 进程；将 `skill_eval` 收编进统一的异步 DB/Blade/预览/事件总线基础设施；引入单一 `AttemptStateMachine` 收口四重状态字段写入。
3. **巨石文件与反转依赖**
   - `RunDetail.tsx`（3,734 行，还被 `skillEval/ArtifactPreview.tsx` 反向 import）、`blade_service.py`（3,555 行）、`api.py`（2,209 行）、`db.py`（1,906 行）、`run_dispatch.py`（1,291 行）造成循环依赖与高修改成本。
   - **目标架构**：后端按领域拆包并抽象 `BaseCliAdapter`；前端拆出独立 `components/viewers/`、`wire/`、`cost/` 模块并启用 `React.lazy` 代码分割。
4. **后处理分析子系统重叠与双轨基础设施**
   - `insights/`、`analysis/`（死代码）、`attribution_client.py`、`rubric_evolution/`、`profiles/`+`feedback/` 职责高度重叠；成本存在新旧双状态机；LLM Provider 与 `_parse_json` 各自复制 3–4 份；前端存在 3 套 fetch 客户端与 2 套 i18n。
   - **目标架构**：果断做减法——归并共享 HTTP/LLM Client 与 JSON 解析工具，下线 `analysis/` 与旧 `legacy_run_key` 成本链路，冻结 `rubric_evolution/`，前端统一请求缓存层与类型化 i18n。
5. **外部 Env 代码在 API 主进程内无沙盒执行**
   - `env_loader.py` 直接在 API 进程 `import` 外部 `core.py`/`scorer.py` 并修改 `sys.path`，与内存中的明文 `run_credentials` 共享进程空间，长期需隔离到子进程执行。

---

## 三、最值得做的优化（高 ROI）

1. **彻底消除异步事件循环上的同步 I/O 阻塞**：`_init_db_sync` 改为按 `db_path` 一次性初始化；`_open_sync` 自动关闭连接且仅启动时设置 WAL；将快照生成、Wire finalize、SQLite 读写和 JSONL 读取统一下沉 `asyncio.to_thread` 或 `aiosqlite`；移除 `skill_eval` `controls()` 每 200ms 一次的冗余同步续租写库。
2. **用事件驱动 + 增量缓存替换高频轮询 SSE 与 $O(N^2)$ 投影**：主站与 Skill Eval SSE 改用 `asyncio.Event`/pub-sub（终态立即关流），`/process` 按 `(run_id, head)` 缓存增量投影，前端在页面隐藏时暂停轮询。
3. **消除大文件双倍下载与整包内存缓冲**：修复 `skill_eval/artifacts.py` 每文件下载两遍的问题；上传/导出改用流式读写；修复 `SkillPackageEditor.tsx` 逐字节拼接 Base64 卡死主线程的问题；调度已实现但未接线的 `Store.collect_unreferenced`（补全 `previews/` 清理）。
4. **修复并恢复全量 CI 测试门禁**：修复 14 个前端测试的 `<I18nProvider>` 包装并把 CI 恢复为全量 `npm test`；后端 CI 加入 `ruff` 检查，并对可选 `blade_agent_kit` 依赖用例加 `pytest.importorskip`。
5. **清理所有无界进程级缓存与锁字典**：为 `_create_locks`、`_inbound_seq`、`_event_data_paths`、`Worker.signals`、`_ATTEMPT_RUN_CACHE`、`_FINALIZE_LOCKS` 等加上生命周期清理或有界 LRU。

---

## 四、最值得深入做下去的功能（战略取舍）

### 强烈建议押注的核心方向

1. **打通「Skill Eval 工作区」与「Core Octagon 实验矩阵」**
   - 让 `ExperimentBuilder` 直接以 `skill_eval_versions` 不可变快照作为评测对象，支持 **Skill v1 vs v2 × 多模型 × 多重复 × 自动 Rubric/Pairwise 评分 × Cost/Wire 观测**，并逐步扩展到多 Agent 共同评测同一个 Skill——打造行业稀缺的 **Skill 工程化评测与回归平台**。
2. **深化「ATIF 统一轨迹 + 根因归因 + Pairwise 比较评分」的差异可视化**
   - 补齐 `blade-agent` (SDK) ATIF 与 `kimi-code` Wire Native 解析以实现 7 家完全对等；在前端将“决策分叉点对比（Decision Fork Diff）”与“归因证据直达”升级为一等公民体验。
3. **深化「Iteration 多轮审阅返工」与「Experiments 鲁棒性/变异测试」**
   - 基于高质量的 `iteration/` 底座与修好竞态后的 `experiments/`+`mutations/`，输出面向工程决策的统计视图（Pass@k、重复方差、提示词扰动敏感度、成本-质量 Pareto 前沿）。
4. **全链路「真实账单 vs 估算成本对账」+「提交前 Preflight 体检」**
   - 补齐 `X-Octagon-Turn-Id` 在各 Adapter 的注入以支持按轮次定位 Token/费用峰值；推出 `POST /api/runs/preflight` 把环境依赖、沙盒状态、磁盘空间、模态兼容性前置到提交前检查。

### 建议冻结或做减法的方向

- **冻结 `rubric_evolution/`（5,318 LOC）**：在具备真正可执行、可验真的动态 Rubric 评分执行器之前停止投入（当前无法激活环境 rubric 且离线回放存在金标自证缺陷）。
- **下线/合并 `analysis/`（1,193 LOC）**：完全未接线且与 `insights/`、`attribution_client.py` 职责重叠。
- **简化 `profiles/` + `feedback/learning.py`**：移除围绕 4 条静态规则过度包装的 epoch/watermark/EMA 学习机制，回归清晰的静态预设配置。

---

## 五、建议落地节奏

| 阶段 | 周期 | 重点目标 |
|---|---|---|
| **阶段一：安全与止血** | 第 1 周 | 修复 `_copy_uploads` 任意文件读与密钥泄漏、Rejudge 先删后检、Anthropic SSE/CC 2× Token、Skill Eval 多轮 SQL、`RunGroupLive` 死循环、Leader outbox 与 Skill Eval Worker 崩溃；恢复全量前端单测进 CI |
| **阶段二：长稳与去债** | 第 2–3 周 | 修复 `_open_sync` FD 泄漏 + `BEGIN IMMEDIATE` + `_init_db_sync` 热路径；同步 I/O 全面移出事件循环；修复 Sweeper 取消评分任务；归并 LLM Client / JSON 解析并下线死代码；拆分 `RunDetail.tsx` 共享 Viewer/Wire/Cost 模块 |
| **阶段三：护城河发力** | 第 4 周起 | 确立 Canonical Trajectory 单一事实源（消除 6 套解析重复）；将 `scoring_jobs` 拆为独立 worker 进程；打通 Skill Eval 快照 → Core Experiments 矩阵、自动评分与成本/轨迹观测 |
