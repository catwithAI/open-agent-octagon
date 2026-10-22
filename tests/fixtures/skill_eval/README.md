# Skill Eval fixture 来源

`tests/skill_eval/conftest.py::skill_files` 为手写的自包含样本，不含用户数据；坏 ZIP 在安全测试中生成。

`blade_beta42_events.json` 来自 2026-10-22 对预览服务 beta.42 的显式协议验收，使用 beta.41 SDK、合成 Skill/物料和用户指定的 DeepSeek Flash 模型。仅保留公开 turn/snapshot 与工具关联片段；会话 ID、宿主机路径及一次性 marker 已替换。它不是模型实际 provider 请求，也不包含私有推理。

SDK 编解码测试另外使用合成 wire 数据，不能把其通过替代真实服务验收。
本地测试拒绝 socket connect；真实联调仅由显式验证脚本执行，真实鉴权不进入 fixture。
