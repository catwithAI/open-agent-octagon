# 环境与任务规范

## 核心理念

1. **环境即黑盒**：每个环境是独立的仿真业务系统或编程题集。
2. **能力由场景声明**：场景可提供业务工具，也可只给任务和材料；框架不推断、更不强制工具。
3. **任务即输入**：任务是一段自然语言指令 + 上下文 + 预期约束。
4. **评分即程序**：scorer 读取执行后状态，计算分数。
5. **Session 隔离**：每次 attempt 独立，数据互不干扰。

## 两类环境

### Skill 环境（type: skill）

agent 通过工具与环境交互，完成业务任务。

```
envs/<env-name>/
├── meta.yaml
├── schema.sql          # env DB schema
├── core.py             # 业务核心，唯一副作用源
├── scorer.py
├── blade_skill/        # blade-agent 入口
│   ├── SKILL.md
│   └── tools.py
├── mcp_server.py       # 可选；显式声明后供支持 MCP 的 agent 使用
└── tasks/
    └── task_001.json
```

### 编程环境（type: coding）

agent 直接编写代码，不需要工具调用。

```
envs/<env-name>/
├── meta.yaml           # type: coding
├── tasks/
│   └── task_001.json   # {prompt, expected_files, timeout}
├── tests/              # 预置测试用例
│   └── test_solution.py
└── scorer.py           # 运行测试 + 检查文件
```

## meta.yaml 规范

每个场景都必须提供以下展示元数据：

- `category`：场景的一级能力分类，只取下表中的固定值。
- `test_focus`：一句话说明主要测什么，优先写能力、关键约束和判分重点。
- `description`：说明任务背景、输入/产物和评分方式，不重复目录名。

一级分类保持少而稳定；学科、行业或 benchmark 名称放在 `type`、描述或后续
`tags` 中，不作为一级分类。场景页按以下分类展示：

| category | 展示名称 | 范围 |
|---|---|---|
| `general-assistant` | 通用助理 | 检索、文件阅读、多模态理解和开放问题求解 |
| `office-productivity` | 办公与内容生产 | 表格、会计材料、演示文稿和多来源业务信息处理 |
| `real-skill` | 外部专有 skill | 接入外部专有 skill 的确定性计算链 |
| `complex-workflow` | 复杂长链路 | 多步工具编排、规划、产物生成和错误恢复 |
| `coding` | 编程与算法 | 代码实现、算法优化、静态分析和隐藏测试 |
| `agent-system` | Agent 系统能力 | 多轮记忆、上下文压缩、子 agent 和可观测性 |
| `safety-hitl` | 安全 · 人在回路 | 高后果或不可逆操作前的确认与安全替代 |
| `baseline` | 基础 · 约束遵守 | 基础工具使用和明确用户约束的遵守 |

### Skill 环境

```yaml
name: travel-planner
type: skill
category: baseline
test_focus: 在预算、日期和偏好约束下完成旅行预订
description: 旅行规划环境，测试 agent 的多步决策和约束遵守

entrypoints:
  blade_skill:
    skill_id: "octagon/travel-planner"
    source_dir: "blade_skill"
    install_mode: "copy"
  mcp:
    enabled: true
    transport: stdio
    name: "octagon-travel-planner"  # 可选；未填时默认 octagon-<env-name>
    command: ["uv", "run", "--project", ".", "python", "envs/travel-planner/mcp_server.py"]

dimensions:
  - name: task_completion
    weight: 40
  - name: constraint_compliance
    weight: 30
  - name: data_accuracy
    weight: 20
  - name: efficiency
    weight: 10

pass_threshold: 60
```

### 能力入口规则

`entrypoints` 是场景能力的唯一事实来源，目录里碰巧存在某个文件不等于启用对应能力。

- 未声明 `entrypoints.mcp`，或 `enabled: false`：框架不生成 MCP 配置、不启动
  `mcp_tap`，也不向 prompt 添加 MCP 文案；即使目录中存在 `mcp_server.py` 也不会推断。
- 声明 `entrypoints.mcp.enabled: true`：`command` 必须由场景完整提供。框架把声明翻译给
  agent CLI，并可在启用 wire capture 时透明包装该命令，但不会替换或创造 server。
- `entrypoints.blade_skill` / `blade_native` 只提供给对应 agent。Blade 专用
  `SKILL.md` 不会自动复制给 Claude Code 或 Codex；共享说明应通过公共场景材料声明。
- 任务可以声明目标能力和资源边界，例如“允许联网”或“不允许联网”；除非任务本身就在
  评测某种协议，否则不应强制 agent 使用 WebSearch、curl、Python 或 MCP 中的某一种。

例如，一个没有业务工具的开放网页问答场景无需声明 `entrypoints`。各 agent 使用自身
实际具备的联网方式完成任务，能力差异由 Octagon 如实记录。

### 编程环境

```yaml
name: two-sum
type: coding
category: coding
test_focus: 实现基础数组算法并通过正确性、质量和效率检查
description: LeetCode 经典题，测试基础编码能力

dimensions:
  - name: correctness
    weight: 60
    description: 测试用例通过率
  - name: code_quality
    weight: 25
    description: 代码可读性和结构
  - name: efficiency
    weight: 15
    description: 时间复杂度

pass_threshold: 60
```

## Task JSON 规范

### Skill 任务

```json
{
  "id": "travel_001",
  "env_name": "travel-planner",
  "prompt": "我要2月16日从北京出发，东京五日游，预算1.5万",
  "context": {
    "current_date": "2026-02-10",
    "user_info": {"name": "张三", "passport": "E12345678"}
  },
  "constraints": {
    "budget": 15000,
    "start_date": "2026-02-16",
    "duration_days": 5
  },
  "timeout_seconds": 600
}
```

### 输入物料（files）

任务需要输入文件（视频、文档等）时用顶层 `files` 声明，**不要**在 prompt 里
写文件的宿主机路径让 agent 自己找——agent 通常跑在沙盒里看不到宿主机文件系统。

```json
{
  "id": "recap_001",
  "env_name": "recording-recap",
  "prompt": "你的工作目录里有一段录屏视频（文件名见上下文的「工作目录下的输入文件」）...",
  "files": ["inputs/pipeline-2min.mp4"],
  "timeout_seconds": 1200
}
```

- 每项是路径字符串或 `{"name": ..., "path": ...}`；相对路径先按 env 目录解析
  （推荐把物料放 `envs/<env>/inputs/`），否则按项目根解析。
- 加载时归一成 `context.uploaded_files`，dispatch 时落到各 agent 的 workspace：
  本地 agent（claude-code / codex）直接拷贝，blade-agent 走 upload API，
  ssh 远端走 scp。agent 在工作目录用文件名即可访问。
- 物料缺失时 attempt 直接失败（`missing_uploaded_file` / `material_upload_failed`），
  不会带着空 workspace 起 agent。

### 编程任务

```json
{
  "id": "twosum_001",
  "env_name": "two-sum",
  "prompt": "实现一个函数 two_sum(nums, target)，返回数组中两个数之和等于 target 的下标。\n\n示例：two_sum([2, 7, 11, 15], 9) → [0, 1]",
  "constraints": {
    "language": "python",
    "expected_files": ["solution.py"]
  },
  "timeout_seconds": 300
}
```

## Env Attempt Server 契约

blade skill tools.py 和 mcp_server.py 都通过 HTTP 调 env attempt server：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/attempts/{attempt_id}/tools/{tool_name}` | 调用 env 工具 |
| GET | `/attempts/{attempt_id}/trace` | 读取 env trace |
| GET | `/attempts/{attempt_id}/final_state` | 读取最终状态 |

请求头：`Authorization: Bearer <env_token>`

### env_token

1. attempt 创建时生成，明文写入 blade session 的 `.octagon/attempt.json`
   或通过环境变量传给 MCP server
2. 服务端校验 token + attempt 状态
3. attempt 进入终态后 token 失效

## blade_skill/tools.py 约束

- 不 import core.py
- 不写业务数据库
- 只读 `.octagon/attempt.json`，通过 HTTP 调 env attempt server
- 使用 `InjectedToolArg` 注入 `session_dir`

## mcp_server.py 约束

- 同样不 import core.py
- 通过环境变量获取 attempt_id / env_token / base_url
- HTTP 调 env attempt server
- 使用 FastMCP 暴露工具

**关键**：blade skill 和 MCP server 走相同的 HTTP 路径，env trace 天然对齐。
这只适用于场景显式提供的业务工具，不表示所有场景或所有 agent 都必须使用 MCP。

## core.py 工具注册

使用 `@env_tool` 装饰器注册业务函数：

```python
from octagon.env_api import env_tool

@env_tool(
    name="flight_search",
    description="搜索航班",
    parameters={...},
)
def flight_search(ctx, departure: str, destination: str, departure_date: str) -> dict:
    ...
```

`ctx` 由 Octagon 注入，包含 attempt_id、env_session_id、db、trace writer。

## Scorer 契约

```python
class Scorer(Protocol):
    def score(
        self,
        *,
        attempt_id: str,
        task: dict,
        env_db: Path,        # skill 场景
        trace: list[dict],   # skill 场景
        work_dir: Path,      # 编程场景
        final_state: dict,
    ) -> list[Score]:
        ...
```

评分基于副作用和产物，不读 agent 的 thinking。
三个 agent 共用同一个 scorer，保证评分标准一致。

## trace 格式

路径：`<octagon_data>/attempts/{attempt_id}/trace.jsonl`

每行：

```json
{
  "timestamp": "2026-04-28T12:00:00.000Z",
  "attempt_id": "att_123",
  "env_session_id": "env_456",
  "tool_name": "flight_search",
  "arguments": {},
  "result": {},
  "is_error": false,
  "duration_ms": 12
}
```

## 自由 prompt

POST /runs 不带 task_id 时，后端自动创建临时 Task（`adhoc_<uuid>`）并落库。

## TaskVariant mutation contract

历史环境未声明 `mutations` 时只支持 `baseline`。需要变异的环境必须在
`meta.yaml` 显式声明；runtime 与 `scripts/lint_env.py` 使用同一校验器：

```yaml
mutations:
  allowed: [baseline, spacing, letter-case, unicode-homoglyph]
  conditional:
    instruction-position:
      requires: [structured_instructions]
  forbidden: []
  scorer_invariant: true
```

surface mutator 仍要求具体 task 声明 `/prompt` mutable region，并为路径、URL、
JSON、代码标识符、精确输出及 canary 提供 protected spans；env allowlist 不会覆盖
task 级语义护栏。

扩展研究 fixture 时请同时遵循：

- [Mutator authoring](./research-mutator-authoring.md)
- [Profile authoring](./research-profile-authoring.md)
- [Attack fixture authoring](./research-attack-authoring.md)
