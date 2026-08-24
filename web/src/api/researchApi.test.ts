import { afterEach, describe, expect, it, vi } from "vitest";

import { reduceGroupStream, initialGroupStreamState } from "./groupStream";
import {
  FeatureUnavailableError,
  _resetCapabilitiesCacheForTests,
  discoverCapabilities,
  researchMutation,
} from "./researchClient";
import { boundary, parseRunGroupSnapshot, type ResearchCapabilities } from "./researchTypes";

const capabilities: ResearchCapabilities = {
  schema_version: "octagon-capabilities-v1",
  features: { experiments: true, insights: false, future_unknown: true },
  details: {
    experiments: {
      enabled: true,
      schema_ready: true,
      dependencies_ready: true,
      unavailable_reasons: [],
    },
  },
};

const snapshot = {
  schema_version: "octagon-run-group-snapshot-v1",
  cursor: 4,
  group: {
    id: "grp_fixture",
    experiment_id: "exp_fixture",
    strategy: "full-matrix",
    status: "running",
    total_cells: 1,
    completed_cells: 0,
    partial_cells: 0,
    failed_cells: 0,
    cancelled_cells: 0,
  },
  cells: [{
    id: "cell_fixture",
    variant_id: "var_fixture",
    repeat_index: 0,
    run_id: null,
    status: "running",
    error_code: null,
  }],
};

const envelope = (sequence: number, event_type: string, data: Record<string, unknown>) => ({
  schema_version: "octagon-run-group-event-v1",
  sequence,
  event_type,
  data,
});

afterEach(() => {
  vi.restoreAllMocks();
  _resetCapabilitiesCacheForTests();
});

describe("research capability and DTO boundary", () => {
  it("accepts partial and unknown capabilities without assuming they are disabled", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(capabilities)));
    const result = await discoverCapabilities();
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.value.features.insights).toBe(false);
      expect(result.value.features.future_unknown).toBe(true);
      expect(result.value.features.run_groups).toBeUndefined();
    }
  });

  it("dedupes concurrent discoverCapabilities calls into a single fetch", async () => {
    // Regression for the perf bug where AppShell + every page independently
    // called discoverCapabilities(), firing a duplicate /api/capabilities
    // request on every navigation.
    const request = vi.spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify(capabilities)));
    const [a, b, c] = await Promise.all([
      discoverCapabilities(), discoverCapabilities(), discoverCapabilities(),
    ]);
    expect(request).toHaveBeenCalledTimes(1);
    expect(a).toEqual(b);
    expect(b).toEqual(c);
  });

  it("refetches discoverCapabilities after a failed call instead of caching the rejection", async () => {
    const request = vi.spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce(new Response(JSON.stringify(capabilities)));
    await expect(discoverCapabilities()).rejects.toThrow("network down");
    const result = await discoverCapabilities();
    expect(request).toHaveBeenCalledTimes(2);
    expect(result.ok).toBe(true);
  });

  it("returns a renderable protocol error instead of throwing from the boundary", () => {
    const result = boundary(parseRunGroupSnapshot, { ...snapshot, cursor: "bad" });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error.path).toBe("$snapshot.cursor");
  });

  it("does not send a mutation when its capability is false", async () => {
    const request = vi.spyOn(globalThis, "fetch");
    await expect(researchMutation(capabilities, "insights", "/api/nope"))
      .rejects.toBeInstanceOf(FeatureUnavailableError);
    expect(request).not.toHaveBeenCalled();
  });
});

describe("run group SSE reducer", () => {
  it("ignores duplicate/out-of-order events and detects a gap", () => {
    let state = reduceGroupStream(
      initialGroupStreamState,
      envelope(4, "snapshot", snapshot),
    );
    state = reduceGroupStream(
      state,
      envelope(5, "cell.transition", { cell_id: "cell_fixture", status: "completed" }),
    );
    expect(state.cursor).toBe(5);
    expect(state.snapshot?.cells[0].status).toBe("completed");

    const duplicate = reduceGroupStream(
      state,
      envelope(5, "cell.transition", { cell_id: "cell_fixture", status: "failed" }),
    );
    expect(duplicate).toBe(state);
    const older = reduceGroupStream(state, envelope(3, "group.transition", { status: "failed" }));
    expect(older).toBe(state);
    const gap = reduceGroupStream(state, envelope(7, "group.transition", { status: "completed" }));
    expect(gap.stale).toBe(true);
    expect(gap.cursor).toBe(5);
  });

  it("uses a reconnect snapshot as the authoritative projection", () => {
    const stale = {
      ...initialGroupStreamState,
      snapshot: parseRunGroupSnapshot(snapshot),
      cursor: 5,
      stale: true,
    };
    const refreshed = { ...snapshot, cursor: 8, group: { ...snapshot.group, status: "completed" } };
    const state = reduceGroupStream(stale, envelope(8, "snapshot", refreshed));
    expect(state.stale).toBe(false);
    expect(state.cursor).toBe(8);
    expect(state.snapshot?.group.status).toBe("completed");
  });

  it("projects leader events and replaces the latest state for the same scope", () => {
    let state = reduceGroupStream(initialGroupStreamState, envelope(4, "snapshot", snapshot));
    const leader = {
      schema_version: "octagon-leader-event-v1",
      event_id: "ldr_first",
      scope_key: "variant:var_fixture/repeat:0",
      sequence: 1,
      metric: "task_score",
      previous_attempt_id: null,
      current_attempt_id: "att_codex",
      previous_value: null,
      current_value: 72,
      delta: null,
      reason: "first",
      provisional: true,
      source_score_fingerprint: "sha256:fixture",
      producer_version: "fixture",
      source_outbox_id: "out_first",
    };
    state = reduceGroupStream(state, envelope(5, "leader_event", leader));
    expect(state.snapshot?.leaders).toHaveLength(1);
    expect(state.snapshot?.leaders[0].current_value).toBe(72);

    state = reduceGroupStream(state, envelope(6, "leader_event", {
      ...leader,
      event_id: "ldr_final",
      sequence: 2,
      current_attempt_id: "att_claude",
      previous_attempt_id: "att_codex",
      previous_value: 72,
      current_value: 86,
      delta: 14,
      reason: "scope-finalized",
      provisional: false,
      source_outbox_id: "out_final",
    }));
    expect(state.snapshot?.leaders).toHaveLength(1);
    expect(state.snapshot?.leaders[0].current_attempt_id).toBe("att_claude");
    expect(state.snapshot?.leaders[0].provisional).toBe(false);
  });

  it("captures malformed SSE without discarding the last good snapshot", () => {
    const ready = reduceGroupStream(initialGroupStreamState, envelope(4, "snapshot", snapshot));
    const broken = reduceGroupStream(ready, { sequence: "bad" });
    expect(broken.snapshot).toEqual(ready.snapshot);
    expect(broken.protocolError).not.toBeNull();
  });
});
