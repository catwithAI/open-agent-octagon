# 需求文档

## 简介

Octagon 当前把本机 CLI 类 agent（claude-code / codex / kimi-code / opencode 家族）和 dsh 的 runtime 直接以宿主机子进程拉起。agent 与后端同用户、同文件系统，除了 cwd 指向 `data/attempts/<id>/skill_workspace` 之外没有任何边界。实际评测中反复观察到 agent 越过 workspace 找答案或绕过评测：

| 作弊面 | 路径 | 能拿到什么 | 本方案 |
|---|---|---|---|
| 场景目录 | `envs_path/<env>/` | `scorer.py` 评分逻辑、`tasks/*` 参考答案，与 `mcp_server.py` 同级 | 不进容器 |
| 兄弟 attempt | `data/attempts/<other>/skill_workspace` | 其他 agent 对同一题的产物 | 不进容器 |
| 后端数据库 | `data/octagon.db` | 历史分数、trace、评分快照 | 不进容器 |
| attempt 目录上一级 | `mcp_config.json`、`events.jsonl` | env_token；adapter 写的执行记录可被篡改 | 只交付只读子目录 |
| 判分阶段 | `skill_workspace` | agent 留后台进程在判分时改产物 | 判分前先杀容器 |
| 公网 | 任意 | 题解、上游补丁 | **不设防**，与现状一致，只记录 |

本功能把每个 attempt 的 agent 放进一个**只含 workspace 的容器**里执行，让答案物理上不存在于 agent 可见范围内，并把执行场合如实记进 attempt 元数据。网络不做限制。

## 关键决策

- **D-01：容器而非 OS 级沙盒。** 防作弊的本质要求是白名单可见性。seatbelt / sandbox-runtime / bubblewrap 是「默认全盘可读、黑名单排除」模型，`envs_path` 可配置、宿主机上还有其他仓库，黑名单列不全，漏一个就是泄露。容器是「没挂载就不存在」。Harbor（Terminal-Bench 2.0 的 harness）藏 `tests/` 与 `solution/` 用的是同一思路：不进镜像，判分时才上传。
- **D-02：Docker，不用 docker-py，不用 Apple container。** 生命周期只需 `docker run` / `kill` / `inspect` / `rm` 四个子进程命令，比同步的 docker-py 更贴合后端 asyncio；Apple container 在 Linux 评测机不能用。mac 开发机用 Docker Desktop 或 OrbStack。
- **D-03：镜像即被测环境。** 全部可测 agent CLI 及其依赖打进同一个镜像并钉版本，镜像 digest 记入 attempt。一镜像一组版本，不做同一 CLI 跨版本对比。这与后端 `Dockerfile` 顶部「CLI 不进镜像」的原则相反，需同步改写那段说明：后端镜像解决的是后端进程的资源隔离，agent 镜像解决的是被测环境本身要可锁定、可复现。
- **D-04：一个 attempt 一个容器，一个 turn 一次 `docker exec`。** 容器在 adapter 开跑前以 `sleep infinity` 起来，活整个 attempt；多轮 conversation 的每一轮、动态 iteration 的每一轮、dsh 的 runtime 进程都是对同一容器的 `docker exec -i`。attempt 结束（正常、Stop、超时、异常）时 `docker kill` 整容器。容器名与标签绑定 attempt_id，跨重启回收按标签查。不存在「只杀一轮」：超时本来就是 attempt 级的 `AttemptDeadline`。
- **D-05：同路径挂载。** workspace 在容器内使用与宿主机完全相同的绝对路径。场景 MCP server 按 `data/attempts/<id>/skill_workspace` 相对推导工作区的逻辑原样可用，`workspace_root`、安全分类器、产物扫描都不需要路径翻译。
- **D-06：网络不限制。** agent 容器接普通 bridge 网络，出网与现状一致。不做白名单代理，不禁用 LLM 服务端工具（WebSearch / WebFetch），只在 `security_meta` 记录 agent 是否具备服务端联网能力。理由：agent 今天就能上网，评测口径不因沙盒而收窄；网络围栏与「不裁剪原生能力」冲突，且各家 agent 的服务端工具旁路大小不一，围了也围不齐。
- **D-07：沙盒全局强制，不可用时明确失败。** `sandbox.enabled: true` 时所有本机 CLI agent 与 dsh 都进容器，不按 agent 可选，run 级不提供覆盖。镜像缺失、docker 不可达、agent 不在镜像里，attempt 以明确的 error_code 失败，不静默回落到宿主机执行。调试需求用 `enabled: false` 的独立实例解决。
- **D-08：dsh 的 runtime 进容器，SDK 客户端留在后端。** 已核实 SDK 与 runtime 之间只有 stdio 一条通道，`session_root` / `cordis` / `cwd` 都是 SDK 打包成环境变量传给 runtime，SDK 自身不读写这些路径。`launch_args_override` 替换 Popen argv 即可。
- **D-09：远端执行的 adapter 只记录、不伪装、不禁用。** blade-agent 的沙盒由 blade server 管理，ssh-claude-code 在远端主机跑；两者都不在本方案围栏内，`security_meta` 如实标记，对比页提示口径不一致。不因沙盒开关而禁用它们。
- **D-10：场景 MCP 入口单文件复制。** 落实 `docs/environments.md` 既有契约（`mcp_server.py` 不 import `core.py`，只经 HTTP 回连后端）：只把入口文件本身复制进容器，不挂载场景目录。「后端直出 MCP over HTTP」作为后续改进记录，随场景重设计一起做。
- **D-11：判分前先杀容器。** agent 退出后 launcher 先 `docker kill` 再进入 scoring，agent 留下的后台进程无法在判分阶段改 workspace。抄自 Harbor separate-verifier 模式。

## 术语

- **workspace**：`data/attempts/<id>/skill_workspace`，agent 唯一可写的业务目录，scorer 只看这里。
- **sandbox_ro**：`data/attempts/<id>/sandbox_ro/`，attempt 级只读目录，只含 `mcp_config.json`、prompt 文件、MCP 入口文件副本、dsh 的 cordis.yml。**不能**把整个 attempt 目录挂进容器，否则 adapter 写的 `events.jsonl` 会暴露。
- **sandbox_home**：`data/attempts/<id>/sandbox_home/`，容器内 `/home/agent`，各 CLI 的隔离 HOME；会话记录留在宿主机侧当产物。
- **launcher**：进程启动抽象。`HostLauncher` 包装现有 `backend/process/runtime.py::agent_process()`，`DockerLauncher` 组装 `docker run`。
- **execution_locus**：`security_meta` 里的执行场合，取值 `host` / `docker-sandbox` / `remote-host` / `docker-sandbox (blade-managed)`。
- **本机 agent**：claude-code、codex、kimi-code、opencode、mimo-code、dsh 六个，即沙盒强制覆盖的范围。

## 需求

### 需求 1 - 文件系统白名单可见性

**用户故事：** 作为评测维护者，我想让 agent 在容器里只看到自己的 workspace 和显式交付的物料，以便场景答案、其他 attempt 和后端数据对 agent 物理上不存在。

#### 验收标准

1. 当 attempt 以沙盒模式执行时，容器内只挂载 workspace（rw、同路径）、sandbox_home（rw，`/home/agent`）、sandbox_ro（ro，`/attempt`）三处；`envs_path`、`data` 其余部分、后端源码、宿主机 HOME、宿主机上安装的任何 CLI 一律不挂载。
2. 在容器内执行 `find / -name scorer.py`，结果应该为空；`envs_path` 与 `data/octagon.db` 不可达；`data/attempts/` 下只能看到本 attempt 的目录骨架。
3. 当 agent 尝试写 `/attempt`、`/etc` 或 workspace 之外任何路径时，系统应该拒绝。
4. 场景 `entrypoints.mcp` 声明的入口脚本必须只拷贝**那一个文件**到 `sandbox_ro/mcp/`，不得挂载或拷贝场景目录的其他内容。
5. 容器以宿主机后端进程的 uid:gid 运行（Linux 传 `--user`；mac 由 Docker Desktop 文件共享层处理，不传），workspace 产物在宿主机侧可读可写，不出现 root 属主文件。
6. 容器进程环境变量里不得出现 env_token 之外的评测内部路径或凭据；env_token 只出现在 `mcp_config.json` 与 MCP server 子进程 env 里，与现状一致。
7. adapter `run()` 返回后（最后一轮结束），launcher 的 attempt 上下文必须在 scoring 开始前完成 `docker kill` 与 `docker wait`；scorer 读取 workspace 时容器已不存在运行中的进程。

### 需求 2 - 网络口径记录

**用户故事：** 作为看对比结果的人，我想知道每个 agent 的联网能力，以便理解沙盒没有围住什么。

#### 验收标准

1. agent 容器接普通 bridge 网络，出网不限；后端不创建专用网络、不运行代理。
2. Env Attempt Server 对容器的地址是 `http://host.docker.internal:<port>`，配置项 `sandbox.env_base_url` 缺省按 `public_base_url` 的端口推导；后端必须绑定 `0.0.0.0` 或 docker 网桥地址，绑定 `127.0.0.1` 时启动检查报错。Linux 上 launcher 传 `--add-host host.docker.internal:host-gateway`。
3. `security_meta.egress_policy` 固定记 `unrestricted`；`security_meta.server_side_network` 记录该 agent 是否具备 LLM 服务端执行的联网工具（claude-code 的 WebSearch / WebFetch、codex 的 web search），取值 `available` / `none`。
4. 配置项 `sandbox.server_side_tools` 默认 `allow`；设为 `deny` 时经 CLI settings 禁用上述工具并在 `server_side_network` 记 `disabled`。场景 `meta.yaml` 可按场景覆盖。

### 需求 3 - 镜像即被测环境

**用户故事：** 作为评测维护者，我想让同一镜像下的结果跨机器可比，以便升级任何一个 CLI 版本都是显式、可追溯的动作。

#### 验收标准

1. 全部六个本机 agent 的 CLI / runtime 及其依赖打进同一个镜像；Dockerfile 中每个 CLI 的版本由 `ARG` 显式传入，不允许 `latest`。首版镜像必须收齐六个，缺一个即视为未完成。
2. 每个 CLI 装完后 `--version` 输出写进镜像标签 `octagon.agent.<name>.version`，运行时由 launcher `docker inspect` 读出并写入 `security_meta`。
3. `security_meta.sandbox_image` 记录 `repo@sha256:digest`，`sandbox_id` 记录容器 id。
4. 后端启动时校验配置的镜像存在，且标签里包含本部署启用的每个本机 agent；缺失时该 agent 的 attempt 以 `sandbox_agent_missing` 失败，镜像不存在以 `sandbox_image_missing` 失败。
5. 基础镜像与 Linux 评测机同发行版（Ubuntu 24.04），保证 glibc 与原生二进制 CLI 对齐。
6. 镜像内 `mcp` 钉 `<2` 且与主项目 `uv.lock` 对齐；dsh runtime 二进制版本与后端 `dsh` extra 钉的同一个。
7. 场景依赖全部进镜像：`meta.yaml` 新增 `prerequisites.python` 列表，镜像构建脚本合并所有已加载场景的声明；字段缺省视为无额外依赖。过渡期由 `docker/agent-runtime/requirements-envs.txt` 兜住现有场景已知依赖（如 matplotlib），现有场景文件不改。
8. 镜像由 CI 在 release tag `sandbox-v*` 触发构建并推送 registry，流程与 blade-agent 的 `sandbox-image.yml` 一致；本地 `make sandbox-image` 仅供开发。
9. 前端对比页按 `sandbox_image` 分组，跨镜像的 attempt 出现在同一 run 内时给出提示。

### 需求 4 - 生命周期与回收

**用户故事：** 作为评测维护者，我想让 Stop、超时、后端重启对沙盒 attempt 的语义与宿主机执行一致，以便不会留下孤儿容器或误杀正在跑的 agent。

#### 验收标准

1. 容器带标签 `octagon.attempt_id` 与 `octagon.run_id`，容器名 `octagon-agent-<attempt_id>`；在 adapter `run()` 之前以 `docker run -d ... sleep infinity` 创建，`run()` 期间每一轮（含 answer_interaction 轮与动态 iteration 轮）以 `docker exec -i` 在同一容器内启动 CLI 进程。
2. Stop 与超时的权威动作是 `docker kill <container>`，随后 `docker wait` 回收；杀 `docker exec` 客户端进程不算完成。某一轮的进程需要终止时同样 `docker kill` 整容器，因为超时是 attempt 级的，attempt 随之结束。
3. 后端关停时不杀容器，与现有「后端关停不杀 CLI 进程」语义一致。
4. `agent_process.json` 新增 `kind: docker` 与 `container_id`，attempt 期间只写一次；跨重启回收改为 `docker inspect` 判活，`docker ps -f label=octagon.attempt_id` 兜底扫描。
5. `--rm` 不开；收尾采集每轮 exec 的 exit code 与 `docker logs` 尾部到 `sandbox_container.json` 后再 `docker rm`。sweeper 按标签清理 `exited` 超过宽限期的容器，以及创建后超过 attempt 最大时限仍在 `running` 的孤儿容器。
6. 按 attempt 施加 `--memory`（与 `--memory-swap` 相等）、`--cpus`、`--pids-limit`、`--cap-drop ALL`、`--security-opt no-new-privileges`。默认 4g / 2.0 / 1024，可按 agent 覆盖。

### 需求 5 - 场景 MCP 入口契约

**用户故事：** 作为场景作者，我想知道 MCP 入口脚本在沙盒里怎么被启动、能依赖什么，以便我的场景不会在沙盒模式下静默失效。

#### 验收标准

1. dispatch 从 `entrypoints.mcp.command` 中解析出唯一的入口 `.py` 文件；解析失败时 attempt 以 `sandbox_mcp_entry_unresolvable` 失败。
2. 容器内的 mcp_config 命令翻译为 `["python3", "/attempt/mcp/<file>"]`，env 保留 `OCTAGON_ATTEMPT_ID` / `OCTAGON_ENV_TOKEN` / `OCTAGON_BASE_URL`，其中 `OCTAGON_BASE_URL` 使用沙盒地址。
3. `env_loader` 静态校验入口脚本不得 import 同目录模块；违反者加载时告警，沙盒模式下拒绝调度。
4. `docs/environments.md` 增加沙盒契约：入口文件自包含、只依赖 `mcp`、`httpx`、标准库与 `prerequisites.python` 声明的包、不读 `envs_path` 下其他文件；需要给 agent 看的物料只能走 `materials.agent`。

### 需求 6 - dsh 接入

**用户故事：** 作为评测维护者，我想让 dsh 与 CLI 类 agent 受同样的边界约束，以便同一 run 里不存在一个「没有沙盒」的本机 agent。

#### 验收标准

1. 沙盒模式下 `DeepSeekHarnessConfig.launch_args_override` 设为 `docker exec -i <container> ...` 加容器内 runtime 路径，容器就是本 attempt 的那一个，SDK 客户端逻辑不变。
2. SDK 组装的 runtime 环境变量（`DSH_CWD`、`DSH_SESSION_ROOT`、`DSH_CORDIS_CONFIG`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_API_KEY` 及 `config.env` 内其余项）翻译为 `-e` 参数进容器；`config.env` 本身只作用于 docker 客户端进程。
3. `cordis.yml` 由 adapter 在宿主机生成放进 `sandbox_ro/`，经 `DSH_CORDIS_CONFIG` 传入；`dsh_sessions`、`.dsh`、`.agents` 目录改到 `sandbox_home` 下；workspace 内挡 rank 200 skill 的空 `.git` 保留。
4. 超时硬杀路径上，SDK `close()` 只杀 docker 客户端进程，容器内 runtime 由 attempt 上下文退出时的 `docker kill` 收掉；`close()` 必须在 attempt 上下文内调用，集成测试断言硬杀后容器内无 runtime 进程存活。
5. `tests/test_adapter_spawn_guard.py` 的 EXEMPT 表保持，dsh 通过 attempt 上下文的 `build_exec_argv` 低层入口而非 `exec()` 接入，容器创建与销毁仍由同一个 attempt 上下文管。

### 需求 7 - 远端 adapter 记录

**用户故事：** 作为看对比结果的人，我想知道 blade-agent 与 ssh-claude-code 不在本方案围栏内，以便在防作弊维度上不把它们当作同等隔离。

#### 验收标准

1. blade-agent 的 `security_meta` 增加 `sandbox_shared: true`、`egress_policy: unrestricted`，`execution_locus` 展示为 `docker-sandbox (blade-managed)`。
2. ssh-claude-code 的 `security_meta` 增加 `sandbox: none`，`execution_locus` 保持 `remote-host`。
3. 沙盒强制开启时，上述两个 adapter 照常可用，不因 `sandbox.enabled` 被拒绝调度。
4. blade adapter 在产物回收后显式删除 blade session 的 workspace 目录；chat 结束后立即通过文件 API 删除 `.octagon/attempt.json`。同 run 内 blade attempt 不做串行、不做多账号。
5. 对比页对 `sandbox_shared` 或 `sandbox: none` 的 attempt 给出口径提示。

### 需求 8 - 配置与失败语义

**用户故事：** 作为部署人员，我想通过一个配置段开启沙盒，并在环境不满足时得到明确错误，以便不会出现「以为在沙盒里其实在宿主机上」的结果。

#### 验收标准

1. `octagon.yaml` 增加 `sandbox` 段：`enabled`、`image`、`env_base_url`、`limits.{memory,cpus,pids}`、`server_side_tools`、`agents.<name>.limits`；不允许按 agent 换镜像。
2. `enabled: true` 时 `build_adapter()` 给六个本机 agent 的 adapter 注入 `DockerLauncher`；run 级不提供覆盖，同一 run 内本机 agent 的执行场合必须一致。
3. docker 不可达、镜像缺失、启用的本机 agent 不在镜像标签里、`public_base_url` 绑定 127.0.0.1，均为启动检查项；沙盒模式下检查失败则拒绝调度并给出对应 error_code，不回落宿主机执行。
4. `AdapterCapabilities.execution_locus` 从类常量改为随 launcher 取值。

## 非目标（本期不做）

- **不限制**出网，不做白名单代理、专用网络或流量记录。
- **不裁剪** agent 的原生能力（skills、Task、内置工具、服务端工具）；沙盒只限制能看到什么。
- **不做**同一 CLI 跨版本对比；一镜像一组版本。
- **不做** wire 层 MCP stdio tap 的容器内注入；沙盒模式下声明 `not-applicable`，与 ssh adapter 同样处理。
- **不改** blade server 的沙盒实现；不改 ssh-claude-code。
- **不改**现有 76 个场景的文件；`prerequisites.python` 字段随后续场景重设计填写。
- **不新增**任何 run 级沙盒开关。

## 后续改进

- **后端直出 MCP over HTTP。** `core.py` 里 `@env_tool` 注册的工具已带 name / description / parameters，Env Attempt Server 可挂一个 streamable-http MCP 端点 `/attempts/{id}/mcp`，Bearer 鉴权复用 env_token。容器里不再需要任何场景文件与 `mcp` / `httpx` 依赖，`mcp_server.py` 薄壳整个消失，场景依赖全部留在后端，`sandbox_mcp_entry_unresolvable` 错误码与 `prerequisites.python` 字段随之删除。claude-code、codex、opencode 均支持 http 类型 MCP 配置，dsh 的 cordis mcp-client 待核。这改的是场景契约本身，随场景重设计一起做。
