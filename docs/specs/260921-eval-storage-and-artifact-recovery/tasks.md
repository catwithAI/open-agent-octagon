# 实施计划

按「先解除磁盘压力、再修数据可信度」排序。任务 1、2 做完即可把一次横评的开销
从 ~123GB 降到 ~60GB；任务 3 再降到 ~25GB；任务 4 让 blade-agent 的分数首次可用。

- [x] 1. 评分快照改硬链接
  - `backend/scoring_snapshot.py` 新增 `_link_or_copy(src, dst)`：`os.link` 优先，
    `EXDEV`/`EMLINK`/`EPERM` 回落 `shutil.copy2` 并记账
  - 两处 `shutil.copytree`（:164 建快照、:244 materialize）传 `copy_function=_link_or_copy`
  - `ScoringSnapshot` 增加 `link_mode: "link"|"copy"|"mixed"`，写进 manifest
  - 单测：同一 attempt 两种模式建快照，`_snapshot_hash` 必须相同；
    mock `os.link` 抛 `EXDEV`，断言回落且 `link_mode == "copy"`
  - 回归：`_make_read_only` 仍生效，快照不可写
  - _需求：2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 2. 归档命令
  - 新增 `backend/tools/archive_attempts.py`：`--data-path` / `--run-id` /
    `--older-than` / `--yes` / `--dry-run`（默认 dry-run）
  - 保留清单与删除清单按 design 第 3 节；删除前打印预计回收体积并要求确认
  - DB migration：`attempts` 增加 `archived_at`、`archived_kinds`
  - attempt 详情 API 返回这两个字段；前端产物区显示「已归档（sandbox_home）」
  - 单测：running/queued/scoring 状态被拒；dry-run 不删文件；归档后元数据正确
  - 先对历史数据跑一次，回收当前 33G 的 `sandbox_home`
  - _需求：3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 3. 容器家目录瘦身
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

- [x] 4. blade-agent 路径提取多源合并
  - `backend/adapters/blade_service.py` 重写 `_agent_edited_paths`：
    拆成 `_paths_from_events`（去掉「工具名与路径同行」约束，全文件扫
    `file_path`/`path` + 扩展名白名单过滤）、`_paths_from_trace`
    （读 `trace.jsonl` 结构化 `arguments`）、`_paths_from_mtime`
    （与基线比 mtime/hash），取并集
  - 绝对路径不再直接丢弃：以 workspace_root 或 blade 项目路径为前缀时剥前缀转相对
  - fixture 测试：用 `att_2e47cfa79012` 的 events.jsonl，
    断言提取路径数 > 2 且包含实际交付物（现状只提取出 2 个）
  - _需求：4.1, 4.2, 4.3, 4.4_

- [x] 5. 优先下载校验与回收记账
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

- [x] 6. 磁盘护栏
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


## 实施记录（2026-09-21）

两处与 spec 的偏差，都是实现时发现 spec 的前提与 main 不符：

**任务 1：`materialize_scoring_input` 不改硬链接。** tasks.md 要求两处
`copytree`（:164 建快照、:244 materialize）都传 `copy_function`。实测不行：
materialize 之后会把工作副本 chmod 成 0644 供 legacy scorer 原地改动，硬链接
会让那次 chmod 写穿到共享 inode，**只读快照就此解冻**（已验证：改完能直接
覆写快照内容）。需求 2.3 会被悄悄破坏。materialize 的语义本就是「一份可改的
私有副本」，必须真复制；它是 per-job 临时目录，不构成 32G 那笔累积开销。

**任务 4/5：不是改现有实现，是新建。** requirements/design 把
`_agent_edited_paths`、`_EDIT_TOOL_MARKERS`、`priority_downloaded` 描述成现状
代码并给了行号（`blade_service.py:168`），但这些符号在 main 与**所有分支**里
都不存在（`git log --all -S` 无命中）——当时的产物回收是纯 BFS，根本没有优先
回收这一层。需求 4/5/6 因此按目标实现，新增 `backend/adapters/edited_paths.py`
承载三源提取，`_download_priority_paths` 承载下载校验。

`att_2e47cfa79012` 的 events.jsonl 在评测机（49）上，本地没有。fixture 测试用
复刻的分片形状（工具名与 arguments 分处不同事件行）覆盖同一失效模式；
**上线前仍需在 49 上对该 attempt 跑一次真实数据回归**，确认提取数 > 2 且覆盖
实际交付物。

**任务 3：匿名卷必须配合镜像预建目录，否则 office 场景全崩。** design 只说把
`lo`/`loroot`/`sysroot`/`fonts` 挂成匿名卷。实测（真 docker）这样不够：**匿名卷
的属主与权限继承镜像里该路径的目录**，镜像里不存在就是 `root:root 0755`，
而容器以宿主机 uid 跑（`--user`），agent 根本写不进去，LibreOffice 起不来——
而且会表现成 agent 能力问题。已在 `docker/agent-runtime/Dockerfile` 预建这六个
目录并 chmod 0777（与 `/home/agent` 同一个理由：构建期不知道宿主机 uid）。

→ **部署顺序有依赖**：任务 3 的代码上线前必须先重建并分发沙盒镜像
（`make sandbox-image`），否则 office 类场景会整体失败。
`tests/test_docker_launcher.py::test_ephemeral_home_dirs_are_writable_by_a_non_root_agent`
就是这条的守门测试（无本地镜像时 skip）。

## 代码评审修复（2026-09-21）

自查 + code-review 发现 7 个问题，均已修复并补了回归测试：

1. **（严重）归档实际一个字节都回收不了。** `sandbox_home` 并**不**在
   `ARTIFACT_SKIP_DIRS` 里（我原先的注释断言错了），快照 copytree 整个 attempt
   目录，改硬链接后就把 33G 运行时全链住了；归档删掉 attempt 侧目录项，inode
   仍被快照引用 —— 实测「声称回收 4.8MB、`du` 纹丝不动」。修法：在
   `scoring_snapshot` 里按 **attempt 根一层**排除这些目录（不进
   `ARTIFACT_SKIP_DIRS`，那份清单按任意层级匹配且与 API/scorer 共用，
   workspace 深处的同名目录是 agent 产物必须保留）；`ARCHIVE_KINDS` 直接复用
   同一常量，杜绝两边漂移。
2. **（严重）`disk_exhausted` 没声明进 `AttemptStatus`。** 终态集合由该 Literal
   扣除非终态推导，漏声明会让它既不算终态也不算非终态 —— run 永远收敛不了、
   normalization 报错、env token 一直有效。
3. **（严重）下载校验在续聊/恢复路径上完全失效。** 基线取自 `download_root`，
   而该路径下它是新建的空 staging 目录，摘要恒为 None，「内容没变就算未命中」
   永不触发 —— 恰恰是这条校验要守的路径。改为取自活的 workspace。
4. **（严重）staging 被丢弃时优先产物仍记为已回收。** BFS 有任何 error/截断就
   `rmtree` 整个 staging，优先文件一并消失，但 `priority_downloaded` 照旧列着，
   于是 attempt 拿着「对空工作区打出的分数」进矩阵且不带 infrastructure 标记。
   改为全部转入 `priority_missing` 并记 `staging_discarded`。
5. 归档写标记无 `busy_timeout`，WAL 下撞锁会在**删完文件之后**抛出，留下
   「空间已回收、无归档记录」且无法靠重跑补救。改为 30s timeout + 失败计入
   `failures` 不抛。
6. 磁盘闸在 job 无 `attempt_id` 时静默 return，job 凭空消失。改为 raise。
7. `_candidate_remote_paths` 的 `lstrip("./")` 按字符集剥，把 `.hidden/x.md`
   削成 `hidden/x.md`，候选全指错位置（与 `normalize_path` 里已修的同类 bug）。

最终实测（10 个 attempt × 3MB 运行时）：建快照新增 **0 MB**（旧实现翻倍），
归档后 30.2 MB → 1.6 MB，**净降 94.7%**。

## 评测机 49 实测验证（2026-09-21）

在 49 上用独立 worktree（`/tmp/octagon-verify`，不动线上工作树）验证，
**推翻了 spec 的两处事实前提**：

### ① BA 的低分不是产物回收失败，是 judge 没配置

`att_2e47cfa79012` 的真实数据：路径提取 **2 → 9 个**（含真正的交付物
`Aurisic_Prepaid_Amortization_Schedule_2025.xlsx`），需求 4.3 达标。
但这个 attempt 的产物**本来就拉回来了**（xlsx 有 3 sheet / 80 单元格），
0 分的实际原因是：

    official_rubric_judge  0  "Blade judge 未配置；请设置 octagon.yaml 的
                               llm_judge 或 LLM_JUDGE_* 环境变量"

同场景下 claude-code / codex / kimi-code / opencode / mimo-code **也都是
gave_up 0 分**；全横评因该原因判 0 的 score 行共 **259 条，跨全部 7 个
agent**（opencode 42、kimi 41、dsh 41、codex 41、cc 35、**BA 35**、mimo 24）。
BA 在其中属中游，不是异常值。

→ **spec「BA 27 个 attempt 低分 = 产物回收失败」的归因不成立**。真正要修的是
judge 配置（`~/restart-octagon.sh` 已 export `LLM_JUDGE_MODEL`，但该批 run
跑在改动之前）。本 PR 的回收改进仍有价值（提取数 2→9、下载校验、四字段记账），
但**不要指望它把 BA 的分数拉起来**。

### ② `sandbox_home` 的 33G 不是 LibreOffice

镜像 `octagon-agent-runtime:20260914` 里**根本没装 LibreOffice**
（`soffice: not found`；office 场景用 python-pptx/openpyxl/docx）。
330 个 attempt、32.8G 的真实构成：

| 目录 | 实测 | spec 是否列入 |
|---|---|---|
| `.local`（lib 6.28G + share 1.66G） | 7.95G | ✗ |
| `.npm`（_cacache） | 5.77G | ✗ |
| `config`（node_modules） | 5.35G | ✗ |
| `.config`（opencode） | 5.35G | ✗ |
| `.tmp`（plugins） | 3.95G | ✗ |
| `.cache` | 2.43G | ✓ |
| `loroot`+`lo`+`sysroot`+`apt`+`fonts` | **1.1G** | ✓ |

spec 的清单只覆盖 **3.5G / 11%**。已按实测改为
`.local/.npm/.cache/.tmp/config` + 原五项兜底 → 覆盖 **27.0G / 82%**。

**`.config` 与 `.claude` 刻意不收**（放弃 5.35G）：沙盒模式下
`host_home()` 返回的就是 `sandbox_home`，adapter 在**容器启动前**把配置写进
`sandbox_home/.config`（opencode 的 XDG_CONFIG_HOME）与 `sandbox_home/.claude`
（CLAUDE_CONFIG_DIR）；盖住它们 agent 就读不到自己的配置。

### ③ 部署阻塞项已在生产镜像上复现并验证修复

`octagon-agent-runtime:20260914` + 匿名卷 + 非 root uid →
`lo/loroot/sysroot/fonts` **全部 NOT WRITABLE**；加一层预建 0777 后全部
WRITABLE。**上线前必须重建镜像**这条已从推断升级为实测。

### 其它

- 49 上（Linux）跑全量测试：**89 passed, 1 skipped**，与 macOS 一致。
- 路径提取性能：13MB events.jsonl 0.2s。
