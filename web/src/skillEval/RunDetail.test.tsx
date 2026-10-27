import {it,expect} from "vitest";
import {screen} from "@testing-library/react";
import {Route,Routes} from "react-router-dom";
import {RunDetail} from "./RunDetail";
import {apiMock,jsonResponse,run,view} from "./test-utils";

it("reopens an existing session using reads only and keeps follow-up available after chat end",async()=>{const fetch=apiMock(path=>{if(path==="/runs/r1")return jsonResponse(run);if(path.endsWith("/messages")||path.endsWith("/interactions"))return jsonResponse({items:[]});});const rendered=view(<Routes><Route path="/skill-eval/runs/:runId" element={<RunDetail/>}/></Routes>,"/skill-eval/runs/r1");await screen.findByText("等待继续");expect(screen.getByLabelText("继续对话")).toBeTruthy();expect(screen.getByText("结束本次运行")).toBeTruthy();rendered.unmount();expect(fetch.mock.calls.every(([,init])=>!init?.method||init.method==="GET")).toBe(true);});
