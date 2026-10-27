import {useEffect,useState} from "react";
import {useNavigate} from "react-router-dom";
import {post,request,upload} from "./api";
import {ErrorBox,Panel,useAction,useCopy} from "./common";
import type {CommittedImport,ImportResult,Skill} from "./types";

interface CatalogItem {id:string;name?:string;version?:string;fingerprint?:string;status?:string}
export function ImportPanel(){
  const c=useCopy(),navigate=useNavigate(),action=useAction();
  const [pending,setPending]=useState<ImportResult|null>(null),[name,setName]=useState(""),[version,setVersion]=useState("");
  const [skills,setSkills]=useState<Skill[]>([]),[catalog,setCatalog]=useState<{status:string;items:CatalogItem[]}>({status:"unconfigured",items:[]});
  const [catalogId,setCatalogId]=useState("");
  useEffect(()=>{const abort=new AbortController();void Promise.all([request<{items:Skill[]}>("/skills",{signal:abort.signal}),request<typeof catalog>("/catalog",{signal:abort.signal})]).then(([s,k])=>{setSkills(s.items);setCatalog(k);}).catch(e=>{if(!abort.signal.aborted)action.setError(e);});return()=>abort.abort();},[]);
  const existing=skills.some(s=>s.name===name.normalize("NFC").trim());
  const accept=(item:ImportResult)=>{setPending(item);setName(item.detected_name);setVersion(item.detected_version||(skills.some(s=>s.name===item.detected_name)?"":"0.0.1"));};
  return <Panel title={c("导入 Skill","Import a Skill")}><p>{c("上传完整包，在这里编辑和试跑；来源目录保持不变。","Upload a package to edit and run. Catalog sources remain unchanged.")}</p>
    <div className="se-toolbar"><label className="se-file-button">{c("上传 SKILL.md / ZIP","Upload SKILL.md / ZIP")}<input aria-label={c("上传 Skill","Upload Skill")} type="file" accept=".zip,.md" disabled={action.busy} onChange={event=>{const file=event.target.files?.[0];event.target.value="";if(!file)return;
      // A single Markdown file is the package's SKILL.md even when the browser
      // reports it under another name (e.g. "skill.md"); the backend requires
      // the exact filename. ZIP packages keep their own name.
      const named=/\.md$/i.test(file.name)&&file.name!=="SKILL.md"?new File([file],"SKILL.md",{type:file.type||"text/markdown"}):file;
      void action.act(async()=>accept(await upload<ImportResult>("/skills/import",named)));}}/></label></div>
    <label>{c("选择已有 Skill","Choose a catalog Skill")}<select value={catalogId} onChange={e=>setCatalogId(e.target.value)} disabled={action.busy||!catalog.items.length}><option value="">{catalog.status==="unconfigured"?c("未配置只读目录，仍可上传","No catalog configured; uploads remain available"):c("选择目录中的 Skill","Choose a Skill")}</option>{catalog.items.map(item=><option key={item.id} value={item.id} disabled={item.status==="invalid"}>{item.name||c("无效包","Invalid package")} {item.version||""}</option>)}</select></label>
    <button disabled={!catalogId||action.busy} onClick={()=>void action.act(async()=>{const item=catalog.items.find(i=>i.id===catalogId)!;accept(await post<ImportResult>("/skills/from-catalog",{catalog_id:catalogId,expected_fingerprint:item.fingerprint}));})}>{c("复制导入","Copy to import")}</button>
    {pending&&<form className="se-stack" onSubmit={event=>{event.preventDefault();void action.act(async()=>{const item=await post<CommittedImport>(`/imports/${pending.id}/commit`,{name,version:version||null});navigate(`/skill-eval/skills/${item.skill.id}?draft=${item.draft.id}&release=${item.release.id}`);});}}>
      <h3>{c("确认名称与版本","Confirm name and version")}</h3><p>{pending.entries.length} {c("个文件，未发布版本","files, unpublished version")}</p>
      <label>{c("Skill 名称","Skill name")}<input value={name} onChange={e=>setName(e.target.value)} required/></label>
      <label>{c("版本号","Version")}<input value={version} onChange={e=>setVersion(e.target.value)} required={existing||pending.metadata.version_candidates.length>1}/></label>
      {existing&&<p className="se-hint">{c("同名内容归入已有 Skill。请输入未使用的版本号，不能覆盖已有 beta。","This name belongs to an existing Skill. Choose an unused version; existing beta versions cannot be overwritten.")}</p>}
      {pending.metadata.version_candidates.length>1&&<p className="se-hint">{c("包内版本信息不一致，请明确填写：","Package versions disagree; explicitly choose a version:")} {pending.metadata.version_candidates.join(" / ")}</p>}
      <button className="primary" disabled={action.busy||!name.trim()||(existing&&!version.trim())}>{c("确认导入","Confirm import")}</button>
    </form>}<ErrorBox error={action.error}/>
  </Panel>;
}
