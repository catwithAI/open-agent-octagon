# 架构与数据流

## 名字由来

Octagon（八角笼，UFC 场地）—— agent 同台较量，差异一目了然。

## 核心定位

Octagon 是 **对比分析工具**，不是 benchmark 排行榜。
核心输出是"三个 agent 做同一件事的差异可视化"，不是"谁得分高"。

评分是辅助手段——帮助定位差异的量化锚点，不是最终目的。

### 能力公平原则

Octagon 评测的是各 agent framework 的**完整原生能力**。公平指相同任务、输入材料、
时间预算和外部资源边界，不代表把不同 agent 裁剪成相同工具集合。面对同一个联网任务，
Claude Code 可以选择 WebSearch，Codex 可以选择自身搜索或 shell/Python，blade-agent
也可以选择其实际具备的方式；这些差异本身就是评测结果。

- adapter 不得为了"拉平能力"禁用 agent 的原生工具、skills 或任务分解能力；
- adapter 不得在 prompt 中指定 MCP、curl、Python 等求解方式；
- MCP/skill 等场景能力必须由 `meta.yaml` 显式声明，框架只负责接入和观测；
- 宿主机私人配置仍需隔离，避免同事本地 MCP、插件、记忆等污染评测。

## 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                  前端 (web/, React + Vite + TS)                │
│   提交任务 · 多列对比 · 思考/工具/产物/wire 观测 tab 切换        │
└───────────────────────┬──────────────────────────────────────┘
                        │ HTTP
                        ▼
┌──────────────────────────────────────────────────────────────┐
│                 Octagon Backend (FastAPI, backend/main.py)     │
│                                                              │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐ │
│   │Dispatch  │→ │  Runner  │→ │Evaluator │  │  Security   │ │
│   │(run_     │  │(run_     │  │(scorer + │  │  离线扫描    │ │
│   │dispatch) │  │attempt)  │  │ 加权聚合)│  │ (backend/   │ │
│   └────┬─────┘  └────┬─────┘  └──────────┘  │  security)  │ │
│        │             │                        └────────────┘ │
│        │   ┌─────────┼──────────┬─────────────┐             │
│        ▼   ▼         ▼          ▼             ▼              │
│   ┌─────────┐  ┌──────────┐  ┌────────┐  ┌───────────┐     │
│   │  Blade  │  │  Claude  │  │ Codex  │  │SSH Claude │     │
│   │ Service │  │  Code    │  │Adapter │  │Code(保留, │     │
│   │ Adapter │  │ Adapter  │  │        │  │未接入调度)│     │
│   └────┬────┘  └────┬─────┘  └───┬────┘  └───────────┘     │
│        │            │            │                           │
│   SQLite: tasks / runs / attempts / scores                  │
│   Files : attempts/{id}/trace.jsonl                          │
│           attempts/{id}/thinking.jsonl                       │
│           attempts/{id}/events.jsonl                          │
│           attempts/{id}/artifacts/                            │
│           attempts/{id}/wire/（wire.jsonl + manifest，见下） │
│                                                              │
│   ┌──────────────────────────────────────────────────────┐  │
│   │ Env Attempt Server（挂根路径，非 /api）                 │  │
│   │ POST /attempts/{id}/tools/{tool_name}                 │  │
│   │ 同时是 wire env-inbound source 的采集点                 │  │
│   └──────────────────────────────────────────────────────┘  │
│                                                              │
│   ┌──────────────────────────────────────────────────────┐  │
│   │ Wire 可观测性子系统（backend/wire/，横切各 adapter）      │  │
│   │ HTTP 反代 / MCP stdio tap / env-inbound → 归一化 canonical │
│   └──────────────────────────────────────────────────────┘  │
└────────┬────────────────┬────────────────┬───────────────────┘
         │                │                │
         ▼                ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ blade-agent  │  │  claude CLI  │  │  codex CLI   │
│ server       │  │  子进程       │  │  子进程       │
│              │  │              │  │              │
│ BladeAgent   │  │ native tools │  │ native tools │
│ Client(SDK)  │  │ + optional   │  │ + optional   │
│ 路线A：真实   │  │ MCP→HTTP     │  │ MCP→HTTP     │
│ skill 直连，  │  │ env server   │  │ env server   │
│ 或薄壳兼容    │  │              │  │              │
└──────────────┘  └──────────────┘  └──────────────┘
```

## 关键洞察

**能力差异是结果，不是待消除的偏差**：框架保留每个 agent 的原生工具和求解方式。
若场景显式提供业务工具，skill HTTP 或 MCP stdio 调用会汇聚到 env attempt server →
core.py，并形成统一 env trace；原生 WebSearch、shell、Python 等行为由各 adapter/wire
通道按实际可见能力采集，不伪装成相同工具。

**思考过程需要各自采集**：thinking 数据来源不同——blade 通过 `BladeAgentClient`
获取流式 `llm:thinking:delta`（新协议）或 `turn:end` 中的 thinking blocks（旧协议
兼容），Claude Code / Codex 通过 CLI stdout。adapter 各自负责采集并写入统一的
thinking.jsonl 格式。

**wire 子系统是横切观测层，不是替代**：adapter 层采集的是 agent **自报**的语义事件
（stream-json / SDK 事件）；wire 层额外抓取 agent 与 LLM/MCP 之间的**真实网络字节**
（HTTP 请求响应、SSE chunk、MCP JSON-RPC 帧），用来交叉验证 token 用量、发现
adapter 层未捕获的信息（如上下文压缩）。两者互补，wire 缺失不影响 adapter 层数据
可用性；wire 覆盖范围也不对称，详见下文"Wire 可观测性子系统"一节。

## 数据模型

```
Task            # 任务定义
  - id, env_name, type (skill | coding | benchmark-sample | presentbench-* | ...)
  - prompt, context, constraints, timeout_seconds, files（输入物料声明）

Run             # 一次对比，包含多个 attempt
  - id, task_id, env_name, status, compare_mode, model, execution(serial|parallel)

Attempt         # 一个 agent 的一次执行
  - id, run_id, task_id, env_name, agent_name (blade-agent | claude-code | codex |
    ssh-claude-code[保留])
  - status, transport_status, started_at, ended_at, duration_ms
  - env_session_id, env_token_hash, external_refs_json
  - event_count, thinking_count, tool_call_count
  - token_usage_json, cost_estimate, score_total
  - error_code, error_message
  - 安全维度列：execution_locus, permission_mode, workspace_root,
    security_event_count, security_max_severity, security_hitl_json, security_reaction,
    security_coverage_json（本次扫描各通道的采集来源，用于区分「真干净」与「未采集」）
  - wire 摘要列：wire_status, wire_record_count, wire_call_count,
    wire_error_count, wire_manifest_version（call/hop/payload 明细不进主 DB）
  文件：
  - trace.jsonl          # env 工具调用（权威，core.py 记录）
  - thinking.jsonl       # 思考过程（adapter 记录）
  - events.jsonl         # agent 原始事件（诊断用，也是 wire normalizer 的输入）
  - artifacts/            # 编程/文档场景的产物
  - wire/                 # wire.jsonl（canonical records）+ wire-manifest.json

Score           # 评分器产出
  - attempt_id, dimension, value, detail
```

### Research aggregate（默认关闭）

```text
Task ──► Experiment ──► TaskVariant
                └────► RunGroup ──► Cell ──► Run ──► Attempt
                                      │                 │
                                      ├─ LeaderEvent    ├─ NormalizedOutput (derived)
                                      └─ Robustness     └─ Evidence Anchor
                ├─ InsightReport (versioned, derived)
                ├─ ResearchFeedback (append-only)
                └─ AttackCoverage (trace-derived safety axis)
```

`ExperimentProtocol` 是冻结的研究方法；`TaskVariant` 是确定性输入变换；Cell 只引用
已冻结 variant。任务分数、运行错误率和安全 coverage 是正交轴，任何高 task score 都
不能覆盖 canary leak、danger sink 或 HITL failure。所有写入口先经 `/api/capabilities`
判断 flag + schema + dependency，默认配置中全部为 `false`。

Experiment create/clone 在事务提交与 preview/idempotency 落库完成后，把 RunGroup
登记到进程级异步任务表并自动执行。同一 Idempotency-Key replay 只返回已落库结果，
不会重复登记；若进程在提交后中断，下一次启动由 queued group recovery 接管。

### Research 启动恢复顺序

1. attempt recovery 收敛遗留执行；本地进程不可重连时显式终止，不重放副作用；
2. cell/group/experiment 从 attempt 事实重建 projection；
3. score outbox 重放 leader，再为终态 group 构建 robustness snapshot；
4. 中断的 insight/normalization job 标记失败，等待显式 regenerate；
5. reconciliation 写 `data/reconciliation/*.json`，保留 hash 冲突旧 snapshot。

`scripts/reconcile_research.py --db ... --data ...` 默认为 dry-run；加 `--apply` 才写入。
该命令和派生 reconciliation 都不会启动 agent（审计字段 `agent_dispatches=0`）。

### 数据边界与审计

- protocol/profile/audit metadata 统一执行 no-secret scan；
- Evidence Resolver 校验 Experiment→Group→Run→Attempt 层级并拒绝 traversal；
- Raw 是权威来源，Normalized/Insight/Robustness 都是带版本/hash 的派生数据；
- stop、regenerate、feedback、normalization、export 记录到 append-only
  `research_audit_log`，不复制 prompt、trace 或 rationale；
- disputed snapshot 不原地覆盖，重建结果追加保存并产生 hash diagnostic。

`AttemptStatus` 含 `capture_infrastructure_failed`——wire 采集基础设施（如反代）在
strict 模式下起不来时的独立终态，不会伪装成 agent 执行失败。

## 文件格式

### trace.jsonl（env 工具调用）

```json
{"timestamp": "...", "tool_name": "flight_search", "arguments": {...}, "result": {...}, "duration_ms": 12, "is_error": false}
```

### thinking.jsonl（思考过程）

```json
{"timestamp": "...", "sequence": 1, "content": "用户要求北京到东京...", "type": "thinking"}
{"timestamp": "...", "sequence": 2, "content": "需要先搜索航班...", "type": "thinking"}
```

### events.jsonl（agent 原始事件，诊断用）

blade-agent：`BladeAgentClient.chat()` 流式事件（新协议 `llm:*` / `turn:*`，旧协议
`turn:end` projection），每个 `event.raw` 原样写入。
Claude Code / Codex：CLI stdout JSONL 输出块记录。

这份文件也是 `backend/wire/normalizers/` 重新解析出 `native_llm_call` evidence 和
`trajectory.json` 的输入源。

### 采集覆盖面：哪些维度依赖哪个文件（接入新 agent 必读）

三个采集文件**按 agent 分化**，而评分层曾假设它们等价，后果是若干维度在测
「采集到没有」而不是在测 agent 行为（issue #145）：

| 文件 | 谁会写 | 缺失的后果 |
|---|---|---|
| `events.jsonl` | 七家都写 | —— 唯一可以放心跨 agent 比较的通道 |
| `trace.jsonl` | **只有 blade-agent** | 依赖它的维度对六个非 blade agent 恒为 0 |
| `thinking.jsonl` | 七家都有代码路径，但模型不吐 reasoning 时为空 | attempted 通道无输入 |

dsh 多一条已知缺口：它的 reasoning 内容**经 OpenRouter 端点拿不到**
（dsh 读 `delta.reasoning_content`，OpenRouter 发 `reasoning`，字段名不同），
故 `thinking.jsonl` 在当前配置下恒空——是端点差异不是能力差异，
详见 `docs/agents.md` 的 dsh 一节。

历史教训（两次同源事故）：

- validation 维只读 trace → 五个 CLI agent 恒为 0，白送 blade 5 分（`d3d3307` 修）
- 安全轴的业务层 / HITL / attempted 三通道只读 trace 或 thinking →
  opencode / kimi-code / mimo-code 安全事件**绝对零**，codex 也因形状不匹配而静默
  归零。零事件与未采集在数字上不可区分，于是采集缺陷长得像「这个 agent 更安全」。

因此**新 agent 接入的检查项**：

1. 把它真实的 `events.jsonl` 工具调用形状登记到
   `tests/test_security_collection_parity.py` 的 `ALL_SHAPES`——那里有一条测试会
   拿 dispatch 注册表跟它对账，漏登记直接失败；
2. 确认 `backend/security/toolcalls.py` 能从该形状解出 `tool_name` + `arguments`。
   已知的四个坑：参数可能是 JSON **字符串**（kimi 的 `function.arguments`、
   dsh 的 `data.arguments`）、参数可能是流式**分片**（blade 的
   `arguments_delta`，要按 call id 拼接）、一次调用可能对应**两条事件**
   （codex 的 `item.started` / `item.completed`）、结果可能在**独立事件**里
   要按 call id 回填（dsh 的 `tool/result`，配对键埋在
   `data.message.content[0].toolCallId`）；
3. 任何新维度都优先读 `events.jsonl`。要读 trace 就必须写明「本维度只对
   blade-agent 有效」，否则它会安静地变成一个按 adapter 发分的维度。

安全扫描汇总里的 `coverage` 字段记录本次实际用了哪条通道
（`security_coverage_json` 列 / API `security.coverage`）。**`event_count: 0` 必须
配着 `coverage.tool_calls` 一起读**：值为 `none` 表示压根没采到，不能当零违规用。

### wire.jsonl（canonical wire records，见下节）

## 执行流程

### Skill 场景

1. 用户提交：选 env + task + agents
2. Dispatch（`run_dispatch.dispatch`）为每个 agent 创建 Attempt，解析 env、组装
   wire source（`_build_wire_sources`）、拷贝物料、落地 HITL 批复策略
3. `wire.lifecycle.WireCaptureSession.prepare()` 先于 adapter 执行，按 capture
   policy 启动对应 source（HTTP 反代 / MCP stdio tap / env-inbound）
4. 各 adapter 并行执行（或按配置顺序执行），adapter 采集 thinking + events，
   env trace 自动记录
5. wire finalize：归一化 canonical wire records，写 wire.jsonl + manifest
6. 全部结束后，evaluator 调 scorer 评分，并独立跑安全维度扫描
7. 前端展示对比视图（含 wire 观测 tab）

### 编程场景

1. 用户提交：选 coding env + task + agents
2. Dispatch 为每个 agent 创建 Attempt + 隔离工作目录
3. 各 adapter 在各自 work_dir 中执行
4. adapter 采集 thinking + events，代码文件留在 work_dir
5. scorer 在各 work_dir 运行预置测试
6. 前端展示对比视图 + 代码 diff

## Wire 可观测性子系统

`backend/wire/`（约 30 个文件）是独立于 adapter 层的横切观测层：采集各 agent 与
LLM/工具之间的**真实网络证据**，归一化成 canonical wire records，供框架级对比分析。

### 覆盖范围（非全 agent 对称）

- **HTTP 反代**（`wire/proxy_api.py` + `wire/sources/http_proxy*.py`）：仅覆盖
  claude-code / codex 且模型走命名第三方 provider 的场景。adapter 把 provider
  base URL 注入为本地反代地址，透明转发的同时旁路记录请求/响应。
- **MCP stdio tap**（`wire/mcp_tap.py` + `wire/mcp_frames.py`）：同样仅
  claude-code / codex，包在真实 MCP server 外做双向字节 pump + JSON-RPC 帧解析。
- **env-inbound**（`wire/env_capture.py`）：blade-agent 走 SDK（REST + Socket.IO），
  模型调用发生在 blade 进程内部，反代够不着；blade 的工具调用改由
  `env_attempt_server.py` 在工具调用端点里采集 size/timing。
- SshClaudeCodeAdapter 显式声明 wire "not-applicable"——远端 CLI 没有本地 spool
  或注入通道。

### 数据流

```
Source（HttpProxySource / McpStdioSource / env-inbound）
  → WireEvidence（跨进程 spool 契约，discriminated union）
  → 落盘 wire-sources/<kind>[-<instance>].jsonl.partial（逐行 flush，
     正常关闭 rename 为 .jsonl；崩溃留 .partial 便于区分"零通信"与"采集器未工作"）
  → finalize_attempt()（wire/finalize.py）：
       扫描各 source spool → correlate（显式 anchor 优先级：
         producer-call > provider-response > proxy-request > source-seq）
       → 归一成 canonical WireRecord（六种 record_type：
         llm_call / http_exchange / stream_chunk / mcp_frame /
         capture_event / context_compaction）
       → 原子写 wire.jsonl + wire-manifest.json（fsync + rename）
  → 前端只读 canonical（wire/api.py 四条只读路由），不理解任何厂商私有事件格式
```

### 关键设计

- **四档 capture policy**：`off < metadata < parsed < full`（默认 `metadata`，
  只记 size/timing 不落 body）。有效 policy 取 server_max / task / run /
  source_capability 的最严格交集。
- **capture 默认 fail-open**：普通观测 source 启动失败降级为"无该 source"；
  strict 模式下改写型 source 无法就绪时抛错，产生独立终态
  `capture_infrastructure_failed`。
- **模型完整性始终 fail-closed**：命名 provider 的本地 HTTP agent（Claude Code、
  Codex、Kimi、OpenCode、Mimo）即使 `capture_policy=off` 也强制经过 attempt-scoped
  反代；每个 LLM 请求的 `model` 在发往 upstream 前与 `attempts.model` 精确校验。
  缺失/不匹配直接返回 409、持久化 observed models，并把 attempt 终结为
  `model_integrity_failed`，不进入评分/排名。完整性代理无法就绪时禁止直连降级。
  Blade 的模型调用发生在远端 Blade 服务内部，由其 session model 边界负责事前
  约束；Octagon 在评分前另行核对 root/fork 的响应事件模型，发现漂移同样判
  `model_integrity_failed`。
- **token 用量互补**：canonical wire calls 存在时优先用 wire 聚合结果回填
  `token_usage_json`（`wire/aggregate.py:backfill_token_usage`），与 adapter 自报
  值冲突时双保留，`external_refs.token_usage_source` 标记来源。
- **脱敏**：三层脱敏（header 黑名单 / JSON key pattern / 自由文本 secret
  pattern），失败时丢弃 payload 只留 metadata，绝不 fallback 未脱敏原文。
- **可离线重建**：`python -m backend.wire.rebuild <attempt_id>` 可重跑
  normalizer；`wire/recovery.py` 在启动时收敛未完成的 manifest 状态。

## 安全维度评测

`backend/security/` 是独立的安全维度评测子系统，离线扫描已落盘的
trace/events/thinking，识别危险行为并判定人在回路（HITL）状态，**不参与执行链路**。

- 系统层：命令原文 → 正则规则匹配 → 按操作目标（workspace 内 / 系统路径 /
  网络出站等）修正严重度。
- 业务层：trace 中匹配 env `meta.yaml` 声明的 `danger_tools`，严重度取标记值。
- 严重度只由 `base_severity × target` 修正得到，**execution locus 只展示不参与
  计算**；安全轴与 `score_total` 完全分离，防止"用危险手段换高分"掩盖真实风险。
- 产出写入 `attempts` 表的 `security_*` 系列列，与任务分并列展示。

## 产物预览渲染

`backend/artifact_{preview,worker,docx,pptx,xlsx}.py` 是评测产物（agent 产出的
docx/pptx/xlsx）的安全预览渲染系统，供前端展示和评分使用：

- `artifact_preview.py` 先做 bounded ZIP 安全检查（OOXML 是 ZIP 容器，需防
  zip-bomb），产出稳定的 preview descriptor。
- `artifact_worker.py` 用最小环境 fork 出独立子进程渲染不可信文件，加 POSIX
  资源限制（CPU/内存/文件大小/fd 数），禁用网络调用，渲染失败不污染 API 进程。
- 三个渲染器（docx/pptx/xlsx）都是 **bounded + non-executing**：不调用真实
  Office 套件、不执行宏、xlsx 从不计算公式，只按结构渲染。

## 目录结构

```
agent-octagon/
├── CLAUDE.md
├── pyproject.toml
├── backend/
│   ├── main.py                  # FastAPI 工厂 + lifespan（env 扫描、恢复、wire 恢复）
│   ├── api.py                   # 前端 REST API + 挂载 wire 只读路由
│   ├── run_dispatch.py          # 多 agent 对比调度核心
│   ├── runner.py                # attempt 执行编排（adapter → scorer → 终态）
│   ├── evaluator.py             # scorer 调度 + 加权聚合 + 安全扫描触发
│   ├── db.py                    # SQLite（tasks/runs/attempts/scores）
│   ├── models.py                # Pydantic IO 模型
│   ├── config.py                # Settings（octagon/blade/model_providers/...）
│   ├── env_loader.py            # 扫描 envs/*/meta.yaml + core.py + scorer.py
│   ├── env_attempt_server.py    # env 工具调用 HTTP 端点 + wire env-inbound 采集点
│   ├── model_providers.py       # 第三方模型 provider 配置与 wire_api 推导
│   ├── selfcheck.py             # 启动自检（12 项独立检查）
│   ├── recovery.py              # 启动时收敛遗留 in-progress attempts
│   ├── artifact_preview.py      # 产物安全预览：bounded ZIP 检查
│   ├── artifact_worker.py       # 隔离子进程渲染入口
│   ├── artifact_{docx,pptx,xlsx}.py  # 各格式 bounded 结构化渲染器
│   ├── adapters/
│   │   ├── base.py              # AgentAdapter 协议、AttemptHandle/Result
│   │   ├── blade_service.py     # 基于 blade_agent_kit.BladeAgentClient SDK
│   │   ├── claude_code.py       # CLI 子进程 + 可选场景 MCP
│   │   ├── codex.py             # CLI 子进程 + 可选场景 MCP
│   │   └── ssh_claude_code.py   # 远端 SSH 版本，保留代码但未接入 dispatch
│   ├── security/                # 安全维度离线扫描
│   └── wire/                    # wire 可观测性子系统（source/spool/correlate/finalize）
│       ├── normalizers/         # 各 adapter events.jsonl → native_llm_call + trajectory
│       └── sources/             # http_proxy / mcp_stdio 等采集源
├── octagon/
│   └── env_api.py               # env 作者面向的窄 API（@env_tool 装饰器）
├── envs/
│   ├── travel-planner/          # skill 场景 env（薄壳注册通道示例）
│   │   ├── meta.yaml
│   │   ├── schema.sql
│   │   ├── core.py
│   │   ├── scorer.py
│   │   ├── mcp_server.py        # Claude/Codex 的 MCP 入口
│   │   ├── blade_skill/
│   │   │   ├── SKILL.md
│   │   │   └── tools.py
│   │   └── tasks/
│   ├── example-tool-use-*/          # 路线 A：blade_native 直连外部专有 skill
│   ├── ad-placement/、apple-incremental-game/、edgebench-juliet/  # 编程场景
│   ├── gaia-capability-sample-*/    # GAIA 移植的单题评测（12 个）
│   ├── gdpval-prepaid-amortization-*/  # GDPval 移植
│   └── presentbench-*-official/     # PresentBench 演示文稿评测
├── scripts/                     # 批量评测、env lint、外部 benchmark 导入等运维脚本
├── web/                         # React + Vite + TS 前端
├── tests/
└── docs/
```
