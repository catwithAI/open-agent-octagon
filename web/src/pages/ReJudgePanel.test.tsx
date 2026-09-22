// ReJudgePanel（judge 重评）的组件测试。
//
// 核心不变式：重评是**两步确认**（重新跑 judge 消耗 token）；评分在途时按钮
// disabled；评分历史来自 append-only attempt_judge_runs，最新 revision 在前；
// 后端拒绝时错误如实渲染，不吞。
import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import type { ComponentProps } from "react";
import { render, screen, cleanup, waitFor, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";

import { I18nProvider } from "../i18n";
import { api, type JudgeRun } from "../api/client";
import { ReJudgePanel } from "../components/ReJudgePanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

beforeEach(() => {
  // 语言态存 localStorage；先设成 zh，I18nProvider 挂载时 detectDefault 读到
  // 中文，断言才能匹配中文文案（其它既有组件测试因没包 provider 已在此环境失败）。
  localStorage.setItem("octagon.lang", "zh");
  // 挂载即拉历史；默认给空历史，避免真实 fetch 噪音
  vi.spyOn(api, "getJudgeRuns").mockResolvedValue({ attempt_id: "att1", items: [] });
});

function judgeRun(over: Partial<JudgeRun> = {}): JudgeRun {
  return {
    id: "jgr_1",
    score_revision: 1,
    score_total: 80,
    status: "completed",
    judge_model: "fake-judge",
    judge_prompt_version: "v1",
    rubric_version: "r1",
    manifest_ref: "m",
    scoring_job_id: "scj_1",
    dimensions: [],
    created_at: "2026-01-01T00:00:00",
    ...over,
  };
}

function mount(props: Partial<ComponentProps<typeof ReJudgePanel>> = {}) {
  render(
    <I18nProvider>
      <ReJudgePanel runId="run1" attemptId="att1" scoringStatus="completed" {...props} />
    </I18nProvider>,
  );
}

describe("ReJudgePanel", () => {
  it("评分历史下拉按最新 revision 在前展示", async () => {
    vi.spyOn(api, "getJudgeRuns").mockResolvedValue({
      attempt_id: "att1",
      items: [
        judgeRun({ id: "jgr_2", score_revision: 2, score_total: 90 }),
        judgeRun({ id: "jgr_1", score_revision: 1, score_total: 80 }),
      ],
    });
    mount();
    await waitFor(() => expect(screen.getByText("评分历史 (2)")).toBeInTheDocument());

    fireEvent.click(screen.getByText("评分历史 (2)"));
    await waitFor(() => expect(screen.getByText("第 1 次")).toBeInTheDocument());
    const revs = screen.getAllByText(/^第 \d+ 次$/).map((el) => el.textContent);
    expect(revs).toEqual(["第 2 次", "第 1 次"]);
    expect(screen.getByText("90")).toBeInTheDocument();
    expect(screen.getByText("80")).toBeInTheDocument();
  });

  it("重评需两步确认：确认后才调 rejudgeAttempt 并触发 onRejudged", async () => {
    const onRejudged = vi.fn();
    vi.spyOn(api, "rejudgeAttempt").mockResolvedValue({
      job_id: "scj_x",
      attempt_id: "att1",
      status: "queued",
    });
    mount({ onRejudged });

    await waitFor(() => expect(screen.getByText("重新评分")).toBeInTheDocument());
    // 第一次点只是进入确认态，不调接口
    fireEvent.click(screen.getByText("重新评分"));
    expect(api.rejudgeAttempt).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText("确认重评？"));
    await waitFor(() =>
      expect(api.rejudgeAttempt).toHaveBeenCalledWith("run1", "att1"),
    );
    await waitFor(() => expect(onRejudged).toHaveBeenCalledTimes(1));
  });

  it("后端拒绝时错误如实渲染", async () => {
    vi.spyOn(api, "rejudgeAttempt").mockRejectedValue(new Error("409: scoring_in_flight"));
    mount();
    await waitFor(() => expect(screen.getByText("重新评分")).toBeInTheDocument());

    fireEvent.click(screen.getByText("重新评分"));
    fireEvent.click(screen.getByText("确认重评？"));
    await waitFor(() =>
      expect(screen.getByText(/重评失败: 409: scoring_in_flight/)).toBeInTheDocument(),
    );
  });

  it("评分在途时重评按钮 disabled", async () => {
    mount({ scoringStatus: "running" });
    await waitFor(() => expect(screen.getByText("评分中")).toBeInTheDocument());
    expect(screen.getByText("评分中")).toBeDisabled();
  });
});
