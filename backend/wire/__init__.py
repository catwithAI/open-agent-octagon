"""通信观测基础层（wire observability）。

采集各 agent 与 LLM/工具之间的通信证据，归一化成 canonical wire records，
供框架级对比分析。见 docs/specs/wire_observability/。

本包只提供数据模型、ID/hash、脱敏、spool/finalize 等基础能力；各 capture
source、normalizer、API 挂在子模块下。核心分析层不理解任何厂商私有事件格式。
"""
