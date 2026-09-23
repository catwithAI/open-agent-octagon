#!/usr/bin/env python3
"""把一次 Octagon run 的各 agent attempt 交给 open-octagon-evals 独立评分。

Octagon 自己的 scorer 已经给出 score_total；这里做的是**另一条评分通路**：
把每个 agent 的真实产物（相对 base commit 的 unified diff）作为证据，按场景
meta.yaml 声明的维度交给 evals 的 agent judge 逐维度打分。两条通路互不引用
——证据里刻意不含 Octagon 的分数，否则 judge 就成了复读机。

用法：
    python scripts/submit_to_evals.py <run_id> [--experiment <id>]
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import os

OCTAGON = os.getenv("OCTAGON_BASE_URL", "http://127.0.0.1:8100")
# evals 的 start.sh 在默认端口被占用时会自动往后挪（8000 → 8001 …）并把实际
# 地址打在启动日志里，所以这里必须可覆盖，不能写死。
EVALS = os.getenv("OCTAGON_EVALS_BASE_URL", "http://127.0.0.1:8000")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENVS_ROOT = Path("/home/bladeai/codes/agent-octagon-representative-envs")

# 每个 diff 片段的上限：judge 的上下文不是无限的，而单个 agent 可能顺手 reformat
# 一整个目录。截断要显式写进证据，不能让 judge 以为它看到的是全量。
MAX_DIFF_CHARS = 60_000

# 命令执行证据的上限（条数 / 单条结果字符数）。`validation` 维度问的是
# 「交付前有没有真的跑过检查」，不给执行痕迹的话 judge 只能判 0——那不是
# agent 没验证，是证据没送到。
MAX_TRACE_ENTRIES = 40
MAX_TRACE_RESULT_CHARS = 1_200

# 各家 agent 的执行类工具名（normalized trace 的 tool_name）。
EXEC_TOOL_NAMES = {
    "bash", "shell", "run", "run_command", "exec", "execute",
    "terminal", "local_shell", "dsh-bash-local", "Bash",
}

# 场景 meta.yaml 的维度（权重 60/25/10/5 归一化到 1.0）。EvalPlan 要求同一
# experiment 的所有 run 共享同一份冻结 plan，所以这里写死成常量。
DIMENSIONS = [
    {
        "id": "functional_correctness",
        "role": "scored",
        "weight": 0.60,
        "method": "agent_judge",
        "question": (
            "该补丁是否真正修复了 Decimal 在给定 decimal_pos 下、指数低于可见精度"
            "时错误回退到科学计数法的缺陷？要求对极小正数、极小负数、不同 "
            "decimal_pos、以及 Decimal 子类都按可见精度归零。"
        ),
        "anchors": [
            {"id": "tiny_positive",
             "pass_if": "Decimal('1e-200') 配 decimal_pos=2 输出 '0.00' 这类按可见精度归零的结果",
             "fail_if": "仍输出 '1.00e-200' 等科学计数法"},
            {"id": "tiny_negative",
             "pass_if": "负的极小值同样归零且保留正确符号处理",
             "fail_if": "只处理了正数分支"},
            {"id": "general_not_hardcoded",
             "pass_if": "修复是基于 exponent 与 decimal_pos 的一般性比较",
             "fail_if": "对示例值或特定阈值硬编码"},
        ],
        "output_schema": {"type": "object", "required": ["value", "reason", "raw"]},
    },
    {
        "id": "regression_safety",
        "role": "scored",
        "weight": 0.25,
        "method": "agent_judge",
        "question": (
            "补丁是否保持了原有格式化行为：普通小数、阈值边界、未提供 decimal_pos "
            "时的科学计数法回退、整数和千分位分组？"
        ),
        "anchors": [
            {"id": "decimal_pos_none",
             "pass_if": "decimal_pos 为 None 时仍走原来的科学计数法回退路径",
             "fail_if": "无条件去掉了科学计数法分支"},
            {"id": "large_numbers",
             "pass_if": "超大数（如 9e200 以上）的既有行为不变",
             "fail_if": "改动同时影响了大数分支且未作论证"},
        ],
        "output_schema": {"type": "object", "required": ["value", "reason", "raw"]},
    },
    {
        "id": "repository_discipline",
        "role": "scored",
        "weight": 0.10,
        "method": "agent_judge",
        "question": (
            "改动是否克制且聚焦？只应触及相关实现与测试，不应破坏仓库结构、"
            "不应以硬编码或改测试的方式绕过检查。"
        ),
        "anchors": [
            {"id": "focused_diff",
             "pass_if": "diff 集中在 django/utils/numberformat.py 及相关测试",
             "fail_if": "大范围改动无关文件或重排代码"},
            {"id": "no_test_gaming",
             "pass_if": "没有删改既有断言来让测试通过",
             "fail_if": "修改或删除了原有测试的期望值"},
        ],
        "output_schema": {"type": "object", "required": ["value", "reason", "raw"]},
    },
    {
        "id": "validation",
        "role": "scored",
        "weight": 0.05,
        "method": "agent_judge",
        "question": "交付前是否真的跑过仓库测试或聚焦的行为检查，并有证据？",
        "anchors": [
            {"id": "ran_checks",
             "pass_if": "证据显示执行过测试或可复现的行为验证",
             "fail_if": "只有代码改动，没有任何验证痕迹"},
        ],
        "output_schema": {"type": "object", "required": ["value", "reason", "raw"]},
    },
]


def http_json(url: str, payload: dict | None = None, timeout: int = 300) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"{url} -> HTTP {exc.code}: {body[:500]}") from exc


def build_diff(attempt_id: str, env_name: str) -> tuple[str, list[str], bool]:
    """agent 工作区相对场景 materials 的 unified diff。

    返回 (diff 文本, 改动文件列表, 是否被截断)。**不比较二进制/新增的大目录**
    ——只逐个文本文件比，缺失或新增都记成一条。
    """
    base = ENVS_ROOT / env_name / "materials" / "repo"
    work = REPO_ROOT / "data" / "attempts" / attempt_id / "skill_workspace" / "repo"
    if not work.is_dir():
        return "", [], False

    chunks: list[str] = []
    changed: list[str] = []
    for work_file in sorted(work.rglob("*")):
        if not work_file.is_file():
            continue
        rel = work_file.relative_to(work)
        # .git 等运行期产物不是交付物的一部分。
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in rel.parts):
            continue
        base_file = base / rel
        try:
            new_text = work_file.read_text(encoding="utf-8").splitlines(keepends=True)
        except (UnicodeDecodeError, OSError):
            continue
        if base_file.is_file():
            try:
                old_text = base_file.read_text(encoding="utf-8").splitlines(keepends=True)
            except (UnicodeDecodeError, OSError):
                continue
        else:
            old_text = []
        if old_text == new_text:
            continue
        changed.append(str(rel))
        chunks.append("".join(difflib.unified_diff(
            old_text, new_text,
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3,
        )))

    diff = "\n".join(chunks)
    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]\n"
    return diff, changed, truncated


def build_execution_evidence(run_id: str, attempt_id: str) -> dict:
    """从 normalized trace 里抽出命令执行痕迹，作为 `validation` 维度的证据。

    只取执行类工具（读文件、改文件不算验证），结果按字符数截断——judge 需要看到
    的是「跑了什么、退出/输出是什么」，不是完整 stdout。
    """
    try:
        trace = http_json(
            f"{OCTAGON}/api/runs/{run_id}/attempts/{attempt_id}/trace", timeout=60,
        )
    except RuntimeError:
        return {"available": False, "commands": [], "total_tool_calls": 0}
    if not isinstance(trace, list):
        return {"available": False, "commands": [], "total_tool_calls": 0}

    commands: list[dict] = []
    for entry in trace:
        if not isinstance(entry, dict):
            continue
        tool = str(entry.get("tool_name") or "")
        if tool not in EXEC_TOOL_NAMES:
            continue
        args = entry.get("arguments")
        if isinstance(args, dict):
            command = args.get("command") or args.get("cmd") or args.get("script") or args
        else:
            command = args
        result = entry.get("result")
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False) if result is not None else ""
        commands.append({
            "tool": tool,
            "command": command if isinstance(command, str) else json.dumps(
                command, ensure_ascii=False)[:MAX_TRACE_RESULT_CHARS],
            "result_excerpt": result[:MAX_TRACE_RESULT_CHARS],
            "result_truncated": len(result) > MAX_TRACE_RESULT_CHARS,
        })

    dropped = max(0, len(commands) - MAX_TRACE_ENTRIES)
    return {
        "available": True,
        "commands": commands[:MAX_TRACE_ENTRIES],
        "commands_omitted": dropped,
        "total_tool_calls": len(trace),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    parser.add_argument("--experiment", default=None)
    args = parser.parse_args()

    experiment_id = args.experiment or f"oao-{args.run_id}"
    run = http_json(f"{OCTAGON}/api/runs/{args.run_id}")
    env_name = run["env_name"]
    task_id = run["task_id"]

    envs = http_json(f"{OCTAGON}/api/envs")
    env_meta = next((e for e in envs if e["name"] == env_name), {})

    task_prompt = ""
    task_file = ENVS_ROOT / env_name / "tasks" / f"{task_id}.json"
    if task_file.is_file():
        task_prompt = json.loads(task_file.read_text(encoding="utf-8")).get("prompt", "")

    submitted: list[dict] = []
    for attempt in run["attempts"]:
        # 评分层只接收上游封口的产物。`gave_up` 也算封口——agent 撞上 deadline
        # 但工作区里有真实改动，evals 的契约明确要求「上游已完成的 run 即使任务
        # 失败也作为评分证据交付」。真正要挡的是起手就挂、工作区是空的那几种
        # （cli_error / session_create_failed），交上去只会得到一个对空证据的幻觉分。
        if attempt["status"] not in ("completed", "gave_up"):
            print(f"skip {attempt['agent_name']:<12} status={attempt['status']} (无产物)")
            continue
        attempt_id = attempt["id"]
        diff, changed, truncated = build_diff(attempt_id, env_name)
        execution = build_execution_evidence(args.run_id, attempt_id)
        # 空 diff **不跳过**：agent 跑满了时间却一行源码没改，本身就是一个要被
        # 记录下来的结果。跳过会让它在对比表里凭空消失，看起来像没参赛。

        # run_id 在 evals 里是**全局**主键，且一旦存过就不允许换 EvaluationInput。
        # 带上 experiment 前缀，这样重跑一轮评分（换证据/换 judge）是新建 run 而
        # 不是撞库——仓库自己的「重复评估同一产物」指引也要求每次用新 run_id。
        eval_run_id = f"{experiment_id}::{attempt['agent_name']}"
        payload = {
            "run_id": eval_run_id,
            "plan_version": "1",
            "producer": {
                "agent_id": attempt["agent_name"],
                "agent_version": "sandbox-image",
                "model_id": attempt.get("model") or "",
                "skill_set": "native",
            },
            "scenario": {"id": env_name, "version": 1},
            "task": {"id": task_id, "prompt": task_prompt},
            "artifact": {
                "snapshot_ref": (
                    f"{OCTAGON}/api/runs/{args.run_id}/attempts/{attempt_id}/artifacts"
                ),
                "content_hash": "sha256:" + hashlib.sha256(
                    diff.encode() or b"<no source changes>").hexdigest(),
            },
            "history": {
                "trajectory_ref": (
                    f"{OCTAGON}/api/runs/{args.run_id}/attempts/{attempt_id}/trace"
                ),
            },
            "upstream_completed": True,
            "dimensions": DIMENSIONS,
        }
        created = http_json(f"{EVALS}/experiments/{experiment_id}/runs", payload)
        print(f"created {attempt['agent_name']:<12} tasks={len(created['task_ids'])} "
              f"changed_files={len(changed)}")

        evidence = {
            "candidate_output": diff or "(no source changes: agent left the repo unmodified)",
            "changed_files": changed,
            "diff_truncated": truncated,
            "task_prompt": task_prompt,
            "tool_call_count": attempt.get("tool_call_count"),
            "duration_ms": attempt.get("duration_ms"),
            "upstream_status": attempt["status"],
            "execution_evidence": execution,
            "scenario_dimensions": env_meta.get("dimensions", []),
        }
        for dim_task_id in created["task_ids"]:
            try:
                result = http_json(
                    f"{EVALS}/tasks/{dim_task_id}/score", {"evidence": evidence},
                )
                print(f"   scored {result['task_id'][-24:]} value={result['value']}")
            except RuntimeError as exc:
                print(f"   FAILED {dim_task_id[-24:]}: {exc}")
        submitted.append({"agent": attempt["agent_name"], "run_id": eval_run_id})

    if not submitted:
        print("没有可评分的 attempt")
        return 1

    print("\n=== experiment score ===")
    print(json.dumps(
        http_json(f"{EVALS}/experiments/{experiment_id}/score"),
        ensure_ascii=False, indent=2,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
