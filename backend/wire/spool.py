"""Source spool writer/reader。

每个 source 进程/instance 独立写自己的
``wire-sources/<kind>[-<instance>].jsonl.partial``，正常关闭时 rename 成
``.jsonl``。崩溃留下 ``.partial``，finalizer 读取其中的完整行并把该 source
标 partial——「没有发生通信」（空 ``.jsonl``）与「采集器没工作」（残留
``.partial`` / 无文件）因此可区分。

写入规则：

- 一行一个已脱敏的 ``WireEvidence`` JSON；
- append-only，写后不原地修改；
- **逐行 flush**：SIGKILL 时已写行不丢（spool 侧保证——flush 到
  内核页缓存足以在进程被杀后存活；机器断电级别的保证不在目标内）；
- 单行有大小上限，超限拒绝写入由调用方计 dropped，不截断出半行 JSON；
- source 自己串行写，不做跨进程 file lock（每个 instance 一个文件，天然不争）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.wire import evidence as _evidence
from backend.wire import policy as _policy

# 单行上限默认 8 MiB：full 档大 payload 走 blob ref，不该出现在 spool 行里。
DEFAULT_MAX_LINE_BYTES = 8 * 1024 * 1024

PARTIAL_SUFFIX = ".partial"


class SpoolError(RuntimeError):
    pass


class SpoolLineTooLarge(SpoolError):
    """单行超限。调用方应丢弃该条 evidence 并计入 dropped，不得截断写入。"""


class SpoolValidationError(SpoolError):
    """append 前校验失败：schema / attempt 归属 / policy 越档。"""


class SpoolWriter:
    """单 source 的 append-only spool writer。

    非线程安全——contract 就是 source 自己串行写；并发请求的 source
    （如 Env Attempt Server）自行在外层对同一 writer 串行化。

    writer 在 append 前完成 schema、attempt、phase 和 policy
    校验——``expected_attempt_id`` 强制 evidence 归属，``max_policy`` 拒绝
    redaction.policy 超过 effective policy 的行（不靠 producer 自觉）。
    """

    def __init__(
        self,
        final_path: Path,
        *,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        expected_attempt_id: str | None = None,
        max_policy: str | None = None,
    ):
        self._final_path = Path(final_path)
        self._partial_path = self._final_path.with_name(
            self._final_path.name + PARTIAL_SUFFIX
        )
        self._max_line_bytes = max_line_bytes
        self._expected_attempt_id = expected_attempt_id
        self._max_policy = max_policy
        self._partial_path.parent.mkdir(parents=True, exist_ok=True)
        # 同 instance 重开必须保留全部历史行：
        # - 只有 .partial（崩溃后 recovery）：直接追加；
        # - 只有 .jsonl（正常关闭后重开）：把 final 改回 .partial 再追加，
        #   否则 close() 的 os.replace 会用只含新行的 partial 覆盖旧 final；
        # - 两者都在（旧 final + 另一次崩溃的 partial）：合并为 final+partial
        #   顺序的单个 .partial。
        if self._final_path.exists():
            if self._partial_path.exists():
                merged = self._final_path.read_bytes() + self._partial_path.read_bytes()
                tmp = self._partial_path.with_name(self._partial_path.name + ".merge")
                tmp.write_bytes(merged)
                os.replace(tmp, self._partial_path)
                self._final_path.unlink()
            else:
                os.replace(self._final_path, self._partial_path)
        self._fh = open(self._partial_path, "ab")
        self._closed = False
        self.lines_written = 0

    @property
    def partial_path(self) -> Path:
        return self._partial_path

    def _validate(self, obj: dict[str, Any]) -> None:
        try:
            _evidence.validate_evidence(obj)
        except Exception as exc:
            raise SpoolValidationError(f"evidence schema 校验失败: {exc}") from exc
        if (
            self._expected_attempt_id is not None
            and obj.get("attempt_id") != self._expected_attempt_id
        ):
            raise SpoolValidationError(
                f"evidence attempt 归属不匹配: {obj.get('attempt_id')!r}"
            )
        if self._max_policy is not None:
            declared = (obj.get("redaction") or {}).get("policy")
            if declared is None or _policy.policy_rank(declared) > _policy.policy_rank(
                self._max_policy
            ):
                raise SpoolValidationError(
                    f"evidence policy 越档: {declared!r} > {self._max_policy}"
                )

    def append(self, evidence: dict[str, Any] | Any) -> None:
        """写一行。接受 dict 或 WireEvidence variant 模型；两者都在 append 前
        过 schema/attempt/policy 校验。"""
        if self._closed:
            raise SpoolError("spool 已关闭，不能再写")
        obj = evidence.model_dump(mode="json") if hasattr(evidence, "model_dump") else evidence
        self._validate(obj)
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        if len(data) > self._max_line_bytes:
            raise SpoolLineTooLarge(
                f"evidence 行 {len(data)} bytes 超过上限 {self._max_line_bytes}"
            )
        self._fh.write(data)
        # 逐行 flush：进程被 SIGKILL 后已写行仍在。
        self._fh.flush()
        self.lines_written += 1

    def close(self) -> Path:
        """正常关闭：fsync + rename 为 ``.jsonl``，返回最终路径。幂等。"""
        if self._closed:
            return self._final_path
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        os.replace(self._partial_path, self._final_path)
        self._closed = True
        return self._final_path

    def abandon(self) -> None:
        """只关文件句柄、保留 ``.partial``（abort 路径，留给 finalizer 判 partial）。"""
        if not self._closed:
            self._fh.flush()
            self._fh.close()
            self._closed = True


@dataclass
class SpoolReadResult:
    """finalizer 读 spool 的结果。

    ``partial=True`` 的两种来源：文件仍是 ``.partial``（source 没有正常关闭），
    或尾行被截断（写到一半崩溃）。两者都要在 manifest 上反映为 source 缺口，
    但已解析出的完整行仍然可用。
    """

    records: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    truncated_tail: bool = False
    parse_errors: int = 0


def iter_spool(path: Path) -> Iterator[dict[str, Any]]:
    """流式读取 spool，逐行 yield 已解析的 record。

    与 :func:`read_spool` 语义完全一致，但**不把整个文件读进内存**：
    调用方若不自行保留，峰值内存只与单行大小相关而非文件大小。

    统计信息（partial / truncated_tail / parse_errors）通过生成器的
    ``return`` 值传出，用 ``yield from`` 或手动捕获 ``StopIteration.value``
    获取；只关心 record 的调用方可以直接迭代。
    """
    path = Path(path)
    stats = SpoolReadResult(partial=path.name.endswith(PARTIAL_SUFFIX))
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return stats
    with fh:
        prev_line: bytes | None = None
        prev_had_newline = True
        for raw in fh:
            # 只有**最后**一行缺尾换行才算截断；先缓一行，确认后面还有内容
            # 再把上一行交出去，避免把中间行误判成半行。
            if prev_line is not None:
                yield from _emit_line(prev_line, stats)
            prev_had_newline = raw.endswith(b"\n")
            prev_line = raw[:-1] if prev_had_newline else raw
        if prev_line is not None:
            if not prev_had_newline:
                # 尾行写到一半就崩了：丢弃并如实标记。
                stats.truncated_tail = True
                stats.partial = True
            else:
                yield from _emit_line(prev_line, stats)
    return stats


def _emit_line(line: bytes, stats: SpoolReadResult) -> Iterator[dict[str, Any]]:
    if not line.strip():
        return
    try:
        yield json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        stats.parse_errors += 1


def read_spool(path: Path) -> SpoolReadResult:
    """读取 spool（``.jsonl`` 或 ``.partial``），跳过损坏尾行并如实报告。

    - 缺尾部换行的最后一行视为截断，丢弃并标 truncated_tail/partial；
    - 中间行 JSON 解析失败计 parse_errors（不中断——单行损坏不应废掉整个 source）；
    - ``.partial`` 后缀本身即 partial。

    会把全部 record 物化进内存。**大 spool 请改用 :func:`iter_spool`**——
    否则大 spool 会让恢复路径的 RSS 涨到危险水平、拖垮宿主机。
    """
    gen = iter_spool(path)
    records: list[dict[str, Any]] = []
    while True:
        try:
            records.append(next(gen))
        except StopIteration as stop:
            stats: SpoolReadResult = stop.value
            stats.records = records
            return stats


def find_spool_file(final_path: Path) -> Path | None:
    """定位 source 的 spool 文件：优先正常关闭的 ``.jsonl``，其次 ``.partial``。"""
    final_path = Path(final_path)
    if final_path.exists():
        return final_path
    partial = final_path.with_name(final_path.name + PARTIAL_SUFFIX)
    if partial.exists():
        return partial
    return None
