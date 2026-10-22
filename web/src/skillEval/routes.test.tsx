import {it,expect} from "vitest";
import {screen} from "@testing-library/react";
import {Routes} from "react-router-dom";
import {skillEvalRoutes} from "./routes";
import {apiMock,jsonResponse,run,view} from "./test-utils";

it.each(["zh","en"])("opens the upload route in %s with no configured catalog",async lang=>{
 localStorage.setItem("octagon.lang",lang);apiMock(path=>{if(path==="/skills")return jsonResponse({items:[]});if(path==="/catalog")return jsonResponse({status:"unconfigured",items:[]});});
 view(<Routes>{skillEvalRoutes}</Routes>);await screen.findByLabelText(lang==="zh"?"上传 Skill":"Upload Skill");expect(screen.getByText(lang==="zh"?"未配置只读目录，仍可上传":"No catalog configured; uploads remain available")).toBeTruthy();
});
it("opens a saved run URL using reads and aborts its requests on unmount",async()=>{
 const fetch=apiMock(path=>{if(path==="/runs/r1")return jsonResponse(run);if(path.endsWith("/messages")||path.endsWith("/interactions"))return jsonResponse({items:[]});});
 const rendered=view(<Routes>{skillEvalRoutes}</Routes>,"/skill-eval/runs/r1");await screen.findByText("等待继续");rendered.unmount();expect(fetch.mock.calls.every(([,init])=>!init?.method||init.method==="GET")).toBe(true);expect(fetch.mock.calls.filter(([,init])=>init?.signal).every(([,init])=>init?.signal?.aborted)).toBe(true);
});
