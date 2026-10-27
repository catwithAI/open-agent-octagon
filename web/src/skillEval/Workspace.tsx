import {useEffect,useRef,useState} from "react";
import {Link,useParams,useSearchParams} from "react-router-dom";
import {post,request,url} from "./api";
import {ErrorBox,Panel,ScopeNotice,useAction,useCopy} from "./common";
import {ArtifactPreview} from "./ArtifactPreview";
import {ImportPanel} from "./ImportPanel";
import {RunConfiguration} from "./RunConfiguration";
import {RunHistory} from "./RunHistory";
import {SkillPackageEditor,type EditorHandle} from "./SkillPackageEditor";
import type {Draft,Entry,Release,Run,Skill} from "./types";
import "./styles.css";

export function Workspace(){
  const {skillId,runId}=useParams();const [params,setParams]=useSearchParams();const c=useCopy(),action=useAction();
  const [skill,setSkill]=useState<Skill|null>(null),[skills,setSkills]=useState<Skill[]>([]),[draft,setDraft]=useState<Draft|null>(null),[source,setSource]=useState<Run|null>(null);
  const [editorOpen,setEditorOpen]=useState(Boolean(runId||params.get("draft"))),[readOnly,setReadOnly]=useState(false);const editor=useRef<EditorHandle>(null),creating=useRef<{runId:string;promise:Promise<Draft>}|null>(null);
  const [sourcePreview,setSourcePreview]=useState<string|null>(null);
  const [versionEntries,setVersionEntries]=useState<Entry[]>([]),[skillText,setSkillText]=useState<string|null>(null);
  const draftId=params.get("draft"),releaseId=params.get("release")||draft?.target_release_id||draft?.source_release_id||skill?.releases?.[0]?.id||"";
  useEffect(()=>{const abort=new AbortController();request<{items:Skill[]}>("/skills",{signal:abort.signal}).then(r=>setSkills(r.items)).catch(e=>{if(!abort.signal.aborted)action.setError(e);});request<{display_only:boolean}>("/capabilities",{signal:abort.signal}).then(r=>setReadOnly(r.display_only)).catch(()=>{});return()=>abort.abort();},[skillId,runId]);
  useEffect(()=>{let active=true;const abort=new AbortController();setSkill(null);setDraft(null);setSource(null);
    if(runId){request<Run>(`/runs/${runId}`,{signal:abort.signal}).then(async run=>{if(!active)return;setSource(run);setSkill(await request<Skill>(`/skills/${run.skill_id}`,{signal:abort.signal}));if(!draftId){if(creating.current?.runId!==runId)creating.current={runId,promise:post<Draft>(`/runs/${runId}/drafts`)};const created=await creating.current.promise;if(active){setDraft(created);const query=new URLSearchParams(params);query.set("draft",created.id);query.set("experiment",run.experiment_id);setParams(query,{replace:true});}}}).catch(e=>{if(active&&!abort.signal.aborted)action.setError(e);});}
    else if(skillId)request<Skill>(`/skills/${skillId}`,{signal:abort.signal}).then(s=>{if(active)setSkill(s);}).catch(e=>{if(!abort.signal.aborted)action.setError(e);});
    return()=>{active=false;abort.abort();};
  },[skillId,runId]);
  useEffect(()=>{if(!draftId)return;const abort=new AbortController();request<Draft>(`/drafts/${draftId}`,{signal:abort.signal}).then(value=>{setDraft(value);setEditorOpen(true);}).catch(e=>{if(!abort.signal.aborted)action.setError(e);});return()=>abort.abort();},[draftId]);
  const selectedVersion=skill?.releases?.find(item=>item.id===releaseId)?.current_version_id;
  useEffect(()=>{setVersionEntries([]);setSkillText(null);if(!selectedVersion)return;const abort=new AbortController();
    request<{entries:Entry[]}>(`/versions/${selectedVersion}`,{signal:abort.signal}).then(value=>setVersionEntries(value.entries)).catch(e=>{if(!abort.signal.aborted)action.setError(e);});
    request<{text?:string}>(`/versions/${selectedVersion}/files?path=SKILL.md`,{signal:abort.signal}).then(value=>setSkillText(value.text??c("文件较大，请下载后查看。","File is large; download it to inspect."))).catch(e=>{if(!abort.signal.aborted)action.setError(e);});
    return()=>abort.abort();
  },[selectedVersion]);
  const browseVersion=(id:string)=>{if(editor.current?.changes().length&&!window.confirm(c("切换版本会离开当前编辑内容，继续？","Switch away from the current edits?")))return;setDraft(null);setEditorOpen(false);const query=new URLSearchParams(params);query.delete("draft");query.set("release",id);setParams(query,{replace:true});};
  const onDraft=(updated:Draft)=>{setDraft(updated);if(updated.target_release_id&&skill)request<Skill>(`/skills/${skill.id}`).then(setSkill).catch(action.setError);};
  const openEditor=async(release:Release)=>{if(editor.current?.changes().length&&!window.confirm(c("切换版本会离开当前编辑内容，继续？","Switch away from the current edits?")))return;const next=await post<Draft>("/drafts",{release_id:release.id});setDraft(next);setEditorOpen(true);const query=new URLSearchParams(params);query.set("draft",next.id);query.set("release",release.id);setParams(query,{replace:true});};
  const freeze=async()=>{if(editor.current)return editor.current.freeze();const release=skill?.releases?.find(r=>r.id===releaseId);if(!release)throw new Error(c("请先选择版本","Select a version first"));return {snapshot_id:release.current_version_id};};
  if(!skillId&&!runId)return <div className="se"><header className="se-page-header"><h1>Skill Eval</h1><p>{c("调试 Skill，保留每次运行的证据。","Refine a Skill and keep evidence from each run.")}</p></header><div className="se-workspace"><ImportPanel/><Panel title={c("已有 Skill","Existing Skills")}>{skills.length?<ul className="se-skill-list">{skills.map(item=><li key={item.id}><Link to={`/skill-eval/skills/${item.id}`}>{item.name}</Link></li>)}</ul>:<p className="se-empty">{c("从左侧上传第一个 Skill。","Upload your first Skill on the left.")}</p>}<ScopeNotice/></Panel></div><ErrorBox error={action.error}/></div>;
  return <div className="se"><header className="se-page-header"><div><Link to="/skill-eval">{c("全部 Skill","All Skills")}</Link><h1>{source?c("基于结果微调","Refine from a result"):skill?.name||"Skill Eval"}</h1></div>{source&&<Link to={`/skill-eval/runs/${source.id}`}>{c("返回来源运行","Back to source run")}</Link>}</header><ErrorBox error={action.error}/>
    {!skill?<p>{c("载入中…","Loading…")}</p>:<><div className="se-workspace">
      <div className="se-stack">{source&&<Panel title={c("来源结果与证据","Source result and evidence")}><p>{source.experiment.name} · {source.source_release_label||c("临时内容","Temporary content")}</p><pre className="se-result-text">{source.final_result?.final_reply?.text||(source.ended_at?c("没有可用的最终文字回复，可查看产物与流程。","No final text reply is available; inspect artifacts and process."):c("运行尚未固定最终结果，可查看当前流程。","No final result is fixed yet; inspect the current process."))}</pre><ul>{source.final_result?.artifacts.filter(item=>item.state==="complete").map(item=><li key={item.id}><button onClick={()=>setSourcePreview(item.id)}>{item.relative_path}</button></li>)}</ul>{sourcePreview&&<ArtifactPreview descriptorPath={`/artifacts/${sourcePreview}/preview`} downloadUrl={url(`/artifacts/${sourcePreview}`)} onClose={()=>setSourcePreview(null)}/>}<p>{c("备注：","Note: ")}{source.note.text||c("暂无备注","No note")}</p><Link to={`/skill-eval/runs/${source.id}?view=process`}>{c("查看完整流程","View complete process")}</Link></Panel>}
      <RunConfiguration skillId={skill.id} releaseId={releaseId||undefined} sourceRunId={source?.id} freeze={freeze} onStarted={()=>editor.current?.allowLeave()}/></div>
      <div className="se-stack">{source&&draft?<Panel title={c("微调完整 Skill 包","Refine the full Skill package")}><SkillPackageEditor ref={editor} draft={draft} onDraft={onDraft} readOnly={readOnly}/></Panel>:<Panel title={c("Skill 详情","Skill details")}><h3>{skill.name}</h3><label>{c("版本","Version")}<select value={releaseId} onChange={event=>browseVersion(event.target.value)}>{skill.releases?.map(release=><option key={release.id} value={release.id}>{release.version_label} · {release.status==="published"?c("正式版","Published"):c("未发布","Unpublished")}</option>)}</select></label>
        <div className="se-toolbar"><button disabled={!releaseId||action.busy||readOnly} onClick={()=>{const release=skill.releases?.find(item=>item.id===releaseId);if(release)void action.act(()=>openEditor(release));}}>{c("编辑 Skill 包","Edit Skill package")}</button>{releaseId&&<a href={url(`/releases/${releaseId}/export`)}>{c("导出所选版本","Export selected version")}</a>}{draft&&<span>{draft.entries.length} {c("个文件","files")}</span>}</div>{skillText!==null&&<pre className="se-result-text" aria-label="SKILL.md">{skillText}</pre>}<ul className="se-resource-list">{(draft?.entries||versionEntries).map(entry=><li key={entry.file_id}>{entry.path}<small>{entry.size} B</small></li>)}</ul></Panel>}
      {!source&&<RunHistory skillId={skill.id}/>}</div></div>
      {!source&&editorOpen&&draft&&<Panel title={c("编辑 SKILL.md 与资源文件","Edit SKILL.md and package files")}><SkillPackageEditor ref={editor} draft={draft} onDraft={onDraft} readOnly={readOnly}/></Panel>}
    </>}
  </div>;
}
