"""第三方模型 provider 配置与模型标识符解析。

模型标识符约定：

- `"<provider>/<model>"`（首段命中 octagon.yaml `model_providers` 的 key）
  → 走命名 provider（CC 注入 ANTHROPIC_BASE_URL，Codex 注入 -c 覆盖）。
- 其余（如 `"opus"`、`"upstream/z-ai/glm-5.2"`）→ provider=None，整串作模型名，
  走各 adapter 的默认 provider。blade 侧模型名天然带 `/`（gateway source 前缀），
  只要 source 名不与 provider 重名就不会被误拆。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

# canonical / in-memory 协议名。
WireProtocol = Literal[
    "openai-chat-completions",
    "anthropic-messages",
    "openai-responses",
]

# 旧 kind → canonical。before-validator 接受新旧值，旧 octagon.yaml 零改动。
_LEGACY_KIND_MAP: dict[str, str] = {
    "openai-chat": "openai-chat-completions",
    "anthropic": "anthropic-messages",
    "vllm-responses": "openai-responses",
}

# canonical kind → Codex -c model_providers.*.wire_api 值（chat|responses）。
_KIND_TO_WIRE_API: dict[str, str] = {
    "openai-chat-completions": "chat",
    "anthropic-messages": "chat",
    "openai-responses": "responses",
}


class ModelProviderSection(BaseModel):
    kind: WireProtocol
    base_url: str
    api_key_env: str | None = None
    # key 直填（octagon.yaml 已 gitignore，blade api_key 同样放这里）。
    # 解析优先级见 resolve_api_key：环境变量命中优先，未设时回落到这里。
    # 避免「后端进程忘了 export → CC 报 Not logged in / Codex 报 Missing env」。
    api_key: str | None = None
    # 认证方式：bearer 注入 ANTHROPIC_AUTH_TOKEN（发
    # Authorization: Bearer），api-key 注入 ANTHROPIC_API_KEY（发 x-api-key）。
    # 不做 token 值转写。None → 按 kind 取默认（见 effective_auth_mode）。
    auth_mode: Literal["bearer", "api-key"] | None = None
    # 旧字段：Codex wire_api，deprecated。仍接受但其值必须与
    # canonical kind 推导出的 wire_api 一致，否则配置加载 fail fast。新配置不写
    # 该字段——由 kind 推导（见 effective_wire_api）。
    wire_api: str | None = None
    # 部分内网 gateway（如 blade llm-gateway）鉴权/记账依赖自定义头，如
    # "x-user-id: octagon"。格式与 Claude CLI 的 ANTHROPIC_CUSTOM_HEADERS
    # 一致（"Header: value"，多个用 \n 分隔），原样透传，不解析。
    custom_headers: str | None = None
    # 该 provider 前缀服务于哪个 agent（如 or-cc → claude-code、or-codex → codex）。
    # 后端不消费；暴露给前端做 same-model 下拉「裸模型名 + 按选中 agent 拼前缀」。
    # None 表示未绑定 agent（向后兼容旧配置）。
    agent: str | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _canonicalize_kind(cls, v: object) -> object:
        # 接受旧值统一为 canonical，未知值原样透传给 Literal 报错。
        if isinstance(v, str) and v in _LEGACY_KIND_MAP:
            return _LEGACY_KIND_MAP[v]
        return v

    @model_validator(mode="after")
    def _check_wire_api_consistency(self) -> "ModelProviderSection":
        # wire_api 若显式给出，必须与 canonical kind 一致，否则 fail fast。
        if self.wire_api is not None:
            expected = _KIND_TO_WIRE_API[self.kind]
            if self.wire_api != expected:
                raise ValueError(
                    f"provider wire_api={self.wire_api!r} 与 kind={self.kind!r} "
                    f"不一致（应为 {expected!r}）；wire_api 已 deprecated，"
                    "新配置请只写 kind。"
                )
        return self

    def effective_wire_api(self) -> str:
        """Codex -c 用的 wire_api：由 canonical kind 推导（wire_api 已 deprecated）。"""
        return _KIND_TO_WIRE_API[self.kind]

    def effective_auth_mode(self) -> Literal["bearer", "api-key"]:
        """auth_mode 未显式设时的默认：所有 protocol 默认 bearer。"""
        return self.auth_mode or "bearer"


@dataclass
class ModelRef:
    raw: str  # 用户输入原串，落 DB / 展示用
    provider: str | None  # None = adapter 默认 provider
    model: str  # provider 内的模型名


def parse_model_ref(
    raw: str, providers: dict[str, ModelProviderSection]
) -> ModelRef:
    if "/" in raw:
        prefix, rest = raw.split("/", 1)
        if prefix in providers:
            return ModelRef(raw=raw, provider=prefix, model=rest)
    return ModelRef(raw=raw, provider=None, model=raw)


def resolve_api_key(provider: ModelProviderSection) -> str | None:
    if provider.api_key_env:
        from_env = os.environ.get(provider.api_key_env)
        if from_env:
            return from_env
    return provider.api_key
