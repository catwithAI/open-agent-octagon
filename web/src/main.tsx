import React, { Suspense, lazy } from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { Submit } from "./pages/Submit";
import { RunList } from "./pages/RunList";
import { skillEvalRoutes } from "./skillEval/routes";
import { AppShell } from "./components/AppShell";
import { I18nProvider } from "./i18n";
import "./styles.css";

const SameModelSubmit = lazy(() => import("./pages/SameModelSubmit").then((m) => ({ default: m.SameModelSubmit })));
const MultiModelSubmit = lazy(() => import("./pages/MultiModelSubmit").then((m) => ({ default: m.MultiModelSubmit })));
const RunDetail = lazy(() => import("./pages/RunDetail").then((m) => ({ default: m.RunDetail })));
const Scenarios = lazy(() => import("./pages/Scenarios").then((m) => ({ default: m.Scenarios })));
const ScenarioDetail = lazy(() => import("./pages/ScenarioDetail").then((m) => ({ default: m.ScenarioDetail })));
const ExperimentBuilder = lazy(() => import("./pages/ExperimentBuilder").then((m) => ({ default: m.ExperimentBuilder })));
const RunGroupLive = lazy(() => import("./pages/RunGroupLive").then((m) => ({ default: m.RunGroupLive })));
const ResearchResults = lazy(() => import("./pages/ResearchResults").then((m) => ({ default: m.ResearchResults })));
const ExperimentInsights = lazy(() => import("./pages/ExperimentInsights").then((m) => ({ default: m.ExperimentInsights })));
const ExperimentList = lazy(() => import("./pages/ExperimentList").then((m) => ({ default: m.ExperimentList })));
const ExperimentOverview = lazy(() => import("./pages/ExperimentOverview").then((m) => ({ default: m.ExperimentOverview })));
const Profiles = lazy(() => import("./pages/Profiles").then((m) => ({ default: m.Profiles })));
const SystemCapabilities = lazy(() => import("./pages/SystemCapabilities").then((m) => ({ default: m.SystemCapabilities })));
const ResearchOverview = lazy(() => import("./pages/ResearchOverview").then((m) => ({ default: m.ResearchOverview })));

function App() {
  return (
    <I18nProvider>
    <BrowserRouter>
      <AppShell>
        <Suspense fallback={<div className="muted" style={{ padding: 24 }}>Loading…</div>}>
        <Routes>
          {skillEvalRoutes}
          <Route path="/overview" element={<ResearchOverview />} />
          <Route path="/" element={<Submit />} />
          <Route path="/same-model" element={<SameModelSubmit />} />
          <Route path="/multi-model" element={<MultiModelSubmit />} />
          <Route path="/runs" element={<RunList />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/scenarios" element={<Scenarios />} />
          <Route path="/scenarios/:envName" element={<ScenarioDetail />} />
          <Route path="/experiments/new" element={<ExperimentBuilder />} />
          <Route path="/experiments" element={<ExperimentList />} />
          <Route path="/experiments/:experimentId" element={<ExperimentOverview />} />
          <Route path="/experiments/:experimentId/groups/:groupId" element={<RunGroupLive />} />
          <Route path="/experiments/:experimentId/groups/:groupId/results" element={<ResearchResults />} />
          <Route path="/experiments/:experimentId/insights" element={<ExperimentInsights />} />
          <Route path="/profiles" element={<Profiles />} />
          <Route path="/system" element={<SystemCapabilities />} />
        </Routes>
        </Suspense>
      </AppShell>
    </BrowserRouter>
    </I18nProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
