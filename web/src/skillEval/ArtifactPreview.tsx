import {useEffect,useState} from "react";
import {DocumentViewer,PresentationViewer,WorkbookViewer,HtmlArtifactView} from "../pages/RunDetail";
import type {ArtifactPreviewDescriptor} from "../api/client";
import {request} from "./api";
import {ErrorBox,useCopy} from "./common";

export function ArtifactPreview({descriptorPath,downloadUrl,onClose}:{descriptorPath:string;downloadUrl:string;onClose:()=>void}){
  const c=useCopy();const [descriptor,setDescriptor]=useState<(ArtifactPreviewDescriptor&{text?:string|null})|null>(null),[error,setError]=useState<unknown>(null),[media,setMedia]=useState("");
  useEffect(()=>{const abort=new AbortController();let timer:ReturnType<typeof setTimeout>|undefined;let objectUrl="";
    setDescriptor(null);setError(null);setMedia("");
    const load=async()=>{try{const value=await request<ArtifactPreviewDescriptor&{text?:string|null}>(descriptorPath,{signal:abort.signal});if(abort.signal.aborted)return;setDescriptor(value);if(value.status==="rendering"){timer=setTimeout(()=>void load(),value.poll_after_ms||500);return;}
      if(value.status==="ready"&&["image","video","audio"].includes(value.artifact.type)){const response=await fetch(downloadUrl,{signal:abort.signal});if(!response.ok)throw new Error(`HTTP ${response.status}`);const blob=await response.blob();if(!abort.signal.aborted){objectUrl=URL.createObjectURL(new Blob([blob],{type:value.artifact.media_type}));setMedia(objectUrl);}}
    }catch(e){if(!abort.signal.aborted)setError(e);}};void load();return()=>{abort.abort();if(timer)clearTimeout(timer);if(objectUrl)URL.revokeObjectURL(objectUrl);};
  },[descriptorPath,downloadUrl]);
  const content=descriptor?.content;
  return <section className="se-panel se-artifact-preview"><header><h2>{descriptor?.artifact.name||c("产物预览","Artifact preview")}</h2><div className="se-toolbar"><a href={downloadUrl}>{c("下载原文件","Download original")}</a><button onClick={onClose}>{c("关闭预览","Close preview")}</button></div></header><div className="se-panel-body"><ErrorBox error={error}/>
    {!descriptor&&!error&&<p>{c("正在检查文件…","Checking file…")}</p>}{descriptor?.status==="rendering"&&<p role="status">{c("正在生成预览…","Rendering preview…")}</p>}
    {descriptor?.status==="ready"&&<>{descriptor.artifact.type==="text"&&<pre>{descriptor.text}</pre>}{descriptor.artifact.type==="html"&&<HtmlArtifactView html={descriptor.text||""}/>}{descriptor.artifact.type==="image"&&media&&<img src={media} alt={descriptor.artifact.name} style={{maxWidth:"100%"}}/>}{descriptor.artifact.type==="video"&&media&&<video src={media} controls style={{maxWidth:"100%"}}/>}{descriptor.artifact.type==="audio"&&media&&<audio src={media} controls/>}{content?.kind==="workbook"&&<WorkbookViewer workbook={content}/>} {content?.kind==="presentation"&&<PresentationViewer presentation={content}/>} {content?.kind==="document"&&<DocumentViewer document={content}/>}</>}
    {descriptor&&["unsupported","failed"].includes(descriptor.status)&&<p className="se-hint">{c("此文件暂不能预览，可下载原文件。","Preview unavailable; download the original file.")} {descriptor.error?.message}</p>}
    {descriptor?.capability_gaps.length?<p className="se-hint">{c("预览限制：","Preview limitations: ")}{descriptor.capability_gaps.join(", ")}</p>:null}
  </div></section>;
}
