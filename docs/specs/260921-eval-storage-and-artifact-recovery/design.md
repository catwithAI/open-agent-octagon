# 设计文档

## 概述

四块改动，彼此独立可分别上线：

1. **容器家目录瘦身**（需求 1）— 改 `docker_launcher.py` 的挂载与镜像。
2. **快照硬链接**（需求 2）— 改 `scoring_snapshot.py` 的复制原语。
3. **归档命令**（需求 3）— 新增脚本 + attempt 元数据字段。
4. **BA 产物回收**（需求 4/5/6）— 改 `blade_service.py` 的路径提取与下载校验。

外加一条横切的**磁盘护栏**（需求 7），落在调度与 stop 路径上。

前三块解决「一次横评 123GB」，第四块解决「BA 的分数不可用」。

## 1. 容器家目录瘦身

### 现状

`backend/process/docker_launcher.py` 的白名单挂载：

| 宿主机 | 容器内 | 权限 |
|---|---|---|
| `<data>/attempts/<id>/skill_workspace` | 同绝对路径 | rw |
| `<data>/attempts/<id>/sandbox_home` | `/home/agent` | rw |
| `<data>/attempts/<id>/sandbox_ro` | `/attempt` | ro |

agent 在 `/home/agent` 下装 LibreOffice 运行时、apt 缓存、字体，
这些全部穿透 bind mount 落到宿主机，每个 attempt 一份。

### 方案

保持 `/home/agent` 仍是 bind mount（agent 的真实产出要留证据），
但把**可重建子目录**用 `--tmpfs` 或 `-v` 匿名卷盖掉，使写入落在容器可写层：

```
--tmpfs /home/agent/.cache:rw,size=2g
--tmpfs /home/agent/apt:rw,size=1g
-v /home/agent/lo          # 匿名卷，容器销毁即回收
-v /home/agent/loroot
-v /home/agent/sysroot
-v /home/agent/fonts
```

**为什么不是「跑完再删」**：写入成本已经付掉了（IO 与空间峰值都发生在运行期），
事后删只是再付一次 IO。tmpfs/匿名卷让它从一开始就不碰宿主机磁盘。

**tmpfs 的内存代价**：评测机 14G 内存、并发 6，`.cache` 2g × 6 = 12g 会打爆。
因此只有 `.cache` 与 `apt` 用 tmpfs 且 size 收到 512m，
`lo`/`loroot`/`sysroot`/`fonts` 用匿名卷（落 `/var/lib/docker`，仍占磁盘但
**容器销毁即释放**，不随 attempt 目录累积）。

**开关**：`sandbox.ephemeral_home_dirs`（list，默认上述六项）。
需求 1.4 的场景级放开通过 env `meta.yaml` 的
`sandbox.keep_home_dirs: ["renders"]` 实现，从默认列表里减项。

### 风险

LibreOffice 首次运行会重建 profile，可能增加每 attempt 几秒启动开销。
office 类场景本来就跑几百秒，可接受；但需求 1.2 要求实测验证产物不变。

## 2. 快照硬链接

### 现状

`backend/scoring_snapshot.py:164`

```python
shutil.copytree(source, copied_attempt, symlinks=True, ignore=_ignore_non_deliverables)
```

`copytree` 默认用 `copy2`，整树复制。330 个 attempt → 32G 快照。

### 方案

给 `copytree` 传 `copy_function=_link_or_copy`：

```python
def _link_or_copy(src: str, dst: str) -> None:
    """优先硬链接；跨设备或不支持时回落复制并记账。"""
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EMLINK, errno.EPERM):
            raise
        shutil.copy2(src, dst)
        _LINK_FALLBACK.add(dst)   # 供 link_mode 记账
```

`symlinks=True` 保持不变（符号链接仍按符号链接复制，不解引用）。

### 为什么安全

- **判分期不可改**：sandbox spec D-11 已保证判分前容器已被 kill，agent 无法再写；
  `_make_read_only` 把快照置只读。硬链接共享 inode，但没有写入方。
- **哈希稳定**：`_snapshot_hash` 读的是文件内容，硬链接不改变内容（需求 2.4）。
- **源被删不影响快照**：硬链接持有 inode 引用，`rm` 源文件后快照仍可读 ——
  这正是归档（第 3 块）能安全删 attempt 目录的前提。

### 记账

`ScoringSnapshot` 增加 `link_mode: "link" | "copy" | "mixed"`，
写进快照 manifest。需求 2.2 要求回落不得静默。

## 3. 归档命令

### 接口

```
python -m backend.tools.archive_attempts --data-path ./data \
    [--run-id RUN] [--older-than 7d] [--yes] [--dry-run]
```

默认 `--dry-run` 行为：列出将删目录与预计回收体积，不执行（需求 3.2）。

### 保留清单

保留：`wire-*`、`skill_workspace`、`events.jsonl`、`trajectory.json`、
`thinking.jsonl`、`conversation.jsonl`、`security_*`、`recovery.json`、
`sandbox_container.json`、`*.json` 元数据。

删除：`sandbox_home`、`sandbox_ro`、`.opencode-iso-home` 等 agent 私有运行时目录。

### 状态标记

attempt 表新增 `archived_at TEXT NULL` 与 `archived_kinds TEXT NULL`（JSON 数组）。
API 的 attempt 详情返回这两个字段；前端在产物区显示「已归档（sandbox_home）」，
而不是空白（需求 3.4 / 3.5）。

### 与硬链接的关系

第 2 块上线后，快照与 attempt 共享 inode。归档删 attempt 目录里的文件时，
**快照仍持有引用，空间不会释放**。因此归档必须同时处理快照，或
——更简单——**归档只删不在快照保留清单里的目录**（`sandbox_home` 本来就被
`_ignore_non_deliverables` 排除在快照之外，不共享 inode）。
本设计采用后者：归档与快照互不干扰。

## 4. blade-agent 产物回收

### 4.1 路径提取（需求 4）

现状 `_agent_edited_paths`（`blade_service.py:168`）：

```python
if not any(marker in line for marker in _EDIT_TOOL_MARKERS):
    continue                      # ← BA 的分片事件在这里被全部丢掉
for pattern in _EDIT_PATH_PATTERNS:
    ...
    if raw.startswith("/"): continue   # ← 绝对路径也丢
```

实测 `att_2e47cfa79012`：15883 行命中编辑标记，最终只提取出 2 个路径。
原因是 BA 把工具调用拆成分片，`{"function": {"name": "Write"}}` 与
`{"arguments": "{\"file_path\": \"...\"}"}` 落在不同事件里。

改为三源合并：

```python
def _agent_edited_paths(attempt_dir: Path, *, workspace_root: str = "") -> list[str]:
    out = _OrderedSet()
    out |= _paths_from_events(attempt_dir)    # 不再要求同行有工具名
    out |= _paths_from_trace(attempt_dir)     # trace.jsonl 的结构化 arguments
    out |= _paths_from_mtime(attempt_dir)     # 与基线比 mtime/hash 的差异
    return list(out)
```

- `_paths_from_events`：去掉 marker 同行约束，改为**全文件扫** `file_path`/`path`，
  再用后缀白名单（源码/文档/表格类扩展名）与路径合法性过滤，避免把
  `Read` 的入参也当成编辑目标 —— 多拉几个文件的成本远低于漏掉真正的交付物。
- `_paths_from_trace`：`trace.jsonl` 是结构化的 `{tool_name, arguments:{...}}`，
  直接取 `arguments.file_path`，比正则可靠。
- `_paths_from_mtime`：复用横评期间验证过的判据 —— 物料拷贝进来的文件保留源仓库
  mtime（常差几天），agent 编辑集中在 attempt 运行那几分钟，
  以 attempt 目录 mtime 前推窗口即可分开。

绝对路径不再直接丢弃：若以 `workspace_root` 或 blade 项目路径为前缀，
剥掉前缀转成相对路径。

### 4.2 下载校验（需求 5）

```python
baseline = _baseline_digest(download_root / rel)   # 下载前的内容摘要
...
target.write_bytes(content)
if _digest(target) == baseline:
    priority_missing.append(rel)       # 内容没变 = 没真正拿到
    continue                            # 试下一个候选路径
priority_downloaded.append(rel)
```

`_baseline_digest` 在写入前取，缺失文件返回 `None`（此时任何内容都算命中）。

### 4.3 BFS 与记账（需求 6）

BFS 上限维持 500，但**优先路径不受其约束**：优先阶段跑完后，
若 `priority_missing` 非空，再对这些路径做一次定向重试（换候选前缀），
然后才进 BFS。`artifact_sync` 固定写四个字段：

```json
{"truncated_at": 500, "total_listed": 500,
 "priority_downloaded": [...], "priority_missing": [...]}
```

`priority_missing` 非空时，attempt 的 `failure_kind` 置 `infrastructure`（需求 6.3），
scorer 侧据此把 detail 写成「产物未回收」而非「未改动」（需求 5.4）。

## 5. 磁盘护栏（需求 7）

- **调度闸**：`run_dispatch` 在取新 attempt 前检查可用空间，低于
  `octagon.min_free_disk_gb`（默认 15）时不再启动新 attempt，
  run 状态记 `paused_low_disk`。
- **stop 路径可用性**：现状磁盘满时 stop 返回 500，根因是停止流程里有写盘动作
  （落 manifest / 写状态）。stop 的关键路径改为**先在内存里标记取消、再尽力落盘**，
  落盘失败不阻断取消。
- **错误可辨识**：磁盘导致的失败统一 `error_code: "disk_exhausted"`。

## 测试策略

| 需求 | 测试 |
|---|---|
| 1.2 | office 场景（ppt-visual-repair）改动前后各跑一次，比对 scorer 输出 |
| 1.3 | 跑一批 attempt，断言 `sandbox_home` 中位体积 < 10MB |
| 2.4 | 同一 attempt 用 copy / link 两种模式建快照，断言 `_snapshot_hash` 相同 |
| 2.2 | 构造跨设备场景（bind mount 到另一文件系统），断言回落且 `link_mode == "copy"` |
| 3.3 | 对 running 状态的 attempt 调归档，断言被拒 |
| 4.3 | 以 `att_2e47cfa79012` 的 events.jsonl 为 fixture，断言提取路径数 > 2 且含实际交付物 |
| 5.2 | mock `download_file` 返回与基线相同的内容，断言进 `priority_missing` 而非 `priority_downloaded` |
| 6.2 | 断言 `artifact_sync` 四字段恒存在 |
| 7.2 | 模拟磁盘满（`statvfs` mock），断言 stop 仍返回 200 |

## 上线顺序

2 → 3 → 1 → 4 → 7。

先做快照硬链接（改动最小、收益 32G、风险最低），再做归档（把历史数据也清掉），
这两步完成后磁盘压力即可解除；容器家目录瘦身需要改镜像与实测 office 场景，
放第三步；BA 回收独立于前三者，可并行但需要一次完整 BA 跑验证；
磁盘护栏最后，因为前面做完之后它的触发概率已经很低。
