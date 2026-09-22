import { useEffect, useState } from "react";

import { api, type JudgeRun } from "../api/client";
import { useI18n } from "../i18n";

// 单个 attempt 的 judge 重评 + 评分历史下拉。
//
// 行为：点「重新评分」→ 两步确认（重评会重新跑一次 judge，消耗 judge token）
// → 后端把该 attempt 重新入队评分；历史分数来自 append-only
// attempt_judge_runs（revision 1,2,3…，最新在前），绝不清除。
export function ReJudgePanel({
  runId,
  attemptId,
  scoringStatus,
  onRejudged,
}: {
  runId: string;
  attemptId: string;
  scoringStatus?: string;
  onRejudged?: () => void;
}) {
  const { t } = useI18n();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [judgeRuns, setJudgeRuns] = useState<JudgeRun[] | null>(null);

  const inFlight = scoringStatus === "queued" || scoringStatus === "running";
  const scoringDone =
    scoringStatus === "completed" || scoringStatus === "failed"
    || scoringStatus === "timed_out" || scoringStatus === "cancelled";

  const load = async () => {
    try {
      setJudgeRuns((await api.getJudgeRuns(runId, attemptId)).items);
    } catch {
      // 历史加载失败不阻塞重评主按钮；展开时显示空态
      setJudgeRuns([]);
    }
  };

  useEffect(() => {
    void load();
  }, [runId, attemptId]);

  // 评分结束（成功/失败）→ 刷新历史下拉
  useEffect(() => {
    if (scoringDone) void load();
  }, [scoringDone]);

  const rejudge = async () => {
    setBusy(true);
    setError("");
    try {
      await api.rejudgeAttempt(runId, attemptId);
      setConfirming(false);
      onRejudged?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rejudge-panel">
      <div className="rejudge-actions">
        {confirming ? (
          <>
            <button className="btn-ghost" disabled={busy} onClick={() => void rejudge()}>
              {t("runDetail.rejudge.confirm")}
            </button>
            <button className="btn-ghost" disabled={busy} onClick={() => setConfirming(false)}>
              {t("runDetail.rejudge.cancel")}
            </button>
          </>
        ) : (
          <button
            className="btn-ghost"
            disabled={busy || inFlight}
            title={t("runDetail.rejudge.title")}
            onClick={() => setConfirming(true)}
          >
            {busy
              ? t("runDetail.rejudge.busy")
              : inFlight
                ? t("runDetail.rejudge.disabledScoring")
                : t("runDetail.rejudge.button")}
          </button>
        )}
        <details className="rejudge-history">
          <summary>
            {t("runDetail.rejudge.history", { n: judgeRuns?.length ?? 0 })}
          </summary>
          {!judgeRuns || judgeRuns.length === 0 ? (
            <p className="muted">{t("runDetail.rejudge.historyEmpty")}</p>
          ) : (
            <ul className="rejudge-history-list">
              {judgeRuns.map((run) => (
                <li key={run.id}>
                  <span className="font-mono">
                    {t("runDetail.rejudge.rev", { n: run.score_revision })}
                  </span>
                  <span className="rejudge-history-score font-mono">{run.score_total}</span>
                  <span className="muted">{run.status}</span>
                  {run.judge_model && <span className="muted font-mono">{run.judge_model}</span>}
                  {run.rubric_version && <span className="muted font-mono">{run.rubric_version}</span>}
                  <span className="muted">{run.created_at}</span>
                </li>
              ))}
            </ul>
          )}
        </details>
      </div>
      {error && <div className="error" role="alert">{t("runDetail.rejudge.errorTitle")}: {error}</div>}
    </div>
  );
}
