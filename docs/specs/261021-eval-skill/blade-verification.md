# Blade 真实联调（2026-10-22）

目标为用户指定的预览服务 `https://agent.preview.bladeai.com.cn/`。鉴权从当前 worktree gitignored 配置载入，不写入文档、夹具或命令参数。模型由用户明确选定为 `provider-fd5995eebe::deepseek-v4-1-flash-261017`。

本地 Python SDK 与原生 CLI：**2612.0.0-beta.41**；CLI commit `8690d27`。早期只读预检服务为 beta.42，协议验证期间为 beta.43；最终主链/控制链服务实际为 **2612.0.0-beta.44**。没有修改远端版本，也没有把初始目标 beta.41 写成最终远端实测版本。REQ-14 按用户指定服务进行兼容验收。

## 真实应用主链

[脱敏报告](evidence/blade-live-workflow.json)，UTC 2026-10-22 08:09 开始。入口 `scripts/verify-skill-eval-workflow.py` 使用临时 DB、数据目录、合成 Skill 与物料，通过实际 FastAPI 路由、RunService、Worker 和 SDK 执行，未用 FakeRuntime。

- 导入含 SKILL.md、0755 脚本与二进制资源的包，新建命名实验，以未保存快照运行；脚本实际直接执行并核对标记与二进制 SHA-256。
- 真实提问进入 waiting_input，显式提交合成答案 Alpha 与新附件，同一会话继续；不是产品自动代答。
- 主动结束并回收最终结果、保存备注；修改全包后手动确认携带全部用户输入，创建第二个会话；replace 全空建立第三次运行，以单独可见平台指令启动。
- 三次最终结果比较、另号发布、导出及重新导入通过。六项 checks 全 verified，errors 为空。
- 三个 run：`85c232becc384209bdd6e086f5c0210d`、`66dc0e23cca4491c89c70806e73c6871`、`ab4cb2384ccd4be7b2a85637e50b9bc7`。session、实际模型/provider、SDK/server 版本及最终索引在报告中。

## 真实控制链

[脱敏报告](evidence/blade-live-controls.json)，UTC 2026-10-22 08:12 开始。run `143b82a78c9743be95734dca099106bb`。真实定向 stop 确认 stopped；停止后同会话继续；生成中主动 finish，实际中断并回收；结束后的消息被拒绝。四项 checks 全 verified，errors 为空。

两份最终报告中的四个自建 session 均为 deleted_owned_session。未扫描/清理用户其他会话；临时数据不写入生产记录。

## 协议结论

| 能力 | 结论与边界 |
| --- | --- |
| Skill 上传与实际激活 | verified，上传回执与 refs 激活分开；脚本结果/字节另验证实际使用 |
| 二进制与执行位 | verified，base64 与 executable 保留，实际直接执行脚本 |
| 提示/物料执行与全空用户输入 | verified；全空使用可见平台启动指令，不伪造用户提示；不采用未知的空 wire 消息契约 |
| 公开过程和历史 | verified，turn:snapshot、root loop:turn、tool_use/result、公开 thinking 等按来源采集；实际 provider 请求与私有推理 unavailable |
| 多轮、问题应答及附件 | verified，headless=false、真实 tool_call_id、结构化 selections/custom；没有自动回答 |
| 停止与主动结束 | verified，真实 chat_run_id 定向控制；chat:end 不是整次结束 |
| 最终文件 | verified，原输入保留、产物字节核验；跨文件原子快照不作保证 |
| 崩溃/网络所有窗口、远端保留期限、所有模型别名 | 未穷举，不能由主链通过推断；本地故障测试及安全失败规则见验收记录 |

`scripts/verify-skill-eval-blade.py --preflight` 为只读预检；`--execute` 必须显式指定模型。真实调用从不由 CI 自动执行。复跑命令见 [deployment.md](deployment.md)，先确认对应部署环境与费用授权。
