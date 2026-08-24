import { describe, expect, it } from "vitest";

import { buildVariantSpecs } from "./ExperimentBuilder";

describe("ExperimentBuilder variant controls", () => {
  it("always keeps a single immutable baseline", () => {
    expect(buildVariantSpecs({})).toEqual([{
      schema_version: "octagon-variant-spec-v1",
      mutator: "baseline",
      version: "1",
      seed: 0,
      intensity: "identity",
      params: {},
    }]);
  });

  it("maps surface gear levels and instruction position to protocol fields", () => {
    const specs = buildVariantSpecs({
      spacing: "high",
      "letter-case": null,
      "unicode-homoglyph": "medium",
      "instruction-position": "shuffle",
    });

    expect(specs.map((item) => item.mutator)).toEqual([
      "baseline",
      "spacing",
      "unicode-homoglyph",
      "instruction-position",
    ]);
    expect(specs[1]).toMatchObject({ intensity: "high", params: {} });
    expect(specs[2]).toMatchObject({ intensity: "medium", params: {} });
    expect(specs[3]).toMatchObject({
      intensity: "default",
      params: { position: "shuffle" },
    });
  });
});
