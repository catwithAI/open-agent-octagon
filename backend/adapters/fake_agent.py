"""Fake agent adapter —— 无凭据的确定性适配器，仅供 CI / 冒烟使用。

真实 agent（claude-code / codex / blade-agent...）都需要外部 CLI 或 API key，
干净 CI 环境里跑不了。本适配器不依赖任何外部进程或网络，用**任务自带的期望产物**
确定性地「完成」一次 attempt：把产物写进 skill_workspace，落几条 events.jsonl，
返回一个 terminal 的 AdapterResult。这样端到端链路（dispatch → run → scorer →
API/前端）可以在无凭据下被验证。

约定：任务在 `task_context["fake_agent"]` 下声明这次要产出什么，例如

    "fake_agent": {
        "files": {"answer.txt": "hello"},   # 写到 skill_workspace 下的文件
        "tool_calls": [                       # 可选：要「调用」的 env 工具
            {"name": "submit", "arguments": {"value": 42}}
        ]
    }

`files` 让 coding 类 scorer（读 workspace 产物）能判分；`tool_calls` 让 skill 类
scorer（读 env DB / trace）也能拿到确定性输入。缺省则只落一条 completed 事件。

本适配器是示例/测试设施，不参与真实对比评测。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from .base import AdapterCapabilities, AdapterResult, AdapterRunInput


class FakeAgentAdapter:
    """确定性、无凭据的适配器。见模块 docstring。"""

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            execution_locus="host",
            network_required="local_service",
        )

    async def run(
        self,
        task: AdapterRunInput,
        env: Any,
        data_path: Path,
    ) -> AdapterResult:
        started = time.monotonic()
        attempt_dir = data_path / "attempts" / task.attempt_id
        workspace = attempt_dir / "skill_workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"

        spec = _fake_spec(task.task_context)
        events: list[dict[str, Any]] = []
        events.append(_event("task_started", {"prompt": task.task_prompt[:200]}))

        # 1) 写产物文件（coding 类 scorer 读 workspace）
        files: dict[str, str] = spec.get("files") or {}
        for rel, content in files.items():
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(str(content), encoding="utf-8")
            events.append(_event("file_written", {"path": rel}))

        # 2) 调用 env 工具（skill 类 scorer 读 env DB / trace）
        tool_calls: list[dict[str, Any]] = spec.get("tool_calls") or []
        tool_call_count = 0
        for call in tool_calls:
            name = call.get("name")
            arguments = call.get("arguments") or {}
            ok = await _invoke_env_tool(task, name, arguments)
            tool_call_count += 1
            events.append(
                _event("tool_call", {"name": name, "ok": ok, "arguments": arguments})
            )

        events.append(_event("task_completed", {"status": "ok"}))
        _write_events(events_path, events)

        duration_ms = int((time.monotonic() - started) * 1000)
        return AdapterResult(
            attempt_id=task.attempt_id,
            status="completed",
            transport_status="ok",
            events_count=len(events),
            last_event_at=events[-1]["ts"],
            tool_call_count=tool_call_count,
            duration_ms=duration_ms,
            security_meta={"execution_locus": "in_process_fake"},
        )


def _fake_spec(task_context: dict[str, Any]) -> dict[str, Any]:
    spec = task_context.get("fake_agent")
    return spec if isinstance(spec, dict) else {}


def _event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": kind,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload,
    }


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


async def _invoke_env_tool(
    task: AdapterRunInput, name: str | None, arguments: dict[str, Any]
) -> bool:
    """通过 Env Attempt Server 调一个 env 工具，与真实 agent 走同一条链路。

    失败不抛，返回 False——fake agent 的职责是产生确定性 trace，不是保证工具成功。
    """
    if not name:
        return False
    url = f"{task.env_base_url.rstrip('/')}/attempts/{task.attempt_id}/tools/{name}"
    headers = {"Authorization": f"Bearer {task.env_token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=arguments, headers=headers)
            return resp.status_code < 400
    except httpx.HTTPError:
        return False
