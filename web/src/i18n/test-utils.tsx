import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

import { I18nProvider, type Lang } from "./index";

function createMemoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, String(value));
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
    clear: () => {
      values.clear();
    },
    key: (index: number) => [...values.keys()][index] ?? null,
    get length() {
      return values.size;
    },
  };
}

export function setTestLocale(lang: Lang = "zh"): void {
  const storage = createMemoryStorage();
  storage.setItem("octagon.lang", lang);
  vi.stubGlobal("localStorage", storage);
}

export function renderWithI18n(
  ui: ReactElement,
  options?: RenderOptions & { lang?: Lang },
): RenderResult {
  const { lang = "zh", ...renderOptions } = options ?? {};
  setTestLocale(lang);
  return render(<I18nProvider>{ui}</I18nProvider>, renderOptions);
}
