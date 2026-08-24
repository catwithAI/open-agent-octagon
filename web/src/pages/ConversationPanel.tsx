// 多轮 conversation 展示——turn 分段 + 压缩评测状态 + capability gap。
//
// 数据来自 attempt detail 的 conversation 块：summary / turns / evaluation。
// 关键约束：
// - 五种 evaluation 状态显式区分（observed / not_observed_under_budget /
//   unsupported / incomplete / insufficient_calls），不把"未触发"当失败；
// - capability gap（limitations）必须可见（aggregate-only、unattributed、session
//   broken 等）；
// - aggregate-only / unsupported 时**不绘制伪调用曲线**——只展示状态与 gap。
import type { AttemptConversation, CompactionStatus } from "../api/client";
import { useI18n } from "../i18n";

const STATUS_LABEL_KEY: Record<CompactionStatus, string> = {
  observed: "conversationPanel.status.observed",
  not_observed_under_budget: "conversationPanel.status.notObservedUnderBudget",
  unsupported: "conversationPanel.status.unsupported",
  incomplete: "conversationPanel.status.incomplete",
  insufficient_calls: "conversationPanel.status.insufficientCalls",
};

// 状态语气：positive=检出、neutral=如实未触发、warn=证据缺口。用 class 而非颜色
// 硬编码，配色走 styles.css。
const STATUS_TONE: Record<CompactionStatus, "ok" | "neutral" | "warn"> = {
  observed: "ok",
  not_observed_under_budget: "neutral",
  unsupported: "warn",
  incomplete: "warn",
  insufficient_calls: "neutral",
};

const CONTINUITY_LABEL_KEY: Record<string, string> = {
  continuous: "conversationPanel.continuity.continuous",
  broken: "conversationPanel.continuity.broken",
  unknown: "conversationPanel.continuity.unknown",
};

const LIMITATION_LABEL_KEY: Record<string, string> = {
  "aggregate-only-usage": "conversationPanel.limitation.aggregateOnlyUsage",
  "subagent-identity-unattributed": "conversationPanel.limitation.subagentUnattributed",
  "session-continuity-broken": "conversationPanel.limitation.sessionContinuityBroken",
  "capture-incomplete": "conversationPanel.limitation.captureIncomplete",
  "pressure-below-declared-window": "conversationPanel.limitation.pressureBelowWindow",
};

export function ConversationPanel({ conversation }: { conversation?: AttemptConversation }) {
  const { t } = useI18n();
  if (!conversation) {
    return null;
  }
  const { summary, turns, evaluation } = conversation;

  const limitationText = (raw: string): string =>
    LIMITATION_LABEL_KEY[raw] ? t(LIMITATION_LABEL_KEY[raw]) : raw;

  // legacy 单轮 attempt：无多轮 conversation，只提示，不渲染空 turn 表。
  if (summary.is_legacy) {
    return (
      <section className="conversation-panel" aria-label={t("conversationPanel.aria.turns")}>
        <div className="wire-section-title">{t("conversationPanel.turnsTitle")}</div>
        <p className="conversation-legacy">{t("conversationPanel.legacy")}</p>
      </section>
    );
  }

  return (
    <section className="conversation-panel" aria-label={t("conversationPanel.aria.turns")}>
      <div className="wire-section-title">
        {t("conversationPanel.turnsProgress", {
          completed: summary.completed_turn_count ?? 0,
          total: summary.turn_count,
        })}
      </div>

      <div className="conversation-summary-row">
        <span className={`chip chip-${summary.session_continuity === "broken" ? "warn" : "neutral"}`}>
          {CONTINUITY_LABEL_KEY[summary.session_continuity]
            ? t(CONTINUITY_LABEL_KEY[summary.session_continuity])
            : summary.session_continuity}
        </span>
        {summary.score_turn_id && (
          <span className="chip chip-neutral">{t("conversationPanel.scoreTurn", { id: summary.score_turn_id })}</span>
        )}
        {summary.partial && <span className="chip chip-warn">{t("conversationPanel.traceTruncated")}</span>}
      </div>

      <TurnTable turns={turns} scoreTurnId={summary.score_turn_id ?? null} />

      <EvaluationCard evaluation={evaluation} limitationText={limitationText} />
    </section>
  );
}

function TurnTable({
  turns,
  scoreTurnId,
}: {
  turns: AttemptConversation["turns"];
  scoreTurnId: string | null;
}) {
  const { t } = useI18n();
  if (turns.length === 0) {
    return <p className="conversation-empty">{t("conversationPanel.noTurnRecords")}</p>;
  }
  return (
    <table className="conversation-turns">
      <thead>
        <tr>
          <th>#</th>
          <th>{t("conversationPanel.col.turn")}</th>
          <th>{t("conversationPanel.col.purpose")}</th>
          <th>{t("conversationPanel.col.status")}</th>
          <th>{t("conversationPanel.col.prompt")}</th>
        </tr>
      </thead>
      <tbody>
        {turns.map((t2) => (
          <tr key={t2.turn_id} data-turn-id={t2.turn_id}>
            <td>{t2.turn_index ?? "?"}</td>
            <td>
              {t2.turn_id}
              {t2.turn_id === scoreTurnId && <span className="chip chip-ok mini">{t("conversationPanel.scoreTag")}</span>}
            </td>
            <td>{t2.purpose ?? "—"}</td>
            <td>
              <span className={`chip chip-${turnTone(t2.status)} mini`}>{turnStatusLabel(t2.status, t)}</span>
              {t2.status === "failed" && t2.error_summary && (
                <span className="turn-error"> · {t2.error_summary}</span>
              )}
            </td>
            <td>{t2.prompt_bytes != null ? `${t2.prompt_bytes} B` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function turnStatusLabel(status: string, t: (k: string) => string): string {
  switch (status) {
    case "completed":
      return t("conversationPanel.turnStatus.completed");
    case "failed":
      return t("conversationPanel.turnStatus.failed");
    case "interaction_answered":
      return t("conversationPanel.turnStatus.answered");
    case "started":
      return t("conversationPanel.turnStatus.started");
    default:
      return status;
  }
}

function turnTone(status: string): "ok" | "neutral" | "warn" {
  if (status === "completed" || status === "interaction_answered") return "ok";
  if (status === "failed") return "warn";
  return "neutral";
}

function EvaluationCard({
  evaluation,
  limitationText,
}: {
  evaluation: AttemptConversation["evaluation"];
  limitationText: (raw: string) => string;
}) {
  const { t } = useI18n();
  const tone = STATUS_TONE[evaluation.compaction_status];
  // aggregate-only / 证据不足：不绘制任何调用曲线（那会伪造边界）——只显示状态 + gap。
  const showsGaps = evaluation.limitations.length > 0;
  return (
    <div className="conversation-evaluation" aria-label={t("conversationPanel.aria.compaction")}>
      <div className="conversation-eval-header">
        <span className={`chip chip-${tone}`} data-status={evaluation.compaction_status}>
          {t(STATUS_LABEL_KEY[evaluation.compaction_status])}
        </span>
        {evaluation.compaction_status === "observed" && (
          <span className="chip chip-neutral">{t("conversationPanel.compactionCount", { count: evaluation.compaction_count })}</span>
        )}
        <span className="chip chip-neutral">{t("conversationPanel.scope", { scope: scopeLabel(evaluation.agent_scope, t) })}</span>
        <span className={`chip chip-${evaluation.observability_completeness === "complete" ? "ok" : "warn"}`}>
          {t("conversationPanel.evidence", { level: completenessLabel(evaluation.observability_completeness, t) })}
        </span>
      </div>

      <div className="conversation-eval-scores">
        <span>
          {t("conversationPanel.retention", {
            value: evaluation.retention_score != null ? `${Math.round(evaluation.retention_score * 100)}%` : "—",
          })}
        </span>
        <span>{t("conversationPanel.taskScore", { value: evaluation.task_score != null ? evaluation.task_score : "—" })}</span>
      </div>

      {showsGaps && (
        <div className="conversation-eval-gaps" aria-label={t("conversationPanel.aria.capabilityGap")}>
          <span className="gap-title">{t("conversationPanel.capabilityGapTitle")}</span>
          {evaluation.limitations.map((l) => (
            <span key={l} className="chip chip-warn mini" data-gap={l}>
              {limitationText(l)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function scopeLabel(scope: string, t: (k: string) => string): string {
  switch (scope) {
    case "main":
      return t("conversationPanel.scopeLabel.main");
    case "subagent":
      return t("conversationPanel.scopeLabel.subagent");
    case "mixed":
      return t("conversationPanel.scopeLabel.mixed");
    default:
      return t("conversationPanel.scopeLabel.none");
  }
}

function completenessLabel(c: string, t: (k: string) => string): string {
  switch (c) {
    case "complete":
      return t("conversationPanel.completeness.complete");
    case "partial":
      return t("conversationPanel.completeness.partial");
    default:
      return t("conversationPanel.completeness.incomplete");
  }
}
