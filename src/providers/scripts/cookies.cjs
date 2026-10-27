// Read cookies only from an explicitly selected local debugging browser.
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const base = new URL(input.cdp_url);
  if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(base.hostname)) throw Error('local debugger required');
  const providerUrl = new URL(input.origin);
  const origin = providerUrl.origin;
  const claudeCookies = new Set(['sessionKey','lastActiveOrg','anthropic-device-id','__cf_bm','cf_clearance']);
  if (!['https://gemini.google.com', 'https://claude.ai'].includes(origin)) throw Error('unsupported cookie origin');
  const response = await fetch(new URL('/json/list', base), {signal: AbortSignal.timeout(5000)});
  const tabs = await response.json();
  const tab = tabs.find(t => { try { return new URL(t.url).origin === origin && t.type === 'page'; } catch { return false; } });
  if (!tab) throw Error('Open Gemini in the connected browser first');
  const wsurl = new URL(tab.webSocketDebuggerUrl);
  if (!['127.0.0.1', 'localhost'].includes(wsurl.hostname)) throw Error('local websocket required');
  const socket = new WebSocket(wsurl);
  const timeout = setTimeout(() => { socket.close(); process.exit(1); }, 8000);
  socket.addEventListener('open', () => socket.send(JSON.stringify({id: 1, method:'Network.getCookies', params:{urls:[origin]}})));
  socket.addEventListener('message', event => {
    const data = JSON.parse(event.data);
    if (data.id !== 1) return;
    if (data.error) throw Error('cookie capture failed');
    const cookies = {};
    for (const cookie of data.result.cookies || []) {
      const domain = String(cookie.domain || '').replace(/^\./, '').toLowerCase();
      const appliesToOrigin = providerUrl.hostname === domain || providerUrl.hostname.endsWith(`.${domain}`);
      const allowed = origin === 'https://gemini.google.com'
        ? appliesToOrigin
        : claudeCookies.has(cookie.name);
      if (allowed && cookie.name && typeof cookie.value === 'string') cookies[cookie.name] = cookie.value;
    }
    process.stdout.write(JSON.stringify(cookies));
    clearTimeout(timeout); socket.close();
  });
})().catch(() => { process.stderr.write('Session capture failed; check the connected browser'); process.exit(1); });
