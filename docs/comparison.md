# 对比方案设计

## 核心思路

Octagon 的价值不是给 agent 打分排名，而是**让不同 agent 的能力差异可见**。
同一个任务，多个 agent 的执行过程和产物放在一起，
用可复现的证据判断差异来自哪里。

最终产出不是一个总分，而是 **agent framework 级别的蒸馏**：

短期——从对比中定位各 agent 的能力差异：
- 哪些任务理解错了？
- 哪些工具调用策略比其他 agent 差？
- 哪些错误恢复能力不足？

长期——从对比中提炼可迁移的 framework 层模式：
- 工具选择策略：什么时候该调、调几次、参数怎么构造
- 多步规划：如何拆解复杂任务、何时回退
- 错误恢复：工具报错后的重试/替代/放弃决策
- 上下文管理：长对话中如何维持目标一致性

这些模式不是修单个 task 的 bug，而是可用于改进任意 agent framework 的
runtime loop / tool host / orchestrator / prompt engineering。

## 对比维度

### 1. 思考过程对比

| Agent | 思考数据来源 | 格式 |
|-------|-------------|------|
| blade-agent | `turn:patch` / `turn:end` 事件中的 thinking blocks + `GET /messages` 兜底 | JSONL |
| Claude Code | stdout 中的 `thinking` 输出（`--output-format json` 时结构化） | JSON blocks |
| Codex | stdout reasoning 输出 | 文本 |

关注点：
- 思考链长度：各 agent 是否过度思考或思考不足？
- 决策分叉点：面对同一个工具调用选择，三者的推理路径有什么不同？
- 错误认知：是否有 agent 基于错误假设做出决策？
- 回退与修正：遇到错误后，谁的恢复策略更好？

### 2. 工具调用对比

| Agent | 工具调用来源 | 格式 |
|-------|-------------|------|
| blade-agent | env trace（权威）+ `turn:end` 事件中的 `tool_calls` | JSONL |
| Claude Code | MCP tool call 日志 + env trace | JSONL |
| Codex | MCP tool call 日志 + env trace | JSONL |

关注点：
- 调用次数：谁更高效？重复调用是否合理？
- 参数准确性：同一个工具，参数填得对不对？
- 调用顺序：是否有更优的调用策略？
- 错误处理：工具返回错误后，下一步怎么做？

### 3. 最终产物对比

**Skill 场景**：

| 指标 | 采集方式 |
|------|----------|
| 业务结果正确性 | env DB final state + scorer |
| 约束遵守 | scorer 维度分 |
| 完成度 | scorer 维度分 |

**编程场景**：

| 指标 | 采集方式 |
|------|----------|
| 代码正确性 | 预置测试用例运行结果 |
| 代码质量 | 可选 LLM judge |
| 文件结构 | 文件系统 diff |

## 两类场景的 env 设计

### 场景 A：显式业务工具执行

场景可以显式给不同 agent 提供同一组业务函数的适配入口：

```
blade-agent → skill tools.py → HTTP → env attempt server → core.py
Claude Code → MCP server (mcp_server.py) → core.py
Codex       → MCP server (mcp_server.py) → core.py
```

这些业务工具的 env trace 可以统一记录，但 agent 仍保留各自原生能力。某个 agent
选择 WebSearch、shell 或其他内建工具而没有调用场景业务工具，也应作为真实策略差异展示，
框架不得通过提示词或 CLI flags 强迫三者走同一条路径。

要回答的问题：
- 各 agent 是否会正确理解 skill 能力边界？
- 各 agent 是否会多调、少调、错调工具？
- 某个 agent 的参数构造和错误恢复是否弱于其他 agent？
- 最终业务结果差异是否能从 trace 中解释？

### 场景 B：编程问题

新增 env 类型 `coding`，结构：

```
envs/coding-xxx/
├── meta.yaml          # type: coding；须含 name（与目录名一致）、schema_version: "1.0"
├── tasks/
│   └── task_001.json  # {id, prompt, test_cases, expected_files, timeout_seconds}
│                      # id/prompt 必填非空字符串（旧字段名 task_id/query/timeout 兼容）
├── tests/             # 预置测试用例
│   └── test_solution.py
└── scorer.py          # 运行测试 + 检查文件 → 分数
```

> env 契约由 `backend/env_loader.py` 运行时校验。

三个 agent 各自在隔离目录中工作：
- blade-agent：在 session workspace 中编写代码
- Claude Code：在 `--output-dir` 指定的目录中编写
- Codex：在 `--dir` 指定的目录中编写

scorer 拷贝预置测试到产物目录运行，统计通过率。

要回答的问题：
- 各 agent 是否能把同一自然语言输入转成正确代码修改？
- 某个 agent 的文件定位、编辑策略、测试使用是否弱于其他 agent？
- 最终产物失败时，失败原因是理解、实现、验证还是工具链？

## 数据模型扩展

现有数据模型基本够用，关键扩展：

```
Attempt（扩展字段）
  - thinking_log      # 思考过程文件路径（JSONL）
  - tool_calls_log    # 工具调用文件路径（JSONL，env trace）
  - artifacts_dir     # 产物目录路径（编程场景的代码文件）
  - agent_events_log  # agent 原始事件日志（诊断用）
  - token_usage       # {input_tokens, output_tokens, total_tokens}
  - duration_ms       # 总耗时
  - cost_estimate     # 预估花费（美元）
```

Run 模型不变——一个 Run 包含多个 Attempt（每个 agent 一个）。

## 对比视图设计

### 主对比页（RunDetail）

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
├───────────────┴───────────────┴───────────────┴─────────────┤
│ [思考过程] [工具调用] [最终产物]  ← tab 切换                   │
├─────────────────────────────────────────────────────────────┤
│ 三列内容，按时间线对齐或按步骤对齐                              │
└─────────────────────────────────────────────────────────────┘
```

### Tab: 思考过程

三列并排显示 thinking 内容，按时间或按决策步骤对齐。
重点高亮：分歧点（三者选择不同策略的位置）。

### Tab: 工具调用

时间线视图，三列对齐。每次调用显示：
- 工具名 + 关键参数
- 返回结果摘要
- 耗时

高亮：调用顺序差异、参数差异、错误恢复差异。

### Tab: 最终产物

Skill 场景：维度得分对比表 + 关键业务数据对比。
编程场景：代码 diff 视图 + 测试结果对比。

## 结果沉淀：从 run 到 framework 蒸馏

### 短期：per-run 结论

每次 run 结束后保留一条人工结论，格式尽量短：

```
run_id:
  capability_gap: "某 agent 参数构造遗漏预算约束"
  evidence: ["trace: 第 3 次 hotel_search 未传 max_price", "score: constraint_compliance 低 20 分"]
  likely_layer: "tool-planning"
  next_action: "补充 skill 工具选择训练样例"
```

ponytail: 先用一条文本结论，不做复杂标签体系；当结论数量多到难筛选时再结构化。

### 长期：framework 级蒸馏

当 per-run 结论积累到足够多（~20+ runs），开始提炼 framework 层模式：

| 蒸馏层 | 输入 | 产出 | 可改进的 framework 层 |
|--------|------|------|---------------------|
| **工具策略** | 三方 trace 对比中的调用序列差异 | 最优调用模式（何时调、调几次、参数构造规则） | tool host / skill prompt |
| **规划能力** | thinking 对比中的任务拆解差异 | 多步规划模板、回退触发条件 | orchestrator / mode spec |
| **错误恢复** | trace 中错误后的行为差异 | 重试/替代/放弃的决策树 | runtime loop / hook |
| **上下文管理** | 长对话中目标漂移的对比 | 关键信息保持策略 | compaction / memory |

蒸馏产出不是代码 PR，而是**可验证的行为规格**：
"在 X 条件下，agent 应该做 Y 而不是 Z"。
每条规格附带来源 run_id 和 trace 证据，可回归验证。

## M1 最小可对比的边界

M1 做到“三者同题可跑、结果可看”：
1. example-tool-use env（示例场景，已有）
2. blade adapter（已有代码基础）
3. Claude Code adapter（新增，CLI 子进程 + MCP）
4. Codex adapter（CLI 子进程 + MCP，先跑通最小参数）
5. 三列对比视图（blade vs Claude Code vs Codex）
6. 工具调用对比（env trace 天然对齐）
7. 基础 thinking / reasoning 采集（能展示即可）

M1 不做：
- 思考过程对齐算法
- 差异自动标注
- 成本统计
- 复杂标签体系

M2 再做：
- 编程场景 env
- 代码 diff 和测试结果对比
- 各 agent 能力差异清单的结构化沉淀

## 任务超时预算（测「单位时间能力上限」）

创建 run / 定义 task 时可设 `timeout_seconds`：

- **设正数**：把时间预算**告知 agent**，并在执行层强制超时。文案统一（三 adapter 共用 `time_budget_notice`）：告知总时长，并引导「先尽快产出可提交结果，再用剩余时间迭代优化，时间到即结束」。用于测 agent 在单位时间内能把分数逼到多高——对 EdgeBench 这类连续评分、越优化越高分的迭代型任务尤其有意义。
- **设 `null`（不限时）**：不注入任何时间约束（文案不出现），执行层也不设总超时（inactivity 看门狗仍生效，防真卡死）。作为「不限时」基线。

**注入通道的公平性说明（已知不对等，如实标注）**：时间预算语义上是「框架级约束」，理应走 system prompt。但三个框架的注入能力不对等：

- **Claude Code**：走原生 `--append-system-prompt`（干净的 system 通道）。
- **Codex**：`codex exec` 的 PROMPT 是唯一 instructions 入口，无独立 system 通道 → 回落 message **顶部**拼接。
- **blade-agent**：`ChatSendPayload` 只有 `message` 字段，无独立 system 通道 → 同样回落 message 顶部。

三者文案完全一致、位置都在最前，语义等价；差异仅在「system vs user-message 顶部」这一通道层级，源于框架本身能力差异，非评测设计取舍。分析同题对比时若涉及指令遵从度，需知悉这一点。
