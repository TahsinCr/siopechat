const {test}=require('node:test');
const assert=require('node:assert/strict');
const {connect}=require('../../src/providers/scripts/connection.cjs');
test('a disconnected debugger rejects pending work immediately and refuses later calls',async()=>{
 const previous=global.WebSocket;
 class Socket {
  constructor(){this.handlers={};Socket.last=this;queueMicrotask(()=>this.emit('open'));}
  addEventListener(name,fn){(this.handlers[name]??=[]).push(fn);}
  emit(name){for(const fn of this.handlers[name]||[])fn({});}
  send(){queueMicrotask(()=>this.emit('close'));}
  close(){this.emit('close');}
 }
 global.WebSocket=Socket;
 try{
  const c=await connect('ws://127.0.0.1:9222/devtools/page/test',new URL('http://127.0.0.1:9222'));
  const start=Date.now();
  await assert.rejects(c.call('Runtime.evaluate',{},5000),/disconnected/);
  assert.ok(Date.now()-start<1000);
  await assert.rejects(c.call('Runtime.evaluate'),/disconnected/);
  c.close();
 }finally{global.WebSocket=previous;}
});
