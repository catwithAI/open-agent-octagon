import {useMemo,useState} from "react";

/** Filter a large file list by path and render it in bounded pages. */
export function usePathFilter<T>(items:T[],path:(item:T)=>string,page=250){
  const [query,setQueryValue]=useState(""),[limit,setLimit]=useState(page);
  const needle=query.trim().toLowerCase();
  const matched=useMemo(()=>needle?items.filter(item=>path(item).toLowerCase().includes(needle)):items,[items,needle,path]);
  const visible=matched.slice(0,limit);
  const setQuery=(value:string)=>{setQueryValue(value);setLimit(page);};
  return {query,setQuery,visible,matched,hasMore:matched.length>visible.length,showMore:()=>setLimit(limit=>limit+page),reset:()=>{setQueryValue("");setLimit(page);}};
}
