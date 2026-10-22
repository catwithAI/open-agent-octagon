# Skill Eval 验收记录

日期：2026-10-22。范围：首期 blade-agent Skill Eval；未提交、合并或部署，配置默认仍禁执行。下文 pass 表示当前产品约定在列出的环境与用例中通过，不表示所有部署、模型和故障窗口均已穷举。

## 任务完成与实施差异

| 任务 | 状态 | 证据 |
| --- | --- | --- |
| T01–T04 基线/夹具/协议 | 完成 | baseline、SDK 契约测试、blade-verification 与真实报告 |
| T05–T16 数据/资产/配置 | 完成 | tests/skill_eval 资产、schema、catalog、HTTP、安全与并发用例 |
| T17–T20 证据/过程/SSE | 完成 | capture/event_store/process_projection/process_api/sse |
| T21–T29 运行与恢复 | 完成 | worker/runtime/messages/interactions/stop/artifacts/finish/recovery；真实双链 |
| T30–T34 查询/备注/重跑/比较/API | 完成 | history/notes/rerun_plans/comparisons/api_contract/display_only |
| T35–T43 前端 | 完成 | web/src/skillEval 16 个测试文件、29 项测试；build |
| T44 设计和浏览器 | 完成 | Pencil 20 画板、5 张实际页面截图、下述点击记录 |
| T45 本地故障与回归 | 完成 | workflows/fault_matrix、后端全量、旧 Blade 聚焦；既有前端失败单列 |
| T46 CI/部署/真实验收 | 完成 | CI 新模块门禁、deployment、23 REQ 表、两份真实报告；未自动部署 |

任务正文为逐步操作规格，实施采用以下明确收敛：来源事实与 seq 同一个 SQLite 事务提交，不另存 raw/JSONL 尾段，因此测试的是 DB 回滚及通知失败后的补读，不存在第二份 raw 尾段修复。过程投影从固定 head 确定性重建，schema/head 游标替代持久投影 generation；极长历史缓存不是本期完成承诺。编辑使用 textarea 与整包操作缓冲，没有新增代码编辑器依赖。同名附件采用独立 input ID 路径，不静默覆盖。空闲会话不自动清理，不承诺远端永久保留。

## 最后本地检查

| 检查 | 结果 |
| --- | --- |
| `UV_CACHE_DIR=/tmp/octagon-skill-eval-uv uv run pytest -q` | 392 passed、4 skipped（9.97s） |
| `uv run pytest -q tests/skill_eval` | 142 passed（2.51s）；本地禁外网、临时 DB，不使用 PAT |
| `uv run pytest -q tests/skill_eval/test_api_contract.py tests/test_blade_sdk_compat.py tests/test_blade_cli_adapter.py tests/test_blade_priority_recovery.py` | 40 passed；其中新增 API 2 项，既有 Blade 聚焦 38 项 |
| `npm --prefix web test -- src/skillEval` | 29 passed、16 文件 |
| `npm --prefix web run build` | 通过；存在单 JS chunk >500KB 的构建警告 |
| `npm --prefix web test -- --run` | 94 passed、88 failed，39 文件；退出 1 |
| compileall（backend/octagon/scripts）、`uv lock --check`、`git diff --check` | 通过 |
| 临时故意失败的 Vitest probe | 退出 1，已删除 probe；CI 不吞新模块失败 |

4 项 skip 来自既有后端测试，没有新增跳过。旧前端基线是 65 passed / 88 failed；新模块增加 29 passed，失败数仍 88。主要既有问题是未包装 I18nProvider 与旧 localStorage mock，详见 baseline。未把旧失败改为忽略，也未称全量前端通过。完整前端日志在本机 `/tmp/skill-eval-frontend-full-final.log`，后续仍应单独修复既有测试基础设施。

## 真实验收与版本

- 模型：用户选定 `provider-fd5995eebe::deepseek-v4-1-flash-261017`，未改用其他模型。
- 本地 SDK/CLI 均 **2612.0.0-beta.41**，CLI commit `8690d27`；最终预览服务实际为 **2612.0.0-beta.44**。REQ-14 原描述 beta.41 是接入基线，远端在开发期间更新；没有伪称测过远端 beta.41。
- [真实主链报告](evidence/blade-live-workflow.json)：UTC 08:09 开始，三次运行，6 个 checks verified，errors=[]。包含未保存整包执行、直接执行 0755 脚本、二进制 SHA-256、原输入不变、问题应答与新附件、全量输入重跑、全空启动、三次比较及发布导出重导。
- [真实控制报告](evidence/blade-live-controls.json)：UTC 08:12 开始，4 个 checks verified，errors=[]。包含真实定向 stop、停止后继续、生成中 finish、晚到消息拒绝。
- FastAPI HTTP→RunService→Worker→SDK→最终存储使用真实 Runtime；模型身份、来源事件及结果在报告中。四个自建会话全部清理；生产 DB、旧实验/评分队列和用户会话未改。

## 23 项需求

证据中的裸测试名均指 `tests/skill_eval/test_<name>.py`，前端组件测试位于 `web/src/skillEval/`。真实报告与浏览器演示分开，不能互相替代。

| 需求 | 结论 | 证据 |
| --- | --- | --- |
| REQ-01 导航与 Skill 详情/历史 | pass | routes、Workspace 组件；设计 01/04、浏览器入口 |
| REQ-02 上传可独立执行 | pass | imports/packages/create_runs/workflows；真实主链导入到执行 |
| REQ-03 完整资源与独立物料 | pass | drafts/inputs/body_limits；SkillPackageEditor、ChatComposer |
| REQ-04 未运行导出与重导 | pass | snapshots_exports/packages；真实发布导出重导 |
| REQ-05 独立记录、冻结与幂等 | pass | create_runs/worker/recovery；真实三个独立 session |
| REQ-06 公开过程与缺口 | pass | capture_policy/event_store/process_api；真实 projected_turn_count |
| REQ-07 行动/返回/react 关联 | pass | process_projection 跨 loop、重试、历史补缺；ProcessView |
| REQ-08 真实停止与同记录恢复 | pass | 真实控制链；recovery/sse/stop_generation 故障测试（真实崩溃未穷举） |
| REQ-09 详情、产物与返回 | pass | previews/history；RunDetail/Records；result 截图与列表滚动恢复 |
| REQ-10 未保存重跑及手动确认 | pass | rerun_plans/workflows；RerunConfirm；真实第二次未保存重跑 |
| REQ-11 两次及多次比较 | pass | comparisons；ComparePage；真实三次比较、手机 picker |
| REQ-12 模型分组与 Skill 历史 | pass | history；Records/Workspace；实际身份与未确认分开 |
| REQ-13 可选备注与准确快照导出 | pass | notes/snapshots_exports；RunNote；真实备注与导出 |
| REQ-14 指定服务真实集成 | pass | 两份 evidence 报告；本地 beta.41/目标服务实际 beta.44，见版本说明 |
| REQ-15 仅 blade-agent | pass | capabilities/create_runs/api_contract；RunConfiguration |
| REQ-16 正文/脚本整包编辑 | pass | drafts/save_versions；Editor/VersionActions；真实未保存包执行 |
| REQ-17 来源证据、差异与输入 | pass | Workspace/Editor/ComparePage；refine/rerun-confirm 截图 |
| REQ-18 重名/缺号/重号与并发 | pass | imports/schema_assets；ImportPanel；设计 14 |
| REQ-19 beta 保存及新号正式发布 | pass | save_versions/publish_versions；VersionActions；真实发布重导 |
| REQ-20 命名实验与归属 | pass | experiments/schema_runs/history/comparisons；RunConfiguration |
| REQ-21 可空输入及正常交互 | pass | 真实第一运行问答+附件、第三运行全空；interactions/messages |
| REQ-22 主动结束与最终结果 | pass | finish_run/artifacts/comparisons；真实主链和控制链 |
| REQ-23 正式版派生/试跑/冻结 | pass | save_versions/snapshots_exports/create_runs；VersionActions |

## 浏览器与设计验收

使用自包含离线演示脚本，不读取用户 octagon.yaml 或 PAT。FakeRuntime 的模型标签明确为模拟，以下截图不冒充真实模型执行。

1. 上传/Skill 入口 → 详情正文与资源 → 同 Skill 全历史；选定命名实验，提示和附件留空启动，回复后仍 waiting_continue。
2. 发送问题触发 waiting_input，选择 Alpha 并显式提交；同一运行继续。主动结束后读取最终结果，点击 report.txt 查看实际缓存字节。
3. 点击基于结果微调，同时查看来源结果、备注和产物。编辑 SKILL.md 后保留未保存标记，切换文件/证据不丢缓冲。
4. 点击重跑，两项无预选；手动选择全带输入，核对提示/追问/回答/附件清单，确认才创建新记录。结束后原记录未改变。
5. 历史选择三次运行进入比较；窄屏 390px 上基准/当前列切换，实测文档 scrollWidth=390，无水平溢出。

[实现截图画廊](ui/implementation/index.html)：result、refine、rerun-confirm、comparison、mobile 五张。[Pencil 画廊](ui/index.html)：20 张，原 12 张修订、新增 8 张关键状态与入口规格；导出后逐组目视检查并修复一处裁切。源文件已在当前 spec 目录保存。

## 已知限制与未穷举项

- 首期没有评分。只评估文字结论/新文件，不验证写外部系统或原地修改/删除上传物料的实际效果；页面清楚说明。观察到输入改变保留证据，不能称副作用验证成功；没有执行层全面拦截/回滚保证。
- 仅显示公开来源中的 thinking/参数/返回及真实 react 引用。未提供、被采集上限关闭、脱敏失败或超限的正文有缺口；不生成私有推理，不将会话事件称实际 provider 请求。
- 本地故障测试覆盖未知接受、绑定后恢复、断流、附件失败、租约/并发、finish/send 竞态、最终回收重试等；未真实杀进程穷举每一网络/提交窗口，无法确认时安全失败，不自动重发 chat/answer。
- 仅验用户选定模型；未穷举全部模型别名、所有 Office/媒体格式。预览复用既有隔离渲染，失败可下载；远端文件集跨文件原子性和远端保留期限不保证。
- 过程投影当前按固定 head 重建，超长运行的性能缓存可后续优化；构建大 chunk 警告尚在。
- 备注修订冲突保留本地内容并报错，需要重新载入服务器修订；没有自动合并。
- 本次没有待用户补充的产品决定；部署开关按部署环境配置，不自动修改生产配置或执行发布。

## 最后修正

最终回归复查发现比较页首次载入的 effect 清理可能取消用户刚发起的差异请求。改为布局提交阶段同步重置，增加旧请求晚返回不得覆盖新基准差异的组件测试。修复后独立模块 29 passed（16 文件）、全前端 94 passed / 88 failed（182 项）、build 通过；后端最终 392 passed / 4 skipped，其中新模块 142 passed。
