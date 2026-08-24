// wire 观测曲线/分类的纯逻辑（可单测，无 DOM 依赖）。RunDetail 与测试共用。
import type { WireManifest, WireRecord, WireUsage } from "../api/client";

// null ≠ 0（canonical）：只把数值当有效点，其余（null/undefined）为断点。
export function usageValue(u: WireUsage | undefined, key: keyof WireUsage): number | null {
  const v = u?.[key];
  return typeof v === "number" ? v : null;
}

// 把一条 series 拆成断点分段：null 处断开，不画 0。
export function curveSegments(
  calls: WireRecord[],
  key: keyof WireUsage,
): Array<Array<[number, number]>> {
  const segs: Array<Array<[number, number]>> = [];
  let cur: Array<[number, number]> = [];
  calls.forEach((c, i) => {
    const v = usageValue(c.data?.usage, key);
    if (v == null) {
      if (cur.length) { segs.push(cur); cur = []; }
    } else {
      cur.push([i, v]);
    }
  });
  if (cur.length) segs.push(cur);
  return segs;
}

// matched（进曲线）= 非 unmatched；unmatched 单独分组（覆盖 llm+http）。
export function splitMatched(calls: WireRecord[], hops: WireRecord[]) {
  const matched = calls.filter((c) => c.correlation?.confidence !== "unmatched");
  const unmatched = [...calls, ...hops].filter(
    (c) => c.correlation?.confidence === "unmatched");
  return { matched, unmatched };
}

export type WireView =
  | { kind: "not_available" }
  | {
      kind: "available";
      status: string;
      hasCurve: boolean;
      curveCount: number;
      aggregateOnly: boolean;
      gaps: WireGap[];       // 结构化降级原因（保留 field/source/detail）
      unmatchedCount: number;
    };

// 结构化 gap：reason + 可选归属（哪个 source/field/具体原因）。
export type WireGap = {
  reason: string;            // 稳定标识（phase_degraded / conflicts / trunc_* / source / manifest gap）
  source?: string;          // 归属 source kind（source 类 gap）
  status?: string;          // source 状态
  failureReason?: string;   // source 的 failure_reason
  field?: string;           // manifest gap 的 field
  count?: number;           // 如 conflicts 数量
};

// 从 manifest + 加载结果推导视图状态（空态/降级态/曲线）——固定 fixture 验收。
export function deriveWireView(
  manifest: WireManifest | null,
  calls: WireRecord[],
  hops: WireRecord[],
  truncCalls: boolean,
  truncHops: boolean,
): WireView {
  const st = manifest?.status;
  if (!st || st === "not_available" || st === "not-applicable") {
    return { kind: "not_available" };
  }
  const aggregateOnly = (manifest?.sources ?? []).some(
    (s) => (s.capabilities as Record<string, unknown> | undefined)?.call_boundary === "aggregate-only");
  const { matched, unmatched } = splitMatched(calls, hops);
  const gaps: WireGap[] = [];
  if (manifest?.phase_attribution === "degraded") gaps.push({ reason: "phase_degraded" });
  if ((manifest?.totals?.conflicts ?? 0) > 0)
    gaps.push({ reason: "conflicts", count: manifest?.totals?.conflicts });
  for (const s of manifest?.sources ?? []) {
    if (s.status !== "complete")
      // 保留 failure_reason，用户能看到「哪个采集器为什么失败」
      gaps.push({ reason: "source", source: s.kind, status: s.status,
        failureReason: s.failure_reason ?? undefined });
  }
  for (const g of manifest?.gaps ?? [])
    // 保留 field，用户能看到「哪个字段」
    gaps.push({ reason: g.reason, field: g.field });
  if (truncCalls) gaps.push({ reason: "trunc_calls" });   // 评审 R6：分别标注
  if (truncHops) gaps.push({ reason: "trunc_hops" });
  return {
    kind: "available",
    status: st,
    hasCurve: matched.length > 0,
    curveCount: matched.length,
    aggregateOnly,
    gaps,
    unmatchedCount: unmatched.length,
  };
}
