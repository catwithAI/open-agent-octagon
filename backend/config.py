"""Octagon backend 配置。

测试契约(`tests/test_t01_project_skeleton.py`):
- `load_settings()` 返回 `Settings`
- `settings.octagon.data_path` 是 `pathlib.Path`,可被 `OCTAGON_DATA_PATH` 覆盖
- `settings.blade.base_url` 可被 `BLADE_BASE_URL` 覆盖
- `repr(settings)` 不暴露 `BLADE_API_KEY` 明文
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .model_providers import ModelProviderSection

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("octagon.yaml")


class OctagonSection(BaseModel):
    data_path: Path = Path("./data")
    envs_path: Path = Path("./envs")
    profiles_path: Path = Path("./profiles")
    # blade skill `tools.py` 通过 HTTP 回调 Env Attempt Server 的地址。
    # blade-agent 进程访问此地址,所以必须是 blade 容器/进程能解析到的对外
    # URL,不能写 0.0.0.0。开发期同机部署 127.0.0.1 即可。
    public_base_url: str = "http://127.0.0.1:8100"
    # 外部专有 skill 源目录（部分 env 的 core.py import 它）。仓库外资产，换机器
    # 必须改此项——不填则 env 加载时报明确错误，而不是撞上某人的绝对路径。
    # load_settings 会把它桥接到 SELECTED_SKILLS_DIR 环境变量供 env core.py 读取。
    selected_skills_path: Path | None = None
    # wire blob API 开关：Octagon 当前没有用户级 auth，知道
    # run/attempt ID 即可调 API，因此 parsed/full blob 下载默认禁用，直到权限
    # 模型明确；专用 benchmark 部署可显式打开。
    wire_blob_api_enabled: bool = False
    # wire capture policy 上限：run/task 请求的 capture_policy 与此求最严格
    # 交集。默认 metadata（只记 size/timing，不落 body）；专用 benchmark 部署可提到
    # full 才允许落脱敏后的协议原文。None = 不额外收紧（由请求侧决定，仍受 off 默认）。
    wire_capture_max_policy: Literal["off", "metadata", "parsed", "full"] | None = None
    # 单个 attempt 的评分硬上限。checklist judge 数量多时，整体评分可能长时间
    # 挂起，最终以 scorer_unavailable 结束且没有得分。
    # 到点即终止评分并记 timed_out（与设施缺失的 scorer_unavailable 区分），
    # score_total 保持 NULL，执行产物不受影响。0 = 不限制。
    scoring_deadline_seconds: int = Field(default=300, ge=0)
    # 纯展示部署：把「不安装执行依赖、不配置密钥」这条运维约定升级成
    # 产品级只读边界。启用后后端拒绝一切执行型写请求（创建/调度/停止/重跑），
    # 不要求任何模型或成本凭据，历史 Run/Attempt/wire/artifact/score/cost
    # 仍可完整读取。前端据 /api/capabilities 隐藏执行入口——但拒绝在后端，
    # 不依赖前端隐藏。
    display_only: bool = False
    # CLI 启动期瞬时故障的有界重试。错误分类见
    # backend/adapters/error_taxonomy：只有被判定为 retryable 的错误
    # （限流、上游 5xx、连接中断等）才会重试，且必须在 execution deadline
    # 内还有余量。1 = 不重试（只跑一次，即接线前的行为）。
    #
    # 上限刻意小：重试的价值在于跨过瞬时抖动，不是把一次注定失败的执行
    # 反复跑到超时——后者只是把同一笔钱烧三遍。
    cli_max_attempts: int = Field(default=3, ge=1, le=5)
    # 常驻 deadline sweeper：周期扫描超过持久化 deadline 的执行/评分任务
    # 并收敛。0 = 关闭（只保留启动恢复，即修复前的行为）。
    deadline_sweep_interval_seconds: int = Field(default=60, ge=0)
    # 宽限期：deadline 到点后再等这么久才由 sweeper 接手，避免抢正常收尾路径。
    deadline_sweep_grace_seconds: int = Field(default=60, ge=0)
    # 启动恢复单个 wire spool 的体积上限。超过即隔离该 attempt（manifest 落
    # failed + 明确原因）而不是尝试 finalize——未设上限时，大 spool 会把恢复
    # 路径的 RSS 推到危险水平、拖垮宿主机，而 systemd 自动重启又会反复进入
    # 同一路径。0 = 不限制。
    wire_recovery_max_spool_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    # Process-wide attempt lease shared by legacy Runs and experiment cells.
    # Attempt (not Run) is the scarce unit: each one owns an agent process or
    # blade session and provider quota while active.
    max_active_attempts: int = Field(default=16, ge=1, le=32)
    # Scoring is deliberately independent from the Agent execution lease.
    max_active_scoring_jobs: int = Field(default=2, ge=1, le=32)
    # Experiment protocol hard ceilings. Client/profile limits may only tighten
    # these values; they can never widen the deployment policy.
    research_max_cells: int = Field(default=1000, ge=1, le=1000)
    research_max_attempts: int = Field(default=32000, ge=1, le=32000)
    research_capture_max_policy: Literal["off", "metadata", "parsed", "full"] = (
        "metadata"
    )
    research_launch_stagger_ms: int = Field(default=300, ge=0, le=5000)
    # Rubric Evolution is a production-derived loop but remains opt-in until an
    # environment has a human-registered active Rubric and provider credentials.
    rubric_evolution_enabled: bool = False
    rubric_evolution_scan_interval_seconds: int = Field(default=300, ge=10, le=86400)
    rubric_evolution_environment_threshold: int = Field(default=10, ge=1, le=10000)
    rubric_evolution_cross_environment_threshold: int = Field(default=100, ge=1, le=100000)
    rubric_evolution_process_threshold: int = Field(default=10, ge=1, le=10000)
    rubric_evolution_association_threshold: int = Field(default=100, ge=1, le=100000)
    rubric_evolution_overlap_ratio: float = Field(default=0.25, ge=0, le=0.5)
    # 磁盘护栏：可用空间低于该值就不再启动新 attempt。0 = 关闭。
    # 2026-09-18 的横评在第 336 个 attempt 处把盘写到 0 字节，之后连 stop 都
    # 失败（写状态也要落盘）；护栏的意义是永远不走到那一步。
    min_free_disk_gb: float = Field(default=15.0, ge=0)


class BladeSection(BaseModel):
    base_url: str = "http://localhost:8020"
    api_key: SecretStr | None = None
    skills_path: Path = Path("/tmp/octagon/blade-skills")
    keep_blade_session: bool = False
    # blade skill 工具在 docker 沙盒内回调 Env Attempt Server 时用的地址。
    # 沙盒视角与本机进程（CC/codex 的 mcp_server）视角不同：本机用
    # public_base_url（127.0.0.1 即可），沙盒要 host.docker.internal 或宿主机
    # 局域网 IP。不填则回落 public_base_url。
    sandbox_env_base_url: str | None = None
    # 普通 REST 请求（建 session、history、文件等）的单请求时限。
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    # 运行仍未超过 task 总时限，但 Socket/history/messages 均无进展时才判卡死。
    inactivity_timeout_seconds: float = Field(default=300.0, gt=0)
    # Socket 断开后 SDK 自动重连、续订和补放事件的宽限。
    reconnect_timeout_seconds: float = Field(default=30.0, gt=0)
    progress_poll_interval_seconds: float = Field(default=4.0, gt=0)


class SandboxLimits(BaseModel):
    """单个 agent 容器的资源限额（docker run --memory/--cpus/--pids-limit）。"""

    memory: str = "4g"
    cpus: float = Field(default=2.0, gt=0)
    pids: int = Field(default=1024, gt=0)


class SandboxAgentOverride(BaseModel):
    limits: SandboxLimits | None = None


class SandboxSection(BaseModel):
    """本机 CLI agent 的 docker 沙盒（spec: docs/specs/260909-agent-sandbox）。

    `enabled: true` 是全局强制：六个本机 agent（claude-code / codex / kimi-code /
    opencode / mimo-code / dsh）全部进容器，不按 agent 可选、不提供 run 级开关。
    沙盒不可用（docker 不可达、镜像缺失、agent 不在镜像里）时 attempt 明确失败，
    不回落宿主机执行。blade-agent / ssh-claude-code 不在此围栏内，只记录。
    """

    enabled: bool = False
    # 完整 repo:tag。启动时 `docker image inspect` 取 digest 与 agent 版本标签。
    image: str | None = None
    # 容器内访问 Env Attempt Server 的地址。不填则按 public_base_url 的端口推导
    # http://host.docker.internal:<port>。
    env_base_url: str | None = None
    limits: SandboxLimits = Field(default_factory=SandboxLimits)
    # LLM 服务端执行的联网工具（claude-code WebSearch/WebFetch、codex web search）
    # 不经容器，沙盒管不到。allow = 不裁剪只记录；deny = 经 CLI settings 禁用。
    server_side_tools: Literal["allow", "deny"] = "allow"
    # 按 agent 覆盖限额。不允许按 agent 换镜像。
    agents: dict[str, SandboxAgentOverride] = Field(default_factory=dict)
    # 家目录里「可重建」的子目录：LibreOffice 运行时、apt 缓存、字体等。
    # 它们由 agent 在运行时写入，每个 attempt 一份、内容几乎相同——330 个
    # attempt 曾因此攒下 33G。挂成容器可写层（匿名卷）或 tmpfs，容器销毁即
    # 释放，不再穿透 bind mount 落到 attempt 目录。
    ephemeral_home_dirs: list[str] = Field(
        default_factory=lambda: ["lo", "loroot", "sysroot", "apt", "fonts", ".cache"]
    )
    # 其中用 tmpfs（走内存）的子集。评测机内存有限（14G / 并发 6），
    # 只有小而热的缓存值得放内存，其余走匿名卷落 docker 存储层。
    tmpfs_home_dirs: list[str] = Field(default_factory=lambda: ["apt", ".cache"])
    # 单个 tmpfs 的上限。并发 6 时最坏占用 = 该值 × tmpfs 目录数 × 并发数，
    # 必须留足余量，别把评测机的内存打爆。
    tmpfs_size: str = "512m"

    def ephemeral_dirs_for(self, keep: list[str] | None = None) -> list[str]:
        """场景级放开：从默认列表里减项（需求 1.4）。

        某些场景确实要把运行时产物留作证据，此时 env 用 `keep_home_dirs`
        指名保留——而不是让所有场景都付这份成本。
        """
        kept = {item.strip() for item in (keep or []) if item.strip()}
        return [name for name in self.ephemeral_home_dirs if name not in kept]

    def limits_for(self, agent_name: str) -> SandboxLimits:
        override = self.agents.get(agent_name)
        if override is not None and override.limits is not None:
            return override.limits
        return self.limits

    def resolve_env_base_url(self, public_base_url: str) -> str:
        if self.env_base_url:
            return self.env_base_url
        from urllib.parse import urlsplit

        parts = urlsplit(public_base_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return f"{parts.scheme or 'http'}://host.docker.internal:{port}"


class SameModelSection(BaseModel):
    """历史遗留：same-model 模式曾经把 blade-agent/claude-code 打到独立的
    远端机器（48），有自己的 API key / vLLM 部署。48 不再使用后，
    run_dispatch.build_adapter 已改为所有 compare_mode 一律走本机
    settings.blade / 本机 ClaudeCodeAdapter，这个 section 不再影响调度。
    字段保留只是为了不破坏 octagon.yaml 里可能还有的旧配置解析，不填任何值
    也完全不影响功能——不要再往这些字段填新的远端地址。
    """

    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_password: SecretStr | None = None
    blade_base_url: str | None = None
    blade_api_key: SecretStr | None = None


class InsightsSection(BaseModel):
    """Independent derived-analysis channel; never inherits judge credentials."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai_compatible", "blade"] = "openai_compatible"
    base_url: str | None = None
    model: str | None = None
    api_key_env: str = Field(default="INSIGHTS_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: float = Field(default=120.0, gt=0, le=1800)
    max_tokens: int = Field(default=4000, ge=256, le=32000)
    temperature: float = Field(default=0.0, ge=0, le=2)
    blade_primary_skill_id: str | None = None
    keep_blade_session: bool = False


class CostSection(BaseModel):
    """Run 级成本核算。

    **Management Key 与 provider 的 api_key 是两个量级的东西**：后者只能花钱，
    前者能创建、查询、禁用任意 key。因此它**特意不参与** `load_settings` 里
    provider key → 环境变量的桥接——那条路径会把值送进每一个 agent 子进程，
    对 Management Key 而言等同于把管理员凭据交给被测对象。
    """

    model_config = ConfigDict(extra="forbid")

    #: 关掉后完全不碰 Management API，run 照常跑，只是没有资金口径。
    enabled: bool = False
    management_key_env: str = Field(
        default="OPENROUTER_MANAGEMENT_KEY", pattern=r"^[A-Z][A-Z0-9_]*$"
    )
    base_url: str = "https://openrouter.ai/api/v1"
    request_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    max_retries: int = Field(default=3, ge=1, le=10)

    #: **旧 run 级 key** 的上限（USD）。当前生产路径仍在用它——
    #: `cost/audit.py` 每个 run 建一把，6 个 agent 共用。
    #:
    #: **不要把它和 attempt/judge 的上限混用**：一 run 一把时 $5 是整次
    #: 实验的闸门；per-attempt 时同样的数值会变成 N 倍总额。两条生命周期
    #: 并存期间必须各自有默认值，否则改一个会悄悄腰斩或放大另一个。
    run_key_limit_usd: float | None = Field(default=5.0, gt=0)

    #: **每个 attempt key** 的上限（USD）。per-attempt 生命周期用。
    #: 6 attempt + judge ≈ 上游总闸门 $7，与旧的 $5/run 同量级。
    attempt_key_limit_usd: float | None = Field(default=1.0, gt=0)

    #: **judge key** 的上限（USD）。评分可能比单个 attempt 贵
    #: （ppt-visual-repair 的多模态 judge），单独给一档。
    judge_key_limit_usd: float | None = Field(default=1.0, gt=0)

    #: run key 的兜底过期（小时）。清理失败的孤儿 key 靠它自动失效，
    #: 必须显著大于最长 run 时长，否则会在 run 中途掐断。
    run_key_expiry_hours: float = Field(default=48.0, gt=0, le=720)

    #: 判稳参数。连续 stable_threshold 次累计 usage 不变才算稳定。
    poll_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    stable_threshold: int = Field(default=3, ge=2, le=20)
    settle_timeout_seconds: float = Field(default=300.0, gt=0, le=7200)
    #: 后台 settler 扫描 pending/settling 的间隔。
    background_scan_interval_seconds: float = Field(default=60.0, gt=0, le=3600)
    max_settle_attempts: int = Field(default=20, ge=1, le=1000)

    #: 创建 run key 失败时：True = 降级为共享 key 上界继续跑（默认），
    #: False = 让 run 失败。默认降级——成本观测不该阻断实验。
    downgrade_on_key_failure: bool = True

    #: 旧的 run 级一把 key 生命周期。默认**关闭**。
    #:
    #: **与 per-attempt 互斥**：打开它会同时关掉
    #: attempt/judge 生命周期，否则一次 run 会开「旧 run key + N 个 attempt
    #: key + judge key」，多花钱且旧审计的 clear_credential(run_id) 会清掉
    #: 正在用的 judge 凭据（两者同以 run_id 为索引）。这是回退开关，不是叠加。
    legacy_run_key_enabled: bool = False

    @property
    def per_attempt_enabled(self) -> bool:
        """per-attempt 生命周期是否生效。与 legacy 互斥。"""
        return self.enabled and not self.legacy_run_key_enabled

    #: 降级路径要观测的共享 key 的 **hash**（不是明文）。配了才能记录
    #: run 前后的 usage 差作为上界；不配则降级后只留一条说明性记录，没有数字。
    #: 该差值含员工个人流量与其他机器流量，**只能**标 upper_bound。
    shared_key_hash: str | None = None

    def key_limit_for(self, scope: str) -> float | None:
        """按 scope 取上限。scope 是 `attempt` / `judge` / `run`（旧路径）。"""
        return {
            "attempt": self.attempt_key_limit_usd,
            "judge": self.judge_key_limit_usd,
        }.get(scope, self.run_key_limit_usd)


class Settings(BaseModel):
    octagon: OctagonSection = Field(default_factory=OctagonSection)
    blade: BladeSection = Field(default_factory=BladeSection)
    same_model: SameModelSection = Field(default_factory=SameModelSection)
    insights: InsightsSection = Field(default_factory=InsightsSection)
    cost: CostSection = Field(default_factory=CostSection)
    sandbox: SandboxSection = Field(default_factory=SandboxSection)
    # CC/Codex 的第三方模型 provider（blade 的模型列表走 /api/blade/models 实时查，
    # 不在这里配）。api key 解析见 resolve_api_key：api_key_env 指向的环境变量
    # 优先，回落到 api_key 直填（octagon.yaml 已 gitignore）；load_settings 会把
    # 直填的 key 桥接进对应环境变量，供只读 env 的消费方使用。
    model_providers: dict[str, ModelProviderSection] = Field(default_factory=dict)
    model_suggestions: list[str] = Field(default_factory=list)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        loaded = yaml.safe_load(fp) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} 顶层必须是 mapping,实际是 {type(loaded).__name__}")
    return loaded


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    octagon = dict(data.get("octagon") or {})
    if v := os.environ.get("OCTAGON_DATA_PATH"):
        octagon["data_path"] = v
    if v := os.environ.get("OCTAGON_ENVS_PATH"):
        octagon["envs_path"] = v
    if v := os.environ.get("OCTAGON_PROFILES_PATH"):
        octagon["profiles_path"] = v
    if v := os.environ.get("OCTAGON_PUBLIC_BASE_URL"):
        octagon["public_base_url"] = v
    if v := os.environ.get("OCTAGON_MAX_ACTIVE_ATTEMPTS"):
        octagon["max_active_attempts"] = v
    if v := os.environ.get("OCTAGON_MAX_ACTIVE_SCORING_JOBS"):
        octagon["max_active_scoring_jobs"] = v
    if v := os.environ.get("SELECTED_SKILLS_DIR"):
        octagon["selected_skills_path"] = v
    data["octagon"] = octagon

    blade = dict(data.get("blade") or {})
    if v := os.environ.get("BLADE_BASE_URL"):
        blade["base_url"] = v
    if v := os.environ.get("BLADE_API_KEY"):
        blade["api_key"] = v
    if v := os.environ.get("BLADE_SKILLS_PATH"):
        blade["skills_path"] = v
    if v := os.environ.get("BLADE_SANDBOX_ENV_BASE_URL"):
        blade["sandbox_env_base_url"] = v
    if v := os.environ.get("BLADE_REQUEST_TIMEOUT_SECONDS"):
        blade["request_timeout_seconds"] = v
    if v := os.environ.get("BLADE_INACTIVITY_TIMEOUT_SECONDS"):
        blade["inactivity_timeout_seconds"] = v
    if v := os.environ.get("BLADE_RECONNECT_TIMEOUT_SECONDS"):
        blade["reconnect_timeout_seconds"] = v
    if v := os.environ.get("BLADE_PROGRESS_POLL_INTERVAL_SECONDS"):
        blade["progress_poll_interval_seconds"] = v
    data["blade"] = blade

    same_model = dict(data.get("same_model") or {})
    if v := os.environ.get("SAME_MODEL_SSH_PASSWORD"):
        same_model["ssh_password"] = v
    if v := os.environ.get("SAME_MODEL_SSH_HOST"):
        same_model["ssh_host"] = v
    if v := os.environ.get("SAME_MODEL_BLADE_BASE_URL"):
        same_model["blade_base_url"] = v
    if v := os.environ.get("SAME_MODEL_BLADE_API_KEY"):
        same_model["blade_api_key"] = v
    data["same_model"] = same_model

    sandbox = dict(data.get("sandbox") or {})
    if v := os.environ.get("OCTAGON_SANDBOX_ENABLED"):
        sandbox["enabled"] = v.strip().lower() in ("1", "true", "yes", "on")
    if v := os.environ.get("OCTAGON_SANDBOX_IMAGE"):
        sandbox["image"] = v
    if v := os.environ.get("OCTAGON_SANDBOX_ENV_BASE_URL"):
        sandbox["env_base_url"] = v
    data["sandbox"] = sandbox

    insights = dict(data.get("insights") or {})
    if v := os.environ.get("INSIGHTS_PROVIDER"):
        insights["provider"] = v
    if v := os.environ.get("INSIGHTS_BASE_URL"):
        insights["base_url"] = v
    if v := os.environ.get("INSIGHTS_MODEL"):
        insights["model"] = v
    if v := os.environ.get("INSIGHTS_API_KEY_ENV"):
        insights["api_key_env"] = v
    data["insights"] = insights
    return data


def load_settings(config_path: Path | None = None) -> Settings:
    raw = _load_yaml(config_path or DEFAULT_CONFIG_PATH)
    raw = _apply_env_overrides(raw)
    settings = Settings(**raw)
    # 桥接：yaml 的 selected_skills_path → SELECTED_SKILLS_DIR 环境变量，供相关 env
    # 的 core.py 读取。setdefault 不覆盖已显式导出的环境变量（显式 env 优先）。
    if settings.octagon.selected_skills_path is not None:
        os.environ.setdefault(
            "SELECTED_SKILLS_DIR", str(settings.octagon.selected_skills_path)
        )
    # 桥接：provider 的 api_key → api_key_env 指向的环境变量。让只读环境变量的
    # 消费方（api.py 的 OpenRouter 模型列表、各 env 的 judge 子进程）也能吃到
    # octagon.yaml 里的 key，不再依赖 .env / shell export。setdefault 同上：
    # 显式导出的环境变量优先。
    for provider in settings.model_providers.values():
        if provider.api_key_env and provider.api_key:
            os.environ.setdefault(provider.api_key_env, provider.api_key)
    if settings.blade.api_key is not None:
        os.environ.setdefault("BLADE_API_KEY", settings.blade.api_key.get_secret_value())
    _log_settings(settings)
    return settings


def _log_settings(settings: Settings) -> None:
    safe = {
        "octagon": {
            "data_path": str(settings.octagon.data_path),
            "envs_path": str(settings.octagon.envs_path),
            "profiles_path": str(settings.octagon.profiles_path),
            "public_base_url": settings.octagon.public_base_url,
            "max_active_attempts": settings.octagon.max_active_attempts,
            "max_active_scoring_jobs": settings.octagon.max_active_scoring_jobs,
            "research_max_cells": settings.octagon.research_max_cells,
            "research_max_attempts": settings.octagon.research_max_attempts,
            "research_capture_max_policy": settings.octagon.research_capture_max_policy,
            "research_launch_stagger_ms": settings.octagon.research_launch_stagger_ms,
        },
        "blade": {
            "base_url": settings.blade.base_url,
            "skills_path": str(settings.blade.skills_path),
            "keep_blade_session": settings.blade.keep_blade_session,
            "api_key_set": settings.blade.api_key is not None,
        },
        "same_model": {
            "ssh_host": settings.same_model.ssh_host,
            "ssh_user": settings.same_model.ssh_user,
            "ssh_password_set": settings.same_model.ssh_password is not None,
            "blade_base_url": settings.same_model.blade_base_url,
        },
        # 只打印 provider 名和 kind，不打 base_url / api_key_env 指向的值
        "model_providers": {
            name: p.kind for name, p in settings.model_providers.items()
        },
        "insights": {
            "provider": settings.insights.provider,
            "model": settings.insights.model,
            "configured": bool(settings.insights.base_url and settings.insights.model),
            "api_key_set": bool(os.environ.get(settings.insights.api_key_env)),
        },
        # 只打印是否配置，不打印 Management Key 本身或它的任何前缀。
        "cost": {
            "enabled": settings.cost.enabled,
            "management_key_set": bool(
                os.environ.get(settings.cost.management_key_env)
            ),
            "run_key_limit_usd": settings.cost.run_key_limit_usd,
            "attempt_key_limit_usd": settings.cost.attempt_key_limit_usd,
            "stable_threshold": settings.cost.stable_threshold,
        },
    }
    logger.info("octagon settings: %s", safe)


def resolve_management_key(settings: Settings) -> str | None:
    """读 Management Key。**只从环境变量读，不接受 yaml 直填。**

    与 provider 的 `api_key` 不同（那个允许直填，因为 octagon.yaml 已 gitignore），
    Management Key 不给直填入口：它能创建任意额度的 key，一旦随手 commit 或
    贴进聊天，损失面远大于一把普通 key。强制走 secret 通道，让泄漏路径更少。
    """
    value = os.environ.get(settings.cost.management_key_env)
    return value or None
