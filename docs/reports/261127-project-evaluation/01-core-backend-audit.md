# 01 — 核心后端引擎深度审计

> 审计日期：2026-11-27 · 审计基线：`9c4cfaf` · **状态更新**：2026-11-28（PR #36、PR #37、PR #38 已合入 `main`；Phase 4 待合入）
>
> 状态图例：`✅ 已合入 (PR #36/#37/#38)` · `🟡 本 PR 待合入 (Phase 4)` · `🟡 部分完成` · `⬜ 未开始`
>
> 审计范围：`backend/main.py`、`api.py`、`db.py`、`runner.py`、`run_dispatch.py`、`run_service.py`、`scoring_queue.py`、`evaluator.py`、`recovery.py`、`sweeper.py`、`disk_guard.py`、`env_loader.py`、`env_attempt_server.py`、`process/docker_launcher.py`、`artifact_preview.py`、`artifact_worker.py`、`display_only.py`、`runtime_state.py`。

---

## 一、具体 Bug 清单

### 1. [P0 安全] `POST /runs` → `_copy_uploads` 任意宿主机文件读取 — `✅ 已合入 (PR #36: T1.1 / T1.2)`
- **位置**：`backend/run_service.py:456`、`backend/run_dispatch.py:1191-1206`、`backend/api.py:2136`
- **根因**：`create_run_plan` 将调用方不可信的 `request.context` 直接合并进 `task_context`（`context = {**task_context, **(request.context or {})}`）。随后 `_copy_uploads` 读取 `task_context["uploaded_files"]`，对每项 `path` **没有任何根目录白名单或 containment 校验**，直接 `shutil.copy2(src, dest)` 复制到 `attempts/<id>/skill_workspace/<name>`。
- **影响**：任意调用方传入 `{"uploaded_files": [{"name": "leak.txt", "path": "/etc/passwd"}]}`（或 `octagon.yaml`、`octagon.db`），即可通过 `GET /api/runs/{run_id}/attempts/{attempt_id}/artifacts/leak.txt` 读取宿主机任意文件。
- **关联密钥风险**：`backend/process/docker_launcher.py:214-215` 直接通过 `docker exec -e f"{key}={value}"` 传递敏感环境变量，宿主机 `ps` / `/proc/<pid>/cmdline` 可直接看到明文 API Key（**已随 PR #36 改为 `0600` `--env-file` / 无值 `-e KEY`**）。

### 2. [P0 数据损毁] `_prepare_rejudge` 先删除并提交旧评分，再做前置校验 — `✅ 已合入 (PR #36: T1.2)`
- **位置**：`backend/scoring_queue.py:225-249`
- **根因**：`_prepare_rejudge` 在第 226 行更新 `attempts.rubric_version`，在第 240–242 行执行 `DELETE FROM scoring_jobs WHERE attempt_id=?`、`DELETE FROM scores WHERE attempt_id=?` 并立即 **`conn.commit()`**；直到第 244–249 行才检查 `attempt_dir.is_dir()`、`state.envs.get(...)`，且之后还要运行可能因磁盘空间不足失败的 `create_scoring_snapshot`。
- **影响**：一旦 attempt 目录已归档删除、env 未加载或创建快照失败，请求返回 HTTP 409/500，但原评分已被永久清空，且流程根本走不到 `rejudge_prev` 回滚逻辑。

### 3. [P1 稳定性] 全库 SQLite 连接/FD 泄漏 + `DEFERRED` 事务锁升级死锁 — `✅ 已合入 (PR #37: T2.1)`
- **位置**：`backend/db.py:747-754, 1349-1353, 1465-1515, 1787-1818`、`backend/evaluator.py:384, 403, 417`、`backend/runner.py:180-233, 695`
- **根因**：
  1. `_open_sync(db_path)` 返回裸 `sqlite3.Connection`，调用方普遍写 `with _open_sync(db_path) as conn:`。在 Python `sqlite3` 中，`Connection.__exit__` 只执行 commit/rollback，**不会调用 `conn.close()`**，持续泄漏连接与 WAL/SHM 文件描述符直到 GC。
  2. `_open_sync` 每次打开连接都执行 `PRAGMA journal_mode=WAL`（包括纯读查询），在并发写入时增加锁竞争；`open_db()`（`db.py:1349-1353`）缺少 `PRAGMA busy_timeout`；`evaluator.py` 和 `runner.py:695` 绕过 `_open_sync` 直接调用裸 `sqlite3.connect()`（无 WAL、无 `busy_timeout`、无外键约束、同样未关闭）。
  3. 读后写路径（`_append_attempt_observation_sync` 先 `SELECT MAX(sequence)+1` 再 `INSERT`、`claim_idempotency`、`_create_attempt_sync`）使用默认 `DEFERRED` 事务而非 `BEGIN IMMEDIATE`。并发写入时两个连接同时持有 SHARED 锁再尝试升级 RESERVED 锁，SQLite 会**立即返回 `SQLITE_BUSY`（忽略 `busy_timeout`）**或触发 `UNIQUE(attempt_id, sequence)` 异常。

### 4. [P1 死锁/状态错乱] 评分 Sweeper 信号量饥饿、任务重启复活与状态误写 — `✅ 已合入 (PR #37: T2.2)`
- **位置**：`backend/sweeper.py:113-128`、`backend/convergence.py:42-121`、`backend/scoring_queue.py:376-418, 863-877`、`backend/runner.py:846-881`
- **根因**：
  1. `sweeper.py` 超时收敛评分时只改 DB，不取消卡住的 `_execute_job` 协程，也不触发 `_invoke_scorer_cancel_hook`，导致默认容量仅 2 的 `_scoring_semaphore` 被永久占满，阻塞全站后续评分（代码注释 `sweeper.py:114-118` 已承认未修复）。
  2. `converge_attempts` 不更新 `scoring_jobs.status`，过期任务仍保持 `'running'`；服务重启时 `schedule_startup_scoring_recovery`（`scoring_queue.py:863-877`）会将其重置为 `'queued'` 并重新执行。
  3. `sweeper.py:123` 将 `attempts.status` 写成 `"scoring_failed"`（注：PR #37 保留该枚举以兼容 `experiments/aggregate.py` 聚合统计）。
  4. `runner.py:846-881`（`_finalize_no_score`）使用无状态守卫的 `UPDATE attempts SET status=... WHERE id=?`，并发竞态下可覆盖 `sweeper` 或 `stop_run` 已写入的 `timeout`/`cancelled` 终态（**PR #37 已加终态守卫**）。

### 5. [P1 幂等死锁] `create_run_plan` 异常导致幂等键永久卡死 & 崩溃对账失效 — `🟡 部分完成`
- **位置**：`backend/run_service.py:397-562`、`backend/db.py:1484, 1569`
- **状态拆解**：
  1. `create_run_plan` 异常释放 `idempotency_keys`：`✅ 已合入 (PR #36: T1.2)`。
  2. `reconcile_idempotency`（`db.py:1569`）`result_id` 崩溃对账边界：`⬜ 待后续补充`。

### 6. [P1 资源泄漏] `cancelled` Run 的 SSE 无限轮询 + 无界进程内字典泄漏 — `✅ 已合入 (PR #36/#37/#38)`
- **位置**：`backend/api.py:1473`、`backend/run_dispatch.py:990-993`、`backend/run_service.py:352, 395`、`backend/env_attempt_server.py:188`、`backend/db.py:1697`
- **状态拆解**：
  1. `_stream_run_events` 终态补 `"cancelled"`：`✅ 已合入 (PR #36: T1.5)`；事件驱动 SSE：`✅ 已合入 (PR #37: T2.3)`。
  2. `_inbound_seq`、`_event_data_paths` 有界化：`✅ 已合入 (PR #37: T2.1)`；`run_service._create_locks` 清理：`✅ 已合入 (PR #38: T3.2)`。

### 7. [P1 并发/预览/沙盒缺陷] `env.db` 初始化竞态、`docker exec` 泄漏与预览错误粘滞 — `✅ 基本完成`
- **位置**：`backend/env_attempt_server.py:101, 150-163`、`backend/process/docker_launcher.py:264-269, 528-529`、`backend/artifact_preview.py:593-620`
- **状态拆解**：
  1. `_open_env_db` 并发初始化锁 + WAL + `hmac.compare_digest`：`✅ 已合入 (PR #37: T2.1)`。
  2. `docker_launcher.exec()` `finally` 回收子进程：`✅ 已合入 (PR #36: T1.1)`；`translate_mcp_specs` 同名脚本覆盖：`⬜ 待后续补充`。
  3. `artifact_preview` 淘汰瞬态失败 `Future` 与 stat 缓存避免重复 SHA-256：`✅ 已合入 (PR #37: T2.3)`；预览超时整组 `os.killpg` 强杀：`🟡 本 PR 待合入 (Phase 4: T4.1)`。

### 8. [P1 恢复与归因缺陷] Recovery 忽略 `evals` 后端、本地错误误标 Blade、`display_only` 启动期写库 — `✅ 已合入 (PR #36/#37)`
- **位置**：`backend/recovery.py:461-468`、`backend/runner.py:354-363`、`backend/run_dispatch.py:472, 821, 859, 940, 1291`、`backend/main.py:105-165`
- **状态拆解**：
  1. `display_only` 启动/停机写库守卫：`✅ 已合入 (PR #36: T1.2)`。
  2. Recovery 兼容 `evals`、非 blade 错误改 `cli_error`、fire-and-forget 任务统一纳管：`✅ 已合入 (PR #37: T2.2)`。

---

## 二、架构问题

1. **巨石模块与循环依赖**（`api.py` / `db.py` / `run_dispatch.py`）— `🟡 部分缓解`（`blade_service.py` / `RunDetail.tsx` 已随 PR #38 拆分，核心路由拆包可按需迭代）
2. **单进程全局可变状态（`runtime_state.py`）与评分进程隔离** — `🟡 本 PR 待合入 (Phase 4: T4.1)`（新增 `backend/workers/scoring_worker.py` 独立租约评分进程与 `in_process`/`subprocess`/`external` 三模）
3. **热路径重复建表迁移（`_init_db_sync`）** — `✅ 已合入 (PR #37: T2.1)`
4. **外部 Env 代码无沙盒同进程加载** — `🟡 本 PR 待合入 (Phase 4: T4.2)`（新增 `backend/workers/env_worker.py` 子进程隔离与管理员密钥/内存凭据剥离）
5. **四重状态字段分散修改** — `✅ 部分缓解 (PR #37: T2.2)`（已补终态乐观守卫与 `scoring_jobs` 联动收敛）

---

## 三、优先优化建议

1. **改造 SQLite 连接与初始化生命周期** — `✅ 已合入 (PR #37: T2.1)`
2. **清除主事件循环同步阻塞与流式 `/upload`** — `✅ 已合入 (PR #37: T2.2 / T2.3)`
3. **事件驱动 SSE 与预览缓存去重** — `✅ 已合入 (PR #37: T2.2 / T2.3)`
4. **复用 HTTP 连接池与轻量存在性查询** — `✅ 已合入 (PR #37: T2.3)`
5. **后台任务统一纳管** — `✅ 已合入 (PR #37: T2.2)`
