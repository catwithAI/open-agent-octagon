import { useEffect, useState } from "react";

import {
  api,
  type AttemptDetail,
  type AttemptMoney,
  type RunCost,
  type RunCostStatus,
} from "../api/client";
import { useI18n } from "../i18n";

// 成本：priced=false 表示有令牌缺定价，展示的是**下界**，必须标注出来，
// 不能让读者把下界当准确成本。
export function costLabel(usd?: number | null, priced?: boolean | null): string {
  if (usd == null) return "-";
  const shown = usd < 0.01 ? `$${usd.toFixed(5)}` : `$${usd.toFixed(4)}`;
  return priced === false ? `≥${shown}` : shown;
}

// 资金口径：统计行里的「费用」只能是上游实扣。
// 拿不到实扣时显示原因（"结算中" / "经网关…"），**绝不**回落本地估算——
// 旧实现在这里显示 att.cost_usd（token × 价表估算），使同一个 run 的
// attempt 卡片与成本卡片给出相差一个数量级的两个金额。
export function MoneyStat({ money }: { money?: AttemptMoney }) {
  const { t } = useI18n();
  if (!money) return null;
  if (money.audited_cost_usd != null) {
    return <span>{t("runDetail.money.audited", { amount: costLabel(money.audited_cost_usd) })}</span>;
  }
  if (!money.unaudited_reason) return null;
  return <span className="cost-unaudited">{t("runDetail.money.unaudited", { reason: money.unaudited_reason })}</span>;
}

// token_cost_accounting：五维用量 + 四档成本明细。
//
// 设计要点：缓存复读单价通常只有 prompt 的 1/10，把"令牌占比"和"成本占比"
// 并排展示，才能一眼看出「令牌大头未必是成本大头」——实测 CC 一次 attempt
// 缓存复读占 99.8% 的令牌，却只占约 38% 的成本。
// 字段顺序有意义（rows 遍历用），值是 i18n messageKey。
const USAGE_FIELD_LABEL_KEYS: Record<string, string> = {
  input_tokens: "runDetail.usage.input_tokens",
  output_tokens: "runDetail.usage.output_tokens",
  cache_read_tokens: "runDetail.usage.cache_read_tokens",
  cache_write_tokens: "runDetail.usage.cache_write_tokens",
  reasoning_tokens: "runDetail.usage.reasoning_tokens",
};

// run 级成本状态的呈现规则。**settling 绝不展示临时 0**——
// 未结算的 0 会被读成"这次没花钱"，比不显示危险得多。
// text 存 i18n messageKey，渲染处 t(meta.text)。
const RUN_COST_STATUS: Record<
  RunCostStatus,
  { text: string; tone: string; showMoney: boolean }
> = {
  pending: { text: "runDetail.runCost.pending", tone: "neutral", showMoney: false },
  settling: { text: "runDetail.runCost.settling", tone: "neutral", showMoney: false },
  final: { text: "runDetail.runCost.final", tone: "good", showMoney: true },
  upper_bound: { text: "runDetail.runCost.upperBound", tone: "warn", showMoney: true },
  failed: { text: "runDetail.runCost.failed", tone: "bad", showMoney: false },
  incomplete: { text: "runDetail.runCost.incomplete", tone: "warn", showMoney: false },
  unavailable: { text: "runDetail.runCost.unavailable", tone: "neutral", showMoney: false },
};

function pct(value?: number | null): string {
  return value == null ? "-" : `${(value * 100).toFixed(1)}%`;
}

export function RunCostCard({ runId }: { runId: string }) {
  const { t } = useI18n();
  const [cost, setCost] = useState<RunCost | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    setCost(null);
    setFailed(false);
    api
      .getRunCost(runId)
      .then((c) => {
        if (!alive) return;
        setFailed(false);
        setCost(c);
      })
      .catch(() => alive && setFailed(true));
    return () => {
      alive = false;
    };
  }, [runId]);

  if (failed || !cost) return null;
  const meta = RUN_COST_STATUS[cost.status] ?? RUN_COST_STATUS.unavailable;
  const up = cost.upstream;
  const est = cost.estimate;
  // 估算全无、上游也没有 → 整张卡没有信息量，不渲染空面板。
  if (!meta.showMoney && est.total_cost_usd == null) return null;

  return (
    <>
      <h3 className="section-title">{t("runDetail.runCost.title")}</h3>
      <div className="score-detail-card">
        <div className="score-detail-header">
          <span>
            {t("runDetail.runCost.upstreamActual")}
            <span className={`cost-badge cost-badge-${meta.tone}`}>{t(meta.text)}</span>
          </span>
          <span className="score-detail-total font-mono">
            {meta.showMoney ? costLabel(up?.total_cost_usd) : "—"}
          </span>
        </div>

        {/* 本地估算是**对照值**，永远不标成实扣；priced=false 时保留 ≥ 下界语义。 */}
        <div className="score-dim-row">
          <div className="score-dim-header">
            <span className="score-dim-label">{t("runDetail.runCost.localEstimate")}</span>
            <span className="score-dim-val font-mono">
              {costLabel(est.total_cost_usd, est.priced)}
            </span>
            {cost.divergence_ratio != null && (
              <span className="score-dim-weight">{t("runDetail.runCost.divergence", { pct: pct(cost.divergence_ratio) })}</span>
            )}
          </div>
        </div>

        {meta.showMoney && up && (
          <div className="score-dim-row">
            <div className="score-dim-header">
              <span className="score-dim-label">{t("runDetail.runCost.execScoring")}</span>
              <span className="score-dim-val font-mono">
                {costLabel(up.execution_cost_usd)} / {costLabel(up.scoring_cost_usd)}
              </span>
              {cost.derived?.scoring_cost_ratio != null && (
                <span className="score-dim-weight">
                  {t("runDetail.runCost.scoringShare", { pct: pct(cost.derived.scoring_cost_ratio) })}
                </span>
              )}
            </div>
          </div>
        )}

        {meta.showMoney && up?.key_limit_usd != null && (
          <div className="score-dim-row">
            <div className="score-dim-header">
              <span className="score-dim-label">{t("runDetail.runCost.budget")}</span>
              <span className="score-dim-val font-mono">
                {costLabel(up.total_cost_usd)} / {costLabel(up.key_limit_usd)}
              </span>
              <span className="score-dim-weight">
                {pct(cost.derived?.budget_consumed_ratio)}
              </span>
            </div>
          </div>
        )}

        {meta.showMoney && cost.derived?.avg_cost_per_session_usd != null && (
          <div className="score-dim-detail">
            {/* 必须写明是均摊：N 个 session 共用一把 key，物理上拆不开到每个 session。 */}
            {t("runDetail.runCost.sessionAvg", { n: cost.derived.session_count, amount: costLabel(cost.derived.avg_cost_per_session_usd) })}
          </div>
        )}

        {cost.by_attempt && cost.by_attempt.length > 0 && (
          <>
            {cost.by_attempt.map((a) => (
              <div className="score-dim-row" key={a.attempt_id}>
                <div className="score-dim-header">
                  <span className="score-dim-label">{a.agent_name ?? a.attempt_id}</span>
                  <span className="score-dim-val font-mono">
                    {a.auditable ? costLabel(a.cost_usd) : "—"}
                  </span>
                  {!a.auditable && (
                    <span className="score-dim-weight">{a.status}</span>
                  )}
                </div>
                {a.activity?.by_model.map((item) => (
                  <div className="score-dim-detail" key={item.model}>
                    <span className="font-mono">{item.model}</span>
                    {" · "}
                    {costLabel(item.usage_usd)}
                    {" · "}
                    {t("runDetail.cost.requestsInline", { n: item.requests ?? "—" })}
                  </div>
                ))}
              </div>
            ))}
          </>
        )}
        {cost.status === "incomplete" && cost.partial_total_cost_usd != null && (
          <div className="score-dim-row">
            <div className="score-dim-header">
              <span className="score-dim-label">{t("runDetail.runCost.accountedPart")}</span>
              <span className="score-dim-val font-mono">
                {costLabel(cost.partial_total_cost_usd)}
              </span>
              <span className="score-dim-weight">{t("runDetail.runCost.excludesMissing")}</span>
            </div>
          </div>
        )}
        {(cost.unpriced_ledger_count ?? 0) > 0 && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.unpricedIssue", { n: cost.unpriced_ledger_count ?? 0 })}
          </div>
        )}
        {cost.missing_attempt_ids && cost.missing_attempt_ids.length > 0 && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.missingAttempts", { n: cost.missing_attempt_ids.length })}
          </div>
        )}
        {cost.judge_missing && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.judgeMissing")}
          </div>
        )}
        {cost.status === "upper_bound" && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.upperBoundIssue")}
          </div>
        )}
        {cost.status === "failed" && (
          <div className="score-primary-issue">
            {t("runDetail.runCost.failedIssue", { code: cost.error_code ? `（${cost.error_code}）` : "" })}
          </div>
        )}
        {est.attempts_missing_cost > 0 && (
          <div className="score-dim-detail">
            {t("runDetail.runCost.estimateMissing", { n: est.attempts_missing_cost })}
          </div>
        )}
      </div>
    </>
  );
}

export type LedgerRow = NonNullable<RunCost["by_attempt"]>[number];

const UPSTREAM_LABEL_KEYS: Record<string, string> = {
  prompt_tokens: "runDetail.upstream.prompt_tokens",
  completion_tokens: "runDetail.upstream.completion_tokens",
  reasoning_tokens: "runDetail.upstream.reasoning_tokens",
};

// 令牌与成本卡片。
//
// **金额只来自上游实扣**（逐 attempt 独立 key 的 usage 差值）——本地
// token×价格表估算已删除：实测高估 26× 且方向不一致（高估 3.5~4× 与
// 低估 5~10× 并存），留着只会让人以为能横向比较。
//
// **令牌来自本地采集**：五维完整，含上游 Activity **不提供**的
// cache read/write 两维。上游三维明细放在展开区做交叉核对。
export function CostCard({
  label,
  detail,
  ledger,
}: {
  label: string;
  detail?: AttemptDetail;
  ledger?: LedgerRow;
}) {
  const { t } = useI18n();
  const [showUpstream, setShowUpstream] = useState(false);
  const usage = detail?.token_usage ?? {};
  const rows = Object.keys(USAGE_FIELD_LABEL_KEYS).filter((f) => usage[f] != null);
  const totalTokens = rows.reduce((sum, f) => sum + (usage[f] ?? 0), 0);
  const up = detail?.upstream_tokens;

  // 金额：只认上游实扣。拿不到就显示"—"，绝不回落估算。
  const money = ledger?.auditable ? costLabel(ledger.cost_usd) : "—";
  const moneyNote = !ledger
    ? t("runDetail.cost.noLedger")
    : ledger.auditable
      ? null
      : ledger.status === "pending" || ledger.status === "settling"
        ? t("runDetail.cost.settling")
        : ledger.error_code === "gateway_bound_agent"
          ? t("runDetail.cost.gatewayBound")
          : ledger.status;

  return (
    <div className="score-detail-card">
      <div className="score-detail-header">
        <span>{label}</span>
        <span className="score-detail-total font-mono">{money}</span>
      </div>
      {moneyNote && <div className="score-verdict">{moneyNote}</div>}
      {detail?.cost?.model && (
        <div className="score-verdict font-mono">{detail.cost.model}</div>
      )}

      {rows.length === 0 && (
        <div className="score-dim-detail">
          {t("runDetail.cost.noTokenUsage")}
        </div>
      )}
      {rows.map((field) => {
        const tok = usage[field] ?? 0;
        const tokPct = totalTokens > 0 ? (tok / totalTokens) * 100 : 0;
        return (
          <div key={field} className="score-dim-row">
            <div className="score-dim-header">
              <span className="score-dim-label">{t(USAGE_FIELD_LABEL_KEYS[field])}</span>
              <span className="score-dim-val font-mono">
                {tok.toLocaleString()}
              </span>
            </div>
            <div className="score-dim-bar">
              <div
                className="score-dim-fill"
                style={{ width: `${Math.min(100, tokPct)}%`, background: "var(--text-2)" }}
              />
            </div>
            <div className="score-dim-detail">{t("runDetail.cost.tokenShare", { pct: tokPct.toFixed(1) })}</div>
          </div>
        );
      })}

      {rows.length > 0 && (
        <>
          <button
            type="button"
            className="cost-upstream-toggle"
            onClick={() => setShowUpstream((v) => !v)}
          >
            {showUpstream ? "▾" : "▸"} {t("runDetail.cost.upstreamDetail")}
          </button>
          {showUpstream && (
            <div className="cost-upstream-body">
              {up ? (
                <>
                  {(["prompt_tokens", "completion_tokens", "reasoning_tokens"] as const).map(
                    (k) => (
                      <div key={k} className="score-dim-header">
                        <span className="score-dim-label">{t(UPSTREAM_LABEL_KEYS[k])}</span>
                        <span className="score-dim-val font-mono">
                          {up[k] != null ? up[k]!.toLocaleString() : "—"}
                        </span>
                      </div>
                    ),
                  )}
                  {up.requests != null && (
                    <div className="score-dim-header">
                      <span className="score-dim-label">{t("runDetail.cost.requestCount")}</span>
                      <span className="score-dim-val font-mono">{up.requests}</span>
                    </div>
                  )}
                  {up.by_model.length > 0 && (
                    <>
                      <div className="score-dim-detail">
                        {t("runDetail.cost.upstreamBilledNote")}
                      </div>
                      {up.by_model.map((item) => (
                        <div className="score-dim-header" key={item.model}>
                          <span className="score-dim-label font-mono">{item.model}</span>
                          <span className="score-dim-val font-mono">
                            {t("runDetail.cost.upstreamRequests", { amount: costLabel(item.usage_usd), n: item.requests ?? "—" })}
                          </span>
                        </div>
                      ))}
                    </>
                  )}
                  <div className="score-dim-detail">
                    {t("runDetail.cost.upstreamThreeDims")}<strong>{t("runDetail.cost.cacheNotProvided")}</strong>{t("runDetail.cost.cacheLocalOnly")}
                  </div>
                </>
              ) : (
                <div className="score-dim-detail">
                  {t("runDetail.cost.noUpstreamYet")}<strong>{t("runDetail.cost.completedUtcDate")}</strong>{t("runDetail.cost.dayCloseNote")}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// run 级小结：只在有缺账时出现，说明缺的是哪几笔、为什么。
export function RunCostSummary({ cost }: { cost: RunCost }) {
  const { t } = useI18n();
  const unpriced = cost.unpriced_ledgers ?? [];
  const total = cost.upstream?.total_cost_usd;
  const partial = cost.partial_total_cost_usd;

  if (total != null) {
    return (
      <div className="cost-run-summary">
        {t("runDetail.costBanner.total")}<strong className="font-mono">{costLabel(total)}</strong>
        {cost.scoring_cost_ratio != null &&
          t("runDetail.costBanner.scoringShare", { pct: (cost.scoring_cost_ratio * 100).toFixed(1) })}
      </div>
    );
  }
  if (unpriced.length === 0) return null;

  // 区分"还在跑"和"永远拿不到"——前者等一等就有，后者是结构性的
  const pending = unpriced.filter((x) => x.pending);
  const blocked = unpriced.filter((x) => !x.pending);
  const sep = t("runDetail.common.listSep");
  return (
    <div className="cost-run-summary">
      {partial != null && (
        <>
          {t("runDetail.costBanner.accounted")}<strong className="font-mono">{costLabel(partial)}</strong>
          {t("runDetail.costBanner.notTotal")}
        </>
      )}
      {pending.length > 0 && (
        <>
          {t("runDetail.costBanner.pending", { agents: pending.map((x) => x.agent_name ?? t("runDetail.costBanner.scoring")).join(sep) })}
          {blocked.length > 0 && t("runDetail.costBanner.listSep")}
        </>
      )}
      {blocked.length > 0 && (
        <>
          {blocked
            .map(
              (x) =>
                t("runDetail.costBanner.blockedItem", {
                  agent: x.agent_name ?? t("runDetail.costBanner.scoring"),
                  reason: x.error_code === "gateway_bound_agent"
                    ? t("runDetail.costBanner.gatewayBound")
                    : x.error_code ?? x.status,
                }),
            )
            .join(sep)}
        </>
      )}
      {t("runDetail.common.period")}
    </div>
  );
}
