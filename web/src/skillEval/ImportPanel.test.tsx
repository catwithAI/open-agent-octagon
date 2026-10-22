import {it,expect} from "vitest";
import {fireEvent,screen,waitFor} from "@testing-library/react";
import {ImportPanel} from "./ImportPanel";
import {apiMock,jsonResponse,view} from "./test-utils";

it("keeps a conflicted import and requires an unused version for an existing name",async()=>{
 const fetch=apiMock((path,init)=>{if(path==="/skills")return jsonResponse({items:[{id:"s1",name:"sample"}]});if(path==="/catalog")return jsonResponse({status:"unconfigured",items:[]});if(path==="/skills/import")return jsonResponse({id:"i1",detected_name:"sample",detected_version:null,entries:[],metadata:{version_candidates:[]}});if(path==="/imports/i1/commit"&&init?.method==="POST")return jsonResponse({error:{code:"version_conflict",message:"Version already exists"}},409);});
 view(<ImportPanel/>);await screen.findByText("未配置只读目录，仍可上传");fireEvent.change(screen.getByLabelText("上传 Skill"),{target:{files:[new File(["---\nname: sample\n---"],"SKILL.md")]}});await screen.findByText("确认名称与版本");expect((screen.getByLabelText("版本号") as HTMLInputElement).value).toBe("");expect((screen.getByText("确认导入") as HTMLButtonElement).disabled).toBe(true);
 fireEvent.change(screen.getByLabelText("版本号"),{target:{value:"beta"}});fireEvent.click(screen.getByText("确认导入"));await screen.findByText("Version already exists");expect(screen.getByLabelText("Skill 名称")).toBeTruthy();expect(fetch.mock.calls.filter(([path])=>String(path).endsWith("/skills/import"))).toHaveLength(1);await waitFor(()=>expect((screen.getByLabelText("版本号") as HTMLInputElement).value).toBe("beta"));
});
