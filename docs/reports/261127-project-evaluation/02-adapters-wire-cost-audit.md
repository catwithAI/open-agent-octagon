# 02 — Adapters、Wire 可观测性、ATIF、Cost 与 Conversation 审计

> 审计日期：2026-11-27 · 审计基线：`9c4cfaf` · **状态更新**：2026-11-27（PR #36 已合入，PR #37 待合入）
>
> 状态图例：`✅ 已合入 (PR #36)` · `🟡 待合入 (PR #37)` · `🟡 部分完成` · `⬜ 未开始`
>
> 审计范围：`backend/adapters/`、`backend/wire/`、`backend/atif/`、`backend/cost/`、`backend/conversation/`、`backend/normalization/`。

---

## 一、具体 Bug 清单

### 1. [P0 统计失真] HTTP 代理在 Anthropic `message_delta` 上清空输入与缓存 Token — `✅ 已合入 (PR #36: T1.3)`
- **位置**：`backend/wire/sources/http_proxy.py:950-963`
- **根因**：`_UsageProbe._parse_sse_line` 写成 `if usage is not None: self.usage = usage`（整对象替换）。已改为按非 `None` 字段合并。

### 2. [P0 统计失真] Claude Code `result` 事件 Token 2 倍重复计数 & Kimi 成本双计缓存 — `🟡 部分完成`
- **位置**：`backend/adapters/token_usage.py:147-166`、`backend/cost/estimate.py:54-75`
- **状态拆解**：
  1. Claude Code `result_usage_tokens()` 2× 重复计数：`✅ 已合入 (PR #36: T1.3)`。
  2. `cost/estimate.py` 按来源传递 `input_semantics`（解决 Kimi 代理回填 inclusive `prompt_tokens` 重复计费）：`⬜ 待 Phase 3/5`。

### 3. [P0 统计失真] 不完整成本账本仍汇报偏低的 `total_cost_usd` — `✅ 已合入 (PR #36: T1.3)`
- **位置**：`backend/cost/ledger.py:387-423`
- **根因**：存在 `missing` 或 `judge_missing` 时未置 `total = None`。已修复。

### 4. [P1 代理资源泄漏] 客户端提前断连导致代理连接与 `inflight` 计数泄漏 — `✅ 已合入 (PR #36: T1.3)`
- **位置**：`backend/wire/sources/http_proxy.py:802-809, 1048-1092`、`backend/wire/proxy_api.py:134-138`
- **根因**：已为 `_stream_body` 与 `forward` 补独立 `try/finally` 释放 `inflight`，并按响应 `Content-Type` / SSE 帧准确判定 `streamed`。

### 5. [P1 沙盒失效] `opencode_family` 与 `dsh` 在 Docker 沙盒中透传宿主机路径与 `127.0.0.1` — `✅ 已合入 (PR #36: T1.3)`
- **位置**：`backend/adapters/opencode_family.py:398`、`backend/adapters/dsh.py:415, 419, 468, 475`
- **根因**：已统一改用 `sandbox.path(workspace)` 与 `sandbox.translate_url(task.env_base_url)`。

### 6. [P1 对等性缺陷] `dsh` 虚报 `iterative_session` & Blade SDK 版 ATIF 恒为空 — `🟡 部分完成`
- **位置**：`backend/adapters/dsh.py:277`、`backend/atif/converters/blade_agent.py:6-14, 74-80`
- **状态拆解**：
  1. `dsh.iterative_session = False`：`✅ 已合入 (PR #36: T1.3)`。
  2. `atif/converters/blade_agent.py` 兼容 `blade_service.py` SDK 格式：`⬜ 待 Phase 3 (T3.2)`。

### 7. [P1 会话泄漏与风暴] `blade_service.py` 异常跳过清理、恢复期 REST 风暴、自动应答无上限 — `⬜ 待 Phase 3 (T3.1)`
- **位置**：`backend/adapters/blade_service.py:1225, 1248-1294, 1434, 1447-1449, 1547-1555, 2270-2317, 2506-2510`、`backend/adapters/blade_cli.py:315-334`
- **说明**：排入 `T3.1` 拆解 `blade_service.py` 时一并彻底修复。

### 8. [P1 安全与句柄泄漏] `blade_cli.py` 凭据文件泄漏、Zip-Slip 绕过与 `ConversationTraceWriter` 泄漏 — `🟡 部分完成`
- **位置**：`backend/adapters/blade_cli.py`；`claude_code.py` / `kimi_code.py` / `opencode_family.py` / `blade_service.py`
- **状态拆解**：
  1. `blade_cli.py` 凭据文件 `0600`+清理、zip-slip `is_relative_to`、`isawaitable`、`ConversationTraceWriter` `finally`：`✅ 已合入 (PR #36: T1.1)`。
  2. 其余 4 个 Adapter 的 `ConversationTraceWriter` 句柄保护：`⬜ 待 Phase 3 (T3.1)` 抽 `BaseCliAdapter` 统一根治。

---

## 二、架构问题

1. **7 个 Adapter × 6 个 Wire Normalizer × 6 个 ATIF Converter 巨量代码复制** — `⬜ 待 Phase 3 (T3.1 / T3.2)`
2. **`blade_service.py` 巨石单体（3,555 LOC）** — `⬜ 待 Phase 3 (T3.1)`
3. **事件与 Token 语义缺乏统一契约** — `⬜ 待 Phase 3 (T3.2)`
4. **新旧两套并行成本系统** — `⬜ 待后续清理`

---

## 三、优先优化建议

1. **消除 Wire 热路径的事件循环阻塞与统一 `_open_sync`** — `🟡 待合入 (PR #37: T2.1 / T2.3)`
2. **修复 4 个 CLI Adapter 的 `stderr` 管道 64KB 死锁** — `⬜ 待 Phase 3 (T3.1)`
3. **控制进程级缓存增长（`_sealed` / `_ATTEMPT_RUN_CACHE` / `_FINALIZE_LOCKS` / `_EXPECTED_CACHE`）** — `🟡 待合入 (PR #37: T2.1 / T2.3)`
4. **抑制 `blade_service.py` 恢复风暴与 HTTP 连接抖动** — `⬜ 待 Phase 3 (T3.1)`

---

## 四、高价值功能机会

1. **在各 Adapter 注入 `X-Octagon-Turn-Id` / `X-Octagon-Turn-Index`**：`http_proxy.py` 与 `finalize.py` 已经就绪，打通后多轮对话的 Wire 归因可从时间戳猜测升级为精确轮次关联。
2. **补齐 `kimi-code` Wire Native Normalizer 与 `blade-agent` (SDK) ATIF 解析**：通过抽离共享解析内核实现 7 家 Agent 在 Wire、ATIF 与安全扫描上的完全对等。
3. **来源自描述的 Token 语义与估算/结算成本漂移对账**：在 `UsagePayload` 携带 `input_semantics`，并在成本视图展示估算费用与真实结算账单的偏差，自动暴露定价表过期或重复计费回归。
