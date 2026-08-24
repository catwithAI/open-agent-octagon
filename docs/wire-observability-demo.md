# 通信观测（wire observability）真实对比 demo

本文把一次**真实**的 same-model / 跨 Agent 对比固化成验收样例：**三套 Agent（blade-agent /
claude-code / codex）**跑同一任务，串行执行、`capture_policy=full`，任务里既有模型调用又有
MCP 工具调用和 Office 产物。目的是证明 wire 观测层能拿到**真实 prompt / response、
token / 调用数、时序、MCP 轨迹、上下文压缩、Office 产物**——而不是 mock 数据。

> **关于 blade-agent（ba）的一条关键前提**：ba 经 `BladeAgentClient` 调**已在运行的**
> blade server（REST + Socket.IO），模型调用发生在 blade server **内部**，**不经过 Octagon
> 反向代理**——所以 ba 这一路**没有 wire transport 正文**（真实 request/response body 采不到）。
> 但 ba 的**产物 diff、评分、native events（token / 调用数 / MCP 轨迹）照常采集**。三方都纳入
> 对比，ba 的"正文"列诚实标注为**不可用（不经反代）**，不假装有。只有走 Octagon 反代的
> **CC / Codex 第三方 provider** attempt 才有 transport 正文。

> ⚠️ 本文档不含任何 API key 或真实敏感 prompt。所有密钥都经 `api_key_env` 引用环境变量，
> payload 里的 prompt 用占位任务。真实 run 产物落在本地 `data/`（已 gitignore），仓库只留
> run ID 指针。

---

## 0. 覆盖范围与已知边界（先读这段）

**这次 demo 真实覆盖：**

- **blade-agent / claude-code / codex 三方**跑同一任务的产物 diff 与评分；
- **CC / Codex** attempt 的**真实 request / response 正文**（`capture_policy=full` + blob API
  开启）；**ba 无 transport 正文**（不经反代，见上方前提）；
- token 用量、调用次数、TTFT / duration、stream chunk（backend 派生）；
- MCP / tool 调用轨迹；
- 上下文 / compaction 观测（后端派生值）；
- PPTX / DOCX / XLSX 产物并排预览。

**这次 demo 明确不覆盖（诚实声明，勿当作已支持）：**

- **Codex 与 blade llm-gateway 不能共用同一 provider 端点。** Codex 新版只支持 OpenAI
  **Responses API**（`/v1/responses`），而 blade gateway 只暴露 `/v1/messages` 与
  `/v1/chat/completions`。因此"CC 和 Codex 走**同一个** blade-local provider"这条路走不通——
  除非另起一个支持 Responses 的兼容代理。本 demo 的做法：
  **CC 走 blade-local（anthropic 端点），Codex 走一个独立的、支持 Responses 的第三方 provider
  （如 openrouter）**。"同一个具名第三方 provider"这一条对 CC/Codex 只能各自成立，跨两者
  暂时做不到——这是当前 runtime 的真实边界，不在本演示的修复范围。
- **Codex native LLM anchor 缺失。** Codex 侧目前没有把它自己的 native LLM 事件锚定到
  wire http_exchange（CC 有、Codex 没有）。所以 Codex attempt 的 `logical_call_id`
  关联依赖反代侧 request_id，native 事件那一路是空的。RunDetail 上 Codex 的调用关联会明显
  比 CC 稀疏——这是真实缺口，不是 bug。
- 全量 TLS 解密、Office pixel-perfect 对比等不在本演示范围。
- Responses↔Chat compat source 是 **experimental API**，未接入真实转换器，本 demo
  不依赖它。

---

## 1. 前置配置（octagon.yaml）

复制 `octagon.yaml.example` 为 `octagon.yaml`（已 gitignore），关键段：

```yaml
octagon:
  data_path: "./data"
  public_base_url: "http://127.0.0.1:8100"   # 沙盒可达的回调地址

blade:
  base_url: "http://127.0.0.1:8020"          # 已运行的 blade-agent server
  api_key: ""                                # 走 BLADE_API_KEY 环境变量

# CC / Codex 的第三方 provider。key 不写明文，经 api_key_env 引用环境变量。
model_providers:
  blade-local:                               # 给 claude-code 用（anthropic 端点）
    kind: anthropic
    base_url: "http://127.0.0.1:30000"       # 本地 blade llm-gateway
    api_key_env: "BLADE_GATEWAY_API_KEY"
    custom_headers: "x-user-id: octagon"
  openrouter:                                # 给 codex 用（需 Responses API）
    kind: openai-chat
    base_url: "https://openrouter.ai/api/v1"
    api_key_env: "OPENROUTER_API_KEY"
    wire_api: "responses"
```

**开启 full 正文采集与 blob 下载**（默认都是关的，见 `backend/config.py`）：

```yaml
# octagon.yaml 顶层，或用环境变量
wire_capture_max_policy: "full"    # server 侧上限；不放开则请求 full 也被夹到 metadata
wire_blob_api_enabled: true        # 放开 parsed/full blob 下载 API
```

> `wire_blob_api_enabled` 默认 `false`：Octagon 当前没有用户级 auth，知道 run/attempt ID
> 就能调 blob API，所以正文下载默认禁用。**只在受控本地环境**放开它做 demo。
> 前端 RunDetail 在 blob API 未开启时会明确显示"内容不可下载（policy 门控）"，不会假装有正文。

环境变量（**不写进仓库**）：

```bash
export BLADE_API_KEY=...            # blade server 长期 key（sk-blade-v2-*）
export BLADE_GATEWAY_API_KEY=...    # 值同 blade.api_key
export OPENROUTER_API_KEY=...
```

---

## 2. 启动

```bash
# 后端
uv run uvicorn backend.api:app --host 0.0.0.0 --port 8100
# 前端
cd web && npm run dev        # 或 npm run build 后由后端托管 dist/
```

确认 blade-agent server 已在 `blade.base_url` 运行（本 demo 用本地容器化部署）。

---

## 3. 提交 payload（same-model / 跨 Agent）

前端「同模型对比（SameModelSubmit）」页对应的 `POST /api/runs` 请求体。**同一逻辑模型**在
CC 和 Codex 上各指定各自 provider 前缀（受第 0 节边界约束）：

```jsonc
{
  "env_name": "<带模型调用+MCP+Office产物的 env>",
  "task_id": "<该 env 下的 task>",
  "agents": ["blade-agent", "claude-code", "codex"],
  "compare_mode": "same-model",
  "execution": "serial",              // 串行：本地模型独占算力，避免互抢
  "models": {                          // same-model：per-agent 指定，必须覆盖所有 agent
    "blade-agent": "<model-id>",                  // ba 原生模型 ID（走 blade server，无反代正文）
    "claude-code": "blade-local/<model-id>",
    "codex": "openrouter/<model-id>"   // Codex 侧独立 provider（见第 0 节边界）
  },
  "capture_policy": "full",            // 落写盘前脱敏的协议原文；再与 server 上限求交集
  "timeout_seconds": 1000
}
```

> 让三方跑**同一个逻辑模型**：ba 用 blade 原生 ID，CC 用 blade-local provider 前缀
> 指向同一 blade gateway，Codex 因 Responses 边界（第 0 节）走独立 provider。三方模型逻辑一致，
> 但 ba 的 `capture_policy` 对它无 transport 正文——它照常产出产物/评分/native events。

前端选择器：执行方式选 **串行**，通信采集档选 **full**。页面会提示 full 仍受服务端
`wire_capture_max_policy` 夹取、且 blob 未开启时正文不可下载，并注明只有 CC/Codex 第三方
provider attempt 才有正文可采、ba 无 transport 正文。

> 若只想验证**完整正文**而不受 Codex Responses 边界干扰，把 `agents` 收成 `["claude-code"]`、
> `models` 只留 CC 那一项即可——这是最干净的 full-正文验证路径。

---

## 4. RunDetail 应看到什么

提交后跳 `RunDetail`（`/runs/<run_id>`），逐 attempt 核对：

| 观测项 | 来源 | ba | CC | Codex |
|---|---|---|---|---|
| 评分 / 产物 diff | verification + artifact | ✅ | ✅ | ✅ |
| 真实 request / response 正文 | wire full blob | ❌ 不经反代 | ✅ | ✅（反代侧） |
| token 用量 / 调用次数 | native events + wire | ✅ | ✅ | ✅ |
| TTFT / duration / stream chunk | wire timing（backend 派生） | ⚠️ 仅 native 侧 | ✅ | ✅ |
| MCP / tool 轨迹 | MCP stdio tap / blade events | ✅ | ✅ | ✅ |
| 上下文 / compaction | backend 派生 | ✅ | ✅ | ⚠️ 视 Codex 事件而定 |
| `logical_call_id` 关联密度 | correlate union-find | — 无反代 hop | ✅ 稠密 | ⚠️ 稀疏（native anchor 缺失） |
| PPTX / DOCX / XLSX 并排 | artifact preview | ✅ | ✅ | ✅ |

**诚实标注**：
- **ba 无 wire transport 正文**（不经 Octagon 反代）——它的"真实正文"和"反代侧关联"两列为空，
  但产物/评分/token/MCP 轨迹照常。这是接入方式决定的边界，不是缺陷。
- **Codex** 的 native LLM anchor 缺失，调用关联明显比 CC 稀疏。
- compaction 曲线是 backend 派生值，前端只做"后端派生"展示，不在前端重算。

---

## 5. 验证是真实数据（非 mock）

- wire evidence 里**不含任何 credential / env_token**——`capture_policy` 脱敏在**落盘前**做，
  token 走 Codex 子进程环境继承、**不上命令行**（安全 gate，见 `backend/adapters/codex.py`
  与 `tests/test_t17_codex_adapter.py::test_env_token_never_on_command_line`）。
- 跨协议 `semantic_hash` 相等可证明"同一逻辑调用"的两跳。
- full blob 落在 `data/<run>/<attempt>/wire-blobs/`，RunDetail 正文直接读它——不是构造值。

---

## 6. 本次真实 run 记录（跑完回填）

> 跑完后把真实 run ID 填这里，供复现。**只填 ID，不贴含敏感 prompt 的正文。**

- 日期：`<YYYY-MM-DD>`
- env / task：`<env_name>` / `<task_id>`
- 模型：ba=`<model-id>`，CC=`blade-local/<model-id>`，Codex=`openrouter/<model-id>`（同一逻辑模型）
- `capture_policy`（requested / effective）：`full` / `<server 夹取后>`
- run_id：`<run_xxxx>`
- 结论一句话：`<ba / CC / Codex 在本任务上的关键差异>`
```
