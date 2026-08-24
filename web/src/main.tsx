import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { Submit } from "./pages/Submit";
import { SameModelSubmit } from "./pages/SameModelSubmit";
import { MultiModelSubmit } from "./pages/MultiModelSubmit";
import { RunList } from "./pages/RunList";
import { RunDetail } from "./pages/RunDetail";
import { Scenarios } from "./pages/Scenarios";
import { ExperimentBuilder } from "./pages/ExperimentBuilder";
import { RunGroupLive } from "./pages/RunGroupLive";
import { ResearchResults } from "./pages/ResearchResults";
import { ExperimentInsights } from "./pages/ExperimentInsights";
import { ExperimentList } from "./pages/ExperimentList";
import { ExperimentOverview } from "./pages/ExperimentOverview";
import { Profiles } from "./pages/Profiles";
import { SystemCapabilities } from "./pages/SystemCapabilities";
import { ResearchOverview } from "./pages/ResearchOverview";
import { ScenarioDetail } from "./pages/ScenarioDetail";
import { AppShell } from "./components/AppShell";
import { I18nProvider } from "./i18n";
import "./styles.css";

function App() {
  return (
    <I18nProvider>
    <BrowserRouter>
      <AppShell>
        <Routes>
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
