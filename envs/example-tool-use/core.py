"""example-tool-use —— 最小的 skill 类示例场景。

演示 env 如何通过 `octagon.env_api` 向 agent 暴露业务工具：这里是一个「便签本」，
agent 可以用 `add_note` 存条目、用 `list_notes` 查看已存条目。工具的副作用落在
该 attempt 专属的 env DB（`ctx.db`，schema 见 schema.sql），scorer 据此判分。

skill 类场景的业务状态**只**通过这些工具改变——这正是 OpenAgentOctagon 观测
「agent 用什么工具、遵不遵守约束」的地方。
"""

from __future__ import annotations

from typing import Any

from octagon.env_api import EnvContext, env_tool


@env_tool(
    name="add_note",
    description="把一条便签存进便签本。返回新便签的 id 和当前便签总数。",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "便签内容"},
        },
        "required": ["text"],
    },
)
def add_note(ctx: EnvContext, text: str) -> dict[str, Any]:
    cur = ctx.db.execute("INSERT INTO notes (text) VALUES (?)", (text,))
    ctx.db.commit()
    total = ctx.db.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    return {"id": cur.lastrowid, "total": total}


@env_tool(
    name="list_notes",
    description="列出便签本里所有便签，按存入顺序返回。",
    parameters={"type": "object", "properties": {}},
)
def list_notes(ctx: EnvContext) -> dict[str, Any]:
    rows = ctx.db.execute("SELECT id, text FROM notes ORDER BY id").fetchall()
    return {"notes": [{"id": r[0], "text": r[1]} for r in rows]}
