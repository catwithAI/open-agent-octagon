import {useEffect,useState} from "react";
import {post} from "./api";
import {ErrorBox,InputUpload,useAction,useCopy,useSubmissionKey} from "./common";
import type {Input,Interaction,Run} from "./types";

export function ChatComposer({run,interaction,onSent,readOnly=false}:{run:Run;interaction?:Interaction;onSent:()=>void;readOnly?:boolean}){
  const c=useCopy(),action=useAction(),keys=useSubmissionKey();const [text,setText]=useState(""),[inputs,setInputs]=useState<Input[]>([]),[uploading,setUploading]=useState(false),[selections,setSelections]=useState<Record<string,number[]>>({}),[custom,setCustom]=useState<Record<string,string>>({});
  useEffect(()=>{setText("");setInputs([]);setSelections({});setCustom({});keys.done();},[run.id,interaction?.id]);
  const questions=interaction?.question.arguments?.questions||[];
  const generating=["running","queued","stopping"].includes(run.generation_status)||["queued","preparing","finishing"].includes(run.run_status);
  const ended=["completed","failed","cancelled"].includes(run.run_status);
  if(ended)return <p>{c("本次运行已结束，不能追加消息。可以微调后开启新运行。","This run has ended. Refine the Skill to start a new run.")}</p>;
  const hasAnswer=Object.values(selections).some(values=>values.length)||Object.values(custom).some(value=>value.trim());
  return <form className="se-chat-composer" onSubmit={event=>{event.preventDefault();void action.act(async()=>{const body={text,input_ids:inputs.map(input=>input.id),...(interaction?{expected_revision:interaction.revision,answer:{selections,custom}}:{})};const path=interaction?`/runs/${run.id}/interactions/${interaction.id}/answer`:`/runs/${run.id}/messages`;await post(path,body,keys.forBody(body));keys.done();setText("");setInputs([]);setSelections({});setCustom({});onSent();});}}>
    {interaction&&<section className="se-question"><h3>{c("智能体在等待你的回答","The agent is waiting for your answer")}</h3>{questions.map((question,index)=><fieldset key={index} disabled={readOnly||action.busy}><legend>{question.question}</legend>{question.options?.map((option,optionIndex)=><label key={optionIndex}><input type={question.multiSelect?"checkbox":"radio"} name={`question-${index}`} checked={selections[String(index)]?.includes(optionIndex)||false} onChange={e=>setSelections({...selections,[String(index)]:question.multiSelect?e.target.checked?[...(selections[String(index)]||[]),optionIndex]:(selections[String(index)]||[]).filter(i=>i!==optionIndex):[optionIndex]})}/>{option.label}<small>{option.description}</small></label>)}<label>{c("自定义回答","Custom answer")}<input value={custom[String(index)]||""} onChange={e=>setCustom({...custom,[String(index)]:e.target.value})}/></label></fieldset>)}</section>}
    <label>{interaction?c("补充说明（可选）","Additional details (optional)"):c("继续对话","Continue the conversation")}<textarea aria-label={interaction?c("补充说明","Additional details"):c("继续对话","Continue the conversation")} value={text} disabled={readOnly||generating||action.busy} onChange={e=>setText(e.target.value)} placeholder={c("输入追问、回答或补充说明，也可以附加文件。","Ask a follow-up, answer, add details or attach files.")}/></label>
    <InputUpload items={inputs} onChange={setInputs} onUploadingChange={setUploading} disabled={readOnly||generating||action.busy}/><button className="primary" disabled={readOnly||generating||action.busy||uploading||(!text.trim()&&!inputs.length&&!hasAnswer)}>{interaction?c("提交回答","Send answer"):c("发送","Send")}</button><ErrorBox error={action.error}/>
  </form>;
}
