(function (root) {
  'use strict';
  const ACTIVE = new Set(['queued', 'running', 'cancel_requested']);
  const TERMINAL = new Set(['done', 'failed', 'incomplete', 'skipped', 'cancelled', 'timed_out', 'interrupted']);
  const STORAGE_KEY = 'platform.executionRequests.v1';
  const requestId = () => root.crypto?.randomUUID?.() || Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);

  function createFetch(original, {storage, now = Date.now, identify = requestId} = {}) {
    const memory = new Map();
    const executionFetch = async function(url, init = {}) {
      if (typeof url !== 'string' || !url.startsWith('/api/') || String(init.method).toUpperCase() !== 'POST') {
        return original(url, init);
      }
      let body;
      try { body = JSON.parse(init.body || '{}'); } catch (_) { return original(url, init); }
      if (!body || typeof body !== 'object' || Array.isArray(body) || body.requestId || url === '/api/execution/cancel') {
        return original(url, init);
      }
      const raw = url+'\n'+JSON.stringify(body);
      let key = raw, persisted = false;
      if (root.crypto?.subtle) {
        try {
          const hash = await root.crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw));
          key = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2,'0')).join('');
          persisted = true;
        } catch (_) { /* Memory-only fallback; never persist the raw payload. */ }
      }
      const readRecords = () => {
        let records = {};
        try { records = persisted ? JSON.parse(storage?.getItem(STORAGE_KEY) || '{}') : {}; } catch (_) {}
        if (!records || typeof records !== 'object' || Array.isArray(records)) records = {};
        for (const name of Object.keys(records)) if (!records[name] || records[name].expires <= now()) delete records[name];
        return records;
      };
      const records = readRecords();
      const saved = records[key] || memory.get(key);
      const entry = saved && saved.expires > now() ? saved : {id: identify(), expires: now()+86400000};
      memory.set(key, entry);
      const save = (remove = false) => {
        if (!persisted) return;
        const current = readRecords();
        if (remove) delete current[key]; else current[key] = entry;
        try { storage?.setItem(STORAGE_KEY, JSON.stringify(current)); } catch (_) {}
      };
      save();
      const response = await original(url, {...init, headers: {...init.headers, 'Content-Type':'application/json'},
                                           body: JSON.stringify({...body, requestId: entry.id})});
      try {
        const payload = await response.clone().json();
        if (response.status < 500 && !payload.data?.pending) {
          memory.delete(key);
          save(true);
        }
      } catch (_) { /* Keep the request ID when the outcome is unknown. */ }
      return response;
    };
    executionFetch.completeRequest = async function(url, body) {
      const raw = url+'\n'+JSON.stringify(body);
      let key=raw,persisted=false;
      if(root.crypto?.subtle){try{const hash=await root.crypto.subtle.digest('SHA-256',new TextEncoder().encode(raw));key=Array.from(new Uint8Array(hash),byte=>byte.toString(16).padStart(2,'0')).join('');persisted=true;}catch(_) {}}
      memory.delete(key);
      if(persisted){try{const records=JSON.parse(storage?.getItem(STORAGE_KEY)||'{}');if(records&&typeof records==='object'&&!Array.isArray(records)){delete records[key];storage?.setItem(STORAGE_KEY,JSON.stringify(records));}}catch(_) {}}
    };
    return executionFetch;
  }

  async function pollJob(fetcher, url, {onProgress = () => {}, intervalMs = 800, timeoutMs = 1500000,
                                      now = Date.now, wait = ms => new Promise(resolve => setTimeout(resolve,ms))} = {}) {
    const started = now();
    let errors = 0;
    while (now()-started < timeoutMs) {
      let payload;
      try {
        payload = await fetcher(url, {cache:'no-store'}).then(response => response.json());
        if (!payload.ok || !payload.data) throw new Error(payload.error || '任务查询失败');
        errors = 0;
      } catch (error) {
        if (++errors >= 3) throw new Error('暂时无法查询进度；任务可能仍在运行，请恢复查询。'+error.message);
        await wait(intervalMs);
        continue;
      }
      onProgress(payload.data, Math.floor((now()-started)/1000));
      if (TERMINAL.has(payload.data.status)) return payload.data;
      if (!ACTIVE.has(payload.data.status)) throw new Error('任务状态无法识别，请核验后恢复查询');
      await wait(intervalMs);
    }
    throw new Error('进度等待超时；任务可能仍在完成清理，请恢复查询');
  }

  const api = {createFetch, pollJob, isActive: status => ACTIVE.has(status)};
  root.PLATFORMExecution = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') {
    let storage;
    try { storage = window.sessionStorage; } catch (_) {}
    window.fetch = createFetch(window.fetch.bind(window), {storage});
  }
})(globalThis);
