import {Route} from "react-router-dom";
import {Workspace} from "./Workspace";
import {RunDetail} from "./RunDetail";
import {Records} from "./Records";
import {ComparePage} from "./ComparePage";

export const skillEvalRoutes=[
  <Route key="workspace" path="/skill-eval" element={<Workspace/>}/>,
  <Route key="skill" path="/skill-eval/skills/:skillId" element={<Workspace/>}/>,
  <Route key="run" path="/skill-eval/runs/:runId" element={<RunDetail/>}/>,
  <Route key="refine" path="/skill-eval/runs/:runId/refine" element={<Workspace/>}/>,
  <Route key="compare" path="/skill-eval/compare" element={<ComparePage/>}/>,
  <Route key="records" path="/skill-eval-records" element={<Records/>}/>,
];
