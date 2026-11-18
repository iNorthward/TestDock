(function (root) {
  'use strict';
  const ACTIVE = new Set(['queued', 'running', 'cancel_requested']);
  const TERMINAL = new Set(['done', 'failed', 'incomplete', 'skipped', 'cancelled', 'timed_out', 'interrupted']);
  const STORAGE_KEY = 'platform.executionRequests.v1';
  const requestId = () => root.crypto?.randomUUID?.() || Date.now().toString(36)+'-'+Math.random().toString(36).slice(2);
  const validIdentifier = value => typeof value === 'string' && value.length > 0 && value.length <= 128 && value.trim() === value;

  async function fingerprint(url, body) {
    const raw = url+'\n'+JSON.stringify(body);
    if (root.crypto?.subtle) {
      try {
        const hash = await root.crypto.subtle.digest('SHA-256', new TextEncoder().encode(raw));
        return {key: Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2,'0')).join(''), persisted: true};
      } catch (_) { /* Memory-only fallback; never persist the raw payload. */ }
    }
    return {key: raw, persisted: false};
  }

  function createFetch(original, {storage, identify = requestId} = {}) {
    const memory = new Map();
    const readRecords = () => {
      let records = {};
      try { records = JSON.parse(storage?.getItem(STORAGE_KEY) || '{}'); } catch (_) {}
      if (!records || typeof records !== 'object' || Array.isArray(records)) records = {};
      for (const key of Object.keys(records)) {
        const entry = records[key];
        if (!/^[a-f0-9]{64}$/.test(key) || !entry || !validIdentifier(entry.id) ||
            (entry.executionId !== undefined && !validIdentifier(entry.executionId))) {
          delete records[key];
        } else {
          // Older records included expires. Unknown outcomes do not become safe to replay with age.
          records[key] = {id: entry.id, ...(entry.executionId === undefined ? {} : {executionId: entry.executionId})};
        }
      }
      return records;
    };
    const writeRecords = records => {
      try { storage?.setItem(STORAGE_KEY, JSON.stringify(records)); } catch (_) {}
    };
    const updateEntry = (key, entry, persisted, remove = false) => {
      if (memory.get(key)?.id === entry.id) {
        if (remove) memory.delete(key); else memory.set(key, entry);
      }
      if (!persisted) return;
      const records = readRecords();
      if (records[key]?.id !== entry.id) return;
      if (remove) delete records[key]; else records[key] = entry;
      writeRecords(records);
    };
    const executionFetch = async function(url, init = {}) {
      const {onExecutionRequest, ...options} = init;
      if (typeof url !== 'string' || !url.startsWith('/api/') || String(init.method).toUpperCase() !== 'POST') {
        return original(url, onExecutionRequest ? options : init);
      }
      let body;
      try { body = JSON.parse(init.body || '{}'); } catch (_) { return original(url, onExecutionRequest ? options : init); }
      if (!body || typeof body !== 'object' || Array.isArray(body) || body.requestId || url === '/api/execution/cancel') {
        return original(url, onExecutionRequest ? options : init);
      }
      const {key, persisted} = await fingerprint(url, body);
      const records = persisted ? readRecords() : {};
      const saved = records[key] || memory.get(key);
      const entry = saved || {id: identify()};
      memory.set(key, entry);
      if (persisted) { records[key] = entry; writeRecords(records); }
      onExecutionRequest?.(entry.id);
      const response = await original(url, {...options, headers: {...init.headers, 'Content-Type':'application/json'},
                                           body: JSON.stringify({...body, requestId: entry.id})});
      try {
        const payload = await response.clone().json();
        if (response.status < 500) {
          const data = payload.data;
          if (validIdentifier(data?.executionId) && (data.pending === true || body.background === true)) {
            // A terminal query acknowledges this exact submission, including after a reload.
            updateEntry(key, {...entry, executionId: data.executionId}, persisted);
          } else if (!data?.pending && typeof payload.ok === 'boolean') {
            updateEntry(key, entry, persisted, true);
          }
        }
      } catch (_) { /* Keep the request ID when the outcome is unknown. */ }
      return response;
    };
    executionFetch.completeExecution = function(executionId, originalRequestId) {
      if (!validIdentifier(executionId)) return;
      const matches = entry => entry.executionId === executionId ||
        (entry.executionId === undefined && entry.id === originalRequestId);
      for (const [key, entry] of memory) {
        if (matches(entry)) memory.delete(key);
      }
      const records = readRecords();
      for (const key of Object.keys(records)) {
        if (matches(records[key])) delete records[key];
      }
      writeRecords(records);
    };
    // Compatibility for callers that still hold their exact original request body.
    executionFetch.completeRequest = async function(url, body) {
      const {key, persisted} = await fingerprint(url, body);
      memory.delete(key);
      if (persisted) { const records = readRecords(); delete records[key]; writeRecords(records); }
    };
    return executionFetch;
  }

  async function lookupReceipt(fetcher, requestId, kind) {
    if (!validIdentifier(requestId) || !validIdentifier(kind)) throw new TypeError('请求号或任务类型无效');
    const query = new URLSearchParams({requestId, kind});
    const response = await fetcher('/api/execution/receipt?' + query, {cache: 'no-store'});
    const payload = await response.json();
    if (response.status === 404 && payload.data?.state === 'not_found') return null;
    const receipt = payload.data;
    if (!response.ok || payload.ok !== true || !validIdentifier(receipt?.id) || receipt.kind !== kind ||
        !(receipt.state === 'active' && ACTIVE.has(receipt.status) ||
          receipt.state === 'terminal' && TERMINAL.has(receipt.status) ||
          receipt.state === 'expired' && receipt.resultExpired === true && TERMINAL.has(receipt.originalExecutionStatus))) {
      throw new Error(payload.error || '原请求回执查询未完成');
    }
    return receipt;
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

  const api = {createFetch, lookupReceipt, pollJob, isActive: status => ACTIVE.has(status)};
  root.PLATFORMExecution = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') {
    let storage;
    try { storage = window.sessionStorage; } catch (_) {}
    window.fetch = createFetch(window.fetch.bind(window), {storage});
  }
})(globalThis);
