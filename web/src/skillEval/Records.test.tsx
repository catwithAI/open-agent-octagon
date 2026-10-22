import {it,expect} from "vitest";
import {fireEvent,screen,waitFor} from "@testing-library/react";
import {Records} from "./Records";
import {apiMock,jsonResponse,view} from "./test-utils";

it("restores an expanded unverified model group and filters its second-level records",async()=>{
 const key="requested/unverified:fixture";const fetch=apiMock(path=>{
  if(path==="/record-groups")return jsonResponse({items:[{group_key:key,agent:"blade-agent",requested_model:"fixture",requested_provider:"",resolved_model:null,resolved_provider:null,run_count:2,last_run_at:"2026-10-22"}],next_cursor:null});
  if(path==="/skills")return jsonResponse({items:[]});
  if(path.startsWith("/runs?"))return jsonResponse({items:[],next_cursor:null});
 });view(<Records/>,`/skill-eval-records?group=${encodeURIComponent(key)}`);await screen.findByText("实际身份待确认");expect(screen.getByRole("button",{expanded:true})).toBeTruthy();fireEvent.change(screen.getByLabelText("筛选状态"),{target:{value:"failed"}});await waitFor(()=>expect(fetch.mock.calls.some(([path])=>String(path).includes("status=failed")&&String(path).includes("group_key="))).toBe(true));
});
