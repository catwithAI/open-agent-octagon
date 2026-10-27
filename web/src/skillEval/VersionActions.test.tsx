import {it,expect,vi} from "vitest";
import {fireEvent,screen,waitFor} from "@testing-library/react";
import {SkillPackageEditor} from "./SkillPackageEditor";
import {apiMock,draft,entry,jsonResponse,release,view} from "./test-utils";

it("requires a new version for a published source and preserves edits on conflict",async()=>{
 const source={...draft,target_release_id:null};const onDraft=vi.fn();
 const fetch=apiMock(path=>{if(path==="/releases/rel1")return jsonResponse({...release,status:"published"});if(path.includes("/files?"))return jsonResponse({...entry,text:"original"});if(path.endsWith("/save"))return jsonResponse({error:{code:"version_conflict",message:"Version already exists"}},409);});
 view(<SkillPackageEditor draft={source} onDraft={onDraft}/>);const editor=await screen.findByLabelText("编辑文件 SKILL.md");await waitFor(()=>expect((editor as HTMLTextAreaElement).value).toBe("original"));fireEvent.change(editor,{target:{value:"my unsaved changes"}});fireEvent.click(screen.getByText("保存版本"));await screen.findByText(/正式版不能覆盖/);fireEvent.change(screen.getByLabelText("新版本号"),{target:{value:"1.0"}});fireEvent.click(screen.getByText("确认"));await screen.findByText("Version already exists");expect((screen.getByLabelText("编辑文件 SKILL.md") as HTMLTextAreaElement).value).toBe("my unsaved changes");expect(onDraft).not.toHaveBeenCalled();const call=fetch.mock.calls.find(([path])=>String(path).endsWith("/save"))!;expect(JSON.parse(String(call[1]?.body))).toMatchObject({version:"1.0",changes:[{op:"write",text:"my unsaved changes"}]});expect(fetch.mock.calls.some(([path])=>String(path).endsWith("/runs"))).toBe(false);
});

it("publishes an unused version with every dirty buffer without starting a run",async()=>{
 const fetch=apiMock(path=>{if(path.includes("/files?"))return jsonResponse({text:"original"});if(path.endsWith("/publish"))return jsonResponse({release:{...release,id:"new-release",version_label:"1.0",status:"published"}});});
 view(<SkillPackageEditor draft={draft} onDraft={()=>{}}/>);const editor=await screen.findByLabelText("编辑文件 SKILL.md");await waitFor(()=>expect((editor as HTMLTextAreaElement).value).toBe("original"));fireEvent.change(editor,{target:{value:"new whole package"}});fireEvent.click(screen.getByText("发布正式版"));fireEvent.change(screen.getByLabelText("新版本号"),{target:{value:"1.0"}});fireEvent.click(screen.getByText("确认"));await screen.findByText("已另建正式版，原版本继续保留。");expect(fetch.mock.calls.some(([path])=>String(path).endsWith("/save")||String(path).endsWith("/runs"))).toBe(false);const call=fetch.mock.calls.find(([path])=>String(path).endsWith("/publish"))!;expect(JSON.parse(String(call[1]?.body))).toMatchObject({version:"1.0",expected_release_revision:1,changes:[{text:"new whole package"}]});
});
