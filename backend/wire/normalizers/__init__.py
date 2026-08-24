"""Native event normalizers：producer 私有事件 → WireEvidence v1。

每个 normalizer 读某个 adapter 的 raw events（events.jsonl 等），产出与协议
无关的 ``WireEvidence`` 列表 + 最小 ``trajectory.json``。finalizer 只吃
evidence，不理解任何厂商事件格式。
"""
