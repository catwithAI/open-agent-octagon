import {it,expect,vi} from "vitest";
import {createRef} from "react";
import {fireEvent,screen,waitFor} from "@testing-library/react";
import {SkillPackageEditor,type EditorHandle} from "./SkillPackageEditor";
import {apiMock,draft,entry,jsonResponse,view} from "./test-utils";

it("preserves two dirty buffers and freezes the whole package without saving a version",async()=>{
 const second={...entry,file_id:"f2",path:"scripts/run.py",hash:"b".repeat(64)};const value={...draft,entries:[entry,second]};const fetch=apiMock((path,init)=>{if(path.startsWith("/drafts/d1/files"))return jsonResponse({text:path.includes("SKILL.md")?"original markdown":"original script"});if(path==="/drafts/d1"&&init?.method==="PATCH")return jsonResponse({...value,revision:2,version_is_saved:false});if(path==="/drafts/d1/snapshots")return jsonResponse({id:"v2"});});
 const handle=createRef<EditorHandle>();view(<SkillPackageEditor draft={value} onDraft={()=>{}} ref={handle}/>);
 const markdown=await screen.findByLabelText("编辑文件 SKILL.md");await waitFor(()=>expect((markdown as HTMLTextAreaElement).value).toBe("original markdown"));fireEvent.change(markdown,{target:{value:"unsaved markdown"}});
 fireEvent.click(screen.getByRole("button",{name:"scripts/run.py"}));const script=await screen.findByLabelText("编辑文件 scripts/run.py");await waitFor(()=>expect((script as HTMLTextAreaElement).value).toBe("original script"));fireEvent.change(script,{target:{value:"unsaved script"}});fireEvent.click(screen.getByRole("button",{name:"SKILL.md *"}));expect((screen.getByLabelText("编辑文件 SKILL.md") as HTMLTextAreaElement).value).toBe("unsaved markdown");
 expect(handle.current!.changes()).toHaveLength(2);await handle.current!.freeze();const patch=fetch.mock.calls.find(([path,init])=>String(path).endsWith("/drafts/d1")&&init?.method==="PATCH");expect(JSON.parse(String(patch![1]!.body)).changes.map((change:{text:string})=>change.text).sort()).toEqual(["unsaved markdown","unsaved script"]);expect(fetch.mock.calls.some(([path])=>String(path).endsWith("/save"))).toBe(false);
});

it("combines a rename and modified contents using stable file identity",async()=>{
 const value={...draft,entries:[entry,{...entry,file_id:"f2",path:"code.py"}]};apiMock(path=>path.startsWith("/drafts/d1/files")?jsonResponse({text:"original"}):undefined);const handle=createRef<EditorHandle>();view(<SkillPackageEditor ref={handle} draft={value} onDraft={()=>{}}/>);fireEvent.click(screen.getByRole("button",{name:"code.py"}));const text=await screen.findByLabelText("编辑文件 code.py");await waitFor(()=>expect((text as HTMLTextAreaElement).value).toBe("original"));fireEvent.change(text,{target:{value:"new code"}});vi.spyOn(window,"prompt").mockReturnValue("renamed.py");fireEvent.click(screen.getByRole("button",{name:"重命名"}));const changes=handle.current!.changes();expect(changes).toContainEqual({op:"rename",path:"code.py",to:"renamed.py"});expect(changes.find(change=>change.op==="write")).toMatchObject({path:"renamed.py",file_id:"f2",text:"new code"});
});

it("uploads a new binary resource into the full package without a version save",async()=>{
 apiMock(path=>path.includes("/files?")?jsonResponse({text:"original"}):undefined);const handle=createRef<EditorHandle>();view(<SkillPackageEditor ref={handle} draft={draft} onDraft={()=>{}}/>);
 const file=new File([new Uint8Array([0,255,128])],"data.bin");Object.defineProperty(file,"arrayBuffer",{value:async()=>new Uint8Array([0,255,128]).buffer});vi.spyOn(window,"prompt").mockReturnValue("assets/data.bin");
 fireEvent.change(screen.getByLabelText("上传 Skill 资源"),{target:{files:[file]}});await screen.findByRole("button",{name:"assets/data.bin *"});expect(handle.current!.changes()).toContainEqual({op:"write",path:"assets/data.bin",content_base64:"AP+A",mode:420});expect(screen.getByText("新资源尚未暂存，可导出当前整个 Skill 包。")).toBeTruthy();
});

it("restores one source file without discarding another file's buffer",async()=>{
 const second={...entry,file_id:"f2",path:"code.py",hash:"b".repeat(64)};const value={...draft,entries:[entry,second],base_entries:[entry,second]};
 apiMock(path=>path.includes("/files?")?jsonResponse({text:path.includes("SKILL.md")?"original instructions":"source code"}):undefined);
 const handle=createRef<EditorHandle>();view(<SkillPackageEditor ref={handle} draft={value} onDraft={()=>{}}/>);
 const md=await screen.findByLabelText("编辑文件 SKILL.md");await waitFor(()=>expect((md as HTMLTextAreaElement).value).toBe("original instructions"));fireEvent.change(md,{target:{value:"keep these instructions"}});
 fireEvent.click(screen.getByRole("button",{name:"code.py"}));const code=await screen.findByLabelText("编辑文件 code.py");await waitFor(()=>expect((code as HTMLTextAreaElement).value).toBe("source code"));fireEvent.change(code,{target:{value:"discard this change"}});fireEvent.click(screen.getByText("恢复此文件"));await waitFor(()=>expect((screen.getByLabelText("编辑文件 code.py") as HTMLTextAreaElement).value).toBe("source code"));expect(handle.current!.changes()).toMatchObject([{op:"write",path:"SKILL.md",text:"keep these instructions"}]);
});
