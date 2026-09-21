"""attempt 目录归档：删可重建内容，留证据。

    python -m backend.tools.archive_attempts [--data-path ./data] [--run-id RUN]
        [--older-than 7d] [--yes] [--db-path ...]

一次 7-agent 横评跑到 336 个 attempt 就把 444G 的评测机写满。其中占大头的
`sandbox_home`（33G）是容器家目录 bind mount，里面主要是 LibreOffice 运行时、
apt 缓存、字体——**可重建**，不是证据。归档把这类目录删掉，保留 wire、
workspace、events 等真正有分析价值的部分。

默认 dry-run：先打印将删什么、能回收多少，要 `--yes` 才真删。横评期间经常
要回看现场，所以归档绝不在 attempt 结束时自动触发（spec D-03）。

与评分快照的关系（**这条是归档能否真正回收空间的前提**）：快照改硬链接后，
凡是被快照收进去的文件，删 attempt 侧的目录项都不会释放任何字节——inode 仍被
快照引用。所以 `scoring_snapshot._ATTEMPT_ROOT_RUNTIME_DIRS` 把这里要删的目录
排除在快照之外，两边清单必须保持一致。改这里的 `ARCHIVE_KINDS` 时务必同步，
否则归档会「声称回收 33G、`du` 纹丝不动」。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..scoring_snapshot import _ATTEMPT_ROOT_RUNTIME_DIRS

# 删除清单：agent 私有运行时目录，内容可由镜像与依赖重建。
#
# 直接复用快照的排除清单，而不是各写一份：两者必须逐字一致，否则归档删的
# 文件仍被快照硬链接着，空间根本不会释放。用同一个常量让它不可能漂移。
ARCHIVE_KINDS = tuple(sorted(_ATTEMPT_ROOT_RUNTIME_DIRS))

# 归档前必须已经跑完的状态。running/queued/scoring 时目录仍在被写，
# 删它等于破坏正在进行的 attempt（需求 3.3）。
ACTIVE_STATUSES = frozenset({"queued", "running", "scoring"})


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_duration(raw: str) -> float:
    """把 `7d` / `12h` / `30m` 解析成秒。裸数字按天。"""
    text = raw.strip().lower()
    units = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}
    if text and text[-1] in units:
        return float(text[:-1]) * units[text[-1]]
    return float(text) * 86400.0


def _dir_size(path: Path) -> int:
    """目录占用字节数，不跟随符号链接。"""
    total = 0
    for entry in path.rglob("*"):
        if entry.is_symlink():
            continue
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            # 归档期间文件可能刚被别的进程删掉；统计缺一个不影响决策。
            continue
    return total


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def plan_archive(
    *,
    data_path: Path,
    db_path: Path,
    run_id: str | None = None,
    older_than: float | None = None,
) -> list[dict[str, Any]]:
    """列出可归档的 attempt 及其将被回收的目录。不做任何删除。"""
    attempts_root = Path(data_path) / "attempts"
    if not attempts_root.is_dir():
        return []

    rows: dict[str, dict[str, Any]] = {}
    if Path(db_path).exists():
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            query = "SELECT id, run_id, status, archived_at FROM attempts"
            params: tuple[Any, ...] = ()
            if run_id:
                query += " WHERE run_id=?"
                params = (run_id,)
            for row in conn.execute(query, params):
                rows[str(row[0])] = {
                    "run_id": row[1],
                    "status": str(row[2] or ""),
                    "archived_at": row[3],
                }

    cutoff = time.time() - older_than if older_than is not None else None
    plans: list[dict[str, Any]] = []
    for attempt_dir in sorted(attempts_root.iterdir()):
        if not attempt_dir.is_dir():
            continue
        attempt_id = attempt_dir.name
        meta = rows.get(attempt_id)

        if run_id and (meta is None or meta["run_id"] != run_id):
            continue
        # DB 里没有这行的目录不动：可能是别的部署留下的，宁可不删。
        if meta is None:
            continue
        if meta["status"] in ACTIVE_STATUSES:
            continue
        if cutoff is not None and attempt_dir.stat().st_mtime > cutoff:
            continue

        targets = []
        reclaim = 0
        for kind in ARCHIVE_KINDS:
            target = attempt_dir / kind
            if target.is_dir() and not target.is_symlink():
                size = _dir_size(target)
                targets.append({"kind": kind, "bytes": size})
                reclaim += size
        if not targets:
            continue
        plans.append(
            {
                "attempt_id": attempt_id,
                "run_id": meta["run_id"],
                "status": meta["status"],
                "already_archived_at": meta["archived_at"],
                "targets": targets,
                "reclaim_bytes": reclaim,
            }
        )
    return plans


def _force_removable(root: Path) -> None:
    """快照硬链接会把文件置为 0444；删除只要目录可写，但目录本身也可能只读。"""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        try:
            if path.is_dir():
                path.chmod(0o755)
        except OSError:
            continue


def apply_archive(
    *,
    data_path: Path,
    db_path: Path,
    plans: list[dict[str, Any]],
) -> dict[str, Any]:
    """执行归档，并把状态写回 attempts 表。"""
    attempts_root = Path(data_path) / "attempts"
    archived_at = _now_iso()
    removed_kinds: dict[str, list[str]] = {}
    reclaimed = 0
    failures: list[str] = []

    for plan in plans:
        attempt_id = str(plan["attempt_id"])
        attempt_dir = attempts_root / attempt_id
        done: list[str] = []
        for target in plan["targets"]:
            kind = str(target["kind"])
            path = attempt_dir / kind
            if not path.is_dir():
                continue
            try:
                _force_removable(path)
                shutil.rmtree(path)
            except OSError as exc:
                failures.append(f"{attempt_id}/{kind}: {exc}")
                continue
            done.append(kind)
            reclaimed += int(target["bytes"])
        if done:
            removed_kinds[attempt_id] = done

    if removed_kinds and Path(db_path).exists():
        # 标记必须尽最大努力写成：文件已经删了，这里失败就留下「空间被回收、
        # 却没有归档记录」的状态，而且 plan_archive 下次会跳过这些 attempt
        # （目标目录已不存在），没法靠重跑补救。主库是 WAL 且后端在写，
        # 不设 busy_timeout 会直接撞 "database is locked"。
        try:
            _write_archive_markers(db_path, removed_kinds, archived_at)
        except sqlite3.Error as exc:
            failures.append(f"archive markers: {exc}")

    return {
        "archived_attempts": len(removed_kinds),
        "reclaimed_bytes": reclaimed,
        "archived_at": archived_at,
        "failures": failures,
    }


def _write_archive_markers(
    db_path: Path, removed_kinds: dict[str, list[str]], archived_at: str
) -> None:
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        for attempt_id, kinds in removed_kinds.items():
            # 与既有归档记录合并：同一 attempt 可能分批归档不同类别。
            row = conn.execute(
                "SELECT archived_kinds FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            previous: list[str] = []
            if row and row[0]:
                try:
                    loaded = json.loads(row[0])
                    if isinstance(loaded, list):
                        previous = [str(x) for x in loaded]
                except json.JSONDecodeError:
                    previous = []
            merged = sorted({*previous, *kinds})
            conn.execute(
                "UPDATE attempts SET archived_at=?, archived_kinds=? WHERE id=?",
                (archived_at, json.dumps(merged, ensure_ascii=False), attempt_id),
            )
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.tools.archive_attempts")
    parser.add_argument("--data-path", default="./data")
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--run-id", default=None, help="只归档该 run 的 attempt")
    parser.add_argument(
        "--older-than",
        default=None,
        help="只归档 mtime 早于该时长的 attempt，如 7d / 12h",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="真正执行删除；不给则只打印计划（dry-run）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="显式 dry-run（默认行为，仅用于在脚本里表明意图）",
    )
    args = parser.parse_args(argv)

    data_path = Path(args.data_path)
    db_path = Path(args.db_path) if args.db_path else data_path / "octagon.db"
    older_than = parse_duration(args.older_than) if args.older_than else None

    plans = plan_archive(
        data_path=data_path,
        db_path=db_path,
        run_id=args.run_id,
        older_than=older_than,
    )
    total = sum(int(p["reclaim_bytes"]) for p in plans)

    # 先让人看见要删什么、能回收多少（需求 3.2）。
    for plan in plans:
        kinds = ", ".join(
            f"{t['kind']} {_human(int(t['bytes']))}" for t in plan["targets"]
        )
        print(f"{plan['attempt_id']}  [{plan['status']}]  {kinds}")
    print(
        f"-- {len(plans)} 个 attempt 可归档，预计回收 {_human(total)}",
    )

    if not args.yes or args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "attempts": len(plans),
                    "reclaim_bytes": total,
                    "hint": "加 --yes 执行",
                },
                ensure_ascii=False,
            )
        )
        return 0

    summary = apply_archive(data_path=data_path, db_path=db_path, plans=plans)
    print(json.dumps({"mode": "apply", **summary}, ensure_ascii=False))
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
