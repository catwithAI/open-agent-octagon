# 开发基线（2026-10-22）

工作区分支 `feat/skill-eval-spec`。开始时已有 SDK/CLI 升级与设计文件的未提交改动；本轮保留这些内容，未创建提交。

## 依赖与实际版本

- Python 3.12.12；锁定 SDK `blade-agent-kit==2612.0.0b41`，安装元数据显示 `2612.0.0-beta.41`，为同一测试版的两种表示。
- `.venv/bin/blade version`：`blade 2612.0.0-beta.41 (commit 8690d27)`。本版不接受 `--version`。
- Node v25.6.1、npm 11.9.0；CI 使用 Node 20，尚未在本机复验 Node 20。
- `uv sync --locked --extra dev --extra blade` 成功。本沙箱不能写默认 uv/npm 缓存，使用 `/tmp/octagon-skill-eval-uv`、`/tmp/octagon-skill-eval-npm`；依赖下载经允许的网络执行完成。
- worktree 不存在 `octagon.yaml`，既有配置未提供 Blade API key，亦未设置 `BLADE_AGENT_TOKEN`；没有真实调用目标预览服务或创建会话。

## 测试基线与纠正

1. 初始 `uv run pytest -q`：249 passed、4 skipped、1 failed。失败为 `test_cli_not_found_is_terminal_not_crash`：测试假设 PATH 不存在 blade，而本 worktree 已安装 CLI，导致返回 auth_failed。
2. 该测试增加 `shutil.which` 替身，只隔离查找环境，不改 adapter 行为。`uv run pytest -q tests/test_blade_sdk_compat.py tests/test_blade_cli_adapter.py tests/test_blade_priority_recovery.py`：38 passed。
3. `npm --prefix web ci` 安装锁文件依赖成功；`npm --prefix web run build` 成功（有已有的 bundle 大小提示）。
4. `npm --prefix web test`：65 passed、88 failed，共 153 项/23 文件，退出码 **1**。14 个文件失败、9 个通过。主要失败为组件未包装 I18nProvider；另有 localStorage.setItem 不可用。CI 注释“85/147、失败返回 0”与本机当前事实不符，后续门禁不能据该注释忽略新模块失败。
5. 后端新增文件后的累计结果见 implementation-progress.md，不能把新增模块测试视为真实执行验收。

前端原始本地日志：`/tmp/skill-eval-frontend-baseline.log`；初始后端日志：`/tmp/skill-eval-backend-baseline.log`。这些是当前机器的临时记录，不包含真实服务请求或鉴权。

## 最终回归对照（2026-10-22）

后端全量 392 passed / 4 skipped（9.97s），含新增采集策略 API 回归；最终新增模块数量见 acceptance。旧 Blade 聚焦 38 项通过。新前端 29 项通过；全前端 94 passed / 88 failed，新增通过 29，失败数未增长；build 成功（大 chunk 警告）。真实主链与停止链使用本地 SDK/CLI beta.41、远端 beta.44，证据独立存于 evidence，不覆盖本文件原始基线。
