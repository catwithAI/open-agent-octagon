import {it,expect,vi} from "vitest";
import {fireEvent,screen} from "@testing-library/react";
import {ArtifactPreview} from "./ArtifactPreview";
import {apiMock,jsonResponse,view} from "./test-utils";

it("renders exact final text and preserves an explicit download fallback",async()=>{
 apiMock(path=>path==="/artifacts/a/preview"?jsonResponse({artifact:{name:"report.txt",type:"text",ref:"a"},status:"ready",text:"final bytes",content:null,capability_gaps:[]}):undefined);
 const close=vi.fn();view(<ArtifactPreview descriptorPath="/artifacts/a/preview" downloadUrl="/api/skill-eval/artifacts/a" onClose={close}/>);await screen.findByText("final bytes");expect(screen.getByText("下载原文件").getAttribute("href")).toBe("/api/skill-eval/artifacts/a");fireEvent.click(screen.getByText("关闭预览"));expect(close).toHaveBeenCalledTimes(1);
});
it("shows unsupported previews without pretending the result is empty",async()=>{
 apiMock(()=>jsonResponse({artifact:{name:"data.bin",type:"binary"},status:"unsupported",text:null,content:null,capability_gaps:["unsupported_format"]}));view(<ArtifactPreview descriptorPath="/artifacts/b/preview" downloadUrl="/download" onClose={()=>{}}/>);await screen.findByText(/此文件暂不能预览/);expect(screen.getByText(/unsupported_format/)).toBeTruthy();
});
