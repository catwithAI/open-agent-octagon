import {it,expect} from "vitest";
import {fireEvent,screen,waitFor} from "@testing-library/react";
import {ComparePage} from "./ComparePage";
import {apiMock,jsonResponse,view} from "./test-utils";

it("compares three ended runs and requests the selected column and kind for file diff",async()=>{
 const fetch=apiMock(path=>{
  if(path==="/comparisons/c1")return jsonResponse({id:"c1",baseline_run_id:"r1",columns:["r1","r2","r3"].map((id,index)=>({run_id:id,skill_name:"sample",experiment_name:`experiment-${index}`,status:"completed",requested_model:"fixture",partial:false,note:{text:"",revision:0},result:{complete:true,final_reply:{text:`reply-${id}`},artifacts:[],observations:[]}}))});
  if(path.includes("/diff?"))return jsonResponse(path.includes("path=")?{binary:false,diff:"chosen file diff"}:{files:[{path:"notes.md",changed:true}]});
 });view(<ComparePage/>,"/skill-eval/compare?comparison=c1");await screen.findByText("reply-r3");
 fireEvent.click(screen.getAllByText("产物文件差异")[1]);await screen.findByText("notes.md");fireEvent.click(screen.getByText("notes.md"));await screen.findByText("chosen file diff");
 const last=fetch.mock.calls.at(-1)!;expect(String(last[0])).toContain("left=r1&right=r3&kind=artifact&path=notes.md");
 fireEvent.change(screen.getByLabelText("基准运行"),{target:{value:"r2"}});
 await waitFor(()=>expect(screen.queryByText("chosen file diff")).toBeNull());
 expect(screen.getByText("3 次运行")).toBeTruthy();
});

it("keeps the new baseline diff when an earlier request completes late",async()=>{
 let resolveOld!:(response:Response)=>void;
 apiMock(path=>{
  if(path==="/comparisons/c1")return jsonResponse({id:"c1",baseline_run_id:"r1",columns:["r1","r2","r3"].map(id=>({run_id:id,skill_name:"sample",experiment_name:id,status:"completed",requested_model:"fixture",partial:false,note:{text:"",revision:0},result:{complete:true,final_reply:{text:`reply-${id}`},artifacts:[],observations:[]}}))});
  if(path.includes("/diff?")&&path.includes("left=r1"))return new Promise<Response>(resolve=>{resolveOld=resolve;});
  if(path.includes("/diff?")&&path.includes("left=r2"))return jsonResponse({diff:"new baseline diff"});
 });
 view(<ComparePage/>,"/skill-eval/compare?comparison=c1");await screen.findByText("reply-r3");
 fireEvent.click(screen.getAllByText("回复差异")[1]);
 await waitFor(()=>expect(resolveOld).toBeTypeOf("function"));
 fireEvent.change(screen.getByLabelText("基准运行"),{target:{value:"r2"}});
 fireEvent.click(screen.getAllByText("回复差异")[1]);await screen.findByText("new baseline diff");
 resolveOld(jsonResponse({diff:"stale baseline diff"}));
 await waitFor(()=>expect(screen.queryByText("stale baseline diff")).toBeNull());
 expect(screen.getByText("new baseline diff")).toBeTruthy();
});
