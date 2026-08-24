# Research Capability Expansion — Internal Release Notes

状态：实现完成，所有新 capability 默认关闭。验收仅使用 fixture、临时 SQLite 与 fake
provider；未开放真实 env，未执行真实 agent/provider fan-out。

## 已实现

- Experiment/TaskVariant/RunGroup、bounded coordinator、leader/robustness；
- Profiles/Auto Profile、Evidence/Insights、Feedback/Normalized；
- 合成 prompt-injection env、Attack Coverage、forensic rerun-preview；
- capability-aware AppShell，保留全部旧 Submit/Run 深链；
- ordered reconciliation、no-secret policy、append-only research audit；
- 1000-cell、10k evidence、16 路 SQLite 混合写 fixture 门禁。

## Rollout 与回滚

1. 保持全 false 部署并检查 `/api/capabilities`；
2. 隔离环境按依赖顺序开启 experiments/task_variants/run_groups，再开启分析与 UI 能力；
3. Insights 最后开启，必须单独配置 provider 与 key 环境变量；
4. 任一异常只关闭对应创建/生成 flag。不要删除 DB 表、Experiment 或 derived payload；
5. 回滚后旧 Run、已有 Experiment 只读数据与 audit 仍保留。

## 已知边界

- create/clone 在事务提交后自动异步启动 RunGroup；同一 Idempotency-Key replay
  不会重复启动，提交后进程中断的 queued group 由 startup recovery 接管；
- 本轮未对真实 provider/env 做外部验证；
- frontend 使用语义结构与键盘表格门禁，尚未引入 axe 依赖；
- Vite 测试会输出上游 React Router future-flag 和 esbuild/oxc deprecation warning。
