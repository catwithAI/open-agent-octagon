"""blade-agent 编辑路径提取：分片流式事件也要能还原出交付物。

背景见 docs/specs/260921-eval-storage-and-artifact-recovery 需求 4。
BA 把工具调用拆成分片下发，工具名与 arguments 落在不同事件行里；旧口径要求
两者同行，于是 15883 行带编辑标记的日志只提取出 2 个路径，产物回收全空，
27 个 attempt 拿到形状一致的低分。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from backend.adapters.edited_paths import (
    agent_edited_paths,
    normalize_path,
    paths_from_events,
    paths_from_mtime,
    paths_from_trace,
)


def _write_fragmented_events(attempt_dir: Path) -> None:
    """复刻 BA 的分片形状：工具名与 arguments 分处不同事件行。"""
    rows = [
        # 工具名单独一行——不带任何路径。
        {"type": "tool_call", "delta": {"function": {"name": "Write"}}},
        # arguments 分片，路径在这一行,而这行没有工具名。
        {"type": "tool_call", "delta": {"arguments": '{"file_path": "django/utils/numberformat.py"'}},
        {"type": "tool_call", "delta": {"arguments": ', "content": "..."}'}},
        {"type": "tool_call", "delta": {"function": {"name": "Edit"}}},
        {"type": "tool_call", "delta": {"arguments": '{"file_path": "/workspace/tests/test_format.py"}'}},
        # Read 的入参也会出现；多拉一个文件无所谓，漏掉交付物才致命。
        {"type": "tool_call", "delta": {"arguments": '{"path": "README.md"}'}},
        # 噪声：不是交付物后缀，应被挡掉。
        {"type": "tool_call", "delta": {"arguments": '{"file_path": "/tmp/agent.log"}'}},
        # 逃逸路径必须拒绝——它最终要拼进落地路径。
        {"type": "tool_call", "delta": {"arguments": '{"file_path": "../../etc/passwd.py"}'}},
    ]
    (attempt_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_fragmented_events_yield_the_deliverables(tmp_path: Path) -> None:
    """需求 4.1/4.3：不要求工具名与路径同行，提取数必须显著大于 2。"""
    _write_fragmented_events(tmp_path)

    found = paths_from_events(tmp_path)

    assert "django/utils/numberformat.py" in found
    assert "tests/test_format.py" in found  # 绝对路径剥掉 workspace 前缀
    assert "README.md" in found
    # 噪声与逃逸都被挡住
    assert not any(p.endswith("agent.log") for p in found)
    assert not any(".." in p for p in found)
    assert len(found) > 2


def test_absolute_paths_are_not_discarded(tmp_path: Path) -> None:
    """BA 记的就是容器内绝对路径；直接丢弃等于全空。"""
    assert normalize_path("/workspace/src/main.py") == "src/main.py"
    assert normalize_path("/home/agent/workspace/a/b.py") == "a/b.py"
    assert (
        normalize_path("/srv/blade/proj/out.xlsx", workspace_root="/srv/blade/proj")
        == "out.xlsx"
    )
    # 逃逸一律拒绝
    assert normalize_path("../../etc/passwd.py") is None
    # 依赖树不是交付物
    assert normalize_path("node_modules/left-pad/index.js") is None
    # 非交付物后缀
    assert normalize_path("/var/log/run.log") is None


def test_trace_arguments_are_parsed_structurally(tmp_path: Path) -> None:
    """需求 4.2：trace.jsonl 的结构化 arguments 比正则可靠。"""
    rows = [
        {"tool_name": "Write", "arguments": {"file_path": "report.md", "content": "x"}},
        # arguments 被序列化成字符串再塞进来的情形
        {"tool_name": "Edit", "arguments": '{"file_path": "src/app.py"}'},
        {"tool_name": "Bash", "arguments": {"command": "ls"}},
    ]
    (tmp_path / "trace.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    found = paths_from_trace(tmp_path)

    assert "report.md" in found
    assert "src/app.py" in found


def test_mtime_separates_agent_edits_from_copied_materials(tmp_path: Path) -> None:
    """物料保留源仓库 mtime（差几天），agent 的编辑在运行那几分钟。"""
    workspace = tmp_path / "skill_workspace"
    (workspace / "src").mkdir(parents=True)
    old = workspace / "src" / "untouched.py"
    old.write_text("baseline\n")
    fresh = workspace / "src" / "edited.py"
    fresh.write_text("agent wrote this\n")

    week_ago = time.time() - 7 * 86400
    os.utime(old, (week_ago, week_ago))

    found = paths_from_mtime(tmp_path, window_seconds=600)

    assert "src/edited.py" in found
    assert "src/untouched.py" not in found


def test_sources_are_merged_and_reported(tmp_path: Path) -> None:
    """三源取并集，并报告命中了哪些来源。"""
    _write_fragmented_events(tmp_path)
    (tmp_path / "trace.jsonl").write_text(
        json.dumps({"tool_name": "Write", "arguments": {"file_path": "only_in_trace.md"}})
        + "\n",
        encoding="utf-8",
    )

    paths, sources = agent_edited_paths(tmp_path)

    assert "django/utils/numberformat.py" in paths  # events
    assert "only_in_trace.md" in paths  # trace
    assert "events" in sources and "trace" in sources


def test_no_source_is_reported_as_none(tmp_path: Path) -> None:
    """需求 4.4：提取不到路径时要能说「回收无依据」，而不是当成能力差。"""
    paths, sources = agent_edited_paths(tmp_path)
    assert paths == []
    assert sources == []


def test_duplicate_paths_collapse(tmp_path: Path) -> None:
    rows = [
        {"delta": {"arguments": '{"file_path": "a.py"}'}},
        {"delta": {"arguments": '{"file_path": "./a.py"}'}},
        {"delta": {"arguments": '{"path": "a.py"}'}},
    ]
    (tmp_path / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    assert paths_from_events(tmp_path) == ["a.py"]


def test_escaped_json_inside_arguments_string(tmp_path: Path) -> None:
    """arguments 常被转义一层塞进事件行，要能解开。"""
    row = {"delta": {"arguments": json.dumps('{"file_path": "docs/plan.md"}')}}
    (tmp_path / "events.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert "docs/plan.md" in paths_from_events(tmp_path)


def test_blade_sandbox_workspace_paths(tmp_path: Path) -> None:
    """blade 在沙盒内按 session 开一层目录，那一层不属于工作区内相对路径。

    带着 `sess_xxx/` 去远端一定找不到——这正是产物回收全空的路径形态。
    """
    assert (
        normalize_path("/root/智能助手工作空间/sess_abc123/django/utils/numberformat.py")
        == "django/utils/numberformat.py"
    )
    assert normalize_path("/root/智能助手工作空间/sess_abc123/out.xlsx") == "out.xlsx"
    # 真实目录名不能被当成 session 误删
    assert normalize_path("/workspace/django/utils/x.py") == "django/utils/x.py"
    assert normalize_path("/workspace/src/main.py") == "src/main.py"


def test_redundant_separators_collapse() -> None:
    """路径要拼进落地位置与远端请求，`a/./b` 与 `a//b` 必须归一成同一个 key。"""
    assert normalize_path("a/./b.py") == "a/b.py"
    assert normalize_path("a//b.py") == "a/b.py"
