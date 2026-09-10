# 实施计划

按"可交付用户价值"拆分。顺序：先把启动层抽出来保证零回归，再出镜像与配置，再让 claude-code 真正跑进沙盒，之后其余本机 agent 接入，最后观测、场景契约与远端 adapter 记录。沙盒强制开关在任务 5 完成、六个本机 agent 全部可在容器里跑之后才在生产配置打开。

- [x] 1. Launcher 抽象（零行为变化）
  - 新增 `backend/process/launcher.py`：`AttemptSpec` / `ExecSpec` / `AgentLaunch` / `AttemptSandbox` / `AgentLauncher`，`HostLauncher.attempt()` 为空上下文、`exec()` 包装现有 `agent_process()`
  - 五个 CLI adapter（claude-code / codex / kimi-code / opencode 家族）`run()` 最外层包 `launcher.attempt()`，每轮改经 `sandbox.exec()`；`build_adapter()` 注入 `HostLauncher`
  - `AdapterCapabilities.execution_locus` 改为随 launcher 取值
  - 现有 adapter 单测全绿 + `HostLauncher` 等价性单测
  - _需求：8.2, 8.4_

- [x] 2. agent 运行时镜像（六个全收）
  - `docker/agent-runtime/Dockerfile` + `versions.env` + `requirements-envs.txt`：ubuntu 24.04、系统依赖、node 22、`mcp<2` + `httpx`、场景依赖合并安装
  - claude-code（官方安装器指定版本）、codex（release 静态二进制）、kimi-code（pip `kimi-cli`）、opencode（npm `opencode-ai`）、mimo-code（npm，包名实施时核）、dsh runtime（从 wheel 抽出到 `/opt/dsh/runtime`）；版本全部 `ARG`，`--version` 写镜像标签
  - 构建脚本合并所有已加载场景 `meta.yaml` 的 `prerequisites.python`；`env_loader` 识别该字段，缺省为空
  - `.github/workflows/sandbox-image.yml` 照抄 blade-agent：`sandbox-v*` release 触发，推阿里云与 ghcr；`make sandbox-image` 本地构建
  - 镜像 smoke 测试：逐 CLI `--version` 与标签比对、依赖 import
  - 改写后端 `Dockerfile` 顶部「CLI 不进镜像」说明
  - _需求：3.1, 3.2, 3.5, 3.6, 3.7, 3.8_

- [x] 3. 沙盒配置与启动检查
  - `Settings` 增加 `sandbox` 段：`enabled` / `image` / `env_base_url` / `limits` / `server_side_tools` / `agents.<name>.limits`
  - 后端启动检查：docker 可达、镜像存在且标签含每个启用的本机 agent、`public_base_url` 非 127.0.0.1；`env_base_url` 缺省推导
  - 失败 error_code：`sandbox_image_missing` / `sandbox_agent_missing` / `sandbox_unavailable`
  - _需求：2.2, 3.4, 8.1, 8.3_

- [x] 4. DockerLauncher + claude-code 跑进沙盒
  - `DockerLauncher.attempt()`：`docker run -d ... sleep infinity`，挂载表（同路径 workspace、`/home/agent`、`/attempt`）、标签、限额、cap-drop、`--user` 取宿主机 uid/gid（mac 不传）、`--add-host host.docker.internal:host-gateway`；退出时 `docker kill` + `docker wait`，保证 scoring 前容器无存活进程
  - `AttemptSandbox.exec()`：`docker exec -i -w <cwd> -e ... <container> <argv>` 管道；多轮 conversation 与动态 iteration 逐轮 exec，session 经 `/home/agent` 跨轮延续
  - dispatch 生成 `sandbox_ro/`（mcp_config、prompt、MCP 入口单文件副本）与 `sandbox_home/`
  - MCP 入口翻译：从 `entrypoints.mcp.command` 解析唯一 `.py`，失败 → `sandbox_mcp_entry_unresolvable`
  - claude-code adapter：镜像内 `claude`、容器内 mcp_config 路径、`CLAUDE_CONFIG_DIR`、`server_side_tools: deny` 时写 settings deny 列表、`security_meta` 填 image digest / container_id / 版本 / egress_policy / server_side_network
  - 生命周期：`agent_process.json` 记 `kind: docker`；Stop/超时 → `docker kill` + `docker wait`；关停不杀；重启后 `docker inspect` 判活 + 标签兜底；收尾写 `sandbox_container.json` 再 `docker rm`；sweeper 清 exited
  - 单测（argv/挂载/翻译）+ docker 集成测试（fake CLI：可见性、只读、属主、host.docker.internal 可达、退出后已 kill、回收；无 docker skip）
  - _需求：1.1–1.7, 2.1, 2.3, 2.4, 3.3, 4.1–4.6, 5.1, 5.2_

- [x] 5. 其余本机 agent 接入
  - codex adapter：镜像内 `codex`、`CODEX_HOME`、`-C` 同路径、web search 开关
  - kimi-code adapter：镜像内 `kimi`、`KIMI_CODE_HOME` 指向 `/home/agent`、mcp.json 命令翻译
  - opencode / mimo-code adapter：镜像内 CLI、`*_CONFIG_DIR`、config `mcp` 段命令翻译
  - dsh adapter：`run()` 包 `launcher.attempt()`，`launch_args_override = sandbox.build_exec_argv()`、SDK env 翻译为 `-e`、cordis.yml 进 `sandbox_ro`、状态目录挪到 `sandbox_home`、`close()` 在上下文内调用
  - 单测：每个 adapter 断言 argv 与 env；dsh mock `Popen`，超时路径断言上下文退出时 `docker kill`
  - 本任务完成后生产配置 `sandbox.enabled: true`
  - _需求：6.1–6.5, 8.2_

- [x] 6. 观测与前端
  - wire http proxy 启用时 `llm_base_url` 改 host.docker.internal；MCP tap 沙盒模式声明 `not-applicable`
  - 前端：执行场合卡片展示 `sandbox_container.json`；对比页按 `sandbox_image` 分组提示；对 `sandbox_shared` / `sandbox: none` 的 attempt 给口径提示
  - _需求：3.9, 7.5_

- [x] 7. 场景契约与远端 adapter 记录
  - `env_loader` 静态校验 MCP 入口自包含；`docs/environments.md` 增加沙盒契约与 `prerequisites.python` 字段说明；外部场景仓库过一遍校验（只报告，不改场景）
  - blade adapter：`security_meta` 加 `sandbox_shared` / `egress_policy`；产物回收后删 session workspace、chat 结束删 attempt.json
  - ssh-claude-code adapter：`security_meta` 加 `sandbox: none`
  - 确认两者在 `sandbox.enabled: true` 下照常调度
  - _需求：5.3, 5.4, 7.1–7.4_

## 交付顺序说明

任务 1 单独可合，是后面所有改动的回归基线。任务 2、3 可并行。任务 4 完成后 claude-code 即可在开发实例的沙盒里跑真实 run，此时就能对比沙盒前后的作弊率。任务 5 之后六个本机 agent 全部可进容器，生产配置才打开 `sandbox.enabled`。任务 6、7 是观测与收尾，可按 PR 拆。每个任务提交前跑相关单测；docker 集成测试在有 docker 的 CI runner 上跑。

## 已决事项（2026-09-09 评审）

- 沙盒全局强制，不按 agent 可选，无 run 级开关。
- 网络不限制，不做白名单代理；`server_side_tools` 默认 `allow`。
- 场景依赖全进镜像，`meta.yaml` 加 `prerequisites.python`，现有场景不改，过渡期 `requirements-envs.txt` 兜底。
- MCP 入口单文件复制；「后端直出 MCP over HTTP」列入后续改进。
- blade-agent 与 ssh-claude-code 只记录，不串行、不多账号、不禁用。
- dsh 只有 runtime 进容器，SDK 留宿主机。
- 一镜像一组版本，不做跨版本对比；CI 照抄 blade 的 `sandbox-image.yml` 推阿里云。
- 首版镜像收齐六个本机 agent。
- 一 attempt 一容器，每轮 `docker exec`；最后一轮退出即杀容器再判分；容器用户取宿主机 uid/gid；限额默认 4g / 2.0 / 1024。

## 实施记录（2026-09-09）

全部七个任务已实现，分支 `feat/agent-sandbox`，未提交。评测机实测见 design.md「验证记录」。

尚未验证 / 留待后续：
- 真实模型在沙盒里完成一个带 MCP 的任务：评测机与开发机都没有 provider key，
  claude-code 与 codex 都在容器里跑到了「未登录 / 401」这一步（容器、挂载、
  MCP 入口翻译、host.docker.internal 回连、exec 记录、杀容器、security_meta 全链路已通）。
- kimi-code / opencode / mimo-code / dsh 只做了单测与镜像 smoke（`--version`），
  没有在容器里跑过真实 attempt。kimi-cli 钉的是 1.50.0（docs/agents.md 契约基于 0.29.1，
  已不在 PyPI），事件流契约需重新核对。
- codex 的 `server_side_tools: deny` 用的 `-c web_search="disabled"` 未实测。
- CI 工作流 `sandbox-image.yml` 未跑过（需要 `ALIYUN_REGISTRY_*` 凭据与 release tag）。
- 前端只加了状态文案与 LocusTag 悬浮信息，对比页按 `sandbox_image` 分组提示未做。

## 待决问题

无。
