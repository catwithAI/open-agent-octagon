"""Independent OpenAI-compatible and optional Blade Insight providers."""

from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from backend.config import InsightsSection
from backend.insights.jsonutil import content_as_workspace_tool
from backend.insights.workspace import (
    OPENAI_WORKSPACE_TOOLS,
    EvidenceWorkspace,
)


class InsightProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResult:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None


class InsightProvider(Protocol):
    provider_name: str
    model: str | None

    async def generate(
        self,
        prompt: str,
        *,
        workspace: EvidenceWorkspace | None = None,
    ) -> ProviderResult: ...


async def invoke_generate(
    provider: InsightProvider,
    prompt: str,
    *,
    workspace: EvidenceWorkspace | None = None,
) -> ProviderResult:
    """Call generate(); pass workspace only when the provider accepts it."""
    try:
        signature = inspect.signature(provider.generate)
    except (TypeError, ValueError):
        return await provider.generate(prompt)
    if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()):
        return await provider.generate(prompt, workspace=workspace)
    if "workspace" in signature.parameters:
        return await provider.generate(prompt, workspace=workspace)
    return await provider.generate(prompt)


def _api_key(config: InsightsSection) -> str:
    value = os.environ.get(config.api_key_env)
    if not value:
        raise InsightProviderError(
            f"insight API key environment variable is not set: {config.api_key_env}"
        )
    return value


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: InsightsSection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.provider_name = "openai_compatible"
        self.model = config.model

    async def generate(
        self,
        prompt: str,
        *,
        workspace: EvidenceWorkspace | None = None,
    ) -> ProviderResult:
        if not self.config.base_url or not self.config.model:
            raise InsightProviderError("openai_compatible insights provider is not configured")
        headers = {
            "Authorization": f"Bearer {_api_key(self.config)}",
            "Content-Type": "application/json",
        }
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": messages,
        }
        # json_object plus tools makes some models emit a JSON object immediately
        # instead of calling list/read. Only force JSON when there is no workspace.
        if workspace is None:
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["tools"] = OPENAI_WORKSPACE_TOOLS
            payload["tool_choice"] = "auto"
        usage: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds,
                transport=self.transport,
            ) as client:
                for _step in range(16):
                    response = await client.post(
                        f"{self.config.base_url.rstrip('/')}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()
                    usage = body.get("usage") or usage
                    choice = body["choices"][0]
                    message = choice.get("message") or {}
                    tool_calls = list(message.get("tool_calls") or [])
                    if workspace is not None and not tool_calls:
                        fake = content_as_workspace_tool(message.get("content") or "")
                        if fake is not None:
                            name, arguments = fake
                            tool_calls = [{
                                "id": f"synthetic-{_step}-{name}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }]
                    if workspace is not None and tool_calls:
                        messages.append({
                            "role": "assistant",
                            "content": message.get("content") or "",
                            "tool_calls": tool_calls,
                        })
                        for call in tool_calls:
                            function = call.get("function") or {}
                            raw_args = function.get("arguments") or "{}"
                            try:
                                arguments = json.loads(raw_args) if raw_args else {}
                            except json.JSONDecodeError:
                                arguments = {}
                            if not isinstance(arguments, dict):
                                arguments = {}
                            messages.append({
                                "role": "tool",
                                "tool_call_id": call.get("id") or function.get("name"),
                                "content": workspace.dispatch(
                                    str(function.get("name") or ""),
                                    arguments,
                                ),
                            })
                        payload["messages"] = messages
                        continue
                    text = message.get("content")
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError("empty provider content")
                    return ProviderResult(
                        text=text,
                        provider="openai_compatible",
                        model=self.config.model,
                        input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                    )
                raise ValueError("workspace tool loop exceeded 16 steps")
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InsightProviderError(f"openai_compatible generation failed: {exc}") from exc


class BladeProvider:
    """Optional Blade implementation, imported only when explicitly selected."""

    def __init__(
        self,
        config: InsightsSection,
        *,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.provider_name = "blade"
        self.model = config.model

    def _client(self, token: str) -> Any:
        if self.client_factory is not None:
            return self.client_factory(
                self.config.base_url,
                token=token,
                timeout=self.config.timeout_seconds,
            )
        try:
            from blade_agent_kit import BladeAgentClient
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise InsightProviderError("blade_agent_kit is not installed") from exc
        return BladeAgentClient(
            self.config.base_url,
            token=token,
            timeout=self.config.timeout_seconds,
        )

    async def generate(
        self,
        prompt: str,
        *,
        workspace: EvidenceWorkspace | None = None,
    ) -> ProviderResult:
        if not self.config.base_url or not self.config.model:
            raise InsightProviderError("blade insights provider is not configured")
        if not self.config.blade_primary_skill_id:
            raise InsightProviderError("blade_primary_skill_id is required for blade insights")
        session_id: str | None = None
        parts: list[str] = []
        usage: dict[str, Any] = {}
        try:
            client = self._client(_api_key(self.config))
            async with client:
                session = await client.create_session(
                    intent="octagon consortium insight generation",
                    primary_skill_id=self.config.blade_primary_skill_id,
                    model=self.config.model,
                    memory_enabled=False,
                )
                session_id = session.id
                if workspace is not None:
                    await _upload_workspace(client, session_id, workspace)
                stream = client.chat(session_id, prompt, headless=True)
                try:
                    async for event in stream:
                        raw = event.raw if isinstance(event.raw, dict) else {}
                        if event.kind == "llm:text:delta":
                            content = (raw.get("payload") or {}).get("content")
                            if content:
                                parts.append(str(content))
                        elif event.kind == "turn:end":
                            for block in raw.get("blocks") or []:
                                if block.get("type") in {"text", "output_text"}:
                                    parts.append(str(block.get("content") or ""))
                            usage = raw.get("usage") or usage
                finally:
                    closer = getattr(stream, "aclose", None)
                    if closer is not None:
                        result = closer()
                        if inspect.isawaitable(result):
                            await result
                if session_id and not self.config.keep_blade_session:
                    await client.delete_session(session_id)
        except InsightProviderError:
            raise
        except Exception as exc:
            raise InsightProviderError(f"blade generation failed: {exc}") from exc
        text = "".join(parts).strip()
        if not text:
            raise InsightProviderError("blade generation returned empty content")
        return ProviderResult(
            text=text,
            provider="blade",
            model=self.config.model,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )


async def _upload_workspace(
    client: Any, session_id: str, workspace: EvidenceWorkspace
) -> None:
    for relative, path in sorted(workspace.files.items()):
        parent = str(Path(relative).parent).replace("\\", "/")
        remote_dir = "." if parent in {"", "."} else parent
        await client.upload_file(
            session_id,
            path,
            dir_path=remote_dir,
            remote_path=relative,
        )


def configured_provider(
    config: InsightsSection,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> InsightProvider:
    if config.provider == "blade":
        return BladeProvider(config)
    return OpenAICompatibleProvider(config, transport=transport)
