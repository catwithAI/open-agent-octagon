import { afterEach, describe, expect, it, vi } from "vitest";

import { createIdempotencyKey } from "./idempotency";

afterEach(() => vi.unstubAllGlobals());

describe("createIdempotencyKey", () => {
  it("uses randomUUID in secure browser contexts", () => {
    vi.stubGlobal("crypto", { randomUUID: () => "secure-context-uuid" });
    expect(createIdempotencyKey()).toBe("secure-context-uuid");
  });

  it("generates a UUID-shaped key when randomUUID is unavailable over HTTP", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(0xab);
        return bytes;
      },
    });

    expect(createIdempotencyKey()).toBe("abababab-abab-4bab-abab-abababababab");
  });
});
