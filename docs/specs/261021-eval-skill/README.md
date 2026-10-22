# Skill Eval

2026-10-22：首期功能已实施，真实 Blade 主链及停止控制链通过；完整证据与限制见 [验收记录](acceptance.md)。SDK、当前 worktree 原生 CLI 均锁定 beta.41，最终联调时预览服务实际为 beta.44。未提交、合并或部署，用户配置的执行开关未修改。

- [产品需求](requirement.md)：v1.2，包含全部确认规则及 23 项验收点。
- [开发设计](design.md)：v0.5，已同步实际 API、SQLite 证据存储和恢复策略。
- [开发任务](tasks.md)：46 项实施与验收清单，实施差异及覆盖范围见验收记录。
- [实施进度](implementation-progress.md)、[测试基线](baseline.md)、[技术决定](implementation-decisions.md)。
- [配置与恢复说明](deployment.md)：安装、开关、只读 catalog、限额、备份及显式验收命令。
- [Blade 真实联调](blade-verification.md)：协议与主链证据；[主链报告](evidence/blade-live-workflow.json)、[停止报告](evidence/blade-live-controls.json)。
- [Pencil 源文件](skill-eval.pen)、[20 张设计画板](ui/index.html)、[设计同步记录](ui-change-list.md)。
- [实际页面截图](ui/implementation/index.html)：离线 FakeRuntime 浏览器演示，包含结果、微调、重跑确认、三次比较与手机布局。
- [早期 SDK 迁移研究](spec.md)：保留讨论背景，当前实现以 design 与验收记录为准。

首期只支持 blade-agent。提示和物料均可空；一次运行是可多轮交互的会话，主动结束才固定最终结果。上传、全包编辑、未发布保存、另号发布、未保存试跑/导出、过程、历史、备注与比较已接线。

首期不验证写外部系统、原地修改/删除上传物料的实际效果；页面常驻说明。没有文件产物的文字结论仍是有效结果。完整流程只展示来源公开的数据，不补写私有推理或冒充实际 provider 请求。

Pencil 使用本机 Pen 新版 MCP 的 execute 接口完成编辑、保存与导出；没有修改全局 MCP 配置或用户主仓库原件。设计画廊是静态交互规格，实际交互及真实模型证据分别由浏览器记录和脱敏 JSON 支撑。
