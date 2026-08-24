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

function projectEvent(snapshot: RunGroupSnapshot, event: RunGroupEvent): RunGroupSnapshot {
  const data = event.data as Record<string, unknown>;
  if (event.event_type.startsWith("cell.") && typeof data.cell_id === "string") {
    return {
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
    };
  }
  if (event.event_type.startsWith("group.") && typeof data.status === "string") {
    return { ...snapshot, cursor: event.sequence, group: { ...snapshot.group, status: data.status } };
  }
  if (event.event_type === "leader_event" && typeof data.scope_key === "string") {
    const parsed = parseRunGroupSnapshot({
      ...snapshot,
      leaders: [data],
    }).leaders[0];
    return {
      ...snapshot,
      cursor: event.sequence,
      leaders: [
        ...snapshot.leaders.filter((leader) => leader.scope_key !== parsed.scope_key),
        parsed,
      ].sort((left, right) => left.scope_key.localeCompare(right.scope_key)),
    };
  }
  return { ...snapshot, cursor: event.sequence };
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
  return {
    snapshot: projectEvent(state.snapshot, event),
    cursor: event.sequence,
    stale: false,
    protocolError: null,
    timeline: [...state.timeline, event].slice(-1000),
  };
}
