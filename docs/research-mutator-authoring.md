# Research Mutator Authoring

Mutator 必须是纯函数：输入 `MutationInput + VariantSpec`，输出确定性 prompt/context delta，
不得访问网络、环境变量、当前时间或随机全局状态。相同 source hash、mutator version、seed、
intensity、params 必须得到相同 content hash。

## 接入步骤

1. 在 `backend/mutations/` 实现 mutator，并注册明确的 `id + version`；
2. `check()` 返回 ready/unsupported 与可展示 warning，不吞异常；
3. 只修改 task 声明的 mutable region，逐字保留 protected spans；
4. 在 env `meta.yaml` 的 `mutations.allowed/conditional/forbidden` 声明授权；
5. 为 Unicode、路径、URL、JSON、代码标识符、精确输出和 canary 添加 fixture；
6. preview 必须先展示 diff/hash/cardinality；unsupported 阻断 create。

禁止把 scorer、expected answer 或真实 secret 放入变体。升级行为必须发布新 version，旧
Experiment 永远引用冻结 version/hash。
