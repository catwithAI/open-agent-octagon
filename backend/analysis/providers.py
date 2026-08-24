"""Blade-backed runtrace judge provider.

The judge uses a session-scoped read-only skill, mirroring LLM-as-Judge: Octagon
freezes and supplies evidence; Blade performs semantic interpretation; local code
validates schemas and anchors.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Callable

import httpx

from backend.config import InsightsSection
from backend.insights.providers import (
    InsightProvider,
    InsightProviderError,
    OpenAICompatibleProvider,
    ProviderResult,
    _upload_workspace,
)
from backend.insights.workspace import EvidenceWorkspace

SKILL_NAME = "octagon/runtrace-judge"
_SKILL_PATH = Path(__file__).with_name("blade_skill") / "SKILL.md"


class BladeRuntraceJudgeProvider:
    provider_name = "blade_runtrace_judge"

    def __init__(
        self,
        config: InsightsSection,
        *,
        client_factory: Callable[..., Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.model = config.model
        self.client_factory = client_factory
        self.transport = transport

    def _api_key(self) -> str:
        value = os.environ.get(self.config.api_key_env)
        if not value:
            raise InsightProviderError(
                f"Blade runtrace judge key is not set: {self.config.api_key_env}"
            )
        return value

    def _client(self, token: str) -> Any:
        if self.client_factory is not None:
            return self.client_factory(
                self.config.base_url,
                token=token,
                timeout=self.config.timeout_seconds,
            )
        try:
            from blade_agent_kit import BladeAgentClient
        except ImportError as exc:  # pragma: no cover
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
            raise InsightProviderError("Blade runtrace judge is not configured")
        token = self._api_key()
        session_id: str | None = None
        parts: list[str] = []
        response_content: str | None = None
        submitted_result: str | None = None
        usage: dict[str, Any] = {}
        try:
            client = self._client(token)
            async with client:
                # Bare session first: the judge skill is uploaded per session, so it
                # does not depend on the Blade server's global skill registry.
                session = await client.create_session(
                    intent="Octagon black-box runtrace analysis judge",
                    model=self.config.model,
                    enable_thinking=False,
                    memory_enabled=False,
                )
                session_id = session.id
                skill_content = _SKILL_PATH.read_text(encoding="utf-8")
                await client.upload_session_skill(
                    session_id,
                    name=SKILL_NAME,
                    files=[{"path": "SKILL.md", "content": skill_content}],
                )
                if workspace is not None:
                    await _upload_workspace(client, session_id, workspace)
                # Current Blade servers activate a successfully uploaded
                # session-scoped skill immediately; the historical skills:use
                # route is not part of the public SDK and may not exist.
                stream = client.chat(session_id, prompt, headless=True)
                try:
                    async for event in stream:
                        raw = event.raw if isinstance(event.raw, dict) else {}
                        if event.kind == "llm:text:delta":
                            content = (raw.get("payload") or {}).get("content")
                            if content:
                                parts.append(str(content))
                        elif event.kind == "llm:response:done":
                            payload = raw.get("payload") or {}
                            content = payload.get("content")
                            if isinstance(content, str) and content.strip():
                                response_content = content
                            usage = payload.get("usage") or usage
                        elif event.kind == "turn:end":
                            for block in raw.get("blocks") or []:
                                if block.get("type") in {"text", "output_text"}:
                                    parts.append(str(block.get("content") or ""))
                            usage = raw.get("usage") or usage
                        elif event.kind == "chat:end":
                            result = raw.get("result")
                            if isinstance(result, dict) and isinstance(result.get("result"), str):
                                submitted_result = result["result"]
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
            raise InsightProviderError(f"Blade runtrace judge failed: {exc}") from exc
        text = (submitted_result or response_content or "".join(parts)).strip()
        if not text:
            raise InsightProviderError("Blade runtrace judge returned empty content")
        return ProviderResult(
            text=text,
            provider=self.provider_name,
            model=str(self.config.model),
            input_tokens=usage.get("input_tokens") or usage.get("prompt_tokens"),
            output_tokens=usage.get("output_tokens") or usage.get("completion_tokens"),
        )


def configured_analysis_provider(
    config: InsightsSection,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> InsightProvider:
    if config.provider == "blade":
        return BladeRuntraceJudgeProvider(config, transport=transport)
    return OpenAICompatibleProvider(config, transport=transport)
