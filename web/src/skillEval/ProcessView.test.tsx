import {it,expect} from "vitest";
import {screen,fireEvent} from "@testing-library/react";
import {ProcessView} from "./ProcessView";
import {apiMock,jsonResponse,view} from "./test-utils";

it("shows public reasoning, paired actual returns and honest context limitations",async()=>{apiMock(()=>jsonResponse({cursor:4,gaps:[],actual_model_request:{status:"unavailable"},turns:[{id:"t1",loop:"root",role:"assistant",kind:"message",status:"completed",thinking:"read input first",text:"I will inspect it",context:null,event_id:"e1"},{id:"t2",loop:"root",role:"assistant",kind:"message",status:"completed",thinking:"",text:"The return changes my plan",context:null,event_id:"e2"}],actions:[{id:"a1",turn_id:"t1",name:"Read",arguments:'{"path":"input.txt"}',result:"actual tool result",react_turn_id:"t2",association:"explicit_tool_call_id"}]}));view(<ProcessView runId="r1"/>);await screen.findByText("actual tool result");expect(screen.getByText("read input first")).toBeTruthy();expect(screen.getByText(/会话事件不等同于模型实际请求上下文/)).toBeTruthy();fireEvent.change(screen.getByLabelText("搜索流程"),{target:{value:"actual tool result"}});expect(screen.getByText("actual tool result")).toBeTruthy();expect(screen.queryByText("The return changes my plan")).toBeNull();});

it("reads every page from the same committed process snapshot",async()=>{
 const turn=(id:string,text:string)=>({id,loop:"root",role:"assistant",kind:"message",status:"completed",thinking:"",text,context:null,event_id:id});
 const fetch=apiMock(path=>path.includes("cursor=page-two")?jsonResponse({cursor:10,next_cursor:null,turns:[turn("t2","second page reply")],actions:[],gaps:[]}):jsonResponse({cursor:10,next_cursor:"page-two",turns:[turn("t1","first page reply")],actions:[],gaps:[]}));
 view(<ProcessView runId="r1"/>);await screen.findByText("second page reply");expect(screen.getByText("first page reply")).toBeTruthy();expect(fetch).toHaveBeenCalledTimes(2);
});
