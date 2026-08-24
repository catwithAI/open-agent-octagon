/**
 * 轻量 i18n：Context + useI18n() + t(key)。
 *
 * - 语言状态存 localStorage（key: octagon.lang），默认跟随浏览器（zh-* → zh，其余 → en）。
 * - 文案字典见 messages.ts，扁平 key → { zh, en }。
 * - t(key, vars?) 取当前语言文案；缺失时回落到 key 本身（开发期即可看出漏翻）。
 *   vars 支持 {name} 占位替换。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { MESSAGES } from "./messages";

export type Lang = "zh" | "en";

const STORAGE_KEY = "octagon.lang";

function detectDefault(): Lang {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored === "zh" || stored === "en") return stored;
  } catch {
    /* localStorage 不可用时回落到浏览器语言 */
  }
  if (typeof navigator !== "undefined") {
    const nav = navigator.language || (navigator.languages && navigator.languages[0]) || "";
    if (nav.toLowerCase().startsWith("zh")) return "zh";
  }
  return "en";
}

type I18nContextValue = {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
};

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(detectDefault);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* 忽略持久化失败 */
    }
  }, []);

  useEffect(() => {
    // 反映到 <html lang="...">，利于可访问性与浏览器。
    if (typeof document !== "undefined") {
      document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    }
  }, [lang]);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>) => {
      const entry = MESSAGES[key];
      let text = entry ? entry[lang] : key;
      if (vars) {
        for (const [k, v] of Object.entries(vars)) {
          text = text.replace(new RegExp(`\\{${k}\\}`, "g"), String(v));
        }
      }
      return text;
    },
    [lang],
  );

  const value = useMemo<I18nContextValue>(() => ({ lang, setLang, t }), [lang, setLang, t]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    throw new Error("useI18n must be used within <I18nProvider>");
  }
  return ctx;
}
