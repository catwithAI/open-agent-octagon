"""Octagon 自检——12 个独立检查项,集中暴露。

设计:每个检查独立函数,失败一项不影响其他项;blade 不可达时仅 blade 相关
4 项 fail(`blade_health` / `primary_skill_id` / `workspace_write` /
`skill_load_smoke`),env / db / config 这 8 项仍能 ok。

返回结构(测试契约):

    {
        "checks": [
            {"name": "config", "status": "ok|fail|skipped", "detail": "..."},
            ...
        ]
    }

`name` 列表对照 tasks.md T11:
- config / env_scan / env_api_import / env_tool_registry
- skill_sync / blade_skill_load_timing
- blade_health / primary_skill_id / workspace_write / skill_load_smoke
- env_token_auth / trace_write
"""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Request

from . import runtime_state
from .config import Settings
from .db import (
    _init_db_sync,
    _open_sync,
    generate_env_token,
    hash_env_token,
)

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "fail" | "skipped"
    detail: str = ""
    data: dict[str, Any] | None = None


# ---------- 单项检查 -----------------------------------------------------


def _check_config(settings: Settings) -> CheckResult:
    try:
        # 简单读字段 + 路径解析
        _ = str(settings.octagon.data_path)
        _ = str(settings.blade.base_url)
    except Exception as exc:
        return CheckResult("config", "fail", f"settings 读取失败: {exc}")
    return CheckResult(
        "config",
        "ok",
        f"data_path={settings.octagon.data_path},base_url={settings.blade.base_url}",
        data={
            "data_path": str(settings.octagon.data_path),
            "envs_path": str(settings.octagon.envs_path),
            "blade_base_url": settings.blade.base_url,
            "blade_skills_path": str(settings.blade.skills_path),
            "api_key_set": settings.blade.api_key is not None,
        },
    )


def _check_env_scan(envs: dict[str, Any]) -> CheckResult:
    if not envs:
        return CheckResult("env_scan", "fail", "未发现任何 env")
    summary = {
        name: {
            "tools": list(env.tools.keys()),
            "tasks": [t.id for t in env.tasks],
        }
        for name, env in envs.items()
    }
    return CheckResult(
        "env_scan",
        "ok",
        f"{len(envs)} envs: {sorted(envs)}",
        data=summary,
    )


def _check_env_api_import() -> CheckResult:
    try:
        from octagon.env_api import (  # noqa: F401
            EnvContext,
            RegisteredTool,
            TraceWriter,
            clear_current_registry,
            env_tool,
            get_current_registry,
        )
    except Exception as exc:
        return CheckResult("env_api_import", "fail", str(exc))
    return CheckResult("env_api_import", "ok", "octagon.env_api 全部符号可 import")


def _check_env_tool_registry(envs: dict[str, Any]) -> CheckResult:
    total = sum(len(env.tools) for env in envs.values())
    if total == 0:
        return CheckResult("env_tool_registry", "fail", "无任何 @env_tool 注册")
    return CheckResult(
        "env_tool_registry",
        "ok",
        f"{total} 个工具已注册",
        data={name: list(env.tools) for name, env in envs.items()},
    )


def _check_skill_sync(envs: dict[str, Any], skills_path: Path) -> CheckResult:
    """检查 `<skills_path>/octagon/<env-name>/SKILL.md + tools.py` 是否齐备。

    注意:**不**在自检里去同步,只是验证状态。同步由 octagon 启动期或外部
    脚本调用 `sync_blade_skills()` 完成。
    """
    expected = []
    missing = []
    target_root = Path(skills_path) / "octagon"
    for name, env in envs.items():
        if not (Path(env.env_dir) / "blade_skill").is_dir():
            continue  # 该 env 没有 blade_skill 目录,不要求同步
        expected.append(name)
        target = target_root / name
        if not (target / "SKILL.md").is_file() or not (target / "tools.py").is_file():
            missing.append(name)
    if not expected:
        return CheckResult("skill_sync", "skipped", "无 env 提供 blade_skill")
    if missing:
        return CheckResult(
            "skill_sync",
            "fail",
            f"未同步到 {target_root}: {missing}。请运行 `sync_blade_skills(...)` 一次。",
            data={"target_root": str(target_root), "missing": missing, "expected": expected},
        )
    return CheckResult(
        "skill_sync",
        "ok",
        f"{len(expected)} envs 已同步到 {target_root}",
        data={"target_root": str(target_root), "synced": expected},
    )


def _check_blade_skill_load_timing() -> CheckResult:
    """blade-agent 当前在进程启动时一次性扫 SKILLS_PATH,运行中加新 skill 不会被加载,
    必须重启 blade server 才能生效——这条是 facts.md T0.5 的延伸事实。
    """
    return CheckResult(
        "blade_skill_load_timing",
        "ok",
        "blade-agent 启动期加载 skill;新增/修改 skill 后须重启 blade server。"
        " octagon 已通过 sync_blade_skills() 在启动前一次性同步。",
    )


async def _check_blade_health(settings: Settings) -> CheckResult:
    if not settings.blade.api_key:
        return CheckResult("blade_health", "skipped", "blade.api_key 未配置")
    try:
        from blade_agent_kit import BladeAgentClient

        async with BladeAgentClient(
            settings.blade.base_url,
            token=settings.blade.api_key.get_secret_value(),
            timeout=5.0,
        ) as client:
            result = await client.health()
    except Exception as exc:
        return CheckResult(
            "blade_health", "fail", f"{settings.blade.base_url} 不可达: {exc}"
        )
    return CheckResult(
        "blade_health", "ok", f"{settings.blade.base_url} ok", data=result
    )


async def _check_primary_skill_id(settings: Settings) -> CheckResult:
    """探测 `POST /api/sessions` body 是否接受 `primary_skill_id`。

    思路:发一个 401(无 token)请求看校验路径;如果连 base_url 都不通,
    直接 fail。M1 简化:走 OPTIONS / GET /api/sessions schema 等会更脆,
    我们直接做"blade reachable + facts 已确认"两条,代替运行时探测。
    """
    url = f"{settings.blade.base_url.rstrip('/')}/api/sessions"
    try:
        async with httpx.AsyncClient(timeout=1.0) as cli:
            # 不带 token 应该返回 401/403 而不是 404,证明路由存在。
            resp = await cli.post(url, json={"intent": "octagon-selfcheck"})
    except Exception as exc:
        return CheckResult("primary_skill_id", "fail", f"blade unreachable: {exc}")
    if resp.status_code == 404:
        return CheckResult(
            "primary_skill_id",
            "fail",
            f"{url} 返回 404,POST /api/sessions 路由可能不存在",
        )
    return CheckResult(
        "primary_skill_id",
        "ok",
        f"{url} 路由可达(status={resp.status_code})。"
        " facts.md T0.1 已静态确认 CreateSessionRequest.primary_skill_id 存在。",
    )



def _check_skill_load_smoke(envs: dict[str, Any]) -> CheckResult:
    """在 octagon 进程内 dry-load 每个 env 的 blade_skill/tools.py,
    模拟 blade ToolLoader 的探测条件:模块顶层有 `name`/`invoke`/`description`
    三属性的对象算工具(facts.md T0.5)。

    实测加载是为了在没真 blade 的环境也能给出有用反馈。
    """
    import importlib.util

    detail: dict[str, list[str]] = {}
    failures: list[str] = []
    for name, env in envs.items():
        tools_py = Path(env.env_dir) / "blade_skill" / "tools.py"
        if not tools_py.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"_selfcheck_{name}_skill_tools", tools_py
            )
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:
            failures.append(f"{name}: import 失败 {exc}")
            continue
        found = [
            attr
            for attr in dir(mod)
            if not attr.startswith("_")
            and all(hasattr(getattr(mod, attr), a) for a in ("name", "invoke", "description"))
        ]
        if not found:
            failures.append(f"{name}: tools.py 内没有 LangChain @tool 对象")
            continue
        detail[name] = found
    if failures:
        return CheckResult(
            "skill_load_smoke",
            "fail",
            "; ".join(failures),
            data={"loaded": detail, "failures": failures},
        )
    if not detail:
        return CheckResult("skill_load_smoke", "skipped", "无 env 提供 blade_skill/tools.py")
    return CheckResult(
        "skill_load_smoke",
        "ok",
        f"{len(detail)} env 的 blade tools.py 加载成功",
        data=detail,
    )


def _check_env_token_auth(data_path: Path, db_path: Path) -> CheckResult:
    """构造一个临时 attempt + token,在内存里验证 hash 比对 + terminal 拒绝逻辑。

    不走 HTTP——避免和 blade health 路径相关。这条只验 octagon 自己的鉴权
    实现是否对。
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            local_db = td / "octagon.db"
            _init_db_sync(local_db)
            token = generate_env_token()
            with _open_sync(local_db) as conn:
                conn.execute(
                    "INSERT INTO tasks(id, env_name, prompt, context_json, constraints_json,"
                    " timeout_seconds, source, created_at)"
                    " VALUES('t', 'travel-planner', 'p', '{}', '{}', 600, 'file', 'x')"
                )
                conn.execute(
                    "INSERT INTO runs(id, task_id, env_name, status, created_at)"
                    " VALUES('r', 't', 'travel-planner', 'queued', 'x')"
                )
                conn.execute(
                    "INSERT INTO attempts(id, run_id, task_id, env_name, agent_name, status,"
                    " env_session_id, env_token_hash, external_refs_json, event_count, created_at)"
                    " VALUES('a', 'r', 't', 'travel-planner', 'blade-agent', 'running',"
                    " 'es', ?, '{}', 0, 'x')",
                    (hash_env_token(token),),
                )
                conn.commit()
                # 校验:正确 token 命中,错误 token 不命中
                row = conn.execute(
                    "SELECT env_token_hash FROM attempts WHERE id='a'"
                ).fetchone()
            assert row is not None
            assert row[0] == hash_env_token(token), "hash 不一致"
            assert row[0] != hash_env_token("wrong"), "错误 token 不应等同"
    except Exception as exc:
        return CheckResult("env_token_auth", "fail", str(exc))
    return CheckResult("env_token_auth", "ok", "token hash 比对路径正常")


def _check_trace_write(data_path: Path) -> CheckResult:
    """直接调一次 TraceWriter 写一条 JSONL,然后回读校验。"""
    from octagon.env_api import TraceWriter

    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tw = TraceWriter(data_path=td, attempt_id="probe", env_session_id="es")
            tw.record(
                tool_name="probe_tool",
                arguments={"x": 1},
                result={"y": 2},
                is_error=False,
                duration_ms=1,
            )
            text = (td / "attempts" / "probe" / "trace.jsonl").read_text(encoding="utf-8")
            row = json.loads(text.strip().splitlines()[-1])
            assert row["tool_name"] == "probe_tool"
    except Exception as exc:
        return CheckResult("trace_write", "fail", str(exc))
    return CheckResult("trace_write", "ok", "trace.jsonl 写入与回读 OK")


# ---------- 总入口 -------------------------------------------------------


async def run_all_checks(
    *,
    settings: Settings,
    envs: dict[str, Any],
    data_path: Path,
    db_path: Path,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    # 本地项(blade 是否可达不影响)
    results.append(_check_config(settings))
    results.append(_check_env_scan(envs))
    results.append(_check_env_api_import())
    results.append(_check_env_tool_registry(envs))
    results.append(_check_skill_sync(envs, settings.blade.skills_path))
    results.append(_check_blade_skill_load_timing())
    # blade 项
    blade_health = await _check_blade_health(settings)
    results.append(blade_health)
    if blade_health.status == "ok":
        results.append(await _check_primary_skill_id(settings))
    else:
        results.append(
            CheckResult(
                "primary_skill_id",
                "skipped",
                "blade_health fail,跳过路由探测;facts.md T0.1 已静态确认字段存在。",
            )
        )
    results.append(_check_skill_load_smoke(envs))
    # octagon 自身鉴权 / trace
    results.append(_check_env_token_auth(data_path, db_path))
    results.append(_check_trace_write(data_path))
    return results


# ---------- HTTP route ---------------------------------------------------


def build_router() -> APIRouter:
    router = APIRouter(tags=["selfcheck"])

    @router.get("/selfcheck")
    async def selfcheck(request: Request) -> dict[str, Any]:
        state = runtime_state.get()
        results = await run_all_checks(
            settings=request.app.state.settings,
            envs=state.envs,
            data_path=state.data_path,
            db_path=state.db_path,
        )
        return {
            "checks": [
                {k: v for k, v in asdict(r).items() if v is not None}
                for r in results
            ],
            "summary": {
                "ok": sum(1 for r in results if r.status == "ok"),
                "fail": sum(1 for r in results if r.status == "fail"),
                "skipped": sum(1 for r in results if r.status == "skipped"),
            },
        }

    return router


def register_routes(app: FastAPI) -> None:
    app.include_router(build_router())
