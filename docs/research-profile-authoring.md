# Research Profile Authoring

Profile 是版本化、无 secret 的协议默认值，不是自动执行模板。文件放在 `profiles/*.yaml`，
必须符合 `octagon-profile-v1`。

## 规则

- `id/version` 不可原地改变语义；行为变化发布新 version；
- variants 必须恰好包含一个 baseline，矩阵不得超过 profile 与部署双重 limits；
- provider 只写逻辑引用，严禁 key/token/password/authorization；
- applicability 只使用 env/task metadata，Auto Profile 不读取 prompt 正文；
- recommendation 只返回理由、置信度和 patch，用户必须显式 Accept；
- `forensic` 保持小矩阵（最多 24 cells）、serial、低并发。

提交前运行 `tests/test_rc_p01_profiles.py`、`test_rc_p02_profile_expansion.py` 和 no-secret
policy 测试。Profile flag 关闭不影响已冻结 Experiment。
