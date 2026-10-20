const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { Readable } = require('node:stream');
const { getJson, selectPage, findPage } = require('../../src/providers/chatgpt/scripts/browser/cdp.cjs');
const { readJsonInput } = require('../../src/providers/chatgpt/scripts/io.cjs');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

async function serverFor(t, callback) {
  const server = http.createServer(callback);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => {
    server.closeAllConnections();
    server.close(resolve);
  }));
  return `http://127.0.0.1:${server.address().port}`;
}

test('CDP selects exact ChatGPT origin and ignores lookalike tabs', () => {
  const page = { type: 'page', url: 'https://chatgpt.com/c/a', webSocketDebuggerUrl: 'ws://local/tab' };
  assert.equal(selectPage([
    { ...page, url: 'https://chatgpt.com.example.org' },
    { ...page, type: 'worker' },
    page,
  ]), page);
  assert.throws(() => selectPage([]), /no ChatGPT tab/);
  assert.throws(() => selectPage({}), /invalid page list/);
});

test('shared CDP metadata lookup works with a local fake debugger', async t => {
  const page = { type: 'page', url: 'https://chatgpt.com', webSocketDebuggerUrl: 'ws://local/tab' };
  const base = await serverFor(t, (request, response) => {
    assert.equal(request.url, '/json/list');
    response.end(JSON.stringify([page]));
  });
  assert.deepEqual(await findPage(base), page);
});

test('invalid JSON and HTTP errors are reported', async t => {
  const base = await serverFor(t, (request, response) => {
    response.statusCode = request.url === '/error' ? 503 : 200;
    response.end('invalid');
  });
  await assert.rejects(getJson(`${base}/error`), /HTTP 503/);
  await assert.rejects(getJson(`${base}/invalid`), /invalid Chrome debugger metadata/);
});

test('a stalled debugger is bounded by the request timeout', async t => {
  const base = await serverFor(t, () => {});
  await assert.rejects(getJson(base, 25), /timed out/);
});

test('shared stdin handles UTF-8 chunks and rejects invalid or excessive input', async () => {
  assert.deepEqual(await readJsonInput(Readable.from(['{"text":"', 'ç', '"}'])), { text: 'ç' });
  await assert.rejects(readJsonInput(Readable.from(['bad'])), /invalid JSON input/);
  await assert.rejects(readJsonInput(Readable.from(['{"huge":"too big"}']), 5), /size limit/);
});

test('successful auth capture survives a debugger error during intentional close', async () => {
  let output='';
  class Socket {
    constructor(){this.listeners={};queueMicrotask(()=>this.listeners.open());}
    addEventListener(name,fn){this.listeners[name]=fn;}
    send(){
      if(this.sent)return;this.sent=true;
      for(const [method,params] of [
        ['Network.requestWillBeSent',{requestId:'own',request:{method:'GET',url:'https://chatgpt.com/backend-api/me',headers:{}}}],
        ['Network.requestWillBeSentExtraInfo',{requestId:'own',headers:{}}],
        ['Network.responseReceived',{requestId:'own',response:{status:200}}],
      ])queueMicrotask(()=>this.listeners.message({data:JSON.stringify({method,params})}));
    }
    close(){this.listeners.error();}
  }
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../src/providers/chatgpt/scripts/browser/auth.cjs'),'utf8'),{
    require: name=>name==='url'?{URL}:{findPage:async()=>({webSocketDebuggerUrl:'ws://fake'})},
    WebSocket:Socket,URL,setTimeout:()=>0,clearTimeout:()=>{},
    process:{env:{},stdout:{write(v){output+=v;}},stderr:{write(){}},exit(code){throw Error('unexpected exit '+code);}},
  });
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(JSON.parse(output).url,'https://chatgpt.com/backend-api/me');
});
