import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, type RunRow } from "../api/client";
import { useI18n } from "../i18n";

const PAGE_SIZE = 50;

type TFn = (key: string, vars?: Record<string, string | number>) => string;

function timeAgo(iso: string, t: TFn): string {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60_000);
  if (m < 1) return t("runList.justNow");
  if (m < 60) return t("runList.minutesAgo", { n: m });
  const h = Math.floor(m / 60);
  if (h < 24) return t("runList.hoursAgo", { n: h });
  return t("runList.daysAgo", { n: Math.floor(h / 24) });
}

export function RunList() {
  const { t } = useI18n();
  const [rows, setRows] = useState<RunRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await api.listRuns(PAGE_SIZE, page * PAGE_SIZE);
        if (!alive) return;
        setTotal(data.total);
        setRows(data.items);
      } catch (e) { if (alive) setErr(String(e)); }
    };
    tick();
    const id = setInterval(tick, 4000);
    return () => { alive = false; clearInterval(id); };
  }, [page]);

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  if (rows.length === 0 && !err && page === 0) {
    return (
      <div>
        <h2>{t("runList.title")}</h2>
        <div className="empty">
          <span>{t("runList.empty")}</span>
          <Link to="/" style={{ color: "var(--accent)", textDecoration: "none", fontSize: 13 }}>{t("runList.createFirst")}</Link>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h2>{t("runList.title")}</h2>
      {err && <p className="warning">{err}</p>}
      <table>
        <thead>
          <tr>
            <th>{t("runList.col.run")}</th>
            <th>{t("runList.col.env")}</th>
            <th>{t("runList.col.task")}</th>
            <th>{t("runList.col.agent")}</th>
            <th>{t("runList.col.createdAt")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.run_id}>
              <td>
                <Link to={`/runs/${r.run_id}`}>{r.run_id.slice(4, 16)}</Link>
                {r.compare_mode === "same-model" && (
                  <span style={{ marginLeft: 6, fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "var(--accent)", color: "#fff" }}>{t("runList.badge.sameModel")}</span>
                )}
                {r.compare_mode === "multi-model" && (
                  <span style={{ marginLeft: 6, fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "var(--blue)", color: "#fff" }}>{t("runList.badge.multiModel")}</span>
                )}
              </td>
              <td>{r.env_name}</td>
              <td style={{ fontFamily: "var(--mono)", fontSize: 12 }}>{r.task_id}</td>
              <td>
                {r.attempts ? r.attempts.map((a) => (
                  <span key={a.id} className="agent-chip" data-status={a.status}>
                    {r.compare_mode === "multi-model" ? (a.model ?? a.agent_name) : a.agent_name}
                    {a.score_total != null ? ` ${a.score_total}` : ""}
                  </span>
                )) : <span className="muted">{t("runList.attemptCount", { n: r.attempt_count })}</span>}
              </td>
              <td className="muted">{timeAgo(r.created_at, t)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {pageCount > 1 && (
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 12, fontSize: 13 }}>
          <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>{t("runList.prevPage")}</button>
          <span className="muted">{t("runList.pageInfo", { page: page + 1, total: pageCount, count: total })}</span>
          <button disabled={page >= pageCount - 1} onClick={() => setPage((p) => p + 1)}>{t("runList.nextPage")}</button>
        </div>
      )}
    </div>
  );
}
