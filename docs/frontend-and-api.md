# 前端与 API

## 前端职责

提交对比任务 + 多列对比视图。核心价值是**让差异可见**。技术栈：React + Vite +
TypeScript（`web/src/`：`pages/` 页面、`api/client.ts` API 封装、`components/`
共享组件、`wire/` wire 曲线处理逻辑、`artifacts/` 产物预览面板）。

## 核心页面

### AppShell 与兼容路由

统一入口为 `Experiments / Runs / Scenarios / Profiles / System`。旧 `/`、
`/same-model`、`/multi-model`、`/runs/:id` 全部保留在 Quick run 导航中；关闭 research
capability 只降级新入口，不改变旧深链。Experiment 采用 `Overview → Live → Results →
Evidence/Insights` 渐进披露；RunDetail 继续保留 conversation/tool/trajectory/wire/security/
artifact。RunList 的 attempt 摘要由列表 API 批量返回，不再逐行请求详情。

### Research 页面

- `/experiments`：Experiment 列表；`/experiments/new`：协议 Builder + preview/create；
- `/experiments/:id`：Overview 与 RunGroup 导航；
- `/experiments/:id/groups/:gid`：权威 snapshot + SSE Live；
- `/experiments/:id/groups/:gid/results`：Robustness 与独立 Safety axis；
- `/experiments/:id/insights`：版本化陈述、limitations、Evidence Anchor 与反馈；
- `/profiles`：只读 Profile catalog；`/system`：capability 原因诊断。

前端研究 DTO 在 `web/src/api/researchTypes.ts` 做运行时校验。SSE reducer 忽略重复/
乱序事件，gap 时等待 reconnect snapshot；capability=false 时 mutation 在 `fetch` 前阻断。
Cell 表每页 100，timeline 最多保留 1000 条；Raw 默认，Normalized 必须按需生成。

### 1. Submit / MultiModelSubmit / SameModelSubmit（提交任务）

- `Submit.tsx`：单次提交，选 env + task（或自由输入 prompt）+ agents（blade-agent /
  claude-code / codex，至少选两个）
- `MultiModelSubmit.tsx`：固定 blade-agent，多模型对比（`compare_mode=multi-model`）
- `SameModelSubmit.tsx`：同一逻辑模型跨 agent 对比（`compare_mode=same-model`，
  per-agent 各自指定 provider 前缀）
- 可选配置覆盖（blade server 地址、model、capture_policy、执行方式 serial/parallel 等）
- 点"开打" → 跳转到 RunDetail

### 2. Scenarios（场景与标准）

- 展示 `envs/*/meta.yaml` 中的 `test_focus` / `description` / `dimensions` /
  `pass_threshold` / `prerequisites`，供提交前了解场景评分标准

### 3. RunList（历史列表）

- 表格展示历史 run
- 每行：task 摘要、参与 agents、各 agent 总分、状态、时间
- 按 env / agent / status 过滤

### 4. RunDetail（对比详情） ← 核心页面，也是前端最重的页面

顶部概况卡片：

```
┌─────────────────────────────────────────────────────────────┐
│ Task: 北京→东京五日游，预算1.5万                              │
├───────────────┬───────────────┬───────────────┬─────────────┤
│               │  blade-agent  │  claude code  │   codex     │
├───────────────┼───────────────┼───────────────┼─────────────┤
│ 状态          │  completed    │  completed    │  timeout    │
│ 总分          │  72           │  85           │  -          │
│ 耗时          │  45s          │  32s          │  >600s      │
│ Token         │  12.4k        │  8.2k         │  -          │
│ 工具调用次数   │  14           │  9            │  -          │
│ 预估成本       │  $0.12        │  $0.08        │  -          │
└───────────────┴───────────────┴───────────────┴─────────────┘
```

下方 Tab 区域：

#### Tab: 思考过程

三列并排，滚动同步。每个 thinking block 显示时间戳和内容。
M1 先做纯文本展示，M3 再做差异高亮和步骤对齐。

#### Tab: 工具调用

三列时间线，每次调用一个卡片：工具名、参数、结果摘要、耗时。
相同工具调用自动对齐，差异处标色。

#### Tab: 最终产物

Skill 场景：维度得分表 + 关键业务数据。
编程场景：代码文件树 + 内容 diff + 测试通过率。

#### Tab: 原始日志

agent 原始事件流，调试用。

#### Wire 观测（内嵌在 RunDetail，非独立 tab）

`RunDetail.tsx` 直接内联展示 wire 可观测性数据（详见
[architecture.md](./architecture.md) 的"Wire 可观测性子系统"一节）：

- wire 采集状态徽标（`wire_status` / `wire_record_count` / `wire_call_count` /
  `wire_error_count`）
- 上下文曲线可视化（`web/src/wire/curve.ts` 抽出共享的曲线分段/gap 处理逻辑）
- MCP 工具帧时间线、hop 详情展开
- blob 内容查看（受 `wire_blob_api_enabled` 门控，未开启时明确提示"内容不可
  下载"，不假装有正文）
- attempt 累计 token/调用量卡片

blade-agent 因不经反代，wire transport 正文列显示为"不可用（不经反代）"，
而非空白或错误——见 [wire-observability-demo.md](./wire-observability-demo.md)。

## 前端技术选型

- React + Vite + TypeScript
- 裸 CSS / CSS Modules
- `useState` + fetch
- react-router

## REST API

### Research API（均在 `/api` 下）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/capabilities` | flag/schema/dependency 投影 |
| POST | `/experiments/preview` | 无执行副作用的 variant/protocol 预览 |
| POST | `/experiments` | 使用 preview token + Idempotency-Key 创建，提交后自动异步执行；replay 不重复启动 |
| POST | `/experiments/{id}/clone` | 克隆冻结协议并自动异步执行；replay 不重复启动 |
| GET | `/experiments/{id}/groups/{gid}` | 权威 redacted snapshot |
| GET | `/experiments/{id}/groups/{gid}/stream` | 带 sequence 的 SSE |
| GET | `/experiments/{id}/groups/{gid}/robustness` | 后端聚合结果 |
| GET | `/experiments/{id}/attack-coverage` | 独立安全 coverage |
| POST | `/experiments/{id}/attack-coverage/rerun-preview` | forensic 草案，不执行 |
| GET/POST | `/experiments/{id}/insights...` | 版本列表/生成/反馈 |
| GET/POST | `/attempts/{id}/normalized...` | derived output 查询/生成 |
| GET | `/experiments/{id}/evidence/resolve` | metadata-only anchor resolver |

SSE 首条永远是 `event: snapshot`；后续 envelope 为
`octagon-run-group-event-v1`，客户端以 `sequence` 去重。API 不把 prompt/context 放入
snapshot 或 SSE。

### 任务管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/envs` | 列出所有环境 |
| GET | `/envs/{name}/tasks` | 环境下的任务列表 |
| GET | `/agents` | 可用 agent 列表 + 健康状态 |

### Run 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/runs` | 创建一次对比 |
| GET | `/runs` | 历史列表 |
| GET | `/runs/{id}` | run 详情（含所有 attempt 概况） |
| GET | `/runs/{id}/attempts/{aid}` | 单个 attempt 详情 |
| GET | `/runs/{id}/attempts/{aid}/thinking` | 思考过程日志 |
| GET | `/runs/{id}/attempts/{aid}/trace` | 工具调用日志 |
| GET | `/runs/{id}/attempts/{aid}/events` | 原始事件日志 |

### POST /runs

```json
{
  "env_name": "travel-planner",
  "task_id": "travel_001",
  "agents": ["blade-agent", "claude-code"]
}
```

也允许自由 prompt：

```json
{
  "env_name": "travel-planner",
  "prompt": "我要2月16日从北京出发，东京五日游，预算1.5万",
  "context": {"current_date": "2026-02-10"},
  "agents": ["blade-agent", "claude-code", "codex"]
}
```

### GET /runs/{id} 响应

```json
{
  "id": "run_123",
  "task": {"id": "travel_001", "prompt": "...", "env_name": "travel-planner"},
  "created_at": "...",
  "attempts": [
    {
      "id": "att_001",
      "agent_name": "blade-agent",
      "status": "completed",
      "score_total": 72,
      "duration_ms": 45000,
      "token_usage": {"input_tokens": 8000, "output_tokens": 4400},
      "tool_call_count": 14
    },
    {
      "id": "att_002",
      "agent_name": "claude-code",
      "status": "completed",
      "score_total": 85,
      "duration_ms": 32000,
      "token_usage": {"input_tokens": 5000, "output_tokens": 3200},
      "tool_call_count": 9
    }
  ]
}
```

### Attempt 详情响应

```json
{
  "id": "att_001",
  "run_id": "run_123",
  "agent_name": "blade-agent",
  "status": "completed",
  "score_total": 72,
  "scores": [
    {"dimension": "task_completion", "value": 90, "detail": "已预订往返机票和酒店"}
  ],
  "duration_ms": 45000,
  "token_usage": {"input_tokens": 8000, "output_tokens": 4400},
  "tool_call_count": 14,
  "thinking_summary": "共 12 个思考步骤",
  "final_state": {},
  "execution_locus": "docker-sandbox",
  "permission_mode": null,
  "security_event_count": 0,
  "security_max_severity": null,
  "wire_status": "ready",
  "wire_record_count": 0,
  "wire_call_count": 0
}
```

`security_*` 字段来自独立的安全维度离线扫描（`backend/security/`），与
`score_total` 并列展示、不参与其计算。`wire_*` 字段是 wire 子系统写入的摘要列，
明细通过上面的 wire 只读路由单独获取。blade-agent 因不经反代，`wire_record_count`
等字段通常为 0 或仅含 env-inbound 记录，前端需按此如实展示而非报错。

## Wire 观测只读路由

由 `backend/wire/api.py` 提供，挂载在 `backend/api.py:register_routes()` 中，仅四条只读路由：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/runs/{run_id}/attempts/{attempt_id}/wire` | canonical wire records 列表 |
| GET | `/runs/{run_id}/attempts/{attempt_id}/wire/manifest` | wire manifest（各 source 状态、record/call/error 计数） |
| GET | `/runs/{run_id}/attempts/{attempt_id}/wire/trajectory` | normalizer 产出的 trajectory.json |
| GET | `/runs/{run_id}/attempts/{attempt_id}/wire/blobs/{ref}` | 下载 content-addressed blob（受 `wire_blob_api_enabled` 门控，默认关闭） |

前端不理解任何厂商私有事件格式，只消费 canonical record。

## Env Attempt Server（内部 API）

给 blade skill tools.py 和 mcp_server.py 调用，不直接给前端，挂根路径（非 `/api`）：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/attempts/{attempt_id}/tools/{tool_name}` | 调用 env 工具（同时是 wire env-inbound source 的采集点） |
| GET | `/attempts/{attempt_id}/trace` | 读取 trace |
| GET | `/attempts/{attempt_id}/final_state` | 读取最终状态 |

必须校验 `env_token`（Bearer token，attempt 结束后失效）。

## 实时进度

M1 轮询：`GET /runs/{id}` 每 2s。
后续可加 SSE：attempt 状态变化、新工具调用、评分完成。

## Agent 健康检查

`GET /agents` 返回各 agent 的可用状态：

```json
{
  "agents": [
    {"name": "blade-agent", "status": "available", "base_url": "http://127.0.0.1:8020"},
    {"name": "claude-code", "status": "available", "cli_path": "/usr/local/bin/claude"},
    {"name": "codex", "status": "not_found", "detail": "codex CLI not in PATH"}
  ]
}
```

`ssh-claude-code` adapter 保留代码但当前不由 dispatch 自动选用，不出现在这里的
默认 agent 列表中（详见 [agents.md](./agents.md) 文末）。
