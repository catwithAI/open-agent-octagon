-- example-tool-use 的 env DB schema。
-- Env Attempt Server 在首次访问该 attempt 的 DB 时执行本文件初始化。
CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    text    TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now'))
);
