import {it,expect} from "vitest";
import {act,renderHook} from "@testing-library/react";
import {usePathFilter} from "./usePathFilter";

it("searches the full list while rendering bounded pages",()=>{
  const items=Array.from({length:1000},(_,index)=>`assets/${index}.svg`);
  const {result}=renderHook(()=>usePathFilter(items,item=>item,100));
  expect(result.current.matched).toHaveLength(1000);
  expect(result.current.visible).toHaveLength(100);
  expect(result.current.hasMore).toBe(true);
  act(()=>result.current.showMore());
  expect(result.current.visible).toHaveLength(200);
  act(()=>result.current.setQuery("assets/999"));
  expect(result.current.matched).toEqual(["assets/999.svg"]);
  expect(result.current.visible).toEqual(["assets/999.svg"]);
  act(()=>result.current.setQuery("missing"));
  expect(result.current.matched).toHaveLength(0);
  expect(result.current.hasMore).toBe(false);
});
