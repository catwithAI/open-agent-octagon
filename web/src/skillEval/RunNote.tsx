import {useEffect,useRef,useState} from "react";
import {request} from "./api";
import {ErrorBox,useAction,useCopy,useLeaveGuard} from "./common";
import type {Note} from "./types";

export function RunNote({runId,note,onSaved,readOnly=false}:{runId:string;note:Note;onSaved:()=>void;readOnly?:boolean}){
  const c=useCopy(),action=useAction();const [text,setText]=useState(note.text),[revision,setRevision]=useState(note.revision),[saved,setSaved]=useState(note.text),[message,setMessage]=useState("");const current=useRef(runId);
  useEffect(()=>{if(current.current!==runId){current.current=runId;setText(note.text);setSaved(note.text);setRevision(note.revision);setMessage("");}else if(text===saved&&note.revision!==revision){setText(note.text);setSaved(note.text);setRevision(note.revision);}},[runId,note.revision]);
  useLeaveGuard(text!==saved);
  return <section className="se-note"><h3>{c("运行备注（可选）","Run note (optional)")}</h3><textarea aria-label={c("运行备注","Run note")} value={text} disabled={readOnly} placeholder={c("可记录本次实验的细节、观察或评价。","Record details, observations or comments about this run.")} onChange={e=>{setText(e.target.value);setMessage("");}}/>
    <div className="se-toolbar"><button disabled={readOnly||action.busy||text===saved} onClick={()=>void action.act(async()=>{const result=await request<Note>(`/runs/${runId}/note`,{method:"PUT",body:JSON.stringify({text,expected_revision:revision})});setRevision(result.revision);setSaved(result.text);setMessage(c("备注已保存","Note saved"));onSaved();})}>{c("保存备注","Save note")}</button><button disabled={readOnly||!text} onClick={()=>setText("")}>{c("清空内容","Clear text")}</button>{message&&<span role="status">{message}</span>}</div><ErrorBox error={action.error}/>
  </section>;
}
