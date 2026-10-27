import {useEffect,useState} from "react";
import {Link,useSearchParams} from "react-router-dom";
import {request} from "./api";
import {ErrorBox,useCopy} from "./common";
import {RunHistory} from "./RunHistory";
import type {Group,Page} from "./types";
import "./styles.css";

export function Records(){
  const c=useCopy();const [params,setParams]=useSearchParams();const [page,setPage]=useState<Page<Group>>({items:[],next_cursor:null}),[error,setError]=useState<unknown>(null);
  const cursor=params.get("group_cursor")||"",expanded=params.get("group")||"";
  useEffect(()=>{const abort=new AbortController();request<Page<Group>>(`/record-groups${cursor?`?cursor=${encodeURIComponent(cursor)}`:""}`,{signal:abort.signal}).then(setPage).catch(e=>{if(!abort.signal.aborted)setError(e);});return()=>abort.abort();},[cursor]);
  const setGroup=(key:string)=>{const q=new URLSearchParams(params);if(expanded===key)q.delete("group");else q.set("group",key);q.delete("history_cursor");setParams(q);};
  return <div className="se"><header className="se-page-header"><div><h1>Skill Eval Record</h1><p>{c("按智能体与模型归组，展开查看每次运行。","Grouped by agent and model. Expand a group to inspect individual runs.")}</p></div><Link to="/skill-eval">{c("开始评测","Start an evaluation")}</Link></header><ErrorBox error={error}/>
    {!page.items.length&&!error&&<p className="se-empty">{c("尚无运行记录。","No runs yet.")}</p>}
    <div className="se-groups">{page.items.map(group=><section key={group.group_key} className="se-group"><button className="se-group-header" aria-expanded={expanded===group.group_key} onClick={()=>setGroup(group.group_key)}><span>{expanded===group.group_key?"−":"+"} {group.agent}</span><strong>{group.resolved_model||group.requested_model}</strong><small>{group.resolved_provider||group.requested_provider||c("提供商未确认","Provider unverified")}</small><span>{group.run_count} {c("次运行","runs")}</span>{group.group_key.startsWith("requested/unverified:")&&<small>{c("实际身份待确认","Actual identity unverified")}</small>}</button>{expanded===group.group_key&&<RunHistory groupKey={group.group_key}/>}</section>)}</div>
    <div className="se-toolbar"><button disabled={!cursor} onClick={()=>{const q=new URLSearchParams(params);q.delete("group_cursor");setParams(q);}}>{c("首页","First page")}</button><button disabled={!page.next_cursor} onClick={()=>{const q=new URLSearchParams(params);q.set("group_cursor",page.next_cursor!);setParams(q);}}>{c("下一页","Next page")}</button></div>
  </div>;
}
