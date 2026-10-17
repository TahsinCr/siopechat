"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {createInPage} = require("../../src/providers/chatgpt/scripts/browser/project.cjs");

function fixture() {
  const state = {opened:false, submitted:0, memory:0, attached:true};
  global.HTMLInputElement = class { set value(value) { this.name=value; } dispatchEvent() {} };
  const input = new HTMLInputElement();
  const button = (text, click) => ({textContent:text, getAttribute:()=>null, click});
  const create = button("Create project", () => { state.submitted++; state.attached=false; });
  const memory = button("Project-only", () => { state.memory++; });
  const dialog = {querySelector:()=>input, querySelectorAll:()=>[create,memory]};
  const start = button("New project", () => { state.opened=true; });
  global.document = { querySelectorAll:selector => selector === '[role="dialog"]' ? (state.opened ? [dialog] : []) : [start], contains:()=>state.attached };
  return {state,input};
}

test("native project form submits once with the requested name and memory", async () => {
  const {state,input}=fixture();
  const result=await createInPage({name:"Demo",memory:"project_only",visibility:"private"});
  assert.equal(input.name,"Demo"); assert.equal(state.memory,1); assert.equal(state.submitted,1); assert.equal(result.submitted,true);
});

test("unsupported public creation fails before creating a private project", async () => {
  const {state}=fixture();
  await assert.rejects(createInPage({name:"Demo",memory:"default",visibility:"public"}), /Paylaşım/);
  assert.equal(state.submitted,0);
});
