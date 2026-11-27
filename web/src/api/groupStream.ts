import {
  boundary,
  parseRunGroupEvent,
  parseRunGroupSnapshot,
  type ResearchProtocolError,
  type RunGroupEvent,
  type RunGroupSnapshot,
} from "./researchTypes";

export type GroupStreamState = {
  snapshot: RunGroupSnapshot | null;
  cursor: number;
  stale: boolean;
  protocolError: ResearchProtocolError | null;
  timeline: RunGroupEvent[];
};

export const initialGroupStreamState: GroupStreamState = {
  snapshot: null,
  cursor: 0,
  stale: false,
  protocolError: null,
  timeline: [],
};

type ProjectResult =
  | { ok: true; value: RunGroupSnapshot }
  | { ok: false; error: ResearchProtocolError };

function projectEvent(snapshot: RunGroupSnapshot, event: RunGroupEvent): ProjectResult {
  const data = event.data as Record<string, unknown>;
  if (event.event_type.startsWith("cell.") && typeof data.cell_id === "string") {
    return {
      ok: true,
      value: {
        ...snapshot,
        cursor: event.sequence,
        cells: snapshot.cells.map((cell) => cell.id === data.cell_id ? {
          ...cell,
          ...(typeof data.status === "string" ? { status: data.status } : {}),
          ...(data.run_id === null || typeof data.run_id === "string" ? { run_id: data.run_id } : {}),
          ...(data.error_code === null || typeof data.error_code === "string"
            ? { error_code: data.error_code }
            : {}),
        } : cell),
      },
    };
  }
  if (event.event_type.startsWith("group.") && typeof data.status === "string") {
    return {
      ok: true,
      value: { ...snapshot, cursor: event.sequence, group: { ...snapshot.group, status: data.status } },
    };
  }
  if (event.event_type === "leader_event" && typeof data.scope_key === "string") {
    const parsed = boundary(parseRunGroupSnapshot, {
      ...snapshot,
      leaders: [data],
    });
    if (!parsed.ok) return { ok: false, error: parsed.error };
    const leader = parsed.value.leaders[0];
    if (!leader) return { ok: true, value: { ...snapshot, cursor: event.sequence } };
    return {
      ok: true,
      value: {
        ...snapshot,
        cursor: event.sequence,
        leaders: [
          ...snapshot.leaders.filter((item) => item.scope_key !== leader.scope_key),
          leader,
        ].sort((left, right) => left.scope_key.localeCompare(right.scope_key)),
      },
    };
  }
  return { ok: true, value: { ...snapshot, cursor: event.sequence } };
}

export function reduceGroupStream(
  state: GroupStreamState,
  rawEvent: unknown,
): GroupStreamState {
  const parsed = boundary(parseRunGroupEvent, rawEvent);
  if (!parsed.ok) return { ...state, protocolError: parsed.error };
  const event = parsed.value;
  if (event.event_type === "snapshot") {
    const snapshot = boundary(parseRunGroupSnapshot, event.data);
    if (!snapshot.ok) return { ...state, protocolError: snapshot.error };
    return {
      snapshot: snapshot.value,
      cursor: snapshot.value.cursor,
      stale: false,
      protocolError: null,
      timeline: [],
    };
  }
  if (event.sequence <= state.cursor) return state;
  if (event.sequence !== state.cursor + 1 || state.snapshot === null) {
    return { ...state, stale: true };
  }
  const projected = projectEvent(state.snapshot, event);
  if (!projected.ok) return { ...state, protocolError: projected.error };
  return {
    snapshot: projected.value,
    cursor: event.sequence,
    stale: false,
    protocolError: null,
    timeline: [...state.timeline, event].slice(-1000),
  };
}
