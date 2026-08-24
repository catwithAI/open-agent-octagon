"""Run 专属上游凭据的内存上下文。

**明文 key 的唯一合法去处是两个**：本模块持有的 `RunCredential`，以及 agent /
judge 子进程的环境变量。除此之外一律禁止——不进 DB、不进日志、不进
`external_refs_json`、不进 wire 记录、不进 API 响应。

`__repr__` / `__str__` 已抑制：Python 里最容易的泄漏路径是有人写
`logger.info("cred=%s", cred)`，抑制后即使写了也只出 hash。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Literal

from .. import runtime_state

logger = logging.getLogger(__name__)

#: 注入给 judge 子进程的环境变量名。judge 由 env 脚本拉起，
#: 与 agent adapter 不是同一条路径，用专门的变量名收口。
API_KEY_ENV = "OCTAGON_RUN_LLM_API_KEY"
BASE_URL_ENV = "OCTAGON_RUN_LLM_BASE_URL"
#: 向后兼容别名。
JUDGE_API_KEY_ENV = API_KEY_ENV
JUDGE_BASE_URL_ENV = BASE_URL_ENV


def is_openrouter(base_url: str | None) -> bool:
    """该 endpoint 是否是 OpenRouter。

    run key 由 OpenRouter Management API 签发，**只对 OpenRouter endpoint
    有效**。注给 Kimi / LiteLLM / Blade 等其它 provider 会直接认证失败——
    成本核算不能把正常跑着的实验打挂。
    """
    if not base_url:
        return False
    return "openrouter.ai" in base_url.lower()


@dataclass(frozen=True)
class RunCredential:
    """一次 run 的上游凭据。

    `attribution_mode` 跟着凭据走而不是单独判断：拿到 run 专属 key 才可能
    `ephemeral_run_key`，回落到共享 key 就只能是 `shared_key_upper_bound`。
    把两者绑在一起，调用方无法"用着共享 key 却标成可审计"。
    """

    run_id: str
    api_key: str
    api_key_hash: str
    base_url: str | None = None
    attribution_mode: Literal[
        "ephemeral_run_key", "shared_key_upper_bound"
    ] = "ephemeral_run_key"

    def __repr__(self) -> str:
        return (
            f"RunCredential(run_id={self.run_id!r}, hash={self.api_key_hash!r}, "
            f"mode={self.attribution_mode!r})"
        )

    __str__ = __repr__


def set_credential(credential: RunCredential) -> None:
    """登记 run 凭据。必须在任何 agent 进程启动前调用。"""
    runtime_state.get().run_credentials[credential.run_id] = credential
    logger.info(
        "run credential registered run=%s hash=%s mode=%s",
        credential.run_id,
        credential.api_key_hash,
        credential.attribution_mode,
    )


def get_credential(run_id: str | None) -> RunCredential | None:
    """取 run 凭据。未登记（成本核算关闭、创建失败）时返回 None。

    `runtime_state` 未 bind 时返回 None 而非抛错：adapter 在测试里可能
    脱离完整 app 上下文运行，凭据缺失应当安静回落到既有 provider 配置。
    """
    if not run_id:
        return None
    try:
        state = runtime_state.get()
    except RuntimeError:
        return None
    credential = state.run_credentials.get(run_id)
    return credential if isinstance(credential, RunCredential) else None


def clear_credential(run_id: str) -> None:
    """结算完成后移除内存中的明文。

    幂等：重复调用不报错（重启恢复路径可能重复走到）。
    """
    try:
        state = runtime_state.get()
    except RuntimeError:
        return
    if state.run_credentials.pop(run_id, None) is not None:
        logger.info("run credential cleared run=%s", run_id)


def judge_credential_env(run_id: str | None) -> dict[str, str]:
    """judge 子进程要注入的环境变量。

    没有 run 凭据时返回空 dict——调用方据此知道 judge 会走既有配置，
    此时 scoring 阶段的费用不落在 run key 上，审计必须降级。
    """
    credential = get_credential(run_id)
    if credential is None:
        return {}
    env = {JUDGE_API_KEY_ENV: credential.api_key}
    if credential.base_url:
        env[JUDGE_BASE_URL_ENV] = credential.base_url
    return env


#: 当前 scoring job 的 judge 凭据。**用 contextvar 而非 os.environ**——
#: 后者是进程全局，两个 run 同时评分会互相覆盖（默认
#: `max_active_scoring_jobs=2`，这个窗口真实存在）。contextvar 随
#: `asyncio.to_thread` 传播，每个 job 各自一份，天然隔离。
_judge_credential: ContextVar[RunCredential | None] = ContextVar(
    "octagon_judge_credential", default=None
)

def current_judge_credential() -> "RunCredential | None":
    """当前 scoring job 的 judge 凭据。judge 子进程的 env 由此派生。"""
    return _judge_credential.get()


def judge_env_overlay() -> dict[str, str]:
    """要叠加到 judge 子进程的环境变量。

    **judge 拉子进程时必须显式合并这份 overlay**（而不是让子进程继承
    进程全局 env），否则并发评分会互相串 key。
    """
    credential = _judge_credential.get()
    if credential is None:
        return {}
    env = {API_KEY_ENV: credential.api_key}
    if credential.base_url:
        env[BASE_URL_ENV] = credential.base_url
    return env


@contextmanager
def judge_credentials_active(run_id: str | None) -> Iterator[bool]:
    """在 with 块内绑定本 job 的 judge 凭据。

    绑定到 **contextvar**，不再写 `os.environ`——后者是进程全局，
    两个 run 并发评分时后进的会覆盖先进的，导致成本记到别的 run 头上。

    judge 有两种消费形态，都经 `envs/_judge_credentials.py` 收口：

    - **同进程 HTTP**（`ppt-visual-repair` 等）：调 `run_api_key()` 读 contextvar；
    - **子进程 CLI**（`talent` 走 claude CLI）：`subprocess_env()` 显式传 `env=`。

    **不再写 `os.environ`**——那是此前的泄漏来源。

    yield 出 `used_run_key`：False 表示 judge 会走既有配置，
    此时 scoring 段的费用不在 run key 上，审计必须降级。
    """
    credential = get_credential(run_id)
    if credential is None:
        yield False
        return

    # **只绑 contextvar，绝不写 os.environ**：进程级环境变量
    # 无法区分并发的 scoring job——实测两个 run 同时评分时后进的会覆盖
    # 先进的，两边都读到同一把 key，成本记到别人头上。judge 一律经
    # `envs/_judge_credentials.py` 读 contextvar。
    token = _judge_credential.set(credential)
    try:
        yield True
    finally:
        _judge_credential.reset(token)


def resolve_agent_key(
    run_id: str | None,
    fallback: str | None,
    attempt_id: str | None = None,
    provider_base_url: str | None = None,
) -> tuple[str | None, bool]:
    """agent adapter 的 key 解析。

    **优先 attempt 凭据**（per-attempt 生命周期），回落 run 凭据（旧 run 级
    一把 key），再回落 provider 配置。两条生命周期并存期间必须都认——
    只认其一会让另一条路径静默用错 key。

    **provider 同源校验**：run key 由 OpenRouter 签发，
    只对 OpenRouter endpoint 有效。`provider_base_url` 指向别处时一律
    回落 provider 自己的 key——否则 kimi-cc / kimi-codex 这类 provider
    会拿着 OpenRouter key 打 Kimi endpoint，直接认证失败。

    返回 `(key, used_run_key)`。第二个值让调用方知道这次注入是否可审计——
    **不能只返回 key**，否则回落时审计会静默标成可审计，
    把共享 key 的上界当成实扣（本 spec 最核心的错误模式）。
    """
    if provider_base_url is not None and not is_openrouter(provider_base_url):
        return fallback, False
    for key_id in (attempt_id, run_id):
        credential = get_credential(key_id)
        if credential is not None:
            return credential.api_key, True
    return fallback, False


def resolve_agent_base_url(
    run_id: str | None, fallback: str | None, attempt_id: str | None = None
) -> str | None:
    """base_url 必须与 key 同源，否则会拿 run key 打旧 base_url。

    非 OpenRouter 的 provider 不注入 run key（见 `resolve_agent_key`），
    因此也不能改写它的 endpoint。
    """
    if fallback is not None and not is_openrouter(fallback):
        return fallback
    for key_id in (attempt_id, run_id):
        credential = get_credential(key_id)
        if credential is not None and credential.base_url:
            return credential.base_url
    return fallback
