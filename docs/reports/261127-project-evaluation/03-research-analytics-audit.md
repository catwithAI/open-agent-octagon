# 03 — Research/Experiments、Insights、Rubric Evolution、Iteration 与 Scorers 审计

> 审计日期：2026-11-27 · 审计基线：`9c4cfaf` · **状态更新**：2026-11-27（PR #36 已合入，PR #37 待合入）
>
> 状态图例：`✅ 已合入 (PR #36)` · `🟡 待合入 (PR #37)` · `⏸️ 已冻结/废弃 (D1)` · `🟡 部分完成` · `⬜ 未开始`
>
> 审计范围：`backend/research_api.py`、`experiments/`、`insights/`、`rubric_evolution/`、`iteration/`、`mutations/`、`analysis/`、`feedback/`、`profiles/`、`comparison_scorer.py`、`evals_scorer.py`、`attribution_client.py`。

---

## 一、具体 Bug 清单

### 1. [P0/P1 死链路] 未接线管道与不可激活演进循环 — `⏸️ 已冻结 / 标记废弃 (PR #37: T2.4 / D1)`
- **位置**：`backend/analysis/`、`backend/rubric_evolution/`
- **处理**：按确认决策 D1 明确标记 `analysis` 废弃待删、`rubric_evolution` 冻结不再投入。

### 2. [P1 竞态] Experiments 取消后仍继续创建 Run 并拉起 Agent 子进程 — `🟡 部分完成`
- **位置**：`backend/experiments/coordinator.py:119, 148, 161-169, 325-410`、`backend/experiments/recovery.py:112-123, 194-200`
- **状态拆解**：
  1. `coordinator.py` 校验 `transition_cell` 返回值与 `stop_group` 取消 `group:{id}` 任务：`✅ 已合入 (PR #36: T1.5)`。
  2. `experiments/recovery.py` 查询活跃状态补 `'scoring'`：`⬜ 待后续补充`。

### 3. [P1 全局卡死] Leader Outbox 消费者遇到异常行永久堵死 — `✅ 已合入 (PR #36: T1.5)`
- **位置**：`backend/experiments/leader.py:21, 76, 101-129`
- **处理**：空候选集标记 `consumed_at` 继续推进，`_rank` 安全处理 `duration_ms IS NULL`。

### 4. [P1 永久锁死与会话泄漏] Insights 取消/崩溃后永久 409 + Provider 多重缺陷 — `✅ 已合入 (PR #36: T1.5)`
- **位置**：`backend/insights/service.py:66-117`、`backend/insights/providers.py:127, 229-259`（`analysis/providers.py` 按 D1 冻结）
- **处理**：`BaseException` 释放并发锁、累加多步 token、`delete_session` 进 `finally:` 并去重流式文本。

### 5. [P1 逻辑缺陷] `active_loop.py` 金标自证循环 & 演进调度器零退避重试风暴 — `⏸️ 已冻结 (PR #37: T2.4 / D1)`
- **位置**：`backend/rubric_evolution/active_loop.py`、`scheduler.py`

### 6. [P1 评分校验漏洞] 外部 Scorer 缺边界校验、对比评分读错目录、JSON 提取器 3 份坏拷贝 — `🟡 待合入 (PR #37: T2.4)`
- **位置**：`backend/evals_scorer.py`、`backend/comparison_scorer.py`、`backend/attribution_client.py`、`backend/insights/generator.py`、`backend/rubric_evolution/{pipeline,judge_executor}.py`
- **处理**：强校验非空/有限/`[0,1]` 与维度 ID 匹配；对齐冻结 `scoring-work` 快照目录；统一复用 `insights/jsonutil.parse_json_object`（同步更新 2 处测试桩）。

### 7. [P1 逻辑错误] 中性反馈污染 EMA & 推荐学习结果被 `INSERT OR IGNORE` 丢弃 — `✅/🟡 已完成`
- **位置**：`backend/feedback/learning.py:95`、`backend/profiles/recommend.py:115-117, 143`
- **状态拆解**：`feedback/learning.py` 跳过中性信号 `✅ 已合入 (PR #36: T1.5)`；`profiles/recommend.py` 改 `ON CONFLICT DO UPDATE` `🟡 待合入 (PR #37: T2.4)`。

### 8. [P1 性能与边界] `research_api.py` 阻塞事件循环、`iteration/` 租约易断、`preview_token` 绑定不全 — `🟡 待合入 (PR #37: T2.3 / T2.4)`
- **位置**：`backend/research_api.py`、`backend/iteration/controller.py`、`backend/mutations/preview.py`
- **处理**：`research_api` 全量 `asyncio.to_thread`；`iteration` 增加 reviewer 隔离工作目录、续租锁重试与 `asyncio.Future` 替代 100Hz 忙等；`preview_token` 绑定 `source_hash` 与 `env_name`。

---

## 二、架构问题

1. **后处理分析子系统过度增殖**：`insights/`、`analysis/`、`attribution_client.py`、`rubric_evolution/`、`feedback/`+`profiles/` 形成 5 套互不兼容的诊断与证据打包体系，数据结构与存储位置（SQLite + 4 套零散 JSON 目录树）高度碎片化。
2. **LLM/HTTP 客户端与 JSON 解析重复**：`BladeRuntraceJudgeProvider` 复制了 `BladeProvider` ~90% 代码且修复不同步；3 个 external scorer 各自发裸同步 `httpx.post()` 无连接池与统一重试策略；4 套 JSON 提取器并存。
3. **控制面分裂与双写竞态**：`api.py` 与 `research_api.py` 异常体系与接口风格不统一；`experiments/` 的 cell 终态由 `coordinator.py` 与 `recovery.py` 双写；`rubric_evolution/store.py` 每次读写都调 `_init_db_sync`。

---

## 三、战略评估与取舍建议

| 子系统 | 结论 | 理由 |
|---|---|---|
| **`experiments/` + `mutations/`** | **高价值，继续深化** | 平台实现统计严谨评测（多重复、受控扰动、鲁棒性聚合、Leader 追踪）的核心骨架，修好取消竞态与 outbox 卡死后价值极高。 |
| **`iteration/`** | **高价值，继续深化** | 本模块组中工程质量最高，完美支撑真实多轮审阅返工场景；只需补齐 reviewer 目录隔离与租约重试。 |
| **`evals_scorer` / `comparison_scorer` / `attribution_client`** | **高价值，加固收紧** | 外置单点/成对/列表评分与失败根因归因直击对比评测核心痛点；需补齐响应校验、公共 HTTP Client 与冻结快照对齐。 |
| **`insights/`** | **中高价值，简化并修复** | 证据锚定、自动降级断言的实验总结对用户有明确价值，定位为**唯一**实验级总结层即可。 |
| **`rubric_evolution/`（5,318 LOC）** | **过度设计，建议冻结** | 8 套 schema、无法激活环境 rubric、无数据生产者、离线回放自证，在具备真正可执行动态评分器前不应再投入。 |
| **`analysis/`（1,193 LOC）** | **冗余未接线，建议下线/合并** | 零运行时调用方，功能完全被 `insights/` 与 `attribution_client.py` 覆盖。 |
| **`profiles/` + `feedback/learning.py`** | **过度工程化，建议简化** | 为 4 条静态 `if/elif` 规则维护了 epoch/watermark/EMA 乐观锁状态机且无法改变推荐结果，降级为简单静态预设即可。 |
