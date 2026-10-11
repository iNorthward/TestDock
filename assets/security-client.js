(function (root) {
  'use strict';
  function createFetch(original, {origin, session = null} = {}) {
    let credentials = session;
    return async function (input, init = {}) {
      const target = new URL(typeof input === 'string' ? input : input.url, origin);
      if (target.origin !== origin || !target.pathname.startsWith('/api/') || target.pathname === '/api/session') {
        return original(input, init);
      }
      if (!credentials) {
        credentials = original('/api/session', {cache: 'no-store'}).then(async response => {
          const data = await response.json();
          if (!response.ok || !data.token) throw new Error('面板会话初始化失败，请刷新页面');
          return data.token;
        }).catch(error => { credentials = null; throw error; });
      }
      const headers = new Headers(init.headers || (typeof input === 'object' ? input.headers : undefined));
      headers.set('X-TestDock-Token', await credentials);
      return original(input, {...init, headers});
    };
  }

  function eventStream(url, method) {
    const controller = new AbortController();
    const stream = {close: () => controller.abort(), onmessage: null, onerror: null};
    Promise.resolve().then(async () => {
      const init = {method, signal: controller.signal};
      if (method === 'POST') {
        init.headers = {'Content-Type': 'application/json'};
        init.body = '{}';
      }
      const response = await root.fetch(url, init);
      if (!response.ok) throw new Error((await response.json()).error || '巡检启动失败');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '', complete = false;
      try { while (!controller.signal.aborted) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, end); buffer = buffer.slice(end+2);
          const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (data) {
            const kind = JSON.parse(data).type;
            complete ||= kind === 'complete' || kind === 'error';
            stream.onmessage?.({data});
          }
        }
      } } finally {
        await reader.cancel();
        reader.releaseLock();
      }
      if (!complete && !controller.signal.aborted) throw new Error('巡检流提前断开');
    }).catch(error => { if (!controller.signal.aborted) stream.onerror?.(error); });
    return stream;
  }
  const postEvents = url => eventStream(url, 'POST');
  const getEvents = url => eventStream(url, 'GET');
  function requiresLiveConfirmation(item) {
    return item.mode === 'live' || Boolean(item.requires_write_opt_in || item.requires_unscoped_write_opt_in);
  }
  root.TestDockSecurity = {createFetch, postEvents, getEvents, requiresLiveConfirmation};
  if (typeof module !== 'undefined') module.exports = root.TestDockSecurity;
  if (typeof window !== 'undefined') window.fetch = createFetch(window.fetch.bind(window), {origin: window.location.origin});
})(globalThis);
