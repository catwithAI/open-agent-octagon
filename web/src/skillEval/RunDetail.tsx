import {useCallback,useEffect,useState} from "react";
import {Link,useParams,useSearchParams} from "react-router-dom";
import {post,request,url} from "./api";
import {ArtifactPreview} from "./ArtifactPreview";
import {ChatComposer} from "./ChatComposer";
import {ErrorBox,Panel,ScopeNotice,Status,useAction,useCopy} from "./common";
import {ProcessView} from "./ProcessView";
import {RunNote} from "./RunNote";
import {useRunEvents} from "./useRunEvents";
import {useCompareSelection} from "./RunHistory";
import {createIdempotencyKey} from "../api/idempotency";
import type {Interaction,Run,RunMessage} from "./types";
import "./styles.css";

export function RunDetail(){
  const {runId=""}=useParams();const [params,setParams]=useSearchParams();const c=useCopy(),action=useAction(),selection=useCompareSelection();
  const [loadedRun,setRun]=useState<Run|null>(null),[messages,setMessages]=useState<RunMessage[]>([]),[interactions,setInteractions]=useState<Interaction[]>([]),[error,setError]=useState<unknown>(null),[refresh,setRefresh]=useState(0),[readOnly,setReadOnly]=useState(false),[preview,setPreview]=useState<string|null>(null);
  const run=loadedRun?.id===runId?loadedRun:null;
  useEffect(()=>{setMessages([]);setInteractions([]);setPreview(null);},[runId]);
  const update=useCallback(()=>setRefresh(value=>value+1),[]);const stream=useRunEvents(runId,update);
  useEffect(()=>{const abort=new AbortController();Promise.all([request<Run>(`/runs/${runId}`,{signal:abort.signal}),request<{items:RunMessage[]}>(`/runs/${runId}/messages`,{signal:abort.signal}),request<{items:Interaction[]}>(`/runs/${runId}/interactions`,{signal:abort.signal})]).then(([r,m,i])=>{setRun(r);setMessages(m.items);setInteractions(i.items);setError(null);}).catch(e=>{if(!abort.signal.aborted)setError(e);});return()=>abort.abort();},[runId,refresh]);
  useEffect(()=>{request<{display_only:boolean}>("/capabilities").then(cap=>setReadOnly(cap.display_only)).catch(()=>{});},[]);
  useEffect(()=>{if(!run||["completed","failed","cancelled"].includes(run.run_status))return;const interval=setInterval(update,3000);return()=>clearInterval(interval);},[run?.run_status,update]);
  const view=params.get("view")||"result",ended=Boolean(run&&["completed","failed","cancelled"].includes(run.run_status));const pending=interactions.find(item=>item.state==="pending");
  const back=params.get("from")?.startsWith("/skill-eval")?params.get("from")!:run?`/skill-eval/skills/${run.skill_id}`:"/skill-eval";
  return <div className="se"><header className="se-page-header"><div><Link to={back}>{c("返回 Skill 与历史","Back to Skill and history")}</Link><h1>{run?.skill.name||c("运行详情","Run details")}</h1><p>{run?.experiment.name} · {run?.source_release_label||c("临时内容","Temporary content")} · {run?.requested_model}</p></div>{run&&<Status value={run.run_status}/>}</header><ErrorBox error={error}/><ErrorBox error={action.error}/>
    {run&&<><div className="se-toolbar"><button aria-pressed={view==="result"} onClick={()=>{const next=new URLSearchParams(params);next.set("view","result");setParams(next,{replace:true});}}>{c("结果与对话","Result and conversation")}</button><button aria-pressed={view==="process"} onClick={()=>{const next=new URLSearchParams(params);next.set("view","process");setParams(next,{replace:true});}}>{c("完整流程","Complete process")}</button><small>{c("连接：","Connection: ")}{stream.state}</small>
      {!ended&&<><button disabled={readOnly||action.busy||!(["queued","running","stopping"].includes(run.generation_status)||["queued","preparing"].includes(run.run_status))} onClick={()=>void action.act(async()=>{await post(`/runs/${run.id}/stop-generation`);update();})}>{c("停止当前生成","Stop generation")}</button><button disabled={readOnly||action.busy||run.run_status==="finishing"} onClick={()=>void action.act(async()=>{await post(`/runs/${run.id}/finish`);update();})}>{c("结束本次运行","End this run")}</button></>}
      <Link className="se-button" to={`/skill-eval/runs/${run.id}/refine`}>{c("基于结果微调 Skill","Refine Skill from this result")}</Link><a className="se-button" href={url(`/runs/${run.id}/skill/export`)}>{c("导出本次 Skill","Export this run's Skill")}</a>{ended&&<button onClick={()=>selection.toggle(run.id)}>{selection.selected.includes(run.id)?c("取消比较选择","Remove from comparison"):c("加入比较","Add to comparison")}</button>}
    </div>
    <details className="se-limit-details"><summary>{c("查看评测范围与局限","Evaluation scope and limitations")}</summary><ScopeNotice/></details>
    {run.external_refs.last_stop_error?<p className="se-error">{c("停止结果尚未确认，请检查流程；可再次点击停止当前生成重试。","Stop is unconfirmed. Inspect the process or retry Stop generation.")}</p>:null}
    {run.error_code&&<div className="se-error">{run.error_code} — {run.error_message||c("请查看流程和采集缺口；未确认的操作不会自动重发。","Inspect the process and capture gaps. Uncertain operations are not automatically replayed.")}</div>}
    <div className="se-workspace se-run-layout"><div className="se-stack">
      {view==="process"?<Panel title={c("模型思考、行动与反馈","Model reasoning, actions and feedback")}><ProcessView runId={run.id} refresh={refresh}/></Panel>:<>
        <Panel title={ended?c("最终结果","Final result"):c("本次对话（尚未固定最终结果）","Conversation (final result not fixed)")}>
          {ended?<><pre className="se-result-text">{run.final_result?.final_reply?.text||(run.final_result?.execution_started===false?c("本次未执行模型，没有回复或产物。","The model did not run; no reply or artifacts."):c("未取得最终回复正文，请查看流程和采集范围。","Final reply text is unavailable. Inspect the process and capture policy."))}</pre>{run.final_result&&!run.final_result.complete&&<p className="se-hint">{c("这是部分结果。","This result is partial.")}</p>}</>:<div className="se-conversation">{messages.map(message=><div key={message.id}><article className="se-chat-message se-chat-user"><header>{c("用户输入","User input")} <small>{message.delivery_state}</small></header><pre>{message.text||c("无用户提示","No user prompt")}</pre>{message.attachment_refs.map(input=><a key={input.id} href={url(`/inputs/${input.id}/download`)}>{input.original_name}</a>)}</article>{run.chat_replies?.filter(reply=>reply.message_id===message.id).map(reply=><article key={reply.event_id} className="se-chat-message se-chat-assistant"><header>{c("模型回复","Model reply")}</header><pre>{reply.text}</pre></article>)}</div>)}</div>}
          {run.input_snapshot.system_startup_message&&<details><summary>{c("查看系统启动指令","View system startup instruction")}</summary><pre>{run.input_snapshot.system_startup_message}</pre></details>}
        </Panel>
        {ended&&<Panel title={c("最终文件","Final files")}>{run.final_result?.artifacts.length?<ul className="se-artifact-list">{run.final_result.artifacts.map(artifact=><li key={artifact.id}>{artifact.state==="complete"?<span><button onClick={()=>setPreview(artifact.id)}>{artifact.relative_path}</button> <a href={url(`/artifacts/${artifact.id}`)}>{c("下载","Download")}</a></span>:<span>{artifact.relative_path} — {c("未下载","Not downloaded")} {artifact.error}</span>}<small>{artifact.size??"?"} B</small></li>)}</ul>:<p>{run.artifact_status==="complete"?c("无文件产物。文字结论本身也是有效结果。","No file artifacts. Text conclusions are valid results."):c("文件回收未完成，暂时无法确认是否有产物。","File collection is incomplete; artifact availability is unknown.")}</p>}
          {run.final_result?.gaps.map((gap,i)=><p className="se-hint" key={i}>{gap.path} {gap.reason}</p>)}{run.final_result?.observations.map((observation,i)=><p className="se-hint" key={i}>{c("超出评测范围：","Outside the current evaluation scope: ")}{observation.kind} {observation.path}</p>)}{run.artifact_status!=="complete"&&<button disabled={readOnly||action.busy} onClick={()=>void action.act(async()=>{await post(`/runs/${run.id}/collect`,{},createIdempotencyKey());update();})}>{c("仅重试回收","Retry collection only")}</button>}</Panel>}
      </>}
      {preview&&<ArtifactPreview descriptorPath={`/artifacts/${preview}/preview`} downloadUrl={url(`/artifacts/${preview}`)} onClose={()=>setPreview(null)}/>}
      <ChatComposer key={run.id} run={run} interaction={pending} onSent={update} readOnly={readOnly}/>
    </div><aside className="se-stack"><Panel title={c("运行信息","Run information")}><dl className="se-meta"><dt>{c("智能体","Agent")}</dt><dd>blade-agent</dd><dt>{c("生成状态","Generation status")}</dt><dd>{run.generation_status}</dd><dt>{c("采集范围","Capture policy")}</dt><dd>{run.input_snapshot.capture.policy.requested} → {run.input_snapshot.capture.policy.effective}</dd><dt>{c("采集状态","Capture status")}</dt><dd>{run.capture_status}</dd><dt>{c("SDK / 服务版本","SDK / server version")}</dt><dd>{String(run.external_refs.sdk_version||"?")} / {String(run.external_refs.server_version||"?")}</dd></dl><details><summary>{c("输入与全部交互记录","Inputs and all interactions")}</summary><pre>{run.input_snapshot.prompt||c("无初始用户提示","No initial user prompt")}</pre>{messages.map(message=><details key={message.id}><summary>#{message.seq} {message.kind}</summary><pre>{message.text}</pre><ul>{message.attachment_refs.map(input=><li key={input.id}>{input.original_name}</li>)}</ul></details>)}</details></Panel><Panel title={c("本次实验观察","Run observations")}><RunNote key={run.id} runId={run.id} note={run.note} onSaved={update} readOnly={readOnly}/></Panel></aside></div>
    </>}
  </div>;
}
