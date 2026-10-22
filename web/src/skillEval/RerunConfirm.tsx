import {useEffect,useRef,useState} from "react";
import {post} from "./api";
import {ErrorBox,InputUpload,useAction,useCopy} from "./common";
import type {Input,Plan} from "./types";

export function RerunConfirm({sourceRunId,onConfirm,onCancel}:{sourceRunId:string;onConfirm:(plan:Plan)=>Promise<void>;onCancel:()=>void}){
  const c=useCopy(),action=useAction();const [mode,setMode]=useState(""),[plan,setPlan]=useState<Plan|null>(null),[prompt,setPrompt]=useState(""),[inputs,setInputs]=useState<Input[]>([]),[uploading,setUploading]=useState(false);
  const dialog=useRef<HTMLElement>(null),busy=useRef(action.busy),cancel=useRef(onCancel);busy.current=action.busy;cancel.current=onCancel;
  useEffect(()=>{const previous=document.activeElement as HTMLElement|null;
    const elements=()=>[...(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href]')||[])];
    elements()[0]?.focus();
    const key=(event:KeyboardEvent)=>{if(event.key==="Escape"&&!busy.current){event.preventDefault();cancel.current();}if(event.key!=="Tab")return;const all=elements();const first=all[0],last=all.at(-1);if(!all.length){event.preventDefault();dialog.current?.focus();}else if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}};
    document.addEventListener("keydown",key);return()=>{document.removeEventListener("keydown",key);previous?.focus();};
  },[]);
  return <div className="se-dialog-backdrop"><section ref={dialog} tabIndex={-1} className="se-dialog" role="dialog" aria-modal="true" aria-label={c("确认重跑输入","Confirm rerun inputs")}><h2>{c("确认新一轮运行的输入","Confirm inputs for a new run")}</h2><p>{c("新运行使用全新会话。旧模型回复、产物和备注不会带入。","The new run uses a fresh session. Previous model replies, artifacts and notes are not carried over.")}</p>
    <fieldset disabled={action.busy}><legend>{c("请选择输入方式","Choose input handling")}</legend><label><input type="radio" name="rerun-mode" checked={mode==="reuse_all"} onChange={()=>{setMode("reuse_all");setPlan(null);}}/>{c("带上全部用户输入与附件","Carry all user inputs and attachments")}</label><label><input type="radio" name="rerun-mode" checked={mode==="replace"} onChange={()=>{setMode("replace");setPlan(null);}}/>{c("重新提供（可以留空）","Provide new inputs (may be empty)")}</label></fieldset>
    <button disabled={!mode||action.busy} onClick={()=>void action.act(async()=>setPlan(await post<Plan>("/rerun-plans",{source_run_id:sourceRunId,mode})))}>{c("读取并检查输入","Review input list")}</button>
    {plan&&<div className="se-stack">{mode==="reuse_all"?<div className="se-input-review">{plan.content.source.messages.map((message,index)=><details key={`${message.id}-${index}`} open={index===0}><summary>{index+1}. {message.kind} · {message.attachment_refs.length} {c("个附件","attachments")}</summary><pre>{message.text||c("无用户提示","No user prompt")}</pre><ul>{message.attachment_refs.map(input=><li key={input.id}>{input.original_name} · {input.size} B</li>)}</ul></details>)}</div>:<><label>{c("新提示（可选）","New prompt (optional)")}<textarea value={prompt} onChange={e=>setPrompt(e.target.value)} disabled={action.busy}/></label><InputUpload items={inputs} onChange={setInputs} onUploadingChange={setUploading} disabled={action.busy}/></>}
      <button className="primary" disabled={action.busy||uploading} onClick={()=>void action.act(async()=>{const confirmed=await post<Plan>(`/rerun-plans/${plan.id}/confirm`,{expected_hash:plan.hash,prompt:mode==="replace"?prompt:"",input_ids:mode==="replace"?inputs.map(i=>i.id):[]});await onConfirm(confirmed);})}>{c("确认输入并重跑","Confirm inputs and rerun")}</button></div>}
    <ErrorBox error={action.error}/><button disabled={action.busy} onClick={onCancel}>{c("返回编辑","Back to editing")}</button>
  </section></div>;
}
