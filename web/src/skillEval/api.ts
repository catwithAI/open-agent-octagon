const ROOT="/api/skill-eval";
export class EvalApiError extends Error {constructor(public status:number,public code:string,message:string){super(message);}}
export async function request<T>(path:string,options:RequestInit={}):Promise<T> {
  const headers=new Headers(options.headers);
  if(options.body && !(options.body instanceof FormData))headers.set("Content-Type","application/json");
  const response=await fetch(ROOT+path,{...options,headers,credentials:"same-origin"});
  if(!response.ok){
    let message=`HTTP ${response.status}`,code="request_failed";
    try {const body=await response.json();const error=body.error||body.detail;
      if(typeof error==="string")message=error;
      else if(error && !Array.isArray(error)){message=error.message||message;code=error.code||code;}
      else if(Array.isArray(error))message=error.map(e=>`${e.loc?.slice(1).join(".")}: ${e.msg}`).join("; ");
    }catch{/* Keep the status if the response was not JSON. */}
    throw new EvalApiError(response.status,code,message);
  }
  return response.json() as Promise<T>;
}
export const post=<T,>(path:string,body:unknown={},key?:string)=>request<T>(path,{method:"POST",body:JSON.stringify(body),headers:key?{"Idempotency-Key":key}:undefined});
export async function upload<T>(path:string,file:File){const data=new FormData();data.append("file",file);return request<T>(path,{method:"POST",body:data});}
export async function download(path:string,body?:unknown,filename="skill.zip"){
  const response=await fetch(ROOT+path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:undefined);
  if(!response.ok)throw new EvalApiError(response.status,"download_failed",`Download failed (${response.status})`);
  const url=URL.createObjectURL(await response.blob());const anchor=document.createElement("a");anchor.href=url;anchor.download=filename;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
export const url=(path:string)=>ROOT+path;
