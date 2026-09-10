"""FastAPI 入口。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRouter

from . import runtime_state
from .adapters.blade_service import sync_blade_skills
from .api import register_routes as register_frontend_routes
from .config import Settings, load_settings
from .db import open_db, reconcile_idempotency_sync, resolve_db_path
from .env_attempt_server import register_routes as register_env_attempt_routes
from .env_loader import EnvLoader
from .experiments.recovery import schedule_run_group_recovery
from .experiments.leader import consume_score_outbox
from .recovery import schedule_pending_blade_cleanup, schedule_startup_recovery
from .research_api import register_routes as register_research_routes
from .selfcheck import register_routes as register_selfcheck_routes

logger = logging.getLogger(__name__)


def _task_is_multi_turn(task: object) -> bool:
    """单个 task 是否为多轮 conversation 场景：context 带 >1 轮的 _conversation。

    用于前端置灰 blade「开启思考」——blade 多轮 + Anthropic extended thinking 会撞
    thinking-block signature 400（历史回传的 thinking block signature 失效）。判定
    必须落到 task 级：同一 env 可同时含多轮与单轮 task，env 级判定会误伤单轮 task。

    阈值 `len(conv) > 1` 与 backend.run_dispatch 的 dispatch 兜底保持一致——单元素
    _conversation 只是首轮 prompt 的等价写法，不构成"历史回传"。
    """
    from .conversation.plan import CONVERSATION_CONTEXT_KEY

    # 加载后的 Task 用 .context（AdapterRunInput 才叫 task_context）。
    ctx = getattr(task, "context", None) or getattr(task, "task_context", None) or {}
    conv = ctx.get(CONVERSATION_CONTEXT_KEY) if isinstance(ctx, dict) else None
    return isinstance(conv, list) and len(conv) > 1


def _env_declares_iterative_review(env: object) -> bool:
    meta = getattr(env, "meta", None)
    interaction = meta.get("interaction") if isinstance(meta, dict) else None
    return (
        isinstance(interaction, dict)
        and interaction.get("mode") == "iterative_product_review"
    )


def _env_is_multi_turn(env: object) -> bool:
    """env 内是否存在任一多轮 task（env 级聚合，仅用于场景卡的粗粒度标记）。

    注意：**不要**用它决定单个 task 的 thinking 置灰——那会误伤同 env 下的单轮
    task。置灰判定应走 task 级 `_task_is_multi_turn`（前端按选中的 task 取）。
    """
    return _env_declares_iterative_review(env) or any(
        _task_is_multi_turn(task) for task in getattr(env, "tasks", []) or []
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """uvicorn factory 入口。

    `uv run uvicorn backend.main:create_app --factory --port 8100`
    """
    cfg = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = cfg
        app.state.db = await open_db(cfg.octagon.data_path)
        app.state.envs = EnvLoader(cfg.octagon.envs_path).load_all(
            allow_unavailable_core=True,
        )
        logger.info("loaded envs: %s", list(app.state.envs))
        # Pre-executor releases could activate model-generated Product Rubrics
        # while official scoring still ran the native environment scorer. Repair
        # those pointers before any Run can freeze an invalid version, even when
        # the evolution scheduler is disabled on this deployment.
        from .rubric_evolution.cross_layer import (
            reconcile_unversioned_process_evidence_sync,
        )
        from .rubric_evolution.store import reconcile_unexecutable_active_rubrics_sync

        repaired_rubrics = reconcile_unexecutable_active_rubrics_sync(
            resolve_db_path(cfg.octagon.data_path)
        )
        if repaired_rubrics:
            logger.error(
                "quarantined unexecutable Active Product Rubrics: %s",
                repaired_rubrics,
            )
        repaired_process_evidence = reconcile_unversioned_process_evidence_sync(
            resolve_db_path(cfg.octagon.data_path)
        )
        if any(repaired_process_evidence.values()):
            logger.error(
                "quarantined Process/Association evidence without version provenance: %s",
                repaired_process_evidence,
            )
        synced = sync_blade_skills(cfg.octagon.envs_path, cfg.blade.skills_path)
        logger.info("synced blade skills: %s", synced)
        runtime_state.bind(
            data_path=cfg.octagon.data_path,
            db_path=resolve_db_path(cfg.octagon.data_path),
            envs=app.state.envs,
            settings=cfg,
        )
        # docker 沙盒启动检查：只记录状态，不阻断启动——失败时本机 agent 的
        # attempt 会以 sandbox_unavailable 终态失败（不回落宿主机执行）。
        from .process.sandbox_preflight import check_sandbox

        sandbox_status = check_sandbox(cfg)
        runtime_state.get().sandbox_status = sandbox_status
        if sandbox_status.enabled:
            if sandbox_status.ok:
                logger.info("sandbox: 已启用 image=%s digest=%s agents=%s",
                            sandbox_status.image.reference, sandbox_status.image.digest,
                            ",".join(sandbox_status.image.agents))
            else:
                logger.error("sandbox: 已启用但不可用（%s）：%s——本机 agent 将全部失败",
                             sandbox_status.error_code, sandbox_status.error_message)
        # 退出归因：启动即留痕，并为上一次「无遗言」的退出补写 crash 证据。
        # 必须在任何恢复逻辑之前——恢复本身可能失败，而事故记录不能因此丢失。
        from .platform_events import (
            install_signal_handlers,
            record_startup,
        )

        install_signal_handlers()
        record_startup(resolve_db_path(cfg.octagon.data_path))
        # 同进程内反复启停（测试、reload）时不得继承上一轮的关停标志，
        # 否则本轮的 Stop / 超时会误以为在关停而不杀进程组。teardown 也会清
        # 一次；这里再清是为了容错——上一轮若在 finally 之前就崩了，标志会
        # 留在置位状态。
        from .process.lifecycle import begin_shutdown, end_shutdown

        end_shutdown()
        # 纯展示边界：HTTP 中间件拦不住进程内的恢复与常驻任务——
        # 它们不经过请求路径，却会派发 judge（真实模型调用）并改写历史状态。
        # 因此这里整段跳过；只保留读路径所需的初始化。
        from .display_only import BACKGROUND_WORK_SKIPPED, skip_background_work

        display_only = skip_background_work(cfg.octagon.display_only)
        if display_only:
            logger.warning(
                "纯展示部署：跳过全部启动恢复与常驻后台任务（%s）",
                "；".join(f"{k}（{v}）" for k, v in BACKGROUND_WORK_SKIPPED.items()),
            )
        if not display_only:
            from .scoring_queue import schedule_startup_scoring_recovery

            recovered_scoring_jobs = schedule_startup_scoring_recovery(
                capacity=cfg.octagon.max_active_scoring_jobs
            )
            if recovered_scoring_jobs:
                logger.warning(
                    "scheduled startup recovery for %d scoring job(s)",
                    recovered_scoring_jobs,
                )
            reconciled_keys = reconcile_idempotency_sync(
                resolve_db_path(cfg.octagon.data_path)
            )
            if reconciled_keys:
                logger.warning(
                    "reconciled %d stale idempotency claim(s)", reconciled_keys
                )
            recovered = schedule_startup_recovery(cfg)
            if recovered:
                logger.warning(
                    "scheduled startup recovery for %d attempt(s)", recovered
                )
            pending_cleanups = schedule_pending_blade_cleanup(cfg)
            if pending_cleanups:
                logger.warning(
                    "scheduled pending Blade session cleanup for %d attempt(s)",
                    pending_cleanups,
                )
            recovered_groups = schedule_run_group_recovery(cfg)
            if recovered_groups:
                logger.warning(
                    "scheduled startup recovery for %d run group(s)", recovered_groups
                )
            recovered_scores = consume_score_outbox(
                resolve_db_path(cfg.octagon.data_path)
            )
            if recovered_scores:
                logger.warning(
                    "consumed %d pending score transition(s)", recovered_scores
                )
            # attempt/group recovery can be asynchronous. Derived reconciliation waits
            # for those tasks and never dispatches an agent itself.
            from .reconciliation import schedule_research_reconciliation

            reconciliation_task = schedule_research_reconciliation(
                db_path=resolve_db_path(cfg.octagon.data_path),
                data_path=cfg.octagon.data_path,
            )
            if reconciliation_task is not None:
                runtime_state.get().active_tasks.setdefault(
                    "research:reconciliation", []
                ).append(reconciliation_task)
        # run 成本结算的后台常驻任务：扫描 pending/settling 的审计记录，
        # 把上游 usage 未稳定、或进程重启时丢失的结算续上。
        # 恢复只需 DB 里的 usage_start 与 api_key_hash——内存中的明文凭据丢了
        # 不影响读 usage（读只需 Management Key）。cost.enabled=false 时空转返回。
        if cfg.cost.enabled and not display_only:
            import asyncio as _asyncio

            from .cost.settler import run_background_settler

            app.state.cost_settler_stop = _asyncio.Event()
            settler_task = _asyncio.create_task(
                run_background_settler(
                    resolve_db_path(cfg.octagon.data_path),
                    settings=cfg,
                    stop_event=app.state.cost_settler_stop,
                )
            )
            runtime_state.get().active_tasks.setdefault("cost:settler", []).append(
                settler_task
            )
            logger.info("run cost settler scheduled")
        # 常驻 deadline sweeper：把「启动时扫一次」提升为「周期常驻」，
        # 服务持续运行期间卡死的 attempt 不再等到下次重启才被发现。
        if cfg.octagon.deadline_sweep_interval_seconds > 0 and not display_only:
            import asyncio as _asyncio_sweep

            from .sweeper import run_deadline_sweeper

            app.state.sweeper_stop = _asyncio_sweep.Event()
            runtime_state.get().active_tasks.setdefault("deadline:sweeper", []).append(
                _asyncio_sweep.create_task(
                    run_deadline_sweeper(
                        resolve_db_path(cfg.octagon.data_path),
                        stop_event=app.state.sweeper_stop,
                        interval_seconds=cfg.octagon.deadline_sweep_interval_seconds,
                        grace_seconds=cfg.octagon.deadline_sweep_grace_seconds,
                    )
                )
            )
            logger.info("deadline sweeper scheduled")
        if cfg.octagon.rubric_evolution_enabled and not display_only:
            import asyncio as _asyncio_rubric

            from .analysis.providers import configured_analysis_provider
            from .rubric_evolution.bootstrap import bootstrap_environment_rubrics_sync
            from .rubric_evolution.cross_layer import (
                backfill_process_analysis_artifacts_sync,
                register_process_rubric_v1_sync,
            )
            from .rubric_evolution.scheduler import run_scheduler_loop

            process_rubric_version = register_process_rubric_v1_sync(
                resolve_db_path(cfg.octagon.data_path)
            )
            logger.info("active Process Rubric: %s", process_rubric_version)
            bootstrap_results = bootstrap_environment_rubrics_sync(
                db_path=resolve_db_path(cfg.octagon.data_path),
                envs=app.state.envs,
            )
            registered = [item for item in bootstrap_results if item.status == "registered"]
            if registered:
                logger.info(
                    "bootstrapped %d active rubric(s) from environment judge contracts: %s",
                    len(registered),
                    [item.env_name for item in registered],
                )

            async def _backfill_cross_layer_records() -> None:
                try:
                    result = await _asyncio_rubric.to_thread(
                        backfill_process_analysis_artifacts_sync,
                        db_path=resolve_db_path(cfg.octagon.data_path),
                        data_path=cfg.octagon.data_path,
                    )
                    logger.info("cross-layer analysis backfill completed: %s", result)
                except Exception:
                    logger.exception("cross-layer analysis backfill failed")

            runtime_state.get().active_tasks.setdefault(
                "rubric:cross-layer-backfill", []
            ).append(_asyncio_rubric.create_task(
                _backfill_cross_layer_records(),
                name="rubric-cross-layer-backfill",
            ))

            app.state.rubric_evolution_stop = _asyncio_rubric.Event()
            runtime_state.get().active_tasks.setdefault(
                "rubric:evolution", []
            ).append(
                _asyncio_rubric.create_task(
                    run_scheduler_loop(
                        db_path=resolve_db_path(cfg.octagon.data_path),
                        data_path=cfg.octagon.data_path,
                        provider=configured_analysis_provider(cfg.insights),
                        stop_event=app.state.rubric_evolution_stop,
                        interval_seconds=(
                            cfg.octagon.rubric_evolution_scan_interval_seconds
                        ),
                        environment_threshold=(
                            cfg.octagon.rubric_evolution_environment_threshold
                        ),
                        cross_environment_threshold=(
                            cfg.octagon.rubric_evolution_cross_environment_threshold
                        ),
                        process_threshold=(
                            cfg.octagon.rubric_evolution_process_threshold
                        ),
                        association_threshold=(
                            cfg.octagon.rubric_evolution_association_threshold
                        ),
                        overlap_ratio=cfg.octagon.rubric_evolution_overlap_ratio,
                    ),
                    name="rubric-evolution-scheduler",
                )
            )
            logger.info("rubric evolution scheduler scheduled")
        # wire 观测的落盘状态收敛：与上面的 attempt 级恢复正交，
        # 只处理 in-progress wire manifest，失败不影响启动。
        #
        # **不在 lifespan 里同步跑**：扫描要读 spool，体积由历史数据决定
        # 而非本次启动的意图。同步跑会让 /api/healthz 一直等它，大 spool 会把
        # 这段变成分钟级 + 数 GiB 的分配，服务在整个过程里都不 ready，
        # systemd 重启后又反复进入同一路径，连 stop/disable 都难以插进去。
        # 改成后台任务 + to_thread：启动立即 ready，恢复慢/失败都只影响它自己。
        import asyncio as _asyncio

        async def _wire_recovery_bg() -> None:
            try:
                from .wire.recovery import recover_wire_manifests

                wire_recovered = await _asyncio.to_thread(
                    recover_wire_manifests,
                    cfg.octagon.data_path,
                    resolve_db_path(cfg.octagon.data_path),
                    max_spool_bytes=cfg.octagon.wire_recovery_max_spool_bytes,
                )
                if wire_recovered:
                    logger.warning(
                        "wire recovery handled %d manifest(s)", wire_recovered
                    )
            except Exception:
                logger.exception("wire recovery 扫描失败（忽略，不影响启动）")

        if not display_only:
            runtime_state.get().active_tasks.setdefault("wire:recovery", []).append(
                _asyncio.create_task(_wire_recovery_bg())
            )
        # reverse HTTP capture proxy 的共享 httpx client。
        from .wire.proxy_api import close_proxy_client, open_proxy_client

        await open_proxy_client(app)
        try:
            yield
        finally:
            # Agent 执行生命周期与 API 服务生命周期隔离。必须在 cancel 之前
            # 置位——adapter 的 finally 据此区分「关停」与「Stop / 超时」，
            # 前者刻意保留 CLI 进程组，让 Agent 跨重启继续跑。
            begin_shutdown()
            pending = [
                task
                for tasks in runtime_state.get().active_tasks.values()
                for task in tasks
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await close_proxy_client(app)
            # 退出归因：在关掉 DB 之前写，否则这条记录永远落不了盘。
            try:
                from .platform_events import record_shutdown

                record_shutdown(resolve_db_path(cfg.octagon.data_path))
            except Exception:
                logger.exception("退出事件记录失败（忽略）")
            await app.state.db.close()
            runtime_state.reset()
            # 关停标志的作用域到此为止。它是模块级的，不清掉会泄漏到同进程内
            # 后续创建的 app（测试、reload），使那一轮的 Stop / 超时误以为
            # 在关停而不杀进程组——那正是孤儿进程的成因。
            end_shutdown()

    app = FastAPI(title="agent-octagon", lifespan=lifespan)
    app.state.settings = cfg
    app.state.envs = {}

    # /api 下的核心路由
    api = APIRouter(prefix="/api")

    @api.get("/healthz")
    async def healthz() -> dict[str, object]:
        # display_only 一并报出：部署验收和监控据此确认这台节点
        # 确实是只读展示节点，而不是靠「没配 key」间接推断。
        return {"ok": True, "display_only": cfg.octagon.display_only}

    @api.get("/platform/events")
    async def list_platform_events(limit: int = 50) -> dict[str, object]:
        """后端启动/退出的归因记录：谁发的信号、退出码、运行时长。

        只返回元数据，不含任何凭据或环境变量原文。
        """
        from .platform_events import BOOT_ID, recent_events

        return {
            "boot_id": BOOT_ID,
            "items": await asyncio.to_thread(
                recent_events, resolve_db_path(cfg.octagon.data_path), limit
            ),
        }

    @api.get("/envs")
    async def list_envs() -> list[dict[str, object]]:
        return [
            {
                "name": env.name,
                "skill_id": env.skill_id,
                "description": env.meta.get("description", ""),
                "category": env.meta.get("category", ""),
                "test_focus": env.meta.get("test_focus", ""),
                "pass_threshold": env.meta.get("pass_threshold"),
                "dimensions": env.meta.get("dimensions", []),
                "tool_count": len(env.tools),
                "task_count": len(env.tasks),
                # 多轮 conversation 场景：任一 task 带 _conversation 数组即多轮
                # （前端据此置灰 blade「开启思考」——blade 多轮 + Anthropic extended
                # thinking 会撞 thinking-block signature 400；后端 dispatch 另有兜底）。
                "multi_turn": _env_is_multi_turn(env),
                "available": env.load_error is None,
                "load_error": str(env.load_error) if env.load_error else None,
                # 本机依赖预警（只警告不阻断），前端在场景卡/提交页
                # 提示"跑了会掉分"，把痛点前移到提交之前
                "prerequisite_warnings": env.prerequisite_warnings,
                # 场景对 agent 侧模型的 modality 需求（meta.yaml
                # prerequisites.agent_modalities，机器可读声明）——前端与所选
                # 模型的 input_modalities 交叉预警
                "agent_modalities": (
                    (env.meta.get("prerequisites") or {}).get("agent_modalities", [])
                    if isinstance(env.meta.get("prerequisites"), dict) else []
                ),
                "supported_mutators": list(
                    (env.meta.get("mutations") or {}).get("allowed", ["baseline"])
                ),
                "conditional_mutators": (
                    (env.meta.get("mutations") or {}).get("conditional", {})
                ),
            }
            for env in app.state.envs.values()
        ]

    @api.get("/envs/{name}/tasks")
    async def list_env_tasks(name: str) -> list[dict[str, object]]:
        env = app.state.envs.get(name)
        if env is None:
            raise HTTPException(status_code=404, detail=f"env not found: {name}")
        # 每个 task 附 task 级 multi_turn：前端据「选中的 task」置灰 blade 思考，
        # 避免 env 级聚合误伤同 env 下的单轮 task。
        iterative = _env_declares_iterative_review(env)
        return [
            {**t.model_dump(), "multi_turn": iterative or _task_is_multi_turn(t)}
            for t in env.tasks
        ]

    @api.get("/envs/{name}/meta")
    async def get_env_meta(name: str) -> dict[str, object]:
        """Return the checked-in meta.yaml for one explicitly selected env."""
        env = app.state.envs.get(name)
        if env is None:
            raise HTTPException(status_code=404, detail=f"env not found: {name}")
        try:
            source = (env.env_dir / "meta.yaml").read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"meta.yaml unavailable: {name}"
            ) from exc
        return {"name": name, "meta": env.meta, "meta_yaml": source}

    # 输入物料上限：只用于页面展示，不是下载通道。超限文件仍列出但不返回正文，
    # 由前端提示——静默截断会让人以为看到的就是全部输入。
    _MAX_INPUT_PREVIEW_BYTES = 512 * 1024

    def _iter_input_files(inputs_dir: Path) -> list[dict[str, object]]:
        if not inputs_dir.is_dir():
            return []
        files: list[dict[str, object]] = []
        for path in sorted(inputs_dir.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            files.append({
                "path": path.relative_to(inputs_dir).as_posix(),
                "size": size,
                "too_large": size > _MAX_INPUT_PREVIEW_BYTES,
            })
        return files

    @api.get("/envs/{name}/inputs")
    async def list_env_inputs(name: str) -> dict[str, object]:
        """列出场景 `inputs/` 下的输入物料。

        task 的 `files[].path` 指向这里（如 `inputs/requirement.md`）——不展示
        它们，用户就只能看到任务 prompt，看不到 agent 实际拿到的输入全貌。
        """
        env = app.state.envs.get(name)
        if env is None:
            raise HTTPException(status_code=404, detail=f"env not found: {name}")
        return {
            "name": name,
            "files": await asyncio.to_thread(_iter_input_files, env.env_dir / "inputs"),
        }

    @api.get("/envs/{name}/inputs/{path:path}")
    async def get_env_input(name: str, path: str) -> Response:
        """读取单个输入物料原文。

        路径固定收敛在 `<env>/inputs/` 内：先按 raw parts 拦 `..`/隐藏段/
        反斜杠，再用 resolve + relative_to 兜底——只靠后者不够，`x/../..`
        可能仍落在 root 内，隐藏文件也会绕过列表过滤被直接读取。
        """
        env = app.state.envs.get(name)
        if env is None:
            raise HTTPException(status_code=404, detail=f"env not found: {name}")
        raw_parts = Path(path).parts
        if (
            not raw_parts
            or "\\" in path
            or any(part == ".." or part.startswith(".") for part in raw_parts)
        ):
            raise HTTPException(status_code=404, detail=f"input not found: {path}")
        root = (env.env_dir / "inputs").resolve()
        target = (root / path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise HTTPException(status_code=404, detail=f"input not found: {path}")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"input not found: {path}")
        if target.stat().st_size > _MAX_INPUT_PREVIEW_BYTES:
            raise HTTPException(status_code=413, detail=f"input too large: {path}")
        content = await asyncio.to_thread(
            target.read_text, encoding="utf-8", errors="replace"
        )
        # 一律纯文本返回：输入物料可能是 HTML/Markdown，绝不能以 text/html
        # 出去——那会让它成为与 Octagon 同源的可执行页面。
        return PlainTextResponse(
            content, headers={"X-Content-Type-Options": "nosniff"}
        )

    app.include_router(api)

    # env attempt server 路由保持在根路径（agent 直接调用）
    register_env_attempt_routes(app)
    # frontend API 路由挂到 /api
    register_frontend_routes(app, prefix="/api")
    register_research_routes(app)
    register_selfcheck_routes(app)
    # 纯展示部署的只读边界。**最后安装**：FastAPI 的 http 中间件按
    # 后进先出执行，装在最后才能保证它先于业务路由拦下执行型写请求。
    from .display_only import install as install_display_only

    install_display_only(app, cfg.octagon.display_only)
    return app
