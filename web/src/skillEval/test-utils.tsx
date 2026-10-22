import {afterEach,beforeEach,vi} from "vitest";
import {cleanup,render} from "@testing-library/react";
import {MemoryRouter} from "react-router-dom";
import {I18nProvider} from "../i18n";
import type {ReactNode} from "react";
import type {Capabilities,Draft,Run} from "./types";

const memory=()=>{const values=new Map<string,string>();return {getItem:(key:string)=>values.get(key)??null,setItem:(key:string,value:string)=>values.set(key,String(value)),removeItem:(key:string)=>values.delete(key),clear:()=>values.clear(),key:(index:number)=>[...values.keys()][index]??null,get length(){return values.size;}};};
beforeEach(()=>{vi.stubGlobal("localStorage",memory());vi.stubGlobal("sessionStorage",memory());localStorage.setItem("octagon.lang","zh");vi.spyOn(window,"confirm").mockReturnValue(true);});
afterEach(()=>{cleanup();vi.restoreAllMocks();vi.unstubAllGlobals();});
export function view(ui:ReactNode,route="/skill-eval"){return render(<I18nProvider><MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter></I18nProvider>);}
export const entry={file_id:"f1",path:"SKILL.md",hash:"a".repeat(64),size:30,mode:420,encoding:"utf-8"};
export const draft:Draft={id:"d1",skill_id:"s1",source_run_id:null,source_release_id:"rel1",target_release_id:"rel1",base_version_id:"v1",manifest_hash:"h1",revision:1,entries:[entry],base_entries:[entry],version_is_saved:true};
export const release={id:"rel1",skill_id:"s1",version_label:"0.0.1",status:"unpublished" as const,current_version_id:"v1",revision:1};
export const capabilities:Capabilities={enabled:true,display_only:false,execution:{status:"unverified",reason:"live_acceptance_required"},agents:[{id:"blade-agent"}],limits:{},scope:{enforcement_verified:false}};
export const run:Run={id:"r1",skill_id:"s1",experiment_id:"exp1",draft_id:"d1",skill_version_id:"v1",source_release_id:"rel1",source_release_label:"0.0.1",source_release_revision:1,run_status:"waiting_continue",generation_status:"completed",capture_status:"complete",artifact_status:"pending",requested_model:"fixture",resolved_model:null,external_refs:{},input_snapshot:{prompt:"input",system_startup_message:null,inputs:[],capture:{policy:{requested:"full",effective:"full",downgrade_reason:null}}},note:{text:"",revision:0},skill:{id:"s1",name:"sample"},experiment:{id:"exp1",skill_id:"s1",name:"experiment"},final_result:null,error_code:null,error_message:null,ended_at:null,chat_replies:[]};
export function jsonResponse(body:unknown,status=200){return new Response(JSON.stringify(body),{status,headers:{"Content-Type":"application/json"}});}
export function apiMock(extra:(path:string,init?:RequestInit)=>Response|Promise<Response>|undefined){
  const mock=vi.fn(async(input:RequestInfo|URL,init?:RequestInit)=>{const path=String(input).replace("/api/skill-eval","");const response=extra(path,init);if(response!==undefined)return response;
    if(path==="/capabilities")return jsonResponse(capabilities);
    if(path==="/models")return jsonResponse({status:"available",default:"fixture",models:[{id:"fixture",label:"Fixture"}]});
    if(path==="/releases/rel1")return jsonResponse(release);
    throw new Error(`Unexpected request: ${init?.method||"GET"} ${path}`);
  });vi.stubGlobal("fetch",mock);return mock;
}
