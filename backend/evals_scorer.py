"""octagon-evals 外部 judge 适配器（settings.judge.backend = "evals"）。

把 env 的每个维度打包成 EvaluateRequest 送 evals 的 ``/evaluate``，返回的
[0,1] 标量转回 0-100 维度分——与内置 scorer 的输出形状一致，commit /
outbox / leader / provenance 全链路复用。

judge 血统（model / prompt_version）写进 ``scoring-work/<job_id>/judge_result.json``，
供 ``provenance.read_judge_anchors`` 回读 judge 锚——否则外部 judge 的版本锚会
变 NULL，provenance_complete=1 但 judge 一段是空的。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class EvalsJudgeError(RuntimeError):
    """evals 服务调用/响应失败。上层映射为 scoring_failed（judge_unavailable），
    绝不落业务 0 分——与 scoring_queue 的 judge_infrastructure_error 同一语义。
    """


#: evals 的比较式方法（models.COMPARISON_METHODS）。这些维度是 **run 级**的——
#: 要把同一个 run 下的多个 attempt 聚成一组候选才能裁决，而 /evaluate 是
#: attempt 级单次入口，送进去会被整体 409（api.py 的 method 白名单）。故
#: pointwise 打包时必须先滤掉，否则一个比较维度会让同 env 的所有维度都评不成。
COMPARISON_METHODS = frozenset({
    "pairwise_judge", "pairwise_judge_agentic",
    "listwise_judge", "listwise_judge_agentic",
})

#: /evaluate 接受的 pointwise 方法白名单。env 写了白名单外的方法 → 按缺省
#: agent_judge 处理并告警，不让一个拼错的方法名把整批评分带崩。
_POINTWISE_METHODS = frozenset({
    "deterministic", "agent_judge", "agent_judge_agentic", "jev_judge",
})

DEFAULT_METHOD = "agent_judge"


def _question(name: str, description: Any) -> str:
    """把 meta.yaml 的维度 description 包成**明确的评判指令**。

    env 的 description 常常写的是协议或口径，而不是判据。例如 GDPval 那维写的
    是「BladeAgent LLM judge 对官方 59 条 rubric 严格二元评分（原始总分 89）后
    归一化为 100 分」——这是在描述**怎么评**，不是在问**这份交付物好不好**。
    原样送给 judge，它会合理地理解成「核验这句话是否属实」：2026-09-24 实测
    六个候选里有一个正是这么做的，跑去读 env 的 judge_local.py 确认实现与描述
    相符，然后给了 100 分，全程没碰候选的交付物。

    这里不改写 description 本身（那是 env 作者的表述），只在外面加一层指明
    评判对象是谁——歧义消在适配器里，对所有 env 生效。
    """
    text = str(description or "").strip() or name
    return (
        f"评判维度「{name}」：请依据下面的口径，评估 **attempt_dir 里候选 agent "
        f"的交付物**，给出 [0,1] 的分数。\n\n{text}\n\n"
        "注意：上面这段是本维度的评判口径/协议说明，不是一个待核实的断言——"
        "不要去核验这段话本身是否属实，也不要评判评测环境的实现，"
        "要评的是候选交付物。"
    )


def _dimension_from_meta(item: dict[str, Any]) -> dict[str, Any]:
    """meta.yaml 的一个维度块 → evals Dimension dict。

    维度 ID 直接取 dimension.name：evals 返回的 ``dimension_id`` 即维度名，
    agent-octagon 的 scores.dimension 用它落库，两侧 ID 天然对齐。

    ``method`` / ``role`` / ``anchors`` / ``comparison`` 原样透传给 evals，
    由那边的 ``Dimension`` / ``ComparisonConfig`` 负责校验——这里不重复建模，
    免得两侧的合法取值各自漂移。
    """
    name = str(item["name"])
    method = str(item.get("method") or DEFAULT_METHOD)
    dim: dict[str, Any] = {
        "id": name,
        "version": int(item.get("version", 1)),
        "weight": int(item.get("weight", 1)),
        "method": method,
        "question": _question(name, item.get("description")),
    }
    # 比较维度默认 diagnostic：它的分依赖同组其他 attempt，进了 score_total
    # 就让单个 attempt 的总分随同伴变化，也就没法单独重放。要进总分必须在
    # meta.yaml 里显式写 role: scored。
    default_role = "diagnostic" if method in COMPARISON_METHODS else "scored"
    dim["role"] = str(item.get("role") or default_role)
    if dim["role"] == "diagnostic":
        # 权重强制归零，而不只是靠「不进 scores 列表」。_aggregate_total 是按
        # `weights.get(dimension)` 查权重、`if w > 0` 才计入的——权重为 0 就
        # 保证了即便将来有人改成从 DB 的 scores 表重算总分，diagnostic 维度
        # 也不会被算进去。这是一道结构性的闸，不依赖调用顺序。
        dim["weight"] = 0
    if item.get("anchors"):
        dim["anchors"] = item["anchors"]
    if item.get("comparison"):
        dim["comparison"] = item["comparison"]
    return dim


def _iter_meta_dimensions(env: Any):
    meta = getattr(env, "meta", {}) or {}
    for item in meta.get("dimensions") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        yield item


def comparison_dimensions_from_env(env: Any) -> list[dict[str, Any]]:
    """env 里的比较式维度（run 级，由 run_comparison 那条链消费）。"""
    return [
        _dimension_from_meta(item)
        for item in _iter_meta_dimensions(env)
        if str(item.get("method") or DEFAULT_METHOD) in COMPARISON_METHODS
    ]


def _dimensions_from_env(
    env: Any, *, method_override: str | None = None
) -> list[dict[str, Any]]:
    """env meta.yaml 的 dimensions → evals EvalPlan dimensions（仅 pointwise）。

    ``method_override`` 把所有 pointwise 维度强制成同一方法，不改共享 env 仓库。
    用处：env 的维度绑定了自己的私有资产（rubric / 参考工作簿），非 agentic 的
    judge 只能看到 prompt 里的 JSON，xlsx 这类二进制根本读不了——这时整个 env
    需要 ``agent_judge_agentic``，而 meta.yaml 里往往没写 method（历史默认）。
    比较式维度不受影响：它们本就走另一条链。
    """
    dims: list[dict[str, Any]] = []
    for item in _iter_meta_dimensions(env):
        method = str(item.get("method") or DEFAULT_METHOD)
        if method in COMPARISON_METHODS:
            continue
        if method_override:
            item = {**item, "method": method_override}
            method = method_override
        if method not in _POINTWISE_METHODS:
            logger.warning(
                "env %s 维度 %s 的 method=%r 不被 /evaluate 支持，按 %s 处理",
                getattr(env, "name", "?"), item["name"], method, DEFAULT_METHOD,
            )
            item = {**item, "method": DEFAULT_METHOD}
        dims.append(_dimension_from_meta(item))
    return dims


def judge_visible_path(*parts: Any) -> str:
    """交给**跨进程** judge 的路径必须是绝对的。

    judge 在自己的临时工作区里执行（pi 的 cwd 是 /tmp/octagon-judge-*），
    而 ``octagon.yaml`` 的 ``data_path`` 常写成相对值（``./data``）。把相对
    路径递过去，judge 会在工作区里找不到任何东西——2026-09-24 实测它并不会
    报错，而是开始 ``grep -r`` 整个 /home/bladeai/codes 去猜材料在哪，既慢又
    越出了证据边界。
    """
    return str(Path(*parts).resolve())


#: 非 agentic judge 的内联证据上限。它只能看 prompt，材料必须内联，但无上限
#: 内联会把网关打挂：2026-09-24 实测 135KB 的 evidence 让单次请求挂了 10 分钟
#: （0.4% CPU，纯等响应）。超限**显式截断并写明**，绝不静默丢。
_INLINE_EVIDENCE_MAX_BYTES = 128 * 1024

_AGENTIC_GUIDE = (
    "所有材料都是本机真实路径，用 read/bash 自取，按需读取而不是全量读入。"
    "材料只在这几个路径下，不要到别处搜索：\n"
    "- attempt_dir：被评 attempt 的**冻结评分快照**，候选交付物在其 "
    "skill_workspace/ 下（xlsx/docx 等用 python 读，不要只看文件名）；\n"
    "- env_dir：本 env 的定义目录，评判资产（rubric、参考输入、专家交付物）"
    "在其 private/ 与 inputs/ 下；\n"
    "- atif_trajectory_path：该 attempt 的归一化执行轨迹（ATIF v1.7，"
    "step/tool_call/observation 结构，跨 agent 同构）。要看 agent 做过什么读它，"
    "不要去读 attempt_dir 里各家格式互不相同的 events.jsonl。\n"
    "找不到所需资产时如实说明缺什么，不要凭轨迹反推交付物内容。"
)


def _truncate_inline(value: Any, budget: int) -> tuple[Any, int]:
    """把内联材料压到预算内，返回 (值, 实际字节)。超限时截断并标注。

    截断是**可见的**：返回一个带 ``_truncated`` 说明的对象，而不是悄悄少给
    几条记录——judge 据以知道自己看到的不是全部。
    """
    encoded = len(json.dumps(value, ensure_ascii=False, default=str).encode())
    if encoded <= budget or not isinstance(value, list):
        return value, encoded
    kept: list[Any] = []
    used = 0
    for item in value:
        size = len(json.dumps(item, ensure_ascii=False, default=str).encode()) + 1
        if used + size > budget:
            break
        kept.append(item)
        used += size
    return {
        "_truncated": (
            f"内联预算 {budget} 字节，原始 {len(value)} 条共 {encoded} 字节，"
            f"只给出前 {len(kept)} 条"
        ),
        "records": kept,
    }, used


def build_evidence(
    *,
    data_path: Path,
    attempt_id: str,
    env: Any,
    trace: Any,
    final_state: Any,
    events: Any,
    agentic: bool,
    input_path: Path | None = None,
) -> dict[str, Any]:
    """组装送往 evals 的 evidence。

    两种形态，按 judge 能不能读文件分流：

    - **agentic**（judge 有 read/bash）：只给指针——attempt_dir / env_dir /
      atif_trajectory_path，外加体积可控的 final_state。执行轨迹走 ATIF
      而不是 raw events.jsonl：后者是各家 adapter 的原始格式，六个 agent
      结构互不相同、体积差 74 倍，拿它当证据等于给不同 agent 喂结构不同的
      材料（见 atif_evidence 模块注释的实测数据）。
    - **非 agentic**（judge 只能看 prompt）：材料必须内联，但按
      ``_INLINE_EVIDENCE_MAX_BYTES`` 显式截断。

    ``input_path`` 是**冻结评分输入**的根（``scoring-work/<job_id>``）。被评的
    交付物必须取自它而不是实时 attempt 目录——``input_hash`` / ``snapshot_ref``
    整套机制的前提就是「评的是那一份冻结快照」，内置 scorer 拿到的也是它
    （``evaluate(data_path=scoring_data_path)``）。指向实时目录会让评分不可
    重放，而且交付物根本不在那儿：实测 att_b6978dcb6ce1 的 xlsx 只存在于
    快照的 ``skill_workspace/`` 下。

    ATIF 则仍从实时目录生成：它要读 ``sandbox_home`` 下各家 CLI 的会话转录，
    而冻结快照里没有 sandbox_home。轨迹是归因辅助材料，不是被评对象，这个
    不对称是有意的。
    """
    evidence: dict[str, Any] = {
        "final_state": final_state,
        "attempt_dir": judge_visible_path(
            input_path or data_path, "attempts", attempt_id
        ),
    }
    env_dir = getattr(env, "env_dir", None)
    if env_dir:
        # 不给这个，绑定 env 私有资产的维度（GDPval 官方 59 条 rubric、
        # PresentBench slide judge）拿不到 rubric，只能判「无法核验」——那个 0
        # 与「agent 真的做得差」在库里完全无法区分。
        evidence["env_dir"] = judge_visible_path(env_dir)

    if agentic:
        from .atif_evidence import materialize_atif

        path = materialize_atif(data_path, attempt_id)
        if path is not None:
            evidence["atif_trajectory_path"] = judge_visible_path(path)
        else:
            # blade-agent 等无 ATIF 转换器的情况：如实说明缺口，让 judge 知道
            # 它拿到的证据形态与其他 agent 不对等，而不是默默少一块。
            evidence["atif_trajectory_path"] = None
            evidence["atif_unavailable"] = (
                "该 attempt 没有 ATIF 轨迹（agent 无转换器或转录缺失）；"
                "需要执行过程时读 attempt_dir 下的原始事件文件，并注意其格式"
                "与其他 agent 不同。"
            )
        evidence["evidence_guide"] = _AGENTIC_GUIDE
        return evidence

    budget = _INLINE_EVIDENCE_MAX_BYTES // 2
    evidence["trace"], used = _truncate_inline(trace, budget)
    evidence["events"], _ = _truncate_inline(
        events, max(0, _INLINE_EVIDENCE_MAX_BYTES - used)
    )
    return evidence


def _write_judge_result(
    data_path: Path, job_id: str, models: set[str], prompts: set[str]
) -> None:
    """把 evals 回传的 judge 血统落盘，供 provenance 回读 judge 锚。

    多维度各自带 lineage，合并取并集 `|` 连接——与 read_judge_anchors 的
    合并口径一致。best-effort：写失败不阻塞评分。
    """
    if not models and not prompts:
        return
    target = Path(data_path) / "scoring-work" / job_id / "judge_result.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "model": "|".join(sorted(models)) or None,
                    "prompt_version": "|".join(sorted(prompts)) or None,
                    "provider": "octagon-evals",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("judge_result 写入失败(不影响评分) job=%s", job_id)


def make_evals_scorer(
    *,
    env: Any,
    base_url: str,
    timeout: float,
    data_path: Path,
    job_id: str,
    input_path: Path | None = None,
    method_override: str | None = None,
):
    """返回兼容 scorer 签名的闭包：调 evals ``/evaluate`` 评单个 attempt 全维度。

    签名对齐 ``evaluate()`` 的 scorer 约定
    ``score(*, attempt_id, task, env_db, trace, final_state)``；events 经 **kwargs
    接收（evaluate() 按签名检测是否注入）。
    """
    dimensions = _dimensions_from_env(env, method_override=method_override)
    endpoint = f"{str(base_url).rstrip('/')}/evaluate"
    # 只要有一维是 agentic，证据就走「指针」形态：evidence 是 per-request 的，
    # 一份要同时服务全部维度，而内联大块材料的坏处（撑爆 prompt）对 agentic
    # 维度是纯负担。
    agentic = any(str(d.get("method", "")).endswith("_agentic") for d in dimensions)

    def score(
        *,
        attempt_id: str,
        task: dict[str, Any],
        env_db,
        trace,
        final_state,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        evidence = build_evidence(
            data_path=data_path,
            attempt_id=attempt_id,
            env=env,
            trace=trace,
            final_state=final_state,
            events=kwargs.get("events") or [],
            agentic=agentic,
            input_path=input_path,
        )
        if not dimensions:
            # env 只配了比较式维度：pointwise 无可评，直接返回空而不是发一个
            # dimensions=[] 的请求（evals 侧 min_length=1 会 422）。这些维度的
            # 分由 run 级比较链在全部 attempt 评完后回填。
            logger.info(
                "env %s 无 pointwise 维度，跳过 /evaluate attempt=%s",
                getattr(env, "name", "?"), attempt_id,
            )
            return []
        payload = {
            "evaluation_id": f"{job_id}:{attempt_id}",
            "run_id": attempt_id,
            "scenario": {
                "id": getattr(env, "name", None) or "unknown",
                "version": (getattr(env, "meta", {}) or {}).get("schema_version"),
            },
            "task": task,
            "artifact": {"snapshot_ref": "local", "content_hash": ""},
            "history": {"events_ref": "local"},
            "evidence": evidence,
            "dimensions": dimensions,
            "deadline_seconds": timeout,
            "judge_config": {},
        }
        try:
            resp = httpx.post(endpoint, json=payload, timeout=timeout)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EvalsJudgeError(f"octagon-evals 调用失败: {exc}") from exc
        try:
            body = resp.json()
        except ValueError as exc:
            raise EvalsJudgeError(f"octagon-evals 响应非 JSON: {exc}") from exc
        if body.get("status") != "completed":
            raise EvalsJudgeError(
                f"octagon-evals 评分失败: {body.get('error') or body.get('status')}"
            )
        results: list[dict[str, Any]] = []
        models: set[str] = set()
        prompts: set[str] = set()
        for item in body.get("results") or []:
            try:
                value = int(round(float(item["value"]) * 100))
            except (KeyError, TypeError, ValueError) as exc:
                raise EvalsJudgeError(f"evals 维度分非法: {item}") from exc
            results.append(
                {
                    "dimension": str(item.get("dimension_id", "")),
                    "value": value,
                    "detail": str(item.get("reason") or ""),
                }
            )
            lineage = item.get("lineage") or {}
            if lineage.get("model"):
                models.add(str(lineage["model"]))
            if lineage.get("prompt_version"):
                prompts.add(str(lineage["prompt_version"]))
        _write_judge_result(data_path, job_id, models, prompts)
        return results

    return score
