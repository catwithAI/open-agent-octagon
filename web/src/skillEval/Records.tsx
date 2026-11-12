import {useEffect,useMemo,useState} from "react";
import {Link,useSearchParams} from "react-router-dom";
import {request} from "./api";
import {ErrorBox,useCopy} from "./common";
import {RunHistory} from "./RunHistory";
import type {Group,Page} from "./types";
import "./styles.css";

type DisplayGroup=Group&{display_key:string;group_keys:string[]};

function modelIdentity(group:Group){
  const requested=group.requested_model;const resolved=group.resolved_model;
  return resolved&&resolved!==requested?`${requested}\u001e${resolved}`:requested;
}

function modelLabel(group:Group){
  const requested=group.requested_model;const resolved=group.resolved_model;
  return resolved&&resolved!==requested?`${requested} → ${resolved}`:requested;
}

function identityOf(group:Group){
  return [group.agent,group.resolved_provider||group.requested_provider||"",modelIdentity(group)].join("\u001f");
}

function mergeGroups(groups:Group[]):DisplayGroup[]{
  const merged=new Map<string,DisplayGroup>();
  for(const group of groups){
    const key=identityOf(group);const current=merged.get(key);
    if(current){
      current.group_keys.push(group.group_key);
      current.run_count+=group.run_count;
      if(group.last_run_at>current.last_run_at)current.last_run_at=group.last_run_at;
    }else merged.set(key,{...group,display_key:key,group_keys:[group.group_key]});
  }
  return [...merged.values()].sort((a,b)=>b.last_run_at.localeCompare(a.last_run_at));
}

function selectedGroupKeys(value:string|null){
  if(!value)return [];
  if(value.startsWith("["))try{const parsed=JSON.parse(value);if(Array.isArray(parsed)&&parsed.every(item=>typeof item==="string"))return parsed as string[];}catch{/* fall through to the legacy single-key format */}
  return [value];
}

export function Records(){
  const c=useCopy();const [params,setParams]=useSearchParams();
  const [page,setPage]=useState<Page<Group>>({items:[],next_cursor:null}),[error,setError]=useState<unknown>(null);
  const cursor=params.get("group_cursor")||"";
  const selectedKeys=selectedGroupKeys(params.get("group"));
  const agent=params.get("agent_filter")||"",model=params.get("model_filter")||"",from=params.get("from_filter")||"",to=params.get("to_filter")||"",status=params.get("status_filter")||"";
  const filterKey=[agent,model,from,to,status].join("\u001f");
  const groups=useMemo(()=>mergeGroups(page.items),[page.items]);
  const agents=useMemo(()=>[...new Set(page.items.map(item=>item.agent))].sort(),[page.items]);

  useEffect(()=>{
    const abort=new AbortController();const q=new URLSearchParams();if(cursor)q.set("cursor",cursor);if(agent)q.set("agent",agent);if(model)q.set("model_like",model);if(status)q.set("status",status);
    if(from)q.set("created_after",`${from}T00:00:00+08:00`);if(to)q.set("created_before",`${to}T23:59:59+08:00`);
    const suffix=q.toString()?`?${q}`:"";request<Page<Group>>(`/record-groups${suffix}`,{signal:abort.signal}).then(setPage).catch(e=>{if(!abort.signal.aborted)setError(e);});return()=>abort.abort();
  },[cursor,filterKey]);

  const isExpanded=(group:DisplayGroup)=>selectedKeys.length>0&&selectedKeys.length===group.group_keys.length&&selectedKeys.every(key=>group.group_keys.includes(key));
  const setGroup=(group:DisplayGroup)=>{
    const q=new URLSearchParams(params);if(isExpanded(group))q.delete("group");else q.set("group",group.group_keys.length===1?group.group_keys[0]:JSON.stringify(group.group_keys));q.delete("history_cursor");setParams(q);
  };
  const filter=(name:string,value:string)=>{const q=new URLSearchParams(params);if(value)q.set(name,value);else q.delete(name);q.delete("group_cursor");q.delete("history_cursor");setParams(q);};
  const clearFilters=()=>{const q=new URLSearchParams(params);for(const name of ["agent_filter","model_filter","from_filter","to_filter","status_filter","group_cursor","history_cursor"])q.delete(name);setParams(q);};

  return <div className="se"><header className="se-page-header"><div><h1>Skill Eval Record</h1><p>{c("按智能体、提供商和模型归组，展开查看每次运行。未确认的实际身份会合并展示，并保留状态标记。","Grouped by agent, provider and model. Runs with an unverified identity are shown together with a status marker.")}</p></div><Link to="/skill-eval">{c("开始评测","Start an evaluation")}</Link></header><ErrorBox error={error}/>
    <div className="se-toolbar se-record-filters"><label>{c("智能体","Agent")}<select aria-label={c("筛选智能体","Filter agent")} value={agent} onChange={e=>filter("agent_filter",e.target.value)}><option value="">{c("全部","All")}</option>{agents.map(item=><option key={item} value={item}>{item}</option>)}</select></label><label>{c("模型模糊匹配","Model contains")}<input aria-label={c("筛选模型","Filter model")} placeholder={c("输入模型名片段","Type part of a model name")} value={model} onChange={e=>filter("model_filter",e.target.value)}/></label><label>{c("开始日期","From")}<input aria-label={c("开始日期","From date")} type="date" value={from} onChange={e=>filter("from_filter",e.target.value)}/></label><label>{c("结束日期","To")}<input aria-label={c("结束日期","To date")} type="date" value={to} onChange={e=>filter("to_filter",e.target.value)}/></label><label>{c("状态","Status")}<select aria-label={c("全局筛选状态","Global filter status")} value={status} onChange={e=>filter("status_filter",e.target.value)}><option value="">{c("全部","All")}</option>{["queued","preparing","active","waiting_input","waiting_continue","finishing","completed","failed","cancelled"].map(item=><option key={item} value={item}>{item}</option>)}</select></label>{[agent,model,from,to,status].some(Boolean)&&<button type="button" onClick={clearFilters}>{c("清除筛选","Clear filters")}</button>}</div>
    {!groups.length&&!error&&<p className="se-empty">{c("当前筛选下尚无运行记录。","No runs match the current filters.")}</p>}
    <div className="se-groups">{groups.map(group=>{const expanded=isExpanded(group);const unverified=group.group_keys.some(key=>key.startsWith("requested/unverified:"));return <section key={group.display_key} className="se-group"><button className="se-group-header" aria-expanded={expanded} onClick={()=>setGroup(group)}><span>{expanded?"−":"+"} {group.agent}</span><strong title={modelLabel(group)}>{modelLabel(group)}</strong><small>{group.resolved_provider||group.requested_provider||c("提供商未确认","Provider unverified")}</small><span>{group.run_count} {c("次运行","runs")}</span>{unverified&&<small>{group.group_keys.length>1?c("已合并未确认身份记录","Unverified identity records merged"):c("实际身份待确认","Actual identity unverified")}</small>}</button>{expanded&&<RunHistory groupKeys={group.group_keys}/>}</section>})}</div>
    <div className="se-toolbar"><button disabled={!cursor} onClick={()=>{const q=new URLSearchParams(params);q.delete("group_cursor");setParams(q);}}>{c("首页","First page")}</button><button disabled={!page.next_cursor} onClick={()=>{const q=new URLSearchParams(params);q.set("group_cursor",page.next_cursor!);setParams(q);}}>{c("下一页","Next page")}</button></div>
  </div>;
}
