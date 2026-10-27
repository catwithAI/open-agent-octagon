import {it,expect} from "vitest";
import {fireEvent,screen,waitFor} from "@testing-library/react";
import {Routes,Route} from "react-router-dom";
import {Workspace} from "./Workspace";
import {apiMock,draft,entry,jsonResponse,release,view} from "./test-utils";

it("browses package contents and switches versions without creating a draft",async()=>{
 const other={...release,id:"rel2",version_label:"1.0",current_version_id:"v2",status:"published"};
 const fetch=apiMock((path,init)=>{
  if(path==="/skills")return jsonResponse({items:[{id:"s1",name:"sample"}]});
  if(path==="/skills/s1")return jsonResponse({id:"s1",name:"sample",releases:[release,other]});
  if(path.startsWith("/experiments?"))return jsonResponse({items:[]});
  if(path.startsWith("/runs?"))return jsonResponse({items:[],next_cursor:null});
  if(path==="/versions/v1"||path==="/versions/v2")return jsonResponse({entries:[entry]});
  if(path==="/versions/v1/files?path=SKILL.md")return jsonResponse({text:"Original instructions"});
  if(path==="/versions/v2/files?path=SKILL.md")return jsonResponse({text:"Published instructions"});
  if(path==="/drafts"&&init?.method==="POST")return jsonResponse({...draft,source_release_id:"rel2",target_release_id:null});
  if(path==="/drafts/d1")return jsonResponse({...draft,source_release_id:"rel2",target_release_id:null});
  if(path==="/drafts/d1/files?path=SKILL.md")return jsonResponse({...entry,text:"Published instructions"});
 });
 view(<Routes><Route path="/skill-eval/skills/:skillId" element={<Workspace/>}/></Routes>,"/skill-eval/skills/s1");
 await screen.findByText("Original instructions");
 fireEvent.change(screen.getByLabelText("版本"),{target:{value:"rel2"}});
 await screen.findByText("Published instructions");
 expect(fetch.mock.calls.filter(([,init])=>init?.method==="POST")).toHaveLength(0);
 fireEvent.click(screen.getByText("编辑 Skill 包"));
 await waitFor(()=>expect(fetch.mock.calls.filter(([url,init])=>String(url).endsWith("/drafts")&&init?.method==="POST")).toHaveLength(1));
 const creation=fetch.mock.calls.find(([url,init])=>String(url).endsWith("/drafts")&&init?.method==="POST")!;
 expect(JSON.parse(String(creation[1]?.body))).toEqual({release_id:"rel2"});
});

it("shows the source final reply and restores the experiment when refining",async()=>{
 const {run}=await import("./test-utils");
 apiMock(path=>{
  if(path==="/skills")return jsonResponse({items:[run.skill]});
  if(path==="/runs/r1")return jsonResponse({...run,run_status:"completed",ended_at:"2026-10-22",final_result:{final_reply:{text:"SOURCE_FINAL_REPLY"},artifacts:[]}});
  if(path==="/skills/s1")return jsonResponse({...run.skill,releases:[release]});
  if(path.startsWith("/experiments?"))return jsonResponse({items:[run.experiment]});
  if(path==="/drafts/d1")return jsonResponse(draft);
  if(path==="/versions/v1")return jsonResponse({entries:[entry]});
  if(path.includes("/files?"))return jsonResponse({...entry,text:"source instructions"});
 });
 view(<Routes><Route path="/skill-eval/runs/:runId/refine" element={<Workspace/>}/></Routes>,"/skill-eval/runs/r1/refine?draft=d1&experiment=exp1");
 await screen.findByText("SOURCE_FINAL_REPLY");await waitFor(()=>expect((screen.getByLabelText("实验") as HTMLSelectElement).value).toBe("exp1"));expect(screen.queryByText(/没有可用的最终文字回复/)).toBeNull();
});
