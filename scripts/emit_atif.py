#!/usr/bin/env python3
"""把 attempt 沙盒里的本地对话历史还原成 ATIF-v1.7 trajectory。

在 wire/trajectory/insight 数据结构之外，直接读沙盒 HOME 里各家 CLI 的本地
会话转录（claude-code 的 ``.cc-iso-home/.claude/projects/*.jsonl``、codex 的
``.codex-iso-home/sessions/*.jsonl``），产出可用于归因的 ATIF 对话流。

用法：
    python scripts/emit_atif.py <attempt_dir> [--out PATH] [--agent claude-code|codex]

退出码：0 = ready（或 not_available 且 --out 已给？否——not_available 固定 1）；
        ready 写产物 exit 0；not_available exit 1 并打印原因。

示例：
    python scripts/emit_atif.py data/attempts/att_xxx --out /tmp/traj.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.atif.emitter import emit_attempt_atif  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 attempt 沙盒会话还原 ATIF-v1.7 trajectory"
    )
    parser.add_argument("attempt_dir", help="attempt 数据目录（含 .cc-iso-home / .codex-iso-home）")
    parser.add_argument("--out", help="输出 trajectory.json 路径（缺省 stdout）")
    parser.add_argument("--agent", choices=["claude-code", "codex"], help="显式指定 adapter，缺省按沙盒推断")
    args = parser.parse_args(argv)

    outcome = emit_attempt_atif(
        Path(args.attempt_dir),
        agent_name=args.agent,
    )
    if outcome.status != "ready":
        print(f"not_available: {outcome.reason}", file=sys.stderr)
        return 1

    payload = json.dumps(outcome.trajectory, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        print(f"wrote {out} (schema_version={outcome.trajectory['schema_version']}, "
              f"steps={len(outcome.trajectory['steps'])})")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
