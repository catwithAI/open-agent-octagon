import {useEffect,useRef,useState} from "react";
import {url} from "./api";

export function useRunEvents(runId:string,onUpdate:()=>void){
  const handler=useRef(onUpdate);handler.current=onUpdate;
  const [state,setState]=useState("connecting"),[cursor,setCursor]=useState(0);
  useEffect(()=>{if(typeof EventSource==="undefined"){setState("polling");const interval=setInterval(()=>handler.current(),2000);return()=>clearInterval(interval);}let last=0;let timer:ReturnType<typeof setTimeout>|undefined;const source=new EventSource(url(`/runs/${runId}/events/stream`));
    source.onopen=()=>setState("connected");source.onerror=()=>setState("reconnecting");
    const receive=(event:MessageEvent)=>{try{const payload=JSON.parse(event.data);const seq=Number(payload.seq||event.lastEventId);if(!Number.isFinite(seq)||seq<=last)return;last=seq;setCursor(seq);if(!timer)timer=setTimeout(()=>{timer=undefined;handler.current();},150);}catch{setState("invalid_event");}};
    source.addEventListener("event",receive as EventListener);
    source.addEventListener("reset",()=>{last=0;setCursor(0);handler.current();});
    return()=>{source.close();if(timer)clearTimeout(timer);};
  },[runId]);
  return {state,cursor};
}
