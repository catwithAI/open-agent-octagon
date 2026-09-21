# 需求文档

## 简介

2026-09-18~19 的 7-agent 横评在跑到 336 个 attempt 时把 444G 的评测机写满（0 字节可用），
被迫中止；同一次横评里 blade-agent 的 27 个 attempt 全部拿到形状一致的低分，
事后确认那不是能力问题而是产物回收失败。这两件事都不是偶发故障，而是当前实现的固有开销与固有缺陷，
再跑一次必然重演。

实测数据（2026-09-21，330 个 attempt 目录）：

| 类别 | 总量 | 说明 |
|---|---|---|
| `sandbox_home` | **33G** | 容器家目录，里面主要是 LibreOffice 运行时（`lo` 407MB + `loroot` 577MB）、apt 缓存、字体 |
| wire 相关 | 16G | `wire-blobs` + `wire-sources` + `wire.jsonl`，`capture_policy: parsed` 的产物 |
| `skill_workspace` | 4.6G | agent 真实工作区，含 django/pylint 整个 repo 拷贝 |
| `events.jsonl` | 1.9G | 事件日志 |
| `scoring-snapshots` | **32G** | `create_scoring_snapshot` 用 `copytree` 把上面这些**再复制一份** |

单 attempt 抽样：最小 10MB / 中位 91MB / 最大 519MB / 均值 126MB；
端到端（含快照复制）约 **270MB/attempt**。455 个 attempt 需要 ~123GB，
而其中真正有分析价值的（wire + workspace + events）只有 22G。

本功能把单次横评的磁盘开销从 ~123GB 降到 ~25GB，并修好 blade-agent 的产物回收，
使它的分数首次具备可比性。

## 关键决策

- **D-01：`sandbox_home` 用白名单收窄，不做事后清理。** 它是 `docker run` 的 bind mount
  （`backend/process/docker_launcher.py`，`<data>/attempts/<id>/sandbox_home` → `/home/agent`），
  内容由 agent 在运行时写入，事后 `rm` 只是把已经付出的写入成本再付一次 IO。
  正确做法是让容器内那些**可重建的运行时目录**根本不落在 bind mount 上：
  在容器内把 `lo`、`loroot`、`sysroot`、`apt`、`fonts`、`.cache` 指向容器可写层
  （tmpfs 或镜像内路径），容器一销毁即消失。
- **D-02：`scoring-snapshots` 改硬链接，不改语义。** 快照存在的理由是「判分时 agent
  不能再改产物」（sandbox spec D-11 已用杀容器保证了这一点）与「快照可哈希、可复现」。
  硬链接同样满足：源文件被删或被改写时链接仍指向原 inode，而 `_make_read_only` 已经
  把快照置为只读。**唯一不能硬链接的是跨文件系统的情况**，此时回落到 copy 并记录。
- **D-03：归档策略与采集策略分离。** `capture_policy: parsed` 决定**采集**多少，
  归档决定**保留**多少。跑完并评分后，attempt 目录里只保留
  `wire-*`、`skill_workspace`、`events.jsonl`、`trajectory.json`、`security_*`、DB 相关，
  其余（`sandbox_home`、`sandbox_ro`、`scoring-work` 残留）可回收。
  **归档必须显式触发，不能在 attempt 结束时自动做**——横评期间经常要回看现场。
- **D-04：blade-agent 的优先回收改为「多源合并」，不依赖单一事件格式。**
  现状 `_agent_edited_paths` 只扫 `events.jsonl` 且要求**同一行**既含工具名标记
  又含 `file_path`；而 BA 的工具调用是分片流式的，工具名与 arguments 落在不同事件里
  （实测 `att_2e47cfa79012`：15883 行带编辑标记，只提取出 2 个路径）。
  改为三个来源取并集：① 全文件扫 `file_path`/`path`（不要求同行有工具名）、
  ② `trace.jsonl` 的结构化 `arguments`、③ 远端 workspace 与本地基线的 mtime/hash 差异。
- **D-05：优先下载必须校验落地内容。** 现状只要 `download_file` 不抛异常就记
  `priority_downloaded`，但实测文件内容仍是基线（mtime 停在物料拷贝时间）。
  下载后比对 hash；与基线相同则视为**未命中**，继续尝试下一个候选路径并记进 `errors`。
- **D-06：BFS 上限按「是否已覆盖优先路径」动态放宽，不是简单调大。**
  500 的上限本身是合理的防爆保护。真正的要求是：**优先路径必须全部落地**，
  BFS 只是兜底。优先路径全部命中时 500 足够；未命中时按需继续，并把
  `truncated_at` 与 `priority_missing` 一并写进 `artifact_sync`，让评分侧能区分
  「agent 没做」与「没回收到」。

## 术语

- **bind mount 家目录**：`<data>/attempts/<id>/sandbox_home`，容器内 `/home/agent`。
- **评分快照**：`data/scoring-snapshots/<hash>/`，判分时的冻结副本。
- **归档**：attempt 评分完成后，删除可重建内容、保留证据的操作。
- **优先回收**：blade-agent 产物同步时，先按 agent 实际编辑过的路径定向下载。

## 需求

### 需求 1：容器家目录不再承载可重建运行时

**用户故事：** 作为跑横评的人，我希望一个 attempt 的家目录只保存 agent 真正产生的东西，
这样 330 个 attempt 不会存 330 份几乎一样的 LibreOffice。

#### 验收标准

1. WHEN 容器启动 THEN 系统 SHALL 把 `lo`、`loroot`、`sysroot`、`apt`、`fonts`、`.cache`
   这些可重建运行时目录挂到容器可写层或 tmpfs，而非 bind mount 的 `sandbox_home`。
2. WHEN office 类场景（ppt-visual-repair、presentbench、odysseybench）运行 THEN
   LibreOffice SHALL 仍能正常渲染，产物与改动前逐字节一致或通过既有 scorer。
3. WHEN attempt 结束 THEN `sandbox_home` 的中位体积 SHALL 低于 10MB
   （现状中位 91MB、最大 519MB）。
4. IF 某场景确实需要把运行时产物留作证据 THEN 系统 SHALL 提供场景级开关，
   而不是让所有场景都付这个成本。

### 需求 2：评分快照不再整树复制

**用户故事：** 作为跑横评的人，我不希望同一份产物在磁盘上存两遍。

#### 验收标准

1. WHEN `create_scoring_snapshot` 复制 attempt 目录 THEN 系统 SHALL 优先用硬链接
   （`os.link`）而非 `shutil.copy2`。
2. IF 源与目标跨文件系统导致 `os.link` 失败 THEN 系统 SHALL 回落到复制，
   并在快照元数据里记录 `link_mode: "copy"`，不得静默。
3. WHEN 快照建立后 THEN `_make_read_only` SHALL 仍然生效，快照内容不可被改写。
4. WHEN 对同一 attempt 重复建快照 THEN `_snapshot_hash` 的结果 SHALL 与改动前一致
   （硬链接不改变文件内容，哈希必须稳定）。
5. WHEN 330 个 attempt 全部评分完成 THEN `scoring-snapshots` 的总体积 SHALL 低于 2G
   （现状 32G）。

### 需求 3：可显式触发的归档

**用户故事：** 作为跑完横评要长期保留数据的人，我希望能一键把可重建的部分清掉，
只留证据，而且清之前知道会删什么。

#### 验收标准

1. WHEN 运维执行归档命令 THEN 系统 SHALL 只保留 `wire-*`、`skill_workspace`、
   `events.jsonl`、`trajectory.json`、`security_*`、`recovery.json`、`*.json` 元数据。
2. WHEN 归档执行前 THEN 系统 SHALL 先输出将删除的目录与预计回收体积，
   并要求显式确认（或 `--yes`），不得默认执行。
3. IF attempt 仍处于 `running` / `queued` / `scoring` THEN 系统 SHALL 拒绝归档该 attempt。
4. WHEN 归档完成 THEN 系统 SHALL 在 attempt 元数据写 `archived_at` 与被删类别，
   使后续读取方能区分「没采集」与「已归档」。
5. WHEN 已归档的 attempt 被读取 THEN 前端与 API SHALL 明确显示归档状态，
   而不是表现为数据缺失。

### 需求 4：blade-agent 的编辑路径能被可靠识别

**用户故事：** 作为要评价 blade-agent 的人，我需要它的分数反映它做了什么，
而不是反映产物回收有没有成功。

#### 验收标准

1. WHEN 解析 agent 编辑过的路径 THEN 系统 SHALL 不要求工具名标记与路径出现在同一行
   （BA 的工具调用是分片流式的）。
2. WHEN `events.jsonl` 与 `trace.jsonl` 同时存在 THEN 系统 SHALL 合并两者的结果取并集。
3. WHEN 某 attempt 的 events 里存在 `file_path` 字段 THEN 提取出的路径数 SHALL 大于 0
   （回归基准：`att_2e47cfa79012` 现状提取 2 个，改后应显著增加并覆盖实际交付物）。
4. IF 所有来源都提取不到路径 THEN 系统 SHALL 在 `artifact_sync` 记
   `priority_source: "none"`，使该 attempt 的低分可被识别为「回收无依据」而非能力差。

### 需求 5：优先下载校验落地内容

**用户故事：** 作为看评分结果的人，我需要「下载成功」真的意味着文件变了。

#### 验收标准

1. WHEN 优先路径下载完成 THEN 系统 SHALL 比对落地文件与基线的 hash。
2. IF 落地内容与基线一致 THEN 系统 SHALL 视为未命中，尝试下一个候选路径，
   并把该路径记入 `artifact_sync.priority_missing`。
3. WHEN 所有候选都与基线一致 THEN 系统 SHALL 在 `artifact_sync` 记录该事实，
   且 SHALL NOT 把它记进 `priority_downloaded`。
4. WHEN 评分侧读到 `priority_missing` 非空 THEN 相关维度的 detail SHALL 说明
   「产物未回收」，与「agent 未改动」区分开。

### 需求 6：BFS 截断不再掩盖回收失败

**用户故事：** 作为排查低分的人，我需要一眼看出是没做还是没收到。

#### 验收标准

1. WHEN BFS 达到上限而优先路径尚未全部落地 THEN 系统 SHALL 继续回收优先路径，
   不因总数上限而放弃。
2. WHEN 产物同步结束 THEN `artifact_sync` SHALL 同时包含 `truncated_at`、
   `priority_downloaded`、`priority_missing`、`total_listed` 四个字段。
3. WHEN `priority_missing` 非空 THEN attempt 的 `failure_kind` SHALL 标记为
   `infrastructure`，使其在统计里可与 agent 自身失败分开。

### 需求 7：磁盘护栏进入产品

**用户故事：** 作为跑长时间横评的人，我不希望磁盘写满时才发现，更不希望那时连停止按钮都失效。

#### 验收标准

1. WHEN 可用磁盘低于阈值 THEN 系统 SHALL 拒绝调度新的 attempt，并在 run 级记录原因。
2. WHEN 可用磁盘低于阈值 THEN 系统 SHALL 仍能响应 stop 请求
   （现状：磁盘满时 stop API 返回 500，必须先手动腾空间）。
3. WHEN 磁盘不足导致 attempt 失败 THEN `error_code` SHALL 明确为磁盘原因，
   而不是表现为 scoring 阶段的通用异常。
4. WHERE 部署配置中 THE 阈值 SHALL 可配置，默认 15GB。
