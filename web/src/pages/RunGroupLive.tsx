import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../api/client";
import { initialGroupStreamState, reduceGroupStream } from "../api/groupStream";
import {
  discoverCapabilities,
  getRunGroup,
  researchMutation,
  runGroupStreamUrl,
} from "../api/researchClient";
import type { ResearchCapabilities } from "../api/researchTypes";
import { leaderReasonLabel, mutatorLabel, statusLabel } from "../researchUi";
import { useI18n } from "../i18n";

const PAGE_SIZE = 100;
const CONNECTION_LABEL_KEY = {
  connecting: "runGroupLive.connection.connecting",
  live: "runGroupLive.connection.live",
  reconnecting: "runGroupLive.connection.reconnecting",
};

type AttemptLabel = { agent: string; model: string | null };

function parseScope(scopeKey: string): { variantId: string; repeat: number } | null {
  const matched = /^variant:(.+)\/repeat:(\d+)$/.exec(scopeKey);
  return matched ? { variantId: matched[1], repeat: Number(matched[2]) } : null;
}

export function RunGroupLive() {
  const { t, lang } = useI18n();
  const shortModelLabel = (model: string | null): string => {
    if (!model) return t("runGroupLive.defaultModel");
    const parts = model.split("/");
    return parts.length > 2 ? parts.slice(-2).join("/") : model;
  };
  const { experimentId = "", groupId = "" } = useParams();
  const [capabilities, setCapabilities] = useState<ResearchCapabilities | null>(null);
  const [state, setState] = useState(initialGroupStreamState);
  const [connection, setConnection] = useState<"connecting" | "live" | "reconnecting">("connecting");
  const [page, setPage] = useState(0);
  const [selectedCellId, setSelectedCellId] = useState("");
  const [attemptLabels, setAttemptLabels] = useState<Record<string, AttemptLabel>>({});

  useEffect(() => {
    discoverCapabilities().then((result) => {
      if (result.ok) setCapabilities(result.value);
      else setState((current) => ({ ...current, protocolError: result.error }));
    });
  }, []);

  useEffect(() => {
    if (!capabilities?.features.run_groups) return;
    let alive = true;
    getRunGroup(experimentId, groupId).then((result) => {
      if (!alive) return;
      if (result.ok) setState((current) => ({
        ...current,
        snapshot: result.value,
        cursor: result.value.cursor,
        stale: false,
      }));
      else setState((current) => ({ ...current, protocolError: result.error }));
    });
    const source = new EventSource(runGroupStreamUrl(experimentId, groupId));
    source.onopen = () => setConnection("live");
    source.onerror = () => setConnection("reconnecting");
    source.onmessage = (event) => {
      try { setState((current) => reduceGroupStream(current, JSON.parse(event.data))); }
      catch { setConnection("reconnecting"); }
    };
    for (const eventName of ["snapshot", "group.created", "group.transition", "group.stopped",
      "cell.transition", "cell.projected", "cell.stopped", "leader_event"]) {
      source.addEventListener(eventName, (event) => {
        try { setState((current) => reduceGroupStream(current, JSON.parse((event as MessageEvent).data))); }
        catch { setConnection("reconnecting"); }
      });
    }
    return () => { alive = false; source.close(); };
  }, [capabilities, experimentId, groupId]);

  const cells = state.snapshot?.cells ?? [];
  const leaders = state.snapshot?.leaders ?? [];
  const visible = useMemo(
    () => cells.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
    [cells, page],
  );
  useEffect(() => {
    let alive = true;
    const runIds = new Set<string>();
    for (const leader of leaders) {
      if (!leader.current_attempt_id || attemptLabels[leader.current_attempt_id]) continue;
      const scope = parseScope(leader.scope_key);
      const cell = scope && cells.find((item) =>
        item.variant_id === scope.variantId && item.repeat_index === scope.repeat
      );
      if (cell?.run_id) runIds.add(cell.run_id);
    }
    if (runIds.size === 0) return () => { alive = false; };
    Promise.all([...runIds].map((runId) => api.getRun(runId).catch(() => null))).then((runs) => {
      if (!alive) return;
      setAttemptLabels((current) => {
        const next = { ...current };
        for (const run of runs) {
          for (const attempt of run?.attempts ?? []) {
            next[attempt.id] = { agent: attempt.agent_name, model: attempt.model };
          }
        }
        return next;
      });
    });
    return () => { alive = false; };
  }, [attemptLabels, cells, leaders]);

  const group = state.snapshot?.group;
  if (capabilities && !capabilities.features.run_groups) {
    return <div className="card" role="status">{t("runGroupLive.unsupported")}</div>;
  }
  if (!group) return <div role="status">{t("runGroupLive.loading")}</div>;
  const done = group.completed_cells + group.partial_cells + group.failed_cells + group.cancelled_cells;
  const progress = group.total_cells > 0 ? Math.round((done / group.total_cells) * 100) : 0;
  const selectedCell = cells.find((cell) => cell.id === selectedCellId) ?? cells[0];
  const repeats = [...new Set(visible.map((cell) => cell.repeat_index))].sort((left, right) => left - right);
  const variants = [...new Map(visible.map((cell) => [cell.variant_id, {
    id: cell.variant_id,
    label: cell.mutator_id ?? cell.variant_id,
  }])).values()];
  const finalLeaders = leaders.filter((leader) => !leader.provisional).length;
  const timeline = state.timeline.filter((event) => event.event_type === "leader_event");
  const scopeLabel = (scopeKey: string): string => {
    const scope = parseScope(scopeKey);
    if (!scope) return t("runGroupLive.currentCell");
    const cell = cells.find((item) =>
      item.variant_id === scope.variantId && item.repeat_index === scope.repeat
    );
    return t("runGroupLive.scopeLabel", {
      variant: mutatorLabel(cell?.mutator_id ?? "baseline", lang),
      repeat: scope.repeat + 1,
    });
  };

  return <div className="live-command-center">
    <nav className="experiment-subnav" aria-label={t("runGroupLive.aria.experimentView")}>
      <Link to={`/experiments/${experimentId}`}>{t("runGroupLive.nav.overview")}</Link>
      <Link className="is-active" to={`/experiments/${experimentId}/groups/${groupId}`}>{t("runGroupLive.nav.live")}</Link>
      <Link to={`/experiments/${experimentId}/groups/${groupId}/results`}>{t("runGroupLive.nav.results")}</Link>
    </nav>
    <header className="live-header">
      <span className={`live-status-tag status-${group.status}`}>{statusLabel(group.status, lang)}</span>
      <div className="live-title">
        <h2>{t("runGroupLive.title")}</h2>
        <p>{t("runGroupLive.subtitle", { connection: t(CONNECTION_LABEL_KEY[connection]) })}</p>
      </div>
      <div className="live-progress">
        <div><span>{t("runGroupLive.overallProgress")}</span><strong>{done}/{group.total_cells}</strong></div>
        <div className="live-progress-track" aria-label={t("runGroupLive.aria.progress", { pct: progress })}><span style={{ width: `${progress}%` }} /></div>
      </div>
      <button className="live-stop" onClick={() => capabilities && researchMutation(
        capabilities,
        "run_groups",
        `/api/experiments/${encodeURIComponent(experimentId)}/groups/${encodeURIComponent(groupId)}/stop`,
      )}>{t("runGroupLive.stop")}</button>
    </header>
    {(state.stale || state.protocolError) && <div className="live-alerts">
      {state.stale && <div className="warning" role="alert">{t("runGroupLive.staleAlert")}</div>}
      {state.protocolError && <div className="error" role="alert">{t("runGroupLive.protocolError", { message: state.protocolError.message })}</div>}
    </div>}

    <section className="live-leader-strip" aria-label={t("runGroupLive.aria.leaderStatus")}>
      <article className="live-leader-primary">
        <span>{t("runGroupLive.leaderPerCell")}</span>
        <div><strong>{leaders.length || "—"}</strong><small>{t("runGroupLive.leaderCount", { final: finalLeaders, provisional: leaders.length - finalLeaders })}</small></div>
        <p>{t("runGroupLive.leaderNote")}</p>
      </article>
      {leaders.slice(0, 3).map((leader) => {
        const candidate = leader.current_attempt_id
          ? attemptLabels[leader.current_attempt_id]
          : null;
        return <article className="live-leader-mini" key={leader.scope_key}>
          <span>{t(leader.provisional ? "runGroupLive.provisional" : "runGroupLive.final")} · {t("runGroupLive.taskScore")}</span>
          <strong>{leader.current_value ?? "—"}</strong>
          <small>{scopeLabel(leader.scope_key)}</small>
          <div className="live-leader-candidate">
            <b>{candidate?.agent ?? t("runGroupLive.candidateLoading")}</b>
            {candidate && <em>{shortModelLabel(candidate.model)}</em>}
          </div>
        </article>;
      })}
      {leaders.length === 0 && <article className="live-leader-empty">
        <span>{t("runGroupLive.waitingFirstScore")}</span><p>{t("runGroupLive.leaderFromServer")}</p>
      </article>}
    </section>

    <div className="live-workspace">
      <div className="live-main-column">
        <section className="live-matrix-panel">
          <div className="live-panel-head">
            <div><strong>{t("runGroupLive.executionMatrix")}</strong><span>{t("runGroupLive.cellCount", { visible: visible.length, total: cells.length })}</span></div>
            <div className="live-legend"><span className="queued">{t("runGroupLive.legend.queued")}</span><span className="running">{t("runGroupLive.legend.running")}</span><span className="completed">{t("runGroupLive.legend.completed")}</span><span className="failed">{t("runGroupLive.legend.failed")}</span></div>
          </div>
          <div className="live-matrix-scroll">
            <table className="live-matrix" aria-label={t("runGroupLive.aria.matrix")}>
              <thead><tr><th>{t("runGroupLive.col.variant")}</th>{repeats.map((repeat) => <th key={repeat}>{t("runGroupLive.repeat", { n: repeat + 1 })}</th>)}</tr></thead>
              <tbody>{variants.map((variant) => <tr key={variant.id}>
                <th scope="row"><strong>{mutatorLabel(variant.label, lang)}</strong></th>
                {repeats.map((repeat) => {
                  const cell = visible.find((item) => item.variant_id === variant.id && item.repeat_index === repeat);
                  return <td key={repeat}>{cell ? <button
                    className={`live-cell status-${cell.status}${selectedCell?.id === cell.id ? " is-selected" : ""}`}
                    onClick={() => setSelectedCellId(cell.id)}
                    aria-label={t("runGroupLive.aria.cell", { variant: variant.label, repeat: repeat + 1, status: statusLabel(cell.status, lang) })}
                  ><span>{statusLabel(cell.status, lang)}</span>{cell.error_code && <small>{cell.error_code}</small>}</button> : <span className="live-cell-missing">{t("runGroupLive.notPlanned")}</span>}</td>;
                })}
              </tr>)}</tbody>
            </table>
          </div>
          {cells.length > PAGE_SIZE && <div className="live-pagination">
            <button disabled={page === 0} onClick={() => setPage((value) => value - 1)}>{t("runGroupLive.prevPage")}</button>
            <span>{page + 1}/{Math.max(1, Math.ceil(cells.length / PAGE_SIZE))}</span>
            <button disabled={(page + 1) * PAGE_SIZE >= cells.length} onClick={() => setPage((value) => value + 1)}>{t("runGroupLive.nextPage")}</button>
          </div>}
        </section>

        <section className="live-timeline-panel">
          <div className="live-panel-head"><div><strong>{t("runGroupLive.leaderTimeline")}</strong><span>{t("runGroupLive.incrementalEvents")}</span></div></div>
          <div className="live-timeline">
            {timeline.map((event) => {
              const data = event.data as Record<string, unknown>;
              return <article key={event.sequence}>
                <span className="live-timeline-dot" />
                <div><strong>{scopeLabel(String(data.scope_key ?? ""))} · {leaderReasonLabel(String(data.reason ?? ""), lang)}</strong>
                  <p>{String(data.previous_value ?? "—")} → {String(data.current_value ?? "—")}
                    {data.delta !== null && data.delta !== undefined ? ` · Δ ${String(data.delta)}` : ""}</p></div>
                <time>{event.created_at ? new Date(event.created_at).toLocaleTimeString("zh-CN") : t("runGroupLive.justNow")}</time>
              </article>;
            })}
            {timeline.length === 0 && <div className="live-timeline-empty">{t("runGroupLive.noLeaderEvents")}</div>}
          </div>
        </section>
      </div>

      <aside className="live-inspector" aria-label={t("runGroupLive.aria.inspector")}>
        <div className="live-panel-head"><div><strong>{t("runGroupLive.inspector")}</strong><span>{selectedCell ? t("runGroupLive.selected") : t("runGroupLive.notSelected")}</span></div></div>
        {selectedCell ? <div className="live-inspector-body">
          <div className="live-inspector-status"><span className={`live-status-dot status-${selectedCell.status}`} /><strong>{statusLabel(selectedCell.status, lang)}</strong></div>
          <dl>
            <div><dt>{t("runGroupLive.inspector.variant")}</dt><dd>{mutatorLabel(selectedCell.mutator_id ?? "baseline", lang)}</dd></div>
            <div><dt>{t("runGroupLive.inspector.repeat")}</dt><dd>{selectedCell.repeat_index + 1}</dd></div>
            {selectedCell.error_code && <div><dt>{t("runGroupLive.inspector.error")}</dt><dd>{selectedCell.error_code}</dd></div>}
          </dl>
          {selectedCell.run_id
            ? <Link className="btn" to={`/runs/${selectedCell.run_id}`}>{t("runGroupLive.openEvidence")}</Link>
            : <p>{t("runGroupLive.runNotCreated")}</p>}
        </div> : <div className="live-inspector-empty">{t("runGroupLive.selectFromMatrix")}</div>}
      </aside>
    </div>
  </div>;
}
