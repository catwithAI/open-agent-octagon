import {it,expect} from "vitest";
import {fireEvent,screen} from "@testing-library/react";
import {RunNote} from "./RunNote";
import {apiMock,jsonResponse,view} from "./test-utils";

it("allows saving, clearing and editing notes on an ended result without changing model inputs",async()=>{const fetch=apiMock((_path,init)=>jsonResponse({text:JSON.parse(String(init?.body)).text,revision:1}));view(<RunNote runId="r1" note={{text:"",revision:0}} onSaved={()=>{}}/>);fireEvent.change(screen.getByLabelText("运行备注"),{target:{value:"observation"}});fireEvent.click(screen.getByText("保存备注"));await screen.findByText("备注已保存");expect(fetch.mock.calls[0][1]?.method).toBe("PUT");expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({text:"observation",expected_revision:0});fireEvent.click(screen.getByText("清空内容"));expect((screen.getByLabelText("运行备注") as HTMLTextAreaElement).value).toBe("");});
it("keeps local note text after a revision conflict",async()=>{apiMock(()=>jsonResponse({error:{code:"revision_conflict",message:"Changed elsewhere"}},409));view(<RunNote runId="r1" note={{text:"",revision:0}} onSaved={()=>{}}/>);fireEvent.change(screen.getByLabelText("运行备注"),{target:{value:"keep this"}});fireEvent.click(screen.getByText("保存备注"));await screen.findByText("Changed elsewhere");expect((screen.getByLabelText("运行备注") as HTMLTextAreaElement).value).toBe("keep this");});
