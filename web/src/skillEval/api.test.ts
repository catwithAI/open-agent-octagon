import {describe,it,expect,vi} from "vitest";
import {request,post,EvalApiError} from "./api";
import {apiMock,jsonResponse} from "./test-utils";

describe("Skill Eval API",()=>{
 it("preserves conflict code and sends the same supplied idempotency key",async()=>{const fetch=apiMock(()=>jsonResponse({error:{code:"version_conflict",message:"Choose another version"}},409));await expect(post("/imports/i/commit",{version:"1"},"stable-key")).rejects.toMatchObject({status:409,code:"version_conflict"});expect(new Headers(fetch.mock.calls[0][1]?.headers).get("Idempotency-Key")).toBe("stable-key");});
 it("passes cancellation to fetch and does not turn errors into success",async()=>{const abort=new AbortController();abort.abort();const fetch=vi.fn((_url,options)=>Promise.reject(options.signal.reason));vi.stubGlobal("fetch",fetch);await expect(request("/models",{signal:abort.signal})).rejects.toBeDefined();expect(fetch.mock.calls[0][1].signal).toBe(abort.signal);expect(new EvalApiError(403,"disabled","Disabled").status).toBe(403);});
});
