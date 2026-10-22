import {it,expect,vi} from "vitest";
import {fireEvent,screen,waitFor} from "@testing-library/react";
import {RunConfiguration} from "./RunConfiguration";
import {apiMock,jsonResponse,view} from "./test-utils";

it("runs with only a Skill and keeps the Blade-only selector and scope visible",async()=>{
 const fetch=apiMock(path=>{if(path.startsWith("/experiments?"))return jsonResponse({items:[{id:"e1",skill_id:"s1",name:"Experiment"}]});if(path==="/runs")return jsonResponse({run_id:"r1"});});const freeze=vi.fn().mockResolvedValue({snapshot_id:"v1"});view(<RunConfiguration skillId="s1" freeze={freeze}/>);await screen.findByRole("option",{name:"Fixture"});expect(screen.getByText(/暂不支持写入外部系统/)).toBeTruthy();expect((screen.getByLabelText("智能体") as HTMLSelectElement).options).toHaveLength(1);fireEvent.change(screen.getByLabelText("实验"),{target:{value:"e1"}});fireEvent.click(screen.getByText("运行 Skill"));await waitFor(()=>expect(fetch.mock.calls.some(([path])=>String(path).endsWith("/runs"))).toBe(true));const call=fetch.mock.calls.find(([path])=>String(path).endsWith("/runs"))!;expect(JSON.parse(String(call[1]?.body))).toMatchObject({prompt:"",input_ids:[],agent:"blade-agent",snapshot_id:"v1"});expect(freeze).toHaveBeenCalledTimes(1);
});
