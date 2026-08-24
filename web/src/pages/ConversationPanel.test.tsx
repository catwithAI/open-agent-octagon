// 验收：ConversationPanel 生产 DOM——turn 分段 + 五态 evaluation + capability
// gap，fixture 覆盖 main / sub-agent / unsupported / legacy / aggregate-only。
import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import type { AttemptConversation } from "../api/client";
import { ConversationPanel } from "./ConversationPanel";

afterEach(cleanup);

function conv(over: Partial<AttemptConversation> = {}): AttemptConversation {
  return {
    summary: {
      is_legacy: false,
      turn_count: 3,
      completed_turn_count: 3,
      session_continuity: "continuous",
      score_turn_id: "probe",
      partial: false,
      ...(over.summary ?? {}),
    },
    turns: over.turns ?? [
      turn("setup", 0, "setup", "completed"),
      turn("pressure", 1, "pressure", "completed"),
      turn("probe", 2, "probe", "completed"),
    ],
    evaluation: {
      compaction_status: "observed",
      compaction_count: 1,
      retention_score: 0.92,
      task_score: 84,
      observability_completeness: "complete",
      agent_scope: "main",
      limitations: [],
      ...(over.evaluation ?? {}),
    },
  };
}

function turn(id: string, idx: number, purpose: string, status: string): AttemptConversation["turns"][number] {
  return {
    turn_id: id,
    turn_index: idx,
    purpose,
    action: "send_message",
    producer_session_id: "s1",
    status,
    started_at: null,
    ended_at: null,
    prompt_bytes: 1000,
    prompt_hash: "sha256:x",
    error_code: null,
    error_summary: null,
  };
}

describe("ConversationPanel", () => {
  it("renders nothing without conversation", () => {
    const { container } = render(<ConversationPanel conversation={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("legacy attempt shows single-turn notice, no turn table", () => {
    render(<ConversationPanel conversation={conv({ summary: { is_legacy: true, turn_count: 1, session_continuity: "unknown" } as never })} />);
    expect(screen.getByText(/单轮任务/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("segments turns with purpose and score badge", () => {
    render(<ConversationPanel conversation={conv()} />);
    const rows = screen.getAllByRole("row").slice(1); // skip header
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveAttribute("data-turn-id", "setup");
    expect(rows[2]).toHaveAttribute("data-turn-id", "probe");
    // 评分轮标记只在 probe 行。
    expect(within(rows[2]).getByText("评分")).toBeInTheDocument();
    expect(within(rows[0]).queryByText("评分")).not.toBeInTheDocument();
  });

  it("observed status shows compaction count", () => {
    render(<ConversationPanel conversation={conv()} />);
    const badge = screen.getByText(/已观察到压缩/);
    expect(badge).toHaveAttribute("data-status", "observed");
    expect(screen.getByText("1 次")).toBeInTheDocument();
    expect(screen.getByText(/保真度：92%/)).toBeInTheDocument();
  });

  it("sub-agent scope is labeled", () => {
    render(<ConversationPanel conversation={conv({ evaluation: { agent_scope: "subagent" } as never })} />);
    expect(screen.getByText(/范围：子 agent/)).toBeInTheDocument();
  });

  it("unsupported + aggregate-only shows gap, no fake curve", () => {
    render(
      <ConversationPanel
        conversation={conv({
          evaluation: {
            compaction_status: "unsupported",
            compaction_count: 0,
            retention_score: null,
            task_score: null,
            observability_completeness: "partial",
            agent_scope: "main",
            limitations: ["aggregate-only-usage"],
          },
        })}
      />,
    );
    const badge = screen.getByText(/证据不足/);
    expect(badge).toHaveAttribute("data-status", "unsupported");
    // capability gap 可见。
    const gap = screen.getByText(/仅有累计用量/);
    expect(gap).toHaveAttribute("data-gap", "aggregate-only-usage");
    // 不展示"N 次"压缩计数（未 observed 不绘制伪边界）。
    expect(screen.queryByText(/次$/)).not.toBeInTheDocument();
    // 保真度/任务分缺失显示占位而非 0。
    expect(screen.getByText(/保真度：—/)).toBeInTheDocument();
  });

  it("not_observed_under_budget is neutral, not a failure", () => {
    render(
      <ConversationPanel
        conversation={conv({
          evaluation: {
            compaction_status: "not_observed_under_budget",
            compaction_count: 0,
            retention_score: 0.5,
            task_score: 70,
            observability_completeness: "complete",
            agent_scope: "main",
            limitations: [],
          },
        })}
      />,
    );
    const badge = screen.getByText(/预算内未触发/);
    expect(badge).toHaveAttribute("data-status", "not_observed_under_budget");
  });

  it("broken session continuity is flagged", () => {
    render(<ConversationPanel conversation={conv({ summary: { is_legacy: false, turn_count: 2, session_continuity: "broken" } as never })} />);
    expect(screen.getByText("会话断裂")).toBeInTheDocument();
  });

  it("failed turn shows error summary", () => {
    const t = turn("pressure", 1, "pressure", "failed");
    t.error_summary = "timeout after 30s";
    render(<ConversationPanel conversation={conv({ turns: [turn("setup", 0, "setup", "completed"), t] })} />);
    expect(screen.getByText(/timeout after 30s/)).toBeInTheDocument();
  });
});
