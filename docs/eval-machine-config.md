# 评测机配置速查

写给「要跑横评、但不想读完 339 行 octagon.yaml」的人。
对应评测机 49（`bladeai@100.86.179.54`，仓库在 `~/codes/open-agent-octagon`）。

最后更新：2026-09-21。

---

## 一、只有 6 个旋钮需要你关心

339 行里绝大多数是**设一次就不动**的。横评期间真正会调的只有这些：

| 配置项 | 现值 | 什么时候改 |
|---|---|---|
| `octagon.max_active_attempts` | **6** | 并发。机器 14G 内存，`6 × sandbox.limits.memory` 是内存上限，别超 |
| `sandbox.limits.memory` | **3g** | 单容器内存。曾是 4g，6 并发时理论 24G 打爆 14G 机器，降到 3g |
| `sandbox.image` | `octagon-agent-runtime:pr9` | 换 agent 版本或改 Dockerfile 后要重建并改这里 |
| `blade.keep_blade_session` | **false** | 排查 BA 时临时开 true（能看到它的项目目录），**查完必须改回**，否则 session 无限累积 |
| `octagon.research_capture_max_policy` | **parsed** | wire 采集详细度。磁盘吃紧时降到 `metadata` |
| `llm_judge.model` | `deepseek-4.1-flash` | 换判分模型 |
| `octagon.scoring_deadline_seconds` | **960** | 见下方「判分超时」 |

### ⚠️ 判分超时：两个超时值必须对齐

`octagon.scoring_deadline_seconds`（平台杀评分）与 `llm_judge.timeout`（judge 自己放弃）
是**两个独立的超时**，配反了会丢分数：

- 默认 `scoring_deadline_seconds=300`，而 `llm_judge.timeout=900`
- → judge 还有 600s 额度没用完，平台先把评分杀了
- → 记 `scoring_deadline_exceeded`，`score_total` 保持 NULL，**这个 attempt 的分数就此丢失**

实测 `gdpval-prepaid` 的判分最长 **301s**（均 41s），正好卡在 300s 上。
已显式设成 **960**（> judge 的 900），让 judge 自己的超时先生效——
那样至少能拿到一个明确原因，而不是被平台无差别掐断。

改完要重启才生效（deadline 在 job 执行时从 `state.settings` 读）。

其余小节（`insights` / `cost` / `research_features` 等）跑横评时不用动。

### 新增的磁盘护栏（PR #9）

`octagon.min_free_disk_gb` 未显式配置时默认 **15G**：可用空间低于它就不再调度新
attempt，并把失败标成 `disk_exhausted`（infrastructure，不算 agent 的锅）。
2026-09-18 的横评就是在没有这道闸的情况下把盘写到 0 字节被迫中止的。

---

## 二、12 个 provider 其实只有 6 个在用

`model_providers` 是最唬人的一段，但规律很简单——**看有没有 `agent:` 绑定**：

| 家族 | base_url | 绑定 agent | 状态 |
|---|---|---|---|
| `bl-cc` / `bl-kimi` / `bl-opencode` / `bl-mimo` / `bl-dsh` | `llm.bladeai.com.cn` | claude-code / kimi-code / opencode / mimo-code / dsh | **在用** |
| `ds-codex` | `api.deepseek.com/v1` | codex | **在用** |
| `or-*`（6 个） | `openrouter.ai` | 无 | **备用，没接线** |

**为什么 codex 单独走 DeepSeek 官方**：codex 强制要求 `/v1/responses` 端点，
blade 网关返回 404 route not found。所以是「5 个走 blade + codex 走 DeepSeek」，
不是 6 个都切。

**为什么 `bl-cc` 的 base_url 没有 `/v1`**：它是 `anthropic-messages` 协议，
路径规则与 openai-chat-completions 不同。别照着别人补 `/v1`。

---

## 三、judge 必须靠环境变量，改 yaml 不够 ⚠️

这是最坑的一条，2026-09-18 横评里 **109 个 attempt 因此全判 0 分，跨全部 7 个 agent**。

env 的 judge 以子进程运行，自己找 `octagon.yaml` 时用的是 `_repo_root()`，
对 `gdpval-*/judge_local.py` 来说解析成 `/home/bladeai/codes`（少一层目录），
那里没有 octagon.yaml → 读成空配置 → 判「未配置」给 0 分。

`~/restart-octagon.sh` 已经 export 了三个变量，**用这个脚本重启就没事**：

```bash
export OCTAGON_CONFIG_PATH=/home/bladeai/codes/open-agent-octagon/octagon.yaml
export LLM_JUDGE_MODEL=deepseek-4.1-flash
export BLADE_API_KEY=sk-blade-...
```

三个 judge 是三份**独立实现**（gdpval-prepaid / gdpval-source / presentbench），
改完要逐个验证：`.venv/bin/python ~/octagon-eval-tools/verify_all.py`。

> PR #9 之后，judge 起不来不再被记成 agent 的 0 分：`score_total` 保持 NULL、
> `failure_kind='scoring'`、`error_code='judge_unavailable'`。但**配置本身还是要配对**，
> 否则只是从「错误的 0 分」变成「没有分」。

---

## 四、改配置的正确姿势

```bash
# 1. 备份统一放这里，别再散在仓库里
mkdir -p ~/octagon-archive/yaml
cp octagon.yaml ~/octagon-archive/yaml/octagon-$(date +%Y%m%d-%H%M%S).yaml

# 2. 改完先验证能解析（比重启后看日志快得多）
.venv/bin/python -c "
import yaml; from backend.config import Settings
s=Settings.model_validate(yaml.safe_load(open('octagon.yaml')))
print('ok', s.sandbox.image, s.octagon.max_active_attempts)"

# 3. 重启（必须用这个脚本，它带 judge 三变量 + ulimit 65536）
~/restart-octagon.sh

# 4. 健康检查用 /api/agents（/api/health 是 404）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8100/api/agents
```

`octagon.yaml` 已在 `.gitignore` 里，切分支不会动它。

**kimi-code / opencode / mimo-code 在 `/api/agents` 显示 `not_found` 是正常的**——
它们的 CLI 只装在沙盒镜像里，不在宿主机 PATH。

---

## 五、沙盒镜像：改之前必读

### 重建镜像会静默降级 agent ⚠️

评测机有 4 个**没推上游的本地提交**，其中一个把 agent 版本升到了
claude 2.1.270 / codex 0.154.0 / opencode 1.18.30 / mimo 0.1.14，
而 `origin/main` 的 `versions.env` 还是 2.1.245 / 0.149.1 / 1.18.5 / 0.1.9。

**照 main 重建镜像 = 这 4 个 agent 被静默降级，新旧数据不可比。**
重建前先确认 `docker/agent-runtime/versions.env` 是评测机那一版。

### 家目录哪些目录能挡、哪些不能

PR #9 把可重建的运行时目录挂成匿名卷/tmpfs，不再堆进 attempt 目录（省约 12G）。
**判据不是体积，是有没有人在容器启动前写过它**：

| 能挡（纯运行期缓存） | **绝不能挡**（adapter 在容器启动前写） |
|---|---|
| `.npm` `.cache` `.tmp` | `config`（provider 配置+key）`.config` `.local` `.claude` |
| `lo` `loroot` `sysroot` `apt` `fonts` `.fonts` | `.dsh` `.agents` `dsh_sessions`（会话证据） |

挡错了 = agent 读不到配置，直接跑不起来。`tests/test_docker_launcher.py` 里有
两个测试钉着这条（`test_adapter_written_dirs_are_never_ephemeral` 与
`test_dockerfile_precreates_every_ephemeral_dir`），改清单时它们会拦你。

**匿名卷的属主权限继承镜像里该路径的目录**：镜像里没预建就是 `root:root 0755`，
而容器以宿主机 uid 跑 → 不可写。所以清单里每一项都必须在 Dockerfile 预建并 0777。

---

## 六、磁盘

```bash
~/disk-guard.sh 15                                    # 低于 15G 时清 scoring-work
python -m backend.tools.archive_attempts --data-path ./data          # dry-run，看能回收多少
python -m backend.tools.archive_attempts --data-path ./data --yes    # 真删
```

归档只删 `sandbox_home` / `sandbox_ro` / `.opencode-iso-home` 这类可重建目录，
保留 wire / workspace / events / trajectory 等证据，并在 DB 记 `archived_at`
（读取方据此区分「没采集到」与「已归档回收」）。

实测：282 个 attempt 可回收 **29.7 GB**。

> PR #9 之后评分快照改硬链接，一个 162MB 的 attempt 快照只占 **5.2MB（3%）**，
> 不再是「每份产物存两遍」。

---

## 七、常用命令

```bash
# 提交一次横评（7 agent × 5 轮）
.venv/bin/python ~/octagon-eval-tools/launch3fix.py     # 补 judge 修复后的 3 个场景
.venv/bin/python ~/octagon-eval-tools/launch_all.py     # 全部场景

# 看进度 / 停止
.venv/bin/python ~/octagon-eval-tools/monitor.py
.venv/bin/python ~/octagon-eval-tools/stop2.py

# 出四矩阵
.venv/bin/python ~/octagon-eval-tools/build_report_data.py
```

**ssh 优先用 `bladeai@100.86.179.54`（tailscale）**，`192.168.130.49` 会间歇断连。

`pkill` / `pgrep -f` 时用 `backend[.]main` 这种写法，否则会匹配到自己的 ssh 命令行
（实测踩过：`pgrep -f "build.sh"` 把自己那条 ssh 也算进去，误判成"还在构建"）。
