"""SshClaudeCodeAdapter — 通过 SSH 在远程机器上执行 claude CLI。

prompt 通过文件传递（SCP），不拼入 SSH 命令行，避免 shell 注入。
MCP server 在远端执行，通过 HTTP 回调本地的 env attempt server。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import (
    AdapterCapabilities,
    AdapterResult,
    AdapterRunInput,
    build_security_meta,
    prompt_context,
)
from .token_usage import (
    estimate_tokens_from_event,
    result_usage_tokens,
    usage_input_tokens,
    usage_output_tokens,
)

logger = logging.getLogger(__name__)

REMOTE_MCP_PYTHON = "/tmp/octagon-mcp-venv/bin/python"
REMOTE_BASE_DIR = "/tmp/octagon-attempts"


class SshClaudeCodeAdapter:
    # 能力静态声明：adapter 自身依赖本机 ssh 二进制。
    capabilities = AdapterCapabilities(
        execution_locus="remote-host",
        network_required="public_internet",
        system_requires=("ssh",),
    )

    # wire 观测显式不适用：远端机器上的 CLI 无本地 spool/注入通道，
    # 区别于「支持但未启用」。本 adapter 收到任何非空 injection 都不消费。
    WIRE_SUPPORT = "not-applicable"

    @property
    def wire_capture_capabilities(self) -> dict[str, str]:
        return {"wire": self.WIRE_SUPPORT}

    def __init__(
        self,
        *,
        ssh_host: str,
        ssh_user: str,
        ssh_password: str,
        octagon_project_path: str | Path = ".",
        max_budget_usd: float = 5.0,
    ) -> None:
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_password = ssh_password
        self.octagon_project_path = str(Path(octagon_project_path).resolve())
        self.max_budget_usd = max_budget_usd

    async def run(
        self,
        task: AdapterRunInput,
        env: Any,
        data_path: Path,
    ) -> AdapterResult:
        data_path = Path(data_path)
        attempt_dir = data_path / "attempts" / task.attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        events_path = attempt_dir / "events.jsonl"
        thinking_path = attempt_dir / "thinking.jsonl"

        remote_dir = f"{REMOTE_BASE_DIR}/{task.attempt_id}"

        # write local files for upload
        prompt_text = self._render_prompt(task)
        (attempt_dir / "prompt.txt").write_text(prompt_text, encoding="utf-8")

        mcp_config_path: Path | None = None
        if task.mcp_servers:
            mcp_config = self._build_mcp_config(task, self.octagon_project_path)
            mcp_config_path = attempt_dir / "mcp_config.json"
            mcp_config_path.write_text(
                json.dumps(mcp_config, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        events_count = 0
        thinking_count = 0
        total_input_tokens = 0
        total_output_tokens = 0
        estimated_input_tokens = 0
        estimated_output_tokens = 0
        last_event_at: str | None = None
        final_result: dict | None = None
        model_used: str | None = None
        error_message: str | None = None
        started_at = datetime.now(timezone.utc)

        try:
            # 1. create remote dir
            rc = await self._ssh_exec(f"mkdir -p {remote_dir}")
            if rc != 0:
                return AdapterResult(
                    attempt_id=task.attempt_id,
                    status="cli_error",
                    error_code="ssh_mkdir_failed",
                    error_message=f"failed to create remote dir {remote_dir}",
                )

            # 2. SCP files to remote
            mcp_server_path = self._declared_mcp_script(task)
            upload_ok = await self._upload_files(
                task, attempt_dir, mcp_server_path, remote_dir,
                include_mcp_config=mcp_config_path is not None,
            )
            if not upload_ok:
                return AdapterResult(
                    attempt_id=task.attempt_id,
                    status="cli_error",
                    error_code="scp_upload_failed",
                    error_message="failed to SCP files to remote",
                )

            # 2b. SCP uploaded attachments (video etc) to remote dir
            upload_error = await self._upload_attachments(task, remote_dir, data_path)
            if upload_error:
                return AdapterResult(
                    attempt_id=task.attempt_id,
                    status="cli_error",
                    error_code=(
                        "missing_uploaded_file"
                        if upload_error.startswith("missing uploaded file:")
                        else "scp_upload_failed"
                    ),
                    error_message=upload_error,
                )

            # 3. SSH execute claude CLI
            mcp_arg = (
                f"--mcp-config {remote_dir}/mcp_config.json "
                if mcp_config_path is not None else ""
            )
            claude_cmd = (
                f"cd {remote_dir} && cat prompt.txt | claude -p - "
                f"--output-format stream-json --verbose "
                f"{mcp_arg}"
                f"--dangerously-skip-permissions "
                f"--max-turns 50"
            )
            proc = await asyncio.create_subprocess_exec(
                "sshpass", "-p", self.ssh_password,
                "ssh", "-o", "StrictHostKeyChecking=no",
                f"{self.ssh_user}@{self.ssh_host}",
                claude_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=10 * 1024 * 1024,
            )

            async def _consume() -> None:
                nonlocal events_count, thinking_count, last_event_at
                nonlocal total_input_tokens, total_output_tokens, final_result
                nonlocal estimated_input_tokens, estimated_output_tokens
                nonlocal model_used
                assert proc.stdout is not None
                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    ts = _now_iso()
                    last_event_at = ts

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        _append_jsonl(events_path, {"timestamp": ts, "raw_line": line})
                        events_count += 1
                        continue

                    _append_jsonl(events_path, {"timestamp": ts, **data})
                    events_count += 1
                    est_input, est_output = estimate_tokens_from_event(data)
                    estimated_input_tokens += est_input
                    estimated_output_tokens += est_output

                    msg_type = data.get("type")
                    if msg_type == "system" and data.get("subtype") == "init":
                        model_used = data.get("model") or model_used
                    if msg_type == "assistant":
                        message = data.get("message", {})
                        model_used = message.get("model") or model_used
                        for block in message.get("content", []):
                            if block.get("type") == "thinking":
                                thinking_count += 1
                                _append_jsonl(thinking_path, {
                                    "timestamp": ts,
                                    "sequence": thinking_count,
                                    "content": block.get("thinking", ""),
                                    "type": "thinking",
                                })
                        usage = message.get("usage", {})
                        total_input_tokens += usage_input_tokens(usage)
                        total_output_tokens += usage_output_tokens(usage)
                    elif msg_type == "result":
                        final_result = data
                        result_input, result_output = result_usage_tokens(data)
                        if result_input:
                            total_input_tokens = result_input
                        if result_output:
                            total_output_tokens = result_output

            try:
                await asyncio.wait_for(_consume(), timeout=task.timeout_seconds)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                error_message = f"timeout after {task.timeout_seconds}s"

            if proc.stderr:
                stderr_bytes = await proc.stderr.read()
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                if stderr_text:
                    (attempt_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")
                    if not error_message and proc.returncode and proc.returncode != 0:
                        error_message = stderr_text[:500]

        except Exception as exc:
            return AdapterResult(
                attempt_id=task.attempt_id,
                status="cli_error",
                error_code="unexpected_error",
                error_message=str(exc),
            )
        finally:
            # 4. cleanup remote
            await self._ssh_exec(f"rm -rf {remote_dir}")

        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        status = _classify_outcome(proc.returncode, final_result, error_message)
        token_usage_estimated = False
        if total_input_tokens == 0 and total_output_tokens == 0 and (
            estimated_input_tokens or estimated_output_tokens
        ):
            total_input_tokens = estimated_input_tokens
            total_output_tokens = estimated_output_tokens
            token_usage_estimated = True

        return AdapterResult(
            attempt_id=task.attempt_id,
            status=status,
            external_refs={
                "session_id": final_result.get("session_id") if final_result else None,
                "ssh_host": self.ssh_host,
                "token_usage_estimated": token_usage_estimated,
                "model_used": model_used,
            },
            error_code=None if status == "completed" else (error_message or "cli_error"),
            error_message=error_message,
            events_count=events_count,
            last_event_at=last_event_at,
            thinking_count=thinking_count,
            token_usage={"input_tokens": total_input_tokens, "output_tokens": total_output_tokens},
            duration_ms=duration_ms,
            # 远端宿主机以 skip-permissions 跑，workspace=远程 attempt 目录
            security_meta=build_security_meta(
                execution_locus=self.capabilities.execution_locus,
                permission_mode="--dangerously-skip-permissions",
                workspace_root=remote_dir,
                sandbox_id=self.ssh_host,
                # 远端主机直接跑 claude，不在本方案围栏内，如实记录。
                extra={"sandbox": "none", "egress_policy": "unrestricted"},
            ),
        )

    def _build_mcp_config(self, task: AdapterRunInput, octagon_project_path: str) -> dict:
        if len(task.mcp_servers) != 1:
            raise ValueError("SSH Claude adapter 当前只支持一个场景 MCP server")
        spec = task.mcp_servers[0]
        remote_dir = f"{REMOTE_BASE_DIR}/{task.attempt_id}"
        return {
            "mcpServers": {
                spec.name: {
                    "command": REMOTE_MCP_PYTHON,
                    "args": [f"{remote_dir}/mcp_server.py"],
                    "env": {
                        "OCTAGON_ATTEMPT_ID": task.attempt_id,
                        "OCTAGON_ENV_TOKEN": task.env_token,
                        "OCTAGON_BASE_URL": task.env_base_url,
                    },
                }
            }
        }

    def _render_prompt(self, task: AdapterRunInput) -> str:
        parts = [task.task_prompt]
        context = prompt_context(task.task_context) if task.task_context else {}
        if context:
            parts.append("")
            parts.append("上下文：")
            parts.append(json.dumps(context, ensure_ascii=False, indent=2))
        return "\n".join(parts)

    def _declared_mcp_script(self, task: AdapterRunInput) -> str | None:
        """SSH 适配只上传场景声明命令中的 Python 入口，不再按 env 名猜路径。"""
        if not task.mcp_servers:
            return None
        if len(task.mcp_servers) != 1:
            raise ValueError("SSH Claude adapter 当前只支持一个场景 MCP server")
        spec = task.mcp_servers[0]
        for arg in reversed(spec.args):
            if arg.endswith(".py"):
                path = Path(arg)
                if not path.is_absolute():
                    path = Path(spec.cwd or self.octagon_project_path) / path
                    # 测试/迁移期声明的 cwd 可能来自另一台机器；命令中的相对入口
                    # 仍以当前 Octagon project 为可移植后备解析基准。
                    if not path.is_file():
                        candidate = Path(self.octagon_project_path) / arg
                        if candidate.is_file():
                            path = candidate
                return str(path.resolve())
        raise ValueError("SSH Claude adapter 要求场景 MCP command 包含 Python 脚本入口")

    async def _ssh_exec(self, cmd: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "sshpass", "-p", self.ssh_password,
            "ssh", "-o", "StrictHostKeyChecking=no",
            f"{self.ssh_user}@{self.ssh_host}",
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return proc.returncode or 0

    async def _upload_files(
        self,
        task: AdapterRunInput,
        attempt_dir: Path,
        mcp_server_path: str | None,
        remote_dir: str,
        *,
        include_mcp_config: bool,
    ) -> bool:
        files = [
            (str(attempt_dir / "prompt.txt"), f"{remote_dir}/prompt.txt"),
        ]
        if include_mcp_config:
            assert mcp_server_path is not None
            files += [
                (str(attempt_dir / "mcp_config.json"), f"{remote_dir}/mcp_config.json"),
                (mcp_server_path, f"{remote_dir}/mcp_server.py"),
            ]
        for local, remote in files:
            proc = await asyncio.create_subprocess_exec(
                "sshpass", "-p", self.ssh_password,
                "scp", "-o", "StrictHostKeyChecking=no",
                local, f"{self.ssh_user}@{self.ssh_host}:{remote}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode != 0:
                logger.error("SCP failed for %s -> %s", local, remote)
                return False
        return True

    async def _upload_attachments(self, task: AdapterRunInput, remote_dir: str, data_path: Path) -> str | None:
        uploaded = task.task_context.get("uploaded_files")
        if not uploaded or not isinstance(uploaded, list):
            return None
        workspace = Path(data_path).resolve() / "attempts" / task.attempt_id / "skill_workspace"
        for uf in uploaded:
            name = uf.get("name", "")
            src = workspace / name
            if not src.is_file():
                logger.warning("_upload_attachments: file not found: %s", name)
                return f"missing uploaded file: {name}"
            proc = await asyncio.create_subprocess_exec(
                "sshpass", "-p", self.ssh_password,
                "scp", "-o", "StrictHostKeyChecking=no",
                str(src), f"{self.ssh_user}@{self.ssh_host}:{remote_dir}/{name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode != 0:
                logger.error("SCP attachment failed: %s", name)
                return f"failed to SCP uploaded file: {name}"
            logger.info("_upload_attachments: %s -> %s/%s", name, remote_dir, name)
        return None


def _classify_outcome(
    returncode: int | None,
    final_result: dict | None,
    error_message: str | None,
) -> str:
    if error_message and "timeout" in error_message.lower():
        return "timeout"
    if returncode is None:
        return "cli_error"
    if final_result:
        subtype = final_result.get("subtype", "")
        if "budget" in subtype:
            return "timeout"
        if subtype == "success" and not final_result.get("is_error"):
            return "completed"
        if final_result.get("is_error"):
            return "cli_error"
    if returncode != 0:
        return "cli_error"
    return "completed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_jsonl(path: Path, data: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
