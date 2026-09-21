# 实施计划

按「先解除磁盘压力、再修数据可信度」排序。任务 1、2 做完即可把一次横评的开销
从 ~123GB 降到 ~60GB；任务 3 再降到 ~25GB；任务 4 让 blade-agent 的分数首次可用。

- [ ] 1. 评分快照改硬链接
  - `backend/scoring_snapshot.py` 新增 `_link_or_copy(src, dst)`：`os.link` 优先，
    `EXDEV`/`EMLINK`/`EPERM` 回落 `shutil.copy2` 并记账
  - 两处 `shutil.copytree`（:164 建快照、:244 materialize）传 `copy_function=_link_or_copy`
  - `ScoringSnapshot` 增加 `link_mode: "link"|"copy"|"mixed"`，写进 manifest
  - 单测：同一 attempt 两种模式建快照，`_snapshot_hash` 必须相同；
    mock `os.link` 抛 `EXDEV`，断言回落且 `link_mode == "copy"`
  - 回归：`_make_read_only` 仍生效，快照不可写
  - _需求：2.1, 2.2, 2.3, 2.4, 2.5_

- [ ] 2. 归档命令
  - 新增 `backend/tools/archive_attempts.py`：`--data-path` / `--run-id` /
    `--older-than` / `--yes` / `--dry-run`（默认 dry-run）
  - 保留清单与删除清单按 design 第 3 节；删除前打印预计回收体积并要求确认
  - DB migration：`attempts` 增加 `archived_at`、`archived_kinds`
  - attempt 详情 API 返回这两个字段；前端产物区显示「已归档（sandbox_home）」
  - 单测：running/queued/scoring 状态被拒；dry-run 不删文件；归档后元数据正确
  - 先对历史数据跑一次，回收当前 33G 的 `sandbox_home`
  - _需求：3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 3. 容器家目录瘦身
  - `docker/agent-runtime/Dockerfile`：确认 LibreOffice 运行时路径，
    必要时把 profile 位置指到镜像内固定路径
  - `backend/process/docker_launcher.py`：`sandbox.ephemeral_home_dirs`
    （默认 `lo`/`loroot`/`sysroot`/`apt`/`fonts`/`.cache`）；
    `.cache`、`apt` 用 `--tmpfs`（size 512m），其余用匿名卷
  - env `meta.yaml` 支持 `sandbox.keep_home_dirs`，从默认列表减项
  - **实测门槛**：ppt-visual-repair 与 presentbench 各跑一轮，
    scorer 输出与改动前一致；`sandbox_home` 中位体积 < 10MB
  - 注意并发 6 × tmpfs 的内存占用，评测机只有 14G
  - _需求：1.1, 1.2, 1.3, 1.4_

- [ ] 4. blade-agent 路径提取多源合并
  - `backend/adapters/blade_service.py` 重写 `_agent_edited_paths`：
    拆成 `_paths_from_events`（去掉「工具名与路径同行」约束，全文件扫
    `file_path`/`path` + 扩展名白名单过滤）、`_paths_from_trace`
    （读 `trace.jsonl` 结构化 `arguments`）、`_paths_from_mtime`
    （与基线比 mtime/hash），取并集
  - 绝对路径不再直接丢弃：以 workspace_root 或 blade 项目路径为前缀时剥前缀转相对
  - fixture 测试：用 `att_2e47cfa79012` 的 events.jsonl，
    断言提取路径数 > 2 且包含实际交付物（现状只提取出 2 个）
  - _需求：4.1, 4.2, 4.3, 4.4_

- [ ] 5. 优先下载校验与回收记账
  - 下载前取 `_baseline_digest`，写入后比对；内容未变则记 `priority_missing`
    并尝试下一个候选路径，不计入 `priority_downloaded`
  - 优先路径不受 BFS 500 上限约束：优先阶段结束后对 `priority_missing`
    做一次换前缀重试，再进 BFS
  - `artifact_sync` 固定四字段：`truncated_at` / `total_listed` /
    `priority_downloaded` / `priority_missing`
  - `priority_missing` 非空时 attempt `failure_kind = "infrastructure"`
  - swebench scorer 读到该标记时，detail 写「产物未回收」而非「未改动」
  - **验证门槛**：blade-agent 在 swebench-django-numberformat 上跑一轮，
    `functional_correctness` 不再为 0，且 `repository_discipline` 的
    "no source file changed" 消失
  - _需求：5.1, 5.2, 5.3, 5.4, 6.1, 6.2, 6.3_

- [ ] 6. 磁盘护栏
  - `octagon.min_free_disk_gb`（默认 15）；`run_dispatch` 取新 attempt 前检查，
    不足时不再启动并记 `paused_low_disk`
  - stop 路径改为「先内存标记取消、再尽力落盘」，落盘失败不阻断取消
  - 磁盘导致的失败统一 `error_code: "disk_exhausted"`
  - 单测：mock `statvfs` 返回低可用空间，断言不调度新 attempt 且 stop 仍返回 200
  - _需求：7.1, 7.2, 7.3, 7.4_

## 验收

全部完成后跑一次完整横评（13 task × 7 agent × 5 轮 = 455 attempt），断言：

- 峰值磁盘占用 < 30GB（现状推算 ~123GB）
- blade-agent 的 `functional_correctness` 在 swebench 类场景上非零
- 无 `disk_exhausted` 失败
- 四矩阵数据完整，13 个场景全部有分
