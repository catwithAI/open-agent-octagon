"""扫描 `envs/` 并加载每个 env 的 meta、core 工具、scorer、tasks。

核心约束:

- env 目录名允许 hyphen(`travel-planner`),所以 import `core.py` / `scorer.py`
  必须用 `importlib.util.spec_from_file_location`,**不能**写
  `import envs.travel-planner.core`(语法上就过不了)。
- 不修改全局 `sys.path`——env 的 core/scorer 应该完全自包含,只允许从已安装
  的 `octagon` 顶层包 import 接口。如果将来某个 env 需要本地辅助模块,通过
  在 env 目录内做 sibling import 解决,不是把 `envs/` 加到 sys.path。
- 默认任意一个 env 加载失败立即抛 `EnvLoadError(env_name=..., stage=...)`,不
  silent skip。服务启动可显式允许 core import 失败的 env 以 unavailable 形态
  进入列表,等用户真正使用该 env 时再报错。
- registry 归属:`octagon.env_api` 的 module-level registry 是 `clear → import
  → snapshot` 三步法的"传输带",不是长期持有。每个 env 拿到 snapshot 后
  立刻绑到 `LoadedEnv.tools`。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from octagon.env_api import RegisteredTool, clear_current_registry, get_current_registry

logger = logging.getLogger(__name__)


# 场景页使用的一级能力分类。meta.yaml 只能从这里选一个主分类；更细的领域
# （如 academia / advertising）应写进 type、description 或未来的 tags，而不是
# 继续扩张一级分类，避免页面重新退化成大量“未分类”卡片。
ENV_CATEGORIES = frozenset({
    "general-assistant",
    "office-productivity",
    "real-skill",
    "complex-workflow",
    "coding",
    "user-coding",
    "agent-system",
    "safety-hitl",
    "baseline",
})


class EnvLoadError(RuntimeError):
    """加载某个 env 失败。错误消息必须包含 env 名,便于排查。"""

    def __init__(self, env_name: str, stage: str, detail: str) -> None:
        super().__init__(f"[env={env_name}] {stage} 失败: {detail}")
        self.env_name = env_name
        self.stage = stage
        self.detail = detail


@dataclass
class Task:
    """规范化后的任务定义。

    字段名与 Octagon 标准对齐:`task_id -> id`、`query -> prompt`、
    `timeout -> timeout_seconds`。任何 env 写出来的 task JSON 都按这个 schema
    校验,colosseo 旧格式由 loader 在读入时翻译。
    """

    id: str
    env_name: str
    prompt: str
    context: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 600
    raw: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "env_name": self.env_name,
            "prompt": self.prompt,
            "context": self.context,
            "constraints": self.constraints,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass
class LoadedEnv:
    name: str
    skill_id: str  # "octagon/<name>",自动拼出
    meta: dict[str, Any]
    tools: dict[str, RegisteredTool]
    tasks: list[Task] = field(default_factory=list)
    tasks_by_id: dict[str, Task] = field(default_factory=dict)
    scorer_module: Any = None  # 由 scorer.py exec 产物;允许为 None
    env_dir: Path = field(default_factory=Path)
    load_error: EnvLoadError | None = None
    # prerequisites 真校验：只警告不阻断，供 env 列表标记展示
    prerequisite_warnings: list[str] = field(default_factory=list)


_VALID_ENV_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
KNOWN_MUTATORS = frozenset(
    {"baseline", "unicode-homoglyph", "spacing", "letter-case", "instruction-position"}
)


def validate_mutation_contract(meta: dict[str, Any]) -> dict[str, Any]:
    """Normalize/validate ``meta.yaml mutations`` for runtime and lint."""
    raw = meta.get("mutations")
    if raw is None:
        return {
            "allowed": ["baseline"],
            "conditional": {},
            "forbidden": sorted(KNOWN_MUTATORS - {"baseline"}),
            "scorer_invariant": False,
        }
    if not isinstance(raw, dict):
        raise ValueError("mutations 必须是 mapping")
    allowed = raw.get("allowed", ["baseline"])
    forbidden = raw.get("forbidden", [])
    conditional = raw.get("conditional", {})
    scorer_invariant = raw.get("scorer_invariant", False)
    if not isinstance(allowed, list) or not all(isinstance(x, str) for x in allowed):
        raise ValueError("mutations.allowed 必须是字符串数组")
    if not isinstance(forbidden, list) or not all(isinstance(x, str) for x in forbidden):
        raise ValueError("mutations.forbidden 必须是字符串数组")
    if not isinstance(conditional, dict):
        raise ValueError("mutations.conditional 必须是 mapping")
    if not isinstance(scorer_invariant, bool):
        raise ValueError("mutations.scorer_invariant 必须是 boolean")
    if "baseline" not in allowed:
        raise ValueError("mutations.allowed 必须包含 baseline")
    declared = set(allowed) | set(forbidden) | set(conditional)
    unknown = sorted(declared - KNOWN_MUTATORS)
    if unknown:
        raise ValueError(f"mutations 含未知 mutator: {unknown}")
    overlap = sorted((set(allowed) & set(forbidden)) | (set(conditional) & set(forbidden)))
    if overlap:
        raise ValueError(f"mutations 声明冲突: {overlap}")
    normalized_conditional: dict[str, dict[str, list[str]]] = {}
    for name, rule in conditional.items():
        if not isinstance(rule, dict) or not isinstance(rule.get("requires", []), list):
            raise ValueError(f"mutations.conditional.{name}.requires 必须是数组")
        requires = rule.get("requires", [])
        if not all(isinstance(value, str) and value for value in requires):
            raise ValueError(f"mutations.conditional.{name}.requires 含非法值")
        normalized_conditional[name] = {"requires": sorted(set(requires))}
    return {
        "allowed": sorted(set(allowed)),
        "conditional": normalized_conditional,
        "forbidden": sorted(set(forbidden)),
        "scorer_invariant": scorer_invariant,
    }


def check_name_consistency(meta_name: Any, env_dir_name: str) -> str | None:
    """meta.yaml 的 name 与目录名一致性判断。

    返回错误描述或 None。抽成共享函数供 `_load_one()` 运行时校验与
    `scripts/lint_env.py` 复用同一逻辑，避免两处独立实现漂移。
    """
    if meta_name and meta_name != env_dir_name:
        return f"meta.yaml 中 name={meta_name!r} 与目录名 {env_dir_name!r} 不一致"
    return None


# ---------- prerequisites 真校验 ----------

# 只匹配"短候选词/短候选词（说明）"整行模式：候选词是不含空格/中文的
# ASCII 二进制名（如 "python3"、"LibreOffice/soffice（office_render）"）。
# 完整自然语言句子（含空格、中文连接词，如 "可访问的 Blade server 与 API
# key（...）"）不匹配——宁可漏报（跳过无法判定的项），不能把正文/示例
# 文本误判成二进制名。
_BINARY_HINT = re.compile(
    r"^([A-Za-z][A-Za-z0-9_.+-]{0,39})"
    r"(?:\s*/\s*([A-Za-z][A-Za-z0-9_.+-]{0,39}))?"
    r"(?:\s*[（(][^）)]*[）)])?\s*$"
)


def _extract_binary_candidates(requires: list[Any]) -> list[list[str]]:
    """从 `prerequisites.requires` 文本里启发式提取可判定的二进制名候选。

    "python3" → [["python3"]]；"LibreOffice/soffice（office_render）" →
    [["LibreOffice", "soffice"]]（`/` 分隔的候选命中任一即算满足）；
    "可访问的 Blade server 与 API key（...)" / "export SELECTED_SKILLS_DIR=..."
    → 跳过（不产生候选、不误报）。
    """
    candidates: list[list[str]] = []
    for item in requires:
        if not isinstance(item, str):
            continue
        m = _BINARY_HINT.match(item.strip())
        if not m:
            continue
        names = [g for g in (m.group(1), m.group(2)) if g]
        if names:
            candidates.append(names)
    return candidates


def python_prerequisites(meta: dict[str, Any]) -> list[str]:
    """meta.yaml `prerequisites.python`：场景 MCP 入口/工具需要的 pip 包清单。

    沙盒镜像构建时合并所有场景的声明（docker/agent-runtime/collect_env_requirements.py）。
    缺省视为无额外依赖；形状错误是结构性错误（ValueError → EnvLoadError）。
    """
    prereqs = meta.get("prerequisites")
    if not isinstance(prereqs, dict):
        return []
    raw = prereqs.get("python")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(
        isinstance(x, str) and x.strip() for x in raw
    ):
        raise ValueError("prerequisites.python 必须是非空字符串列表")
    return [x.strip() for x in raw]


def check_prerequisites(meta: dict[str, Any]) -> list[str]:
    """对 meta.yaml 的可判定二进制依赖做 shutil.which() 存在性检查。

    返回警告列表（只警告不阻断——开发机没装某工具是正常状态，
    不该阻断服务启动；结构性错误才走 EnvLoadError）。
    """
    prereqs = meta.get("prerequisites")
    if not isinstance(prereqs, dict):
        return []
    requires = prereqs.get("requires")
    if not isinstance(requires, list):
        return []
    warnings: list[str] = []
    for names in _extract_binary_candidates(requires):
        if not any(_dependency_available(n) for n in names):
            on_missing = prereqs.get("on_missing") or ""
            suffix = f"（{on_missing}）" if on_missing else ""
            warnings.append(
                f"{meta.get('name', '?')}: 未找到 {' 或 '.join(names)}{suffix}"
            )
    return warnings


def _dependency_available(name: str) -> bool:
    """候选依赖是否本机可用：PATH 二进制、Python 包或受支持的运行时镜像。

    requires 里 "PyMuPDF/fitz（pdf→png）" 这类 Python 包在形式上与二进制名
    不可区分，只查 which() 会对装了包的机器持续误报——find_spec 兜底仍是
    纯本地检查（非功能要求：无网络调用）。``harbor-compat-runtime`` 是
    一个语义依赖，不是 PATH 命令；它通过本地 Docker image inspect 判定。
    """
    if name == "harbor-compat-runtime":
        return _harbor_runtime_available()
    if shutil.which(name):
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _harbor_runtime_available() -> bool:
    """检查已配置的 Harbor overlay runtime image 是否存在于本地 Docker。

    只执行 ``docker image inspect``，不会 pull 镜像，也不会访问 Harbor
    catalog。运行时镜像通过 HARBOR_OCTAGON_RUNTIME_IMAGE 配置；保留
    OCTAGON_SANDBOX_IMAGE 作为兼容回退。
    """
    image = (
        os.environ.get("HARBOR_OCTAGON_RUNTIME_IMAGE")
        or os.environ.get("OCTAGON_SANDBOX_IMAGE")
    )
    if not image or shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class EnvLoader:
    """扫描指定目录加载所有 env。

    用法:
        loaded = EnvLoader(envs_path).load_all()
        loaded["travel-planner"].tools["flight_search"]
    """

    def __init__(self, envs_path: Path | str) -> None:
        self._envs_path = Path(envs_path)

    def load_all(self, *, allow_unavailable_core: bool = False) -> dict[str, LoadedEnv]:
        if not self._envs_path.is_dir():
            return {}

        original_sys_path = list(sys.path)
        result: dict[str, LoadedEnv] = {}
        try:
            for env_dir in sorted(p for p in self._envs_path.iterdir() if p.is_dir()):
                # 跳过明显非 env 的目录(隐藏目录、空目录等)
                if env_dir.name.startswith("."):
                    continue
                if not (env_dir / "meta.yaml").is_file() and not (env_dir / "core.py").is_file():
                    continue
                env = self._load_one(
                    env_dir,
                    allow_unavailable_core=allow_unavailable_core,
                )
                result[env.name] = env
        finally:
            # 即使中途抛错也要复原 sys.path
            if sys.path != original_sys_path:
                sys.path[:] = original_sys_path
        return result

    # ----- 单个 env -------------------------------------------------------

    def _load_one(
        self,
        env_dir: Path,
        *,
        allow_unavailable_core: bool = False,
    ) -> LoadedEnv:
        env_name = env_dir.name
        if not _VALID_ENV_NAME.match(env_name):
            raise EnvLoadError(
                env_name, "name", f"目录名只允许小写字母/数字/下划线/连字符: {env_name!r}"
            )

        meta = self._load_meta(env_name, env_dir / "meta.yaml")
        try:
            meta["mutations"] = validate_mutation_contract(meta)
        except ValueError as exc:
            raise EnvLoadError(env_name, "meta", str(exc)) from exc
        # meta.yaml 里的 name 字段必须与目录名一致(防止两边漂移)——判断逻辑
        # 与 scripts/lint_env.py 共享（check_name_consistency）
        name_error = check_name_consistency(meta.get("name"), env_name)
        if name_error:
            raise EnvLoadError(env_name, "meta", name_error)

        # prerequisites 真校验：可判定二进制依赖做 which() 检查，
        # 只警告不阻断
        prereq_warnings = check_prerequisites(meta)
        for w in prereq_warnings:
            logger.warning("env prerequisite: %s", w)
        try:
            python_prerequisites(meta)
        except ValueError as exc:
            raise EnvLoadError(env_name, "meta", str(exc)) from exc

        load_error: EnvLoadError | None = None
        try:
            tools = self._load_core_tools(env_name, env_dir / "core.py")
        except EnvLoadError as exc:
            if not allow_unavailable_core:
                raise
            logger.warning("env core unavailable, deferring error until use: %s", exc)
            tools = {}
            load_error = exc
        scorer_module = self._load_scorer(env_name, env_dir / "scorer.py")
        tasks = self._load_tasks(env_name, env_dir / "tasks")

        return LoadedEnv(
            name=env_name,
            skill_id=f"octagon/{env_name}",
            meta=meta,
            tools=tools,
            tasks=tasks,
            tasks_by_id={t.id: t for t in tasks},
            scorer_module=scorer_module,
            env_dir=env_dir,
            load_error=load_error,
            prerequisite_warnings=prereq_warnings,
        )

    # ----- 各 stage -------------------------------------------------------

    def _load_meta(self, env_name: str, path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise EnvLoadError(env_name, "meta", f"缺少 {path.name}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise EnvLoadError(env_name, "meta", f"YAML 解析失败: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise EnvLoadError(
                env_name, "meta", f"meta.yaml 顶层必须是 mapping,实际是 {type(data).__name__}"
            )
        # schema_version 缺失只警告不阻断：避免一次性对存量 env
        # 造成大范围加载失败；补齐由 Phase 5 的批量编辑完成
        if "schema_version" not in data:
            logger.warning("env %s: meta.yaml 缺少 schema_version", env_name)
        return data

    def _load_core_tools(self, env_name: str, path: Path) -> dict[str, RegisteredTool]:
        if not path.is_file():
            raise EnvLoadError(env_name, "core", f"缺少 {path.name}")
        clear_current_registry()
        module_name = self._module_name_for(env_name, "core")
        try:
            self._exec_module(module_name, path)
        except EnvLoadError:
            raise
        except Exception as exc:
            raise EnvLoadError(env_name, "core", f"import {path.name} 失败: {exc}") from exc
        finally:
            tools = get_current_registry()
            clear_current_registry()  # 防止下一个 env 看到上一个的残留
        return tools

    def _load_scorer(self, env_name: str, path: Path) -> Any | None:
        if not path.is_file():
            return None
        module_name = self._module_name_for(env_name, "scorer")
        try:
            return self._exec_module(module_name, path)
        except Exception as exc:
            raise EnvLoadError(env_name, "scorer", f"import {path.name} 失败: {exc}") from exc

    def _load_tasks(self, env_name: str, tasks_dir: Path) -> list[Task]:
        if not tasks_dir.is_dir():
            return []
        seen_ids: set[str] = set()
        tasks: list[Task] = []
        for path in sorted(tasks_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise EnvLoadError(
                    env_name, "tasks", f"{path.name} JSON 解析失败: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise EnvLoadError(
                    env_name, "tasks", f"{path.name} 顶层必须是 object"
                )
            task = self._normalize_task(env_name, path.name, data, tasks_dir.parent)
            if task.id in seen_ids:
                raise EnvLoadError(
                    env_name, "tasks", f"重复 task id: {task.id} ({path.name})"
                )
            seen_ids.add(task.id)
            tasks.append(task)
        return tasks

    @staticmethod
    def _normalize_task(
        env_name: str, filename: str, data: dict[str, Any], env_dir: Path
    ) -> Task:
        """把 colosseo 旧格式归一到 Octagon 标准 schema。

        - `task_id` -> `id`
        - `query` -> `prompt`
        - `timeout` -> `timeout_seconds`
        - `files` -> `context.uploaded_files`（任务输入物料，dispatch 时落到
          各 agent 的 workspace：本地 agent 拷贝、blade 走 upload API）
        """
        task_id = data.get("id") or data.get("task_id")
        if not task_id or not isinstance(task_id, str):
            raise EnvLoadError(
                env_name, "tasks", f"{filename} 缺少 string 型 id/task_id 字段"
            )
        prompt = data.get("prompt") or data.get("query")
        if not prompt or not isinstance(prompt, str):
            raise EnvLoadError(
                env_name, "tasks", f"{filename} 缺少 string 型 prompt/query 字段"
            )
        timeout_seconds = data.get("timeout_seconds")
        if timeout_seconds is None:
            timeout_seconds = data.get("timeout", 600)
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            raise EnvLoadError(
                env_name,
                "tasks",
                f"{filename} timeout_seconds 必须是正整数,实际 {timeout_seconds!r}",
            )
        declared_env = data.get("env_name", env_name)
        if declared_env != env_name:
            raise EnvLoadError(
                env_name,
                "tasks",
                f"{filename} env_name={declared_env!r} 与目录名 {env_name!r} 不一致",
            )
        context = data.get("context") or {}
        constraints = data.get("constraints") or {}
        if not isinstance(context, dict):
            raise EnvLoadError(env_name, "tasks", f"{filename} context 必须是 object")
        if not isinstance(constraints, dict):
            raise EnvLoadError(env_name, "tasks", f"{filename} constraints 必须是 object")

        files = data.get("files")
        if files is not None:
            if not isinstance(files, list):
                raise EnvLoadError(env_name, "tasks", f"{filename} files 必须是 array")
            uploaded = list(context.get("uploaded_files") or [])
            seen_names = {uf.get("name") for uf in uploaded if isinstance(uf, dict)}
            for entry in files:
                if isinstance(entry, str):
                    name, raw_path = Path(entry).name, entry
                elif isinstance(entry, dict) and entry.get("path"):
                    raw_path = str(entry["path"])
                    name = str(entry.get("name") or Path(raw_path).name)
                else:
                    raise EnvLoadError(
                        env_name, "tasks",
                        f"{filename} files 项必须是 string 或含 path 的 object: {entry!r}",
                    )
                p = Path(raw_path)
                # 优先 env 目录相对路径（env 自包含物料）；否则原样保留，
                # dispatch 时相对项目根解析。存在性到 dispatch 时再硬校验。
                if not p.is_absolute() and (env_dir / p).is_file():
                    raw_path = str((env_dir / p).resolve())
                if name not in seen_names:
                    uploaded.append({"name": name, "path": raw_path})
                    seen_names.add(name)
            context["uploaded_files"] = uploaded
        return Task(
            id=task_id,
            env_name=env_name,
            prompt=prompt,
            context=context,
            constraints=constraints,
            timeout_seconds=timeout_seconds,
            raw=data,
        )

    # ----- 工具 -----------------------------------------------------------

    @staticmethod
    def _module_name_for(env_name: str, kind: str) -> str:
        # hyphen 在 module 名里非法,这里用下划线;前缀避免和真正的包冲突。
        normalized = env_name.replace("-", "_")
        return f"_octagon_env_{normalized}_{kind}"

    @staticmethod
    def _exec_module(module_name: str, path: Path) -> Any:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 创建 import spec")
        module = importlib.util.module_from_spec(spec)
        # sys.modules 注册让 spec.loader.exec_module 内的相对引用有 fallback。
        # 注意:这是 _octagon_env_<name>_<kind> 命名,不会和真实包重名。
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module
