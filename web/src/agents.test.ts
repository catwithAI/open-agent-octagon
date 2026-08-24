// agent catalog 的不变量。
//
// 这份名单原本散在三处（两个 ALL_AGENTS 数组 + 一张配色表），接第 7 家时
// 要同时改三个地方。合并后这里锁住配色相关的不变量。
//
// **「与后端 KNOWN_AGENTS 一致」由 `tests/test_agent_catalog_parity.py` 守**
// ——那是跨语言约束，放后端才能同时读到两边（前端 tsconfig 是浏览器端的，
// 没有 node 类型，为一条断言引入 @types/node 不划算）。

import { describe, expect, it } from "vitest";

import { AGENTS, AGENT_COLORS, AGENT_NAMES } from "./agents";

describe("agent catalog", () => {
  it("name 唯一且非空", () => {
    expect(new Set(AGENT_NAMES).size).toBe(AGENT_NAMES.length);
    expect(AGENT_NAMES.every((n) => n.length > 0)).toBe(true);
  });

  it("每家配色互不相同", () => {
    // 同色会让对比视图里两条线糊在一起，而这种缺陷肉眼要盯很久才发现。
    const colors = AGENTS.map((a) => a.color);
    expect(new Set(colors).size).toBe(colors.length);
  });

  it("配色表覆盖全部 agent", () => {
    for (const name of AGENT_NAMES) {
      expect(AGENT_COLORS[name], `${name} 缺配色`).toBeTruthy();
    }
  });

  it("配色都走 CSS 变量（不写死色值）", () => {
    // 写死 hex 会在主题切换时失配——调色板集中在 styles.css。
    for (const a of AGENTS) {
      expect(a.color, `${a.name} 没用 CSS 变量`).toMatch(/^var\(--[\w-]+\)$/);
    }
  });
});
