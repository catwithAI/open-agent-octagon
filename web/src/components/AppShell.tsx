import { useEffect, useMemo, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";

import { discoverCapabilities } from "../api/researchClient";
import { useI18n } from "../i18n";

type IconName = "boxes" | "dashboard" | "flask" | "menu" | "plus" | "sliders" | "x";

const ICON_PATHS: Record<IconName, ReactNode> = {
  boxes: <><path d="m12 2 4.5 2.5L12 7 7.5 4.5 12 2Z" /><path d="m4.5 8 4.5 2.5L4.5 13 0 10.5 4.5 8Zm15 0 4.5 2.5-4.5 2.5-4.5-2.5L19.5 8ZM12 13l4.5 2.5L12 18l-4.5-2.5L12 13Z" /></>,
  dashboard: <><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /></>,
  flask: <><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3" /><path d="M8 15h8" /></>,
  menu: <><path d="M4 7h16M4 12h16M4 17h16" /></>,
  plus: <><rect x="3" y="3" width="18" height="18" /><path d="M12 8v8M8 12h8" /></>,
  sliders: <><path d="M4 6h6M14 6h6M4 12h10M18 12h2M4 18h2M10 18h10" /><circle cx="12" cy="6" r="2" /><circle cx="16" cy="12" r="2" /><circle cx="8" cy="18" r="2" /></>,
  x: <><path d="m6 6 12 12M18 6 6 18" /></>,
};

function ShellIcon({ name }: { name: IconName }) {
  return <svg className="shell-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="square" strokeLinejoin="miter">
    {ICON_PATHS[name]}
  </svg>;
}

const ROUTE_LABELS: Array<[RegExp, string]> = [
  [/^\/overview$/, "crumb.overview"],
  [/^\/experiments\/new$/, "crumb.experimentNew"],
  [/^\/experiments\/[^/]+\/groups\/[^/]+\/results$/, "crumb.experimentResults"],
  [/^\/experiments\/[^/]+\/groups\/[^/]+$/, "crumb.experimentLive"],
  [/^\/experiments\/[^/]+\/insights$/, "crumb.experimentInsights"],
  [/^\/experiments\/[^/]+$/, "crumb.experimentDetail"],
  [/^\/experiments$/, "crumb.experiments"],
  [/^\/scenarios/, "crumb.scenarios"],
  [/^\/profiles/, "crumb.profiles"],
  [/^\/system/, "crumb.system"],
  [/^\/runs\/[^/]+$/, "crumb.runDetail"],
  [/^\/runs$/, "crumb.runs"],
  [/^\/same-model$/, "crumb.sameModel"],
  [/^\/multi-model$/, "crumb.multiModel"],
  [/^\/$/, "crumb.multiAgent"],
];

function routeLabelKey(pathname: string) {
  return ROUTE_LABELS.find(([pattern]) => pattern.test(pathname))?.[1] ?? null;
}

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { t, lang, setLang } = useI18n();
  const [researchEnabled, setResearchEnabled] = useState<boolean | null>(null);
  const [enabledCount, setEnabledCount] = useState(0);
  const [featureCount, setFeatureCount] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const crumbKey = useMemo(() => routeLabelKey(location.pathname), [location.pathname]);
  const crumb = crumbKey ? t(crumbKey) : "OCTAGON";

  useEffect(() => {
    discoverCapabilities().then((result) => {
      const features = result.ok ? Object.values(result.value.features) : [];
      setResearchEnabled(result.ok && result.value.features.experiments === true);
      setEnabledCount(features.filter(Boolean).length);
      setFeatureCount(features.length);
    }).catch(() => setResearchEnabled(false));
  }, []);

  useEffect(() => setMenuOpen(false), [location.pathname]);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [menuOpen]);

  const navLink = (to: string, label: string, icon: IconName, end = false) =>
    <NavLink to={to} end={end} className={({ isActive }) => `shell-nav-link${isActive ? " is-active" : ""}`}>
      <ShellIcon name={icon} /><span>{label}</span>
    </NavLink>;

  return <div className="app-shell">
    <a className="skip-link" href="#main-content">{t("shell.skipToContent")}</a>
    <button
      className={`shell-backdrop${menuOpen ? " is-open" : ""}`}
      aria-label={t("shell.closeNav")}
      aria-hidden={!menuOpen}
      tabIndex={menuOpen ? 0 : -1}
      onClick={() => setMenuOpen(false)}
    />
    <aside className={`shell-sidebar${menuOpen ? " is-open" : ""}`} aria-label={t("shell.mainNav")}>
      <div className="shell-brand-row">
        <NavLink className="shell-brand" to="/overview" aria-label={t("shell.brandAria")}>
          <span className="shell-brand-glyph">OC</span><span>OCTAGON</span>
        </NavLink>
        <button className="shell-close" aria-label={t("shell.closeNav")} onClick={() => setMenuOpen(false)}>
          <ShellIcon name="x" />
        </button>
      </div>
      <nav className="shell-nav">
        {navLink("/overview", t("shell.nav.overview"), "dashboard", true)}
        {navLink("/experiments/new", t("shell.nav.newExperiment"), "plus", true)}
        {navLink("/experiments", t("shell.nav.experiments"), "flask", true)}
        <div className="shell-nav-spacer" />
        <div className="shell-nav-section">{t("shell.nav.resources")}</div>
        {navLink("/scenarios", t("shell.nav.scenarios"), "boxes")}
        {navLink("/profiles", t("shell.nav.profiles"), "sliders")}
      </nav>
      <div className="shell-sidebar-fill" />
      <details className="shell-legacy">
        <summary>{t("shell.legacy.summary")}</summary>
        <div>
          <NavLink to="/">{t("shell.legacy.multiAgent")}</NavLink>
          <NavLink to="/same-model">{t("shell.legacy.sameModel")}</NavLink>
          <NavLink to="/multi-model">{t("shell.legacy.multiModel")}</NavLink>
          <NavLink to="/runs">{t("shell.legacy.runs")}</NavLink>
        </div>
      </details>
      <div className="shell-capability">
        <div><span className={`shell-status-dot${researchEnabled === false ? " is-warning" : ""}`} />
          {researchEnabled === null ? t("shell.capability.negotiating") : researchEnabled ? t("shell.capability.available") : t("shell.capability.limited")}
        </div>
        <small>{t("shell.capability.version")}{featureCount > 0 ? ` · ${t("shell.capability.enabled", { n: enabledCount, total: featureCount })}` : ""}</small>
      </div>
    </aside>
    <div className="shell-workspace">
      <header className="shell-topbar">
        <button
          className="shell-menu"
          aria-label={t("shell.openNav")}
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen(true)}
        ><ShellIcon name="menu" /></button>
        <span className="shell-mobile-brand">OCTAGON</span>
        <span className="shell-crumb">{crumb}</span>
        <div className="shell-topbar-actions">
          <NavLink className="shell-new-experiment" to="/experiments/new"><span>＋</span> {t("shell.newExperiment")}</NavLink>
          <div className="shell-language" role="group" aria-label={t("shell.lang.aria")}>
            <button
              type="button"
              className={`shell-language-btn${lang === "zh" ? " is-active" : ""}`}
              aria-pressed={lang === "zh"}
              onClick={() => setLang("zh")}
            >中</button>
            <button
              type="button"
              className={`shell-language-btn${lang === "en" ? " is-active" : ""}`}
              aria-pressed={lang === "en"}
              onClick={() => setLang("en")}
            >EN</button>
          </div>
        </div>
      </header>
      <main id="main-content" tabIndex={-1}>{children}</main>
    </div>
  </div>;
}
