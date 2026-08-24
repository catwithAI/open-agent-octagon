"""Wire capture lifecycle。

dispatch 侧的编排对象：``WireCaptureSession.prepare()`` 在 adapter ``run()``
之前完成 source 启动与 injection 合并，``AttemptObserver`` 接口供 runner 推进
phase 与 finalize。

prepare() 严格时序：

    创建 spool → 写 start event → 逐 source start(ctx) → ready 探测
    → 合并/校验 injection → 写 ready event → 返回

默认 fail-open：source 启动失败降级为「无该 source」并记 capability gap，
adapter 拿到的 injection 不包含任何半成品注入（env/base_url 不被污染）。
唯一的 fail-closed 分支：strict 模式下声明了要改写 base URL/command 的
source 无法 ready——此时在 agent 启动前抛 ``CapturePreparationError``，由
dispatch 记录独立的 capture/infrastructure outcome，不伪装成 agent failure。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import urlparse

from backend.wire import ids, paths, spool, writer
from backend.wire.evidence import (
    CaptureEventEvidence,
    CaptureEventPayload,
    CorrelationHints,
    EvidenceProducer,
    EvidenceRedaction,
    EvidenceSource,
    EvidenceTime,
)
from backend.wire.injection import PhaseStateRef, WireInjection
from backend.wire.policy import EffectivePolicy, resolve_effective_policy
from backend.wire.redaction import BLOCKED_HEADERS, DEFAULT_KEY_PATTERN, scrub_text

logger = logging.getLogger(__name__)


# ---------- adapter capability registry----------------------
#
# dispatch 侧的无 I/O 静态声明：injection 的哪些字段该 adapter 能消费。
# 未知/第三方 adapter 得到全 false，因此不会收到它无法消费的注入。

ADAPTER_CAPTURE_CAPABILITIES: dict[str, dict[str, Any]] = {
    "claude-code": {
        "process_env": True,
        "llm_base_url": True,
        "llm_headers": True,
        "mcp_rewrites": True,
    },
    "codex": {
        "process_env": True,
        "llm_base_url": True,
        # codex 的静态 provider header 通道未定义，暂不实现。
        "llm_headers": False,
        "mcp_rewrites": True,
    },
    # kimi / opencode / mimo：base_url 与 headers 都由 adapter 生成的配置写入
    # （kimi 走 KIMI_CODE_CUSTOM_HEADERS，opencode 系走
    # provider.options.headers），故 llm_headers 同样可消费——反代的
    # capture token 正是靠它下发。
    "kimi-code": {
        "process_env": True,
        "llm_base_url": True,
        "llm_headers": True,
        "mcp_rewrites": True,
    },
    "opencode": {
        "process_env": True,
        "llm_base_url": True,
        "llm_headers": True,
        "mcp_rewrites": True,
    },
    "mimo-code": {
        "process_env": True,
        "llm_base_url": True,
        "llm_headers": True,
        "mcp_rewrites": True,
    },
    # dsh：四项都可消费——process_env 走 SDK 的 env 参数，base_url/headers 走
    # adapter 生成的 pi-ai route（capture token 正是靠 headers 下发），
    # mcp_rewrites 走生成 cordis 时改写 mcp-client 的 command/args。
    "dsh": {
        "process_env": True,
        "llm_base_url": True,
        "llm_headers": True,
        "mcp_rewrites": True,
    },
    "blade-agent": {
        # session metadata 透传要等与 blade server 协商出 capability 声明；
        # 静态默认 False，运行时由 resolved adapter 的实际配置刷新（见
        # capture_capabilities_for 的 adapter 分支）。REST/Socket.IO 共用
        # endpoint，任何情况下不允许注入 base URL。
        "blade_session_metadata": False,
    },
    # ssh 远端 CLI 无本地 wire 通道，显式 not-applicable
    # 而非静默全 false（区分「没实现」与「不适用」）。
    "ssh-claude-code": {"wire": "not-applicable"},
}


def capture_capabilities_for(
    agent_name: str, adapter: Any | None = None
) -> dict[str, Any]:
    """adapter capability 声明。

    优先取 resolved adapter 实例的 ``wire_capture_capabilities``（它能反映
    运行时配置，如 blade 的 session_metadata_capability），静态 registry 只是
    没有 adapter 实例时的兜底。capability 校验必须发生在 agent 启动前——
    lifecycle 用它在 merge 阶段丢弃并登记 gap，绝不把 adapter 消费不了的
    注入下发后靠 adapter 自己发现。
    """
    if adapter is not None:
        declared = getattr(adapter, "wire_capture_capabilities", None)
        if declared is not None:
            return dict(declared)
    return dict(ADAPTER_CAPTURE_CAPABILITIES.get(agent_name, {}))


# ---------- source contract----------------------------------


@dataclass
class CaptureContext:
    attempt_id: str
    attempt_dir: Path
    agent_name: str
    phase: str
    policy: EffectivePolicy


class CaptureSource(Protocol):
    kind: str
    # 声明该 source 是否要改写 base URL/command（决定 strict 失败语义）。
    rewrites_transport: bool

    async def start(self, ctx: CaptureContext) -> WireInjection: ...
    async def collect(self, ctx: CaptureContext) -> dict[str, Any]: ...
    async def stop(self, ctx: CaptureContext) -> dict[str, Any]: ...


# ---------- injection 合并与校验 -------------------------------------------


class InjectionMergeError(ValueError):
    """两个 source 对同一标量给出不同值 / 注入了保留名或 secret——配置错误，
    不做 last-wins。"""


class CapturePreparationError(RuntimeError):
    """strict 模式下改写型 source 无法 ready；agent 尚未启动。"""


# process_env 静态保留名：Octagon 自身通道 + Anthropic 认证 + base URL 专用通道。
# provider 的 api_key_env 是任意名称（如 UP_KEY），必须由调用方把 resolved
# provider 配置里的全部 credential env 名作为 protected_env_keys 传入——
# 否则 source 可以用 process_env 覆盖真实 credential。
RESERVED_ENV_KEYS = frozenset(
    {
        "OCTAGON_ATTEMPT_ID",
        "OCTAGON_ENV_TOKEN",
        "OCTAGON_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",  # base URL 只能走 llm_base_url 字段
    }
)

# header name 必须是 RFC 7230 token；value 禁止 CR/LF/NUL 与其他控制字符——
# CC 会把 llm_headers 拼进 ANTHROPIC_CUSTOM_HEADERS（换行分隔），CR/LF 即
# header 注入。
# 正则一律用 \Z 而非 $：$ 接受尾部换行，会给注入留缝。
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_HEADER_VALUE_BAD_RE = re.compile(r"[\r\n\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# llm_base_url / Codex -c 安全字符集：URL 合法字符之外（引号、反斜杠、空白、
# 控制字符）一律拒绝，防止 TOML `-c model_providers.*.base_url="..."` 逃逸。
_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\Z")


def _validate_base_url(url: str) -> None:
    if not _URL_SAFE_RE.match(url) or "'" in url:
        raise InjectionMergeError(f"llm_base_url 含非法字符: {url!r}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InjectionMergeError(f"llm_base_url 必须是 http(s) 绝对 URL: {url!r}")


def _validate_injection(
    inj: WireInjection, protected_env_keys: frozenset[str]
) -> None:
    for key in inj.process_env:
        if key in RESERVED_ENV_KEYS or key in protected_env_keys:
            raise InjectionMergeError(f"process_env 含保留名/受保护 credential env: {key}")
        if DEFAULT_KEY_PATTERN.search(key):
            raise InjectionMergeError(
                f"process_env 疑似携带 secret（认证只能来自 provider config）: {key}"
            )
    for key, value in inj.process_env.items():
        if _HEADER_VALUE_BAD_RE.search(str(value)):
            raise InjectionMergeError(f"process_env[{key}] 含控制字符")
    for name, value in inj.llm_headers.items():
        if name.lower() in BLOCKED_HEADERS:
            raise InjectionMergeError(f"llm_headers 禁止携带认证 header: {name}")
        if not _HEADER_NAME_RE.match(name):
            raise InjectionMergeError(f"llm_headers 非法 header name: {name!r}")
        if _HEADER_VALUE_BAD_RE.search(str(value)):
            raise InjectionMergeError(
                f"llm_headers[{name}] 含 CR/LF/控制字符（header 注入）"
            )
    if inj.llm_base_url is not None:
        _validate_base_url(inj.llm_base_url)


def merge_injections(
    injections: Sequence[WireInjection],
    *,
    capabilities: dict[str, Any] | None = None,
    protected_env_keys: frozenset[str] = frozenset(),
) -> tuple[WireInjection, list[dict[str, str]]]:
    """按 source 顺序合并，同一标量两个非空值即配置错误（不 last-wins）。

    返回 (merged, gaps)：capabilities 里未声明 True 的非空字段被丢弃并生成
    capability gap——禁止 adapter 收到再静默忽略。
    ``protected_env_keys`` 是 resolved provider 的 credential env 名集合，
    与静态 RESERVED_ENV_KEYS 一起构成动态保护集。
    """
    active = [i for i in injections if i.enabled]
    for inj in active:
        _validate_injection(inj, protected_env_keys)
    if not active:
        return WireInjection(), []

    phases = {i.phase for i in active}
    if len(phases) > 1:
        raise InjectionMergeError(f"phase 不一致: {sorted(phases)}")

    def merge_scalar(name: str):
        values = [v for v in (getattr(i, name) for i in active) if v is not None]
        if len(set(values)) > 1:
            raise InjectionMergeError(f"{name} 冲突: 多个 source 给出不同值")
        return values[0] if values else None

    def merge_map(name: str, *, case_insensitive: bool = False) -> dict:
        out: dict = {}
        seen: dict[str, str] = {}  # 规范化 key → 首见原样 key
        for inj in active:
            for k, v in getattr(inj, name).items():
                norm = k.lower() if case_insensitive else k
                if norm in seen:
                    if out[seen[norm]] != v:
                        raise InjectionMergeError(
                            f"{name}[{k}] 冲突: 两个 source 值不同"
                        )
                    continue
                seen[norm] = k
                out[k] = v
        return out

    merged = WireInjection(
        enabled=True,
        phase=active[0].phase,
        process_env=merge_map("process_env"),
        llm_base_url=merge_scalar("llm_base_url"),
        # header 名大小写不敏感（RFC 7230）：X-Ok 与 x-ok 视为同一 header。
        llm_headers=merge_map("llm_headers", case_insensitive=True),
        mcp_rewrites=merge_map("mcp_rewrites"),
        blade_session_metadata=merge_map("blade_session_metadata"),
        phase_state=merge_scalar("phase_state"),
        capture_token=merge_scalar("capture_token"),
    )

    # capability gap：adapter 不支持的非空字段在 agent 启动前丢弃并登记。
    caps = capabilities or {}
    gaps: list[dict[str, str]] = []
    drops: dict[str, Any] = {}
    for fname, empty in (
        ("process_env", {}),
        ("llm_base_url", None),
        ("llm_headers", {}),
        ("mcp_rewrites", {}),
        ("blade_session_metadata", {}),
    ):
        value = getattr(merged, fname)
        if value not in (None, {}, ()) and caps.get(fname) is not True:
            gaps.append({"field": fname, "reason": "adapter_capability_missing"})
            drops[fname] = empty
    if drops:
        merged = WireInjection(
            **{
                **{
                    f: getattr(merged, f)
                    for f in (
                        "enabled",
                        "phase",
                        "process_env",
                        "llm_base_url",
                        "llm_headers",
                        "mcp_rewrites",
                        "blade_session_metadata",
                        "phase_state",
                        "capture_token",
                    )
                },
                **drops,
            }
        )
    return merged, gaps


# ---------- observer 接口------------------------------------


class AttemptObserver(Protocol):
    def phase(self, name: str): ...
    async def agent_result(self, result: Any) -> None: ...
    async def attempt_end(self) -> None: ...


class NullAttemptObserver:
    """wire 关闭时的零开销替身；行为与 wire 层不存在时完全一致。"""

    @asynccontextmanager
    async def phase(self, name: str):
        yield

    async def agent_result(self, result: Any) -> None:
        return None

    async def attempt_end(self) -> None:
        return None


# ---------- WireCaptureSession ---------------------------------------------


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


async def _to_thread(func, /, *args, **kwargs):
    return await asyncio.to_thread(lambda: func(*args, **kwargs))


class WireCaptureSession:
    """单 attempt 的 capture 编排。

    范围：prepare 时序、injection 合并、phase 上下文、abort；
    collect/normalize/finalize 接到 ``agent_result``/``attempt_end``。
    """

    def __init__(
        self,
        *,
        attempt_id: str,
        data_path: Path,
        agent_name: str,
        sources: Sequence[CaptureSource] = (),
        adapter_capabilities: dict[str, Any] | None = None,
        policy: EffectivePolicy | None = None,
        strict: bool = False,
        protected_env_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.attempt_id = attempt_id
        self.data_path = Path(data_path)
        self.agent_name = agent_name
        self.sources = list(sources)
        self.capabilities = (
            adapter_capabilities
            if adapter_capabilities is not None
            else capture_capabilities_for(agent_name)
        )
        self.policy = policy or resolve_effective_policy()
        self.strict = strict
        self.protected_env_keys = protected_env_keys

        self.injection = WireInjection()
        self.gaps: list[dict[str, str]] = []
        self.current_phase: str | None = None
        # phase 归属质量：phase-state 写失败降 degraded，
        # 落 manifest `phase_attribution`。
        self.phase_attribution = "explicit"
        # 测试与 finalizer 都依赖的调用时序记录。
        self.call_log: list[str] = []
        self._state = "created"
        self._event_seq = 0
        self._phase_seq = 0
        self._phase_state_path: Path | None = None
        self._spool: spool.SpoolWriter | None = None
        self._started_at: str | None = None
        # native normalizer 是否产出了 native-event source（agent_result 里跑）
        self._native_source_active = False
        # prepare 阶段声明 native normalizer 存在（即 native source 为 expected）
        self._native_expected = False

    # ---- capture_event spool ------------------------------------------

    def _write_capture_event(
        self,
        event: str,
        *,
        source_instance: str = "lifecycle",
        status: str | None = None,
        reason_code: str | None = None,
        message: str | None = None,
        counters: dict[str, int] | None = None,
    ) -> None:
        if self._spool is None:
            return
        self._event_seq += 1
        ev = CaptureEventEvidence(
            evidence_id=ids.evidence_id(
                attempt_id=self.attempt_id,
                source_kind="capture-events",
                source_instance=source_instance,
                raw_ref=f"lifecycle/{self._event_seq}",
            ),
            attempt_id=self.attempt_id,
            phase=self.current_phase or "attempt_setup",  # type: ignore[arg-type]
            source=EvidenceSource(kind="capture-events", instance=source_instance),
            producer=EvidenceProducer(name="octagon-lifecycle"),
            time=EvidenceTime(observed_at=_now_iso()),
            raw_ref=None,
            correlation_hints=CorrelationHints(),
            capabilities={},
            redaction=EvidenceRedaction(
                policy=self.policy.effective, status="applied"
            ),
            errors=[],
            extensions={},
            payload=CaptureEventPayload(
                event=event,  # type: ignore[arg-type]
                source_instance=source_instance,
                status=status,
                reason_code=reason_code,
                message=message,
                counters=counters,
                effective_capabilities=None,
            ),
        )
        with contextlib.suppress(spool.SpoolError):
            self._spool.append(ev)

    # ---- prepare（严格时序）--------------------------------

    async def prepare(self, phase: str = "agent_run") -> WireInjection:
        self.current_phase = "attempt_setup"
        self.call_log.append("prepare:begin")

        # 是否有 native normalizer（agent_result 阶段从 raw events 产 evidence）。
        # 有 native source 时即便没有 injection-source 也要建 spool，否则
        # agent_result 因 _spool is None 早退——真实 CC attempt 永远不产 wire。
        from backend.wire.normalizers.runner import normalizer_for

        has_native = normalizer_for(self.agent_name) is not None

        integrity_sources = [
            source for source in self.sources
            if getattr(source, "enforces_model_integrity", False)
        ]

        # policy off 仍需启动模型完整性代理，但保持零 capture 落盘。无完整性
        # source 时沿用真正的 noop（如 Blade / 无命名 provider）。
        if self.policy.effective == "off" and integrity_sources:
            collected: list[WireInjection] = []
            for source in integrity_sources:
                self.call_log.append(f"source.start:{source.kind}")
                try:
                    collected.append(await source.start(self._ctx(phase)))
                except Exception as exc:
                    await self.abort_before_or_during_run()
                    reason = scrub_text(f"{type(exc).__name__}: {exc}")
                    raise CapturePreparationError(
                        f"model integrity source {source.kind} 无法 ready: {reason}"
                    ) from exc
            try:
                merged, cap_gaps = merge_injections(
                    collected,
                    capabilities=self.capabilities,
                    protected_env_keys=self.protected_env_keys,
                )
            except InjectionMergeError as exc:
                await self.abort_before_or_during_run()
                raise CapturePreparationError(
                    f"model integrity injection merge failed: {scrub_text(str(exc))}"
                ) from exc
            if cap_gaps or not merged.enabled or not merged.llm_base_url:
                await self.abort_before_or_during_run()
                raise CapturePreparationError(
                    "model integrity proxy injection was dropped by adapter capabilities"
                )
            self.injection = merged
            self._state = "prepared"
            self.call_log.append("prepare:integrity-ready")
            return merged

        # 只在 policy off 时才 noop（零采集）。policy != off 时即便无 native
        # normalizer、无 injection source（如 Blade）也要建 capture context +
        # 写 phase-state control claim——否则 Blade 调 Env Attempt Server 的
        # inbound 请求永远不被采集。env-inbound 是被动 source，
        # 任何 agent 都可能触发。
        if self.policy.effective == "off":
            self._state = "prepared"
            self.call_log.append("prepare:noop")
            return self.injection

        # native source 在 prepare 阶段就声明为 expected：这样即便
        # agent_result 前进程崩溃 / raw 缺失 / normalizer 异常，in-progress 与
        # recovery manifest 也知道 native 本应工作——最终 finalize 见不到
        # native spool 时把它标 failed，而非整体 complete + not-observed。
        self._native_expected = has_native

        # 1) 创建 capture-events spool（writer 侧强制 attempt/policy 校验）
        self._spool = spool.SpoolWriter(
            paths.source_spool_file(self.data_path, self.attempt_id, "capture-events"),
            expected_attempt_id=self.attempt_id,
            max_policy=self.policy.effective,
        )
        collected: list[WireInjection] = []
        for source in self.sources:
            # 事件/gap 始终携带 resolved instance——finalizer 按 instance 归属，
            # 用 kind 会把一个实例的错误扇出污染同 kind 的全部实例。
            instance = getattr(source, "instance", source.kind)
            # 2) start event 先于 source.start
            self._write_capture_event("start", source_instance=instance)
            self.call_log.append(f"source.start:{source.kind}")
            try:
                # 3) source.start 即 ready 探测：返回 injection 表示 ready。
                inj = await source.start(self._ctx(phase))
                collected.append(inj)
            except Exception as exc:
                reason = scrub_text(f"{type(exc).__name__}: {exc}")
                if (
                    getattr(source, "enforces_model_integrity", False)
                    or self.strict and getattr(source, "rewrites_transport", False)
                ):
                    # fail-closed：改写型 source 起不来，agent 不能带着
                    # 半改写的 env 启动。
                    self._write_capture_event(
                        "error", source_instance=instance, message=reason
                    )
                    await self.abort_before_or_during_run()
                    raise CapturePreparationError(
                        f"source {source.kind} 无法 ready: {reason}"
                    ) from exc
                # fail-open：该 source 降级为不存在，记 gap（含 instance）。
                self.gaps.append({
                    "field": source.kind,
                    "instance": instance,
                    "reason": "source_start_failed",
                })
                self._write_capture_event(
                    "error", source_instance=instance, message=reason
                )

        # 4) 合并 + capability 校验。普通 capture source 的冲突仍 fail-open；
        # model-integrity source 的冲突必须 fail-closed，否则 agent 会直连上游。
        try:
            merged, cap_gaps = merge_injections(
                collected,
                capabilities=self.capabilities,
                protected_env_keys=self.protected_env_keys,
            )
        except InjectionMergeError as exc:
            if any(
                getattr(source, "enforces_model_integrity", False)
                for source in self.sources
            ):
                await self.abort_before_or_during_run()
                raise CapturePreparationError(
                    f"model integrity injection merge failed: {scrub_text(str(exc))}"
                ) from exc
            # fail-open：配置冲突时宁可零注入，也不给 adapter 半合并结果。
            self.gaps.append({"field": "injection", "reason": f"merge_failed: {exc}"})
            self._write_capture_event("error", message=scrub_text(str(exc)))
            merged, cap_gaps = WireInjection(), []
        self.gaps.extend(cap_gaps)
        integrity_required = any(
            getattr(source, "enforces_model_integrity", False)
            for source in self.sources
        )
        integrity_proxy_dropped = (
            integrity_required
            and (
                not merged.enabled
                or not merged.llm_base_url
                or any(gap.get("field") == "llm_base_url" for gap in cap_gaps)
            )
        )
        if integrity_proxy_dropped:
            await self.abort_before_or_during_run()
            raise CapturePreparationError(
                "model integrity proxy injection was dropped by adapter capabilities"
            )
        for gap in cap_gaps:
            self._write_capture_event(
                "drop", reason_code=gap["reason"], message=gap["field"]
            )

        # 5) 初始化 phase-state（attempt/phase/sequence，原子写；
        # 独立进程经 injection.phase_state 只读消费）
        self._phase_state_path = paths.phase_state_file(self.data_path, self.attempt_id)
        self._write_phase_state()
        if merged.enabled and merged.phase_state is None:
            merged = dataclasses.replace(
                merged, phase_state=PhaseStateRef(path=self._phase_state_path)
            )

        # prepare-time 建 env-inbound 空 spool：capture 启用即存在
        # 该文件——「零通信」（关闭后空 .jsonl）与「采集器没工作」（无 spool）
        # 可区分。Env Server 后续 append 到同一文件（begin 复用/续接）。
        with contextlib.suppress(Exception):
            env_spool = paths.source_spool_file(
                self.data_path, self.attempt_id, "env-inbound"
            )
            if not env_spool.exists() and not env_spool.with_name(
                env_spool.name + ".partial"
            ).exists():
                spool.SpoolWriter(
                    env_spool, expected_attempt_id=self.attempt_id
                ).close()

        # 6) in-progress manifest：startup recovery 的扫描锚点
        self._started_at = _now_iso()
        from backend.wire import finalize as _finalize

        with contextlib.suppress(Exception):
            _finalize.write_in_progress_manifest(
                data_path=self.data_path,
                attempt_id=self.attempt_id,
                policy=self.policy,
                strict=self.strict,
                started_at=self._started_at,
                declared_sources=self._declared_sources(),
                gaps=self.gaps,
                phase_attribution=self.phase_attribution,
            )

        # 7) ready event
        self._write_capture_event("ready", counters={"sources": len(collected)})
        self.call_log.append("prepare:ready")
        self.injection = merged
        self._state = "prepared"
        return merged

    def _write_phase_state(self) -> None:
        """atomic rename 更新 phase-state 文件；失败降级 degraded 不中断主流程。"""
        if self._phase_state_path is None:
            return
        self._phase_seq += 1
        try:
            writer.atomic_write_json(
                self._phase_state_path,
                {
                    "attempt_id": self.attempt_id,
                    "phase": self.current_phase or "attempt_setup",
                    "sequence": self._phase_seq,
                    "updated_at": _now_iso(),
                    # 独立进程（Env Server）的 control claim：capture 是否启用 +
                    # effective policy。policy off 时根本不写 phase-state（prepare
                    # noop 分支），所以文件存在即「采集已启用」。
                    "capture_enabled": self.policy.effective != "off",
                    "policy": self.policy.effective,
                },
            )
        except Exception as exc:
            if self.phase_attribution != "degraded":
                self.phase_attribution = "degraded"
                self.gaps.append(
                    {"field": "phase_state", "reason": "phase_state_write_failed"}
                )
            self._write_capture_event(
                "error",
                reason_code="phase_state_write_failed",
                message=scrub_text(f"{type(exc).__name__}: {exc}"),
            )

    def _declared_sources(self) -> list[dict[str, str]]:
        declared = [
            {"kind": s.kind, "instance": getattr(s, "instance", s.kind)}
            for s in self.sources
        ] + [{"kind": "capture-events", "instance": "capture-events"}]
        # native source 一旦在 prepare 声明 expected 就进 declared——即便
        # agent_result 未成功产出，finalize 也会因「declared 无 spool」标 failed，
        # 不会伪装成 complete/not-observed。
        if self._native_expected or self._native_source_active:
            declared.append({"kind": "native-event", "instance": "native-event"})
        # env-inbound 也进 declared：capture 启用即声明它，配合
        # prepare 建的空 spool——「零通信」（空 .jsonl）与「采集器没工作」
        # （无 spool）从而可区分。
        declared.append({"kind": "env-inbound", "instance": "env-inbound"})
        return declared

    def _ctx(self, phase: str) -> CaptureContext:
        return CaptureContext(
            attempt_id=self.attempt_id,
            attempt_dir=paths.attempt_dir(self.data_path, self.attempt_id),
            agent_name=self.agent_name,
            phase=phase,
            policy=self.policy,
        )

    # ---- observer 接口 --------------------------------------------------

    @asynccontextmanager
    async def phase(self, name: str):
        prev = self.current_phase
        self.current_phase = name
        self.call_log.append(f"phase:{name}")
        # 先更新 phase-state（文件/控制通道），再启动该 phase 的
        # 工作；独立进程按到达时快照归属。写失败在 _write_phase_state 内降级。
        self._write_phase_state()
        self._write_capture_event(
            "phase_change", status=name, counters={"sequence": self._phase_seq}
        )
        try:
            yield
        finally:
            # 退出同样传播到 phase-state，跨 phase 的后到流量不误归属。
            self.current_phase = prev
            self._write_phase_state()

    async def agent_result(self, result: Any) -> None:
        # flush source、运行 native normalizer。native source 写
        # wire-sources/native-event.jsonl + trajectory.json，attempt_end 的
        # finalize 会把它归一进 canonical。fail-open：normalize 失败不影响 attempt。
        self.call_log.append("agent_result")
        if self._spool is None:
            return  # capture 未启用（零 source）：不产出 native source
        # adapter 累计 usage（用于 finalize 对账，差异写 manifest conflict）
        adapter_usage = getattr(result, "token_usage", None)
        try:
            from backend.wire.normalizers.runner import run_native_normalizer

            produced = await _to_thread(
                run_native_normalizer,
                agent_name=self.agent_name,
                attempt_id=self.attempt_id,
                data_path=self.data_path,
                adapter_usage=adapter_usage if isinstance(adapter_usage, dict) else None,
            )
            if produced:
                self._native_source_active = True
            elif self._native_expected:
                # 声明了 native 但没产出（raw 缺失/无证据）：登记 gap + error
                # 事件，finalize 会据 declared 无 spool 把 native 标 failed。
                self.gaps.append(
                    {"field": "native-event", "instance": "native-event",
                     "reason": "native_no_output"}
                )
                self._write_capture_event(
                    "error", source_instance="native-event",
                    reason_code="native_no_output",
                )
        except Exception as exc:
            logger.exception("wire native normalize fail-open attempt=%s", self.attempt_id)
            if self._native_expected:
                self.gaps.append(
                    {"field": "native-event", "instance": "native-event",
                     "reason": "native_normalize_failed"}
                )
                self._write_capture_event(
                    "error", source_instance="native-event",
                    reason_code="native_normalize_failed",
                    message=scrub_text(f"{type(exc).__name__}: {exc}"),
                )

    async def attempt_end(self) -> None:
        """接受 PREPARED/RUNNING/FINALIZING 任一状态，幂等，吞掉 fail-open 错误。

        停止 source → flush/close spool → 最终 normalize/correlate
        → 原子写 wire.jsonl → finalize manifest。
        """
        if self._state in ("finalized", "aborted"):
            return
        self.call_log.append("attempt_end")
        for source in self.sources:
            with contextlib.suppress(Exception):
                await source.stop(self._ctx(self.current_phase or "attempt_cleanup"))
        try:
            self._write_capture_event("stop")
            if self._spool is not None:
                self._spool.close()
        except Exception:
            logger.exception("wire attempt_end fail-open: spool close 失败")
        # Env inbound writer 必须在 finalize 前正常关闭：否则
        # 停留在 .partial 让 source 被标 partial，且 finalize 后到的 evidence
        # 会写进已生成的 wire.jsonl 之外造成竞态。close 内部 drain 会同步阻塞
        # 等 in-flight 请求 end——放线程池 await，否则请求协程无法在事件循环上
        # 运行到 end_request。
        with contextlib.suppress(Exception):
            from backend.wire.env_capture import close_attempt_spool

            await _to_thread(close_attempt_spool, self.data_path, self.attempt_id)
        # reverse HTTP proxy spool 也在 finalize 前 seal→drain→close；
        # 已 native async（跑在事件循环上），不放线程池。token 随后失效。
        with contextlib.suppress(Exception):
            from backend.wire import capture_token
            from backend.wire.sources.http_proxy import close_attempt as _proxy_close

            await _proxy_close(self.data_path, self.attempt_id)
            capture_token.revoke(self.attempt_id)
        if self._spool is not None:
            # 只有真实启用过 capture（有 spool）才 finalize；
            # 零 source 的 noop session 不落任何 wire 文件（零变化基线）。
            try:
                from backend.wire import finalize as _finalize

                manifest = _finalize.finalize_attempt(
                    data_path=self.data_path,
                    attempt_id=self.attempt_id,
                    policy=self.policy,
                    strict=self.strict,
                    declared_sources=self._declared_sources(),
                    gaps=self.gaps,
                    phase_attribution=self.phase_attribution,
                    started_at=self._started_at,
                    finished_at=_now_iso(),
                )
                # DB 摘要列 + token 聚合回填（第 5 步）；
                # runtime_state 不可用时（单测）跳过
                with contextlib.suppress(Exception):
                    from backend import runtime_state
                    from backend.wire.aggregate import backfill_token_usage

                    state = runtime_state.get()
                    _finalize.update_db_summary(
                        state.db_path, self.attempt_id, manifest
                    )
                    backfill_token_usage(
                        state.db_path, self.data_path, self.attempt_id
                    )
            except Exception:
                logger.exception("wire finalize fail-open: manifest/canonical 生成失败")
        self._state = "finalized"

    async def abort_before_or_during_run(self) -> None:
        """prepare 后、run 前/中的异常路径：stop source + flush spool，保留 .partial。

        幂等，且对已正常 finalize 的 session 是 no-op（dispatch 的统一异常边界
        可能在 run_attempt 自身 attempt_end 之后再次调用）。"""
        if self._state in ("aborted", "finalized"):
            return
        self.call_log.append("abort")
        for source in self.sources:
            with contextlib.suppress(Exception):
                await source.stop(self._ctx(self.current_phase or "attempt_cleanup"))
        if self._spool is not None:
            with contextlib.suppress(Exception):
                self._write_capture_event("error", message="aborted")
                # 保留 .partial：finalizer/recovery 据此区分异常结束。
                self._spool.abandon()
        # env inbound writer：abort 路径也关闭（正常 rename），后续 recovery
        # finalize 能读到完整行；不留悬挂句柄。同 attempt_end——放线程池 await，
        # drain 不阻塞事件循环（本轮）。
        with contextlib.suppress(Exception):
            from backend.wire.env_capture import close_attempt_spool

            await _to_thread(close_attempt_spool, self.data_path, self.attempt_id)
        with contextlib.suppress(Exception):
            from backend.wire import capture_token
            from backend.wire.sources.http_proxy import close_attempt as _proxy_close

            await _proxy_close(self.data_path, self.attempt_id)
            capture_token.revoke(self.attempt_id)
        self._state = "aborted"
