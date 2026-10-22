import {it,expect,vi} from "vitest";
import {act,renderHook} from "@testing-library/react";
import {useRunEvents} from "./useRunEvents";
import "./test-utils";

it("deduplicates replayed cursor notifications and closes the subscription on unmount",()=>{vi.useFakeTimers();class Source {static all:Source[]=[];onopen:(()=>void)|null=null;onerror:(()=>void)|null=null;listeners:Record<string,(event:MessageEvent)=>void>={};close=vi.fn();constructor(public url:string){Source.all.push(this);}addEventListener(kind:string,callback:(event:MessageEvent)=>void){this.listeners[kind]=callback;}}
 vi.stubGlobal("EventSource",Source);const update=vi.fn();const result=renderHook(()=>useRunEvents("r1",update));const source=Source.all[0];act(()=>{source.listeners.event(new MessageEvent("event",{data:JSON.stringify({seq:1})}));source.listeners.event(new MessageEvent("event",{data:JSON.stringify({seq:1})}));vi.advanceTimersByTime(200);});expect(update).toHaveBeenCalledTimes(1);expect(result.result.current.cursor).toBe(1);result.unmount();expect(source.close).toHaveBeenCalledTimes(1);vi.useRealTimers();});
