# Agent 接入说明

Octagon 接入六个正式对比的 agent：blade-agent、Claude Code、Codex，以及
Kimi Code、opencode、MiMo Code。它们地位平等，adapter 接口统一，采集粒度对齐。
另有一个保留但未接入调度的 SSH 版 Claude Code adapter（见文末），供未来接入
真实远端机器时手动启用。

"地位平等"指相同任务与资源边界，不指相同工具集合。Octagon 保留各 agent 的原生
WebSearch、shell、Python、skills、subagent 等能力；能力差距是评测对象。adapter 只隔离
宿主机私人配置，不通过禁用原生工具或强制 MCP 来人为拉平能力。

## 接入方式总览

| Agent | 调用方式 | 工具入口（Skill 场景） | 思考采集 | 状态 |
|-------|----------|----------------------|----------|------|
| **blade-agent** | `blade_agent_kit.BladeAgentClient`（REST + Socket.IO SDK） | 路线 A：blade server 真实注册 skill；或 blade local skill 薄壳 → HTTP 回调 | 流式 `llm:thinking:delta`（新协议）/ `turn:end` blocks（旧协议兼容） | 生产使用中 |
| **Claude Code** | CLI 子进程：`claude` | 原生工具 + 场景可选 MCP | stream-json 中的 thinking 块 | 生产使用中 |
| **Codex** | CLI 子进程：`codex exec` | 原生工具 + 场景可选 MCP | 事件文本关键词启发式 | 生产使用中 |
| **Kimi Code** | CLI 子进程：`kimi -p`（print 模式） | 原生工具 + 场景可选 MCP（`$KIMI_CODE_HOME/mcp.json`） | `role=thinking` 行 | 已接入 |
| **opencode** | CLI 子进程：`opencode run --format json` | 原生工具 + 场景可选 MCP（config `mcp` 段） | `part.type=reasoning` | 已接入 |
| **MiMo Code** | CLI 子进程：`mimo run --format json` | 同 opencode（fork，契约同构） | 同 opencode | 已接入 |
| **SSH Claude Code** | SSH 远程调用 `claude` CLI | 场景 MCP（单个），HTTP 回调本地 env server | 同 Claude Code | 代码保留，dispatch 不自动选用 |

## 统一 Adapter 协议

```python
class AgentAdapter(Protocol):
    capabilities: AdapterCapabilities   # execution_locus / network_required / system_requires，
                                         # 仅静态展示，不驱动调度

    async def run(self, task: Task, env: EnvHandle, data_path: Path) -> AdapterResult:
        """
        执行一次 attempt，返回：
          - status / transport_status
          - thinking_log: Path（思考过程 JSONL）
          - agent_events: Path（原始事件日志，也是 wire normalizer 输入）
          - artifacts_dir: Path | None（编程/文档场景产物）
          - token_usage / duration_ms
          - error_code / error_message（失败时）
        """
```

（定义见 `backend/adapters/base.py`。）

env trace（工具调用）不由 adapter 返回——它由 env attempt server / MCP server
在 core.py 层统一记录，adapter 不需要关心。

三个 adapter 共用的工具函数：`kill_process_tree`（进程组杀，防子进程孤儿）、
`build_security_meta`（安全维度执行场合快照）、`prompt_context` /
`time_budget_notice`（保证时间预算等公共文案在三方渲染一致，见
[comparison.md](./comparison.md) 的"任务超时预算"一节）。

---

## blade-agent 接入

### 原则

- 不在 Octagon 进程内创建 `Engine`，不 import `blade_agent.host`
- 通过 `blade_agent_kit.BladeAgentClient` 调用已运行的 blade-agent server
- 不自己手写 Socket.IO —— SDK 已封装 REST + Socket.IO + 断线处理 + 并发订阅

### 实现现状

`backend/adapters/blade_service.py` 已完全基于 `blade_agent_kit.BladeAgentClient`
SDK 实现（约 1550 行），不是手写 httpx + socketio。httpx 仅剩一处兜底用途：
`GET /api/sessions/{id}/messages` 端点 SDK 未封装，直接用 httpx 调 REST。

`blade_agent_kit` 是 `pyproject.toml` 中声明的真实依赖，当前用**本地 editable
path source**（而非 PyPI 版）：

```toml
[tool.uv.sources]
blade-agent-kit = { path = "../blade-agent/agent-kit-python", editable = true }
```

原因：新流式协议 `turn:events` 只在本地仓库有，PyPI 发布版（≤1.0.30）不支持。
实际路径按各自环境调整，不提交绝对路径。

该依赖被声明为**可选**（`try/except ImportError`）：缺失时用占位类顶替，保证纯
展示 / 只跑 Claude Code / Codex 的部署不受影响，只有真正实例化
`BladeServiceAdapter` 才报错。

### 两种接入路线

- **路线 A（真实网页形态，推荐）**：session 通过 `primary_skill_id` /
  `solution_id` 指向 blade server 上**真实注册**的 skill，prompt 只传任务消息，
  工具引导交给 blade 侧真实 `AGENTS.md` / `SKILL.md`。目前接入外部专有 skill 的
  `example-tool-use-*` 等 env 走这条路线（`meta.yaml` 声明
  `entrypoints.blade_native`）。
- **薄壳兼容（历史通道）**：`sync_blade_skills()` 把各 env 的 `blade_skill/`
  目录同步到 `skills_path/octagon/<env>/`（通过 `BLADE_SKILL_PATHS` 环境变量），
  作为旧 env 的 skill 注册通道。`travel-planner` 等 env 走这条路线。

两种路线在 `meta.yaml` 里通过 `entrypoints.blade_native` 或
`entrypoints.blade_skill` 二选一声明，详见 [environments.md](./environments.md)。

### 已验证环境

已验证 `http://localhost:8020` 可作为默认 blade-agent server：

- `GET /api/health` 返回 ok
- `Authorization: Bearer sk-blade-v2-*` 可访问 `GET /api/auth/me`
- `POST /api/sessions` 可创建 session
- `POST /api/sessions/{id}/upload/.octagon` 可上传 `attempt.json`
- Socket.IO `auth={"token": api_key}` 可连接并 `session:subscribe`
- `DELETE /api/sessions/{id}` 可清理 session

Octagon 可以运行在本机、blade-agent server 所在主机，或其他安装 Claude Code / Codex 的设备上。
要求只有两点：

- Octagon 所在设备能访问 `blade.base_url`
- blade-agent 能访问 Octagon env attempt server，因此 `octagon.public_base_url` 必须写成
  blade-agent 可访问的地址，不能在跨机器部署时写 `127.0.0.1`

### 数据采集

| 维度 | 数据源 | 采集方式 |
|------|--------|---------|
| 思考过程（新协议） | `llm:thinking:delta` → `llm:response:done` | 流式增量拼段 |
| 思考过程（旧协议兼容） | `turn:end` | `event.raw["blocks"]` 中 `type="thinking"` 的块 |
| 工具调用（权威） | env trace | env attempt server 在 core.py 层自动记录 |
| agent 内部工具调用 | `turn:end` 事件 | `event.raw["tool_calls"]` 数组 |
| token 用量 | `turn:end` 事件 | `event.raw["usage"]` 字段（wire 层可交叉回填） |
| sub-agent 拓扑 | `loop_name` | 非 headless 模式下产出真实子 agent，按 `loop_name` 分桶统计 |
| 最终产物 | env DB + trace | scorer 读取 |
| 完整历史（断线补偿） | REST | `GET /api/sessions/{id}/messages`（httpx 直接调，非 SDK 公共 API），配合
  `_monitor_progress()` 轮询兜底 |
| 全部原始事件 | `client.chat()` 流 | 每个 `event.raw` 原样写入 events.jsonl |

新旧协议双路兼容处理，不依赖单一事件格式。

### 调用流程（概要）

```python
from blade_agent_kit import BladeAgentClient

async with BladeAgentClient(base_url, token=api_key) as client:
    session = await client.create_session(
        intent=f"octagon attempt {attempt_id}",
        primary_skill_id=..., # 或 solution_id（路线 A）
        model=model,
        enable_thinking=True,
        memory_enabled=False,
    )

    # 薄壳路线才需要上传 attempt.json；路线 A 直接发任务消息
    await client.upload_file(session.id, attempt_json_path,
                              dir_path=".octagon", remote_path="attempt.json")

    async for event in client.chat(session.id, prompt, headless=False):
        record(event.kind, event.raw)          # → events.jsonl
        collect_thinking_and_tool_calls(event)  # 新旧协议双路分支
        if event.kind == "chat:end":
            break

    # 断线补偿：拿完整历史（SDK 未封装，httpx 直接调）
    messages = await fetch_messages_fallback(base_url, api_key, session.id)

    await client.delete_session(session.id)
```

关键取舍：
- **`headless=False`**：解锁真实 sub-agent fork 拓扑，观测更贴近实际产品形态
- **不用 `client.headless.run()`**：它只返回最终结果，Octagon 要过程数据
- **双超时**：`inactivity_timeout_seconds`（无进展判卡死）+ `task.timeout_seconds`
  （总时限，`None` 则不限时）

### Prompt 模板

薄壳路线仍需在 prompt 中显式引导工具查询流程：

```text
你正在执行 Octagon 评测任务。请使用技能 octagon/{env_name} 完成任务。
先调用 ListSkillTools(skill_id="octagon/{env_name}") 查看工具参数，
再按需调用 RunSkillTool 执行业务操作。

任务：
{task.prompt}

上下文：
{task.context}
```

路线 A 下 `primary_skill_id` 已指向真实注册的 skill，prompt 通常只需任务本身，
不必重复引导工具查询流程。

### 认证

在 blade-agent web 界面或 `POST /api/user/api-keys/` 创建长期 API Key（`sk-blade-v2-*`），
配置到 `octagon.yaml` 的 `blade.api_key` 字段。

不走浏览器 cookie，不自签 JWT。

### 失败分类

`_classify_outcome()` 按 `chat_end_status` / `finish_reason` / `error_message` 分类：

| status | 触发条件 |
|--------|---------|
| `blade_service_unavailable` | `BladeAgentClient` 连接失败 |
| `auth_failed` | `BladeAuthError` |
| `session_create_failed` | `create_session()` 异常 |
| `chat_failed` | `ChatEnd.status == "failed"` 或 `BladeChatError` |
| `timeout` | 超过 task timeout（含 `BladeChatError("socket disconnected")`） |
| `completed` | `ChatEnd.status == "completed"` |

`error_code` 进一步细分（`_outcome_error_code()`）：
`transport_reconnect_exhausted` / `agent_inactivity_timeout` /
`agent_total_timeout` / `llm_stream_incomplete` 等。

### capabilities

`execution_locus = "docker-sandbox"`，`network_required = "local_service"`
（依赖本机/局域网已运行的服务，而非公网 API）。

---

## Claude Code 接入

### 原则

- 通过 `claude` CLI 子进程调用
- 保留 Claude Code 原生 WebSearch、Task、skills 等能力
- 仅当场景显式声明 `entrypoints.mcp` 时加载该 MCP stdio server
- 不修改 Claude Code 本身

### 调用方式

```bash
claude -p "{prompt}" \
  --output-format stream-json \
  --verbose \
  --model {model} \
  --max-budget-usd {N} \
  --dangerously-skip-permissions \
  [--mcp-config {path}] \
  [--append-system-prompt {budget_notice}]
```

逐行解析 stdout JSONL。无 MCP 声明时不传 `--mcp-config`；配置文件由 Octagon
动态生成，`command` 来自场景 `meta.yaml` 的 `entrypoints.mcp.command`，不是
adapter 根据 `env_name` 拼出的路径。

### 第三方模型 provider 路由

`parse_model_ref()` 解析 `"<provider>/<model>"` 前缀。命名 provider 时子进程
独立注入 `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`
（按 `auth_mode` 区分 bearer / api-key 两种 HTTP 认证协议）。

### 本地状态隔离

`CLAUDE_CONFIG_DIR` / `HOME` 指向 attempt 内独立空目录，避免读取宿主机
`~/.claude` 全局配置（记忆、插件、MCP）污染对比结果。

### mcp_server.py

需要向 agent 提供业务工具的 env 可以新增 `mcp_server.py`，并在 `meta.yaml` 中
显式声明：

```python
from mcp.server.fastmcp import FastMCP
import httpx, os

mcp = FastMCP("octagon-{env_name}")

ATTEMPT_ID = os.environ["OCTAGON_ATTEMPT_ID"]
ENV_TOKEN = os.environ["OCTAGON_ENV_TOKEN"]
BASE_URL = os.environ["OCTAGON_BASE_URL"]

@mcp.tool()
def flight_search(departure: str, destination: str, departure_date: str, ...) -> dict:
    """搜索航班。"""
    resp = httpx.post(
        f"{BASE_URL}/attempts/{ATTEMPT_ID}/tools/flight_search",
        headers={"Authorization": f"Bearer {ENV_TOKEN}"},
        json={...},
    )
    resp.raise_for_status()
    return resp.json()
```

关键：MCP server 和 blade skill tools.py 一样，通过 HTTP 调 env attempt server，
不直接 import core.py。这保证 env trace 统一记录，对比数据天然对齐。

### 数据采集

| 维度 | 数据源 | 采集方式 |
|------|--------|---------|
| 思考过程 | Claude stdout | `type=assistant` 消息中 `content[].type=="thinking"` 块 |
| 工具调用 | env trace（权威） | env attempt server 自动记录（与 blade 相同） |
| token 用量 | Claude stdout | JSON 输出中的 usage 字段（wire 层可交叉回填） |
| 最终产物 | env DB + trace / 文件系统 | scorer 读取 |

### wire 观测

Claude Code 是三个 adapter 中 wire injection 能力位最全的：`process_env` /
`llm_base_url` / `llm_headers`（合并进 `ANTHROPIC_CUSTOM_HEADERS`）/
`mcp_rewrites` 全部支持。仅当模型走命名第三方 provider 时才挂 HTTP 反代和
MCP stdio tap，详见 [architecture.md](./architecture.md) 的 "Wire 可观测性子系统"。

### 失败分类

`_classify_outcome()` 看最终 `result` 消息的 `subtype`：`budget → timeout`，
`success → completed`，`is_error → cli_error`。

### capabilities

`permission_mode = "--dangerously-skip-permissions"`，`execution_locus = "host"`。

---

## Codex 接入

### 原则

- 通过 `codex` CLI 子进程调用
- 保留 Codex 原生工具与求解能力
- 仅消费场景显式声明的 MCP；无声明时不生成任何 `mcp_servers.*` 配置

### 调用方式

```bash
codex exec --json \
  --skip-git-repo-check \
  --ephemeral \
  --ignore-rules \
  --dangerously-bypass-approvals-and-sandbox \
  [-c model_providers.<name>.xxx=...] \
  -C {workspace} \
  -o {final_message_path} \
  "{prompt}"
```

### 第三方模型 provider 路由

不修改全局 `config.toml`，而是每次调用用 `-c` 单次覆写命名 provider。

**关键约束**：Codex 只支持 `openai-responses` wire_api，非该协议的 provider 会
在 adapter 启动前 fail-fast（`ValueError`）——这是 Codex 特有的限制，与
Claude Code / blade-agent 不同，评测第三方模型时需注意。

### 本地状态隔离

`CODEX_HOME` 指向 attempt 独立空目录。

### 数据采集

| 维度 | 数据源 | 采集方式 |
|------|--------|---------|
| 思考过程 | stdout 事件 | 无专用字段，靠 `_looks_like_reasoning()` 对事件 JSON 做关键词启发式（比 Claude Code 弱） |
| 工具调用 | env trace（权威） | env attempt server 自动记录 |
| token 用量 | stdout 事件 | 事件字段（wire 层可交叉回填） |
| 最终产物 | env DB + trace / 文件系统 | scorer 读取 |

### wire 观测

`llm_headers` 注入通道未定义（静态 provider header 通道缺失），改走
`env_http_headers` 映射间接实现 capture token 注入。仅当模型走命名第三方
provider 时才挂 HTTP 反代和 MCP stdio tap。

### 失败分类

优先看 `turn.failed` 事件的 `error.message`（"权威失败事件"，优先级高于 stderr
尾行）。

### capabilities

`permission_mode = "--dangerously-bypass-approvals-and-sandbox"`。

---

## Kimi Code 接入

`backend/adapters/kimi_code.py`。以下契约以 **实测（kimi-code 0.29.1）** 为准
——与上游文档的声明有出入时以实机为准。

- **调用**：`kimi -p <prompt> --output-format stream-json -m <model>`。
- **事件流**：每行一个 `{"role": ..., "content": ...}`，**不是**带 `type` 的
  结构化事件。`role=assistant` 是正文，`role=meta` +
  `type=session.resume_hint` 携带 session ID。
- **多轮 resume**：`-r <session_id>`。**本版没有 `--session`**（`-S` 是交互式
  挑选）。禁用 `-c/--continue`：它挑该工作目录最近一次会话，并发多 attempt
  会串到别人的会话上。
- **权限**：`--auto` 与 `-p` **互斥**（CLI 直接报错）。print 模式本身即非交互
  自动执行，不需要额外 flag。
- **模型**：必须显式注册进 `config.toml`（`[providers.*]` + `[models."*"]` 且
  带 `max_context_size`），否则 `config.invalid` 直接退出。adapter 按 attempt
  生成这份配置，只注册本次要用的那一个模型。
- **MCP**：本版**没有 `--mcp-config-file`**；走 `$KIMI_CODE_HOME/mcp.json`，
  dialect（`mcpServers` + `command`/`args`/`env`）与 Claude Code 一致。
- **usage**：0.29 的 stream-json **不吐 token usage**，`token_usage` 留空
  （不填 0——那会被读成"真的用了 0 token"）。
- **失败**：致命错误只出现在 stderr、stdout 一行都没有，故非 0 退出时必须采信
  stderr，否则 attempt 会以"completed 但零事件"收场。

### capabilities

`permission_mode = "print-mode(-p)"`；`KIMI_CODE_HOME` 按 attempt 隔离。
wire 的 capture token 走 `KIMI_CODE_CUSTOM_HEADERS`（换行分隔 `Name: value`，
格式同 CC 的 `ANTHROPIC_CUSTOM_HEADERS`）。

---

## opencode / MiMo Code 接入

`backend/adapters/opencode_family.py`——**一个 adapter 承载两个 agent**。
MiMo Code 是 opencode 的下游 fork，CLI 契约逐字段同构（`run --format json`
的事件流、`sessionID`、`part.text`、`tokens`、`--session` resume、
`*_CONFIG`/`*_CONFIG_DIR` 环境变量），差异只有可执行文件名与环境变量前缀，
收敛在 `_FamilyProfile` 的三个字段里。新增同族 CLI 只需加一条 profile。

实测版本：opencode 1.18.5 / mimo 0.1.9。

- **调用**：`<cli> run --format json -m <provider>/<model> -- <prompt>`。
- **事件流**：`{"type": ..., "sessionID": ..., "part": {...}}`。正文在
  `part.text`（`type=text`），思考在 `part.type=reasoning`，用量在
  `part.tokens`（`input`/`output` 命名，**不是** codex 的
  `input_tokens`/`output_tokens`）。
- **多轮 resume**：`--session <id>`。禁用 `--continue`，理由同 kimi。
- **权限**：`run` 子命令**没有权限 flag**（opencode 的 `--auto` 不在 `run`
  上、mimo 两个都没有）；自动批准只能走
  `OPENCODE_/MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS` 环境变量，否则工具调用会
  挂在审批上直到超时。
- **provider/MCP**：CLI 没有等价于 codex `-c key=value` 的单次覆盖通道，
  配置文件是唯一注入点。adapter 按 attempt 生成一份 JSON，经
  `*_CONFIG` 环境变量单次注入，不碰宿主机 `~/.config/<cli>`。MCP dialect 是
  `mcp.<name> = {type:"local", command:[...]}`——`command` 是**含可执行文件
  本体的单个数组**，与 CC/codex 的 command+args 分开表达不同。
- **失败**：上游错误经 JSON 事件表达时进程**仍可能 0 退出**；顶层 error 事件
  没有 `part`，正文藏在 `error.data.message`。

### capabilities

`permission_mode = "<PREFIX>_DANGEROUSLY_SKIP_PERMISSIONS=1"`。wire 的
capture token 走 config 的 `provider.options.headers`。

---

## DeepSeek Harness（dsh）接入

`backend/adapters/dsh.py`——**唯一走 SDK 而非 CLI 子进程的 CLI 类 agent**。

实测版本：`deepseek-harness-sdk==0.1.0rc6`（macOS arm64）。

- **调用**：`DeepSeekHarness(...)` → `Session.run(prompt)`，stdio JSON-RPC。
  **不走** `dsh --profile headless`——那只把最后一条 assistant 文本吐到 stdout，
  没有结构化输出格式（能吐 JSONL 的 `headless-driver.ts` 被上游明确标注为
  "test infrastructure, not a supported CLI output format"）。
  `BENCHMARK.md` 指定的官方 benchmark 路线就是 Python SDK。
- **安装**：`uv pip install 'agent-octagon[dsh]'`。**不进主依赖**——它拖一个
  49.9 MiB 的 runtime-bin（自带 Node 的单文件 exe），在受限网络下下载可能相当慢。
  部署时要预留时间或预置离线 wheel。
- **可用性判据是 import 探测，不是 PATH**（没有可执行文件）。
- **事件流**：`{"type":"tool/call","seq":N,"time":<epoch ms>,"data":{…}}`。
  工具调用 `data.{name,callId,arguments}`；结果在独立的 `tool/result` 事件，
  配对键在 `data.message.content[0].toolCallId`；用量挂在
  `assistant/message.data.usage`。
- **插件组合按 attempt 生成**（`dsh_cordis.yml`）：SDK 默认的 8 行组合
  **没有文件读写工具**（`fs-local` 只是 provider，`tool-fs` 没挂），
  模型可见工具只有 bash/skill/jobs。adapter 补齐 tool-fs、tool-todo、
  subagent 三行、token-meter、compaction，显式**不挂** web 插件
  （其余六家无内建搜索）。
- **多轮**：同一 `Session` 对象跨轮复用（同 session_id），不需要 resume flag。

### 三个实测才发现、漏了就跑不通的点

1. **`max_tokens` 必须显式传**。`llm-deepseek`/pi-ai 的默认输出上限是 256000
   且**不按 contextWindow 夹紧**，打到任何上下文 < 256K 的模型直接 400
   `CONTEXT_WINDOW_EXCEEDED`。传 `None` 等于没传。
2. **`request_timeout_seconds` 必须显式传**。SDK 默认 `None` = 单次 JSON-RPC
   永不超时（`_request_raw` 在无 timeout 的 `queue.get()` 上死等）。实测踩过：
   initialize 卡住时，8 秒 deadline 的 attempt 跑了 5 分钟仍未返回。
3. **skill 隔离要三招齐上**：`DSH_HOME` + `DSH_AGENTS_HOME` 只挡 rank 400/500；
   Octagon 仓库那批 `.agents/skills`（40+ 个）是 rank 200，由 projectRoot
   （最近的 `.git` 祖先）推导，只能靠**在工作区放一个空 `.git`** 挡住。
   实测：裸跑注入 10919 字符的 skill catalog；三招齐上才是「无可用 skill」。

### capabilities 与差异

`permission_mode = "no-approval-plugin (sdk default)"` ——**不是**某个开关的
名字。SDK 路线下压根没有 approval/sandbox 插件，`DSH_PERMISSION_MODE` 只对
CLI profile 生效。工具无限制执行、无进程内边界，隔离完全靠进程/容器级手段。
如实记录而非伪造一个 `danger-full-access` 的等价物。

**没有取消通道**：SDK 只有 initialize/session_prompt/shutdown 三个 method，
core 的 `Agent.cancel()` 未暴露。超时只能 `close()` 硬杀（实测 0.05s、无残留，
阻塞线程抛 `TransportClosedError` 醒来）。**这意味着 dsh 的超时比其余六家更
粗暴——没有优雅收尾，超时那一刻的产物就是最终产物**，限时场景对比时需注明。

**冷启动 0.11s**（5 次中位数），故每 attempt 起停一个 runtime 进程，
与其余六家的隔离模型一致。

### 已知采集缺口

- **reasoning 内容**：**取决于模型，不是一律拿不到**。dsh 从上游响应的
  `delta.reasoning_content` 构造 reasoning 块；OpenRouter 对不同模型的回传
  字段不一致——
  - `deepseek/deepseek-r1`：只回 `reasoning` 字段（dsh 读不到），
    表现为 `reasoningTokens` 有值但 content block 只有 `text`、
    `thinking.jsonl` 为空；
  - `deepseek/deepseek-v4-flash-0731`：**会回 `reasoning_content`**，
    实测可拿到真实思考文本。

  所以"thinking 为空"要先看模型再下结论，不能当成 dsh 的固有缺口。
- **pi-ai route 的 usage 只有两维**：`inputTokens`/`outputTokens`，
  cache 与 reasoning 都不出现（`mapUsage()` 把 reasoning 折进 output、
  零值 cache 直接省略）。走 `llm-deepseek` 则四维都有。
- **子 agent 身份不可得**：子 session 事件只在 `RunResult.notifications` 里，
  root events 看不到，故 `events.jsonl` 拿不到归属字段。
- **`isError` 不反映命令退出码**：bash 非零退出是 `isError=False`，
  exit code 在结果文本里。`isError=True` 只在工具框架层失败时出现
  （如 `read` 不存在的文件 → `error={name:FsError, code:FS_NOT_FOUND}`）。

### MCP dialect

`{serverName, transport:"stdio", command, args, cwd, env, toolCallTimeoutMs,
failOnStartupError}`。三处不是逐字段直译：

- `serverName` 必须匹配 `[A-Za-z0-9_-]{1,32}` **且全局唯一**（重名是 plugin
  load 阶段硬失败）。Octagon 的 `octagon-<env_name>` 字符集合规但无长度上限
  ——实测 `octagon-agent-parallel-scheduling` 是 33 字符。adapter 用
  「保留 23 字符前缀 + sha1 前 8 位」做确定性映射，并检测映射后重名。
- `cwd` schema 是 `z.string().default('')` 且原样传给 spawn——**空串是非法
  路径不是"继承父进程"**，`None` 必须回退到工作区绝对路径。
- `failOnStartupError` 默认 `false` 会静默无工具，评测里必须设 `true`。

---

## Session 隔离

所有 agent 共享相同的数据隔离目标，但不裁剪成相同能力集合：

- 每个 attempt 独立工作目录
- 独立 env session id / env DB / trace
- 独立 env_token（attempt 结束后失效）
- agent 之间完全不可见对方的执行
- 不读取宿主机私人 MCP、插件、记忆和个人配置（Claude Code / Codex 各自独立
  `CLAUDE_CONFIG_DIR` / `CODEX_HOME`）
- 保留各 agent 产品自身的内建能力

## 编程场景特殊处理

编程场景不需要 MCP / skill 工具——agent 直接编写代码文件。

| Agent | 编程场景调用 |
|-------|-------------|
| blade-agent | SDK 创建 session 时传 `solution_id="app-dev"`，再用 `chat()` 发送题目 prompt，从 workspace 收集代码文件 |
| Claude Code | `claude -p "{题目}"`，在隔离工作目录中编写 |
| Codex | `codex exec "{题目}" -C {work_dir}` |

scorer 在 attempt 结束后：
1. 拷贝预置测试到 work_dir
2. 运行测试，统计通过率
3. 可选：diff 对比代码结构

---

## SSH Claude Code（保留代码，当前未接入调度）

`backend/adapters/ssh_claude_code.py` 实现了通过 SSH 在远程机器上运行 `claude`
CLI 的 adapter：prompt 走 SCP 文件上传（不拼进 SSH 命令行，防注入），MCP server
也上传到远端，通过 HTTP 回调本地 env attempt server。

**当前状态**：`run_dispatch.build_adapter()` 明确采用"所有 compare_mode 下
blade-agent / claude-code 都在本机跑"的统一策略，`SshClaudeCodeAdapter` **不再
由 dispatch 自动选择**，仅保留代码供未来接入其他真正的远端机器时手动启用。
历史上该 adapter 曾用于 `same_model` 对比模式打远端机器，该机器已停用，
`config.py` 中 `SameModelSection` 的相关字段也已标注为历史遗留，不应再填新地址。

与 `claude_code.py` 的区别：

- `capabilities.execution_locus = "remote-host"`，`system_requires = ("ssh",)`
- `wire_capture_capabilities` 返回 `{"wire": "not-applicable"}`——显式声明"不
  适用"而非"未实现"，因为远端 CLI 没有本地 spool 或注入通道
- 只支持单个场景 MCP server（声明多个会直接报错）

如需重新启用，需要在 `run_dispatch.build_adapter()` 中显式恢复选用逻辑，并确认
目标远端机器可用。
