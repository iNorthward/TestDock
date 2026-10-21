(function (root) {
  'use strict';
  const has = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const text = value => value === null || value === undefined || value === '' ? '—' : String(value);
  const identifier = value => typeof value === 'string' && value.trim() === value && value.length > 0 && value.length <= 128;
  function storageKey(kind, pack, page, scope) {
    if (![pack, page, scope].every(value => typeof value === 'string' && value.length > 0)) throw new TypeError('pack、page、scope 必须是非空文本');
    return 'platform.workbench.' + kind + '.' + [pack, page, scope].map(encodeURIComponent).join('.');
  }
  function store(storage, key, notice) {
    return {
      read() {try {const raw = storage?.getItem(key); return raw ? JSON.parse(raw) : null;} catch (_) {notice('无法读取浏览器保存内容，本次仍可继续操作。'); return null;}},
      write(value) {try {if (!storage) throw new Error(); if (value === null) storage.removeItem(key); else storage.setItem(key, JSON.stringify(value)); return true;} catch (_) {notice('浏览器存储不可用，刷新后无法恢复本次内容。'); return false;}},
    };
  }
  function feedback(node) {
    if (!node) throw new TypeError('需要反馈区域');
    node.setAttribute('role', 'status'); node.setAttribute('aria-live', 'polite'); node.setAttribute('aria-atomic', 'true');
    return {set(message, {kind = 'info'} = {}) {node.textContent = message || ''; node.hidden = !message; node.dataset.kind = ['info','success','warning','error'].includes(kind) ? kind : 'info';}};
  }
  function createTaskController({pack, page, scope, storage, fetcher = root.fetch, execution = root.PLATFORMExecution,
                                 onProgress = () => {}, onResult = () => {}, onState = () => {}, onNotice = () => {}, pollOptions = {}}) {
    if (!execution?.pollJob || typeof fetcher !== 'function') throw new TypeError('需要现有 PLATFORMExecution 和已受会话保护的 fetch');
    const saved = store(storage, storageKey('task.v1', pack, page, scope), onNotice);
    let task = null, watching = null;
    const snapshot = () => task ? {...task} : null;
    return {
      snapshot,
      remember({executionId}) {if (!identifier(executionId)) throw new TypeError('executionId 无效'); if (watching) throw new Error('正在查询原任务'); if (task && task.executionId !== executionId) throw new Error('原任务尚未确认终态，不能替换其标识'); task = {version: 1, executionId}; saved.write(task); onState('pending', snapshot()); return snapshot();},
      restore() {const value = saved.read(); if (value && value.version === 1 && identifier(value.executionId)) {task = {version: 1, executionId: value.executionId}; onState('pending', snapshot());} else if (value) onNotice('保存的任务版本或标识无效，请先核验原操作。'); return snapshot();},
      watch() {
        if (watching) return watching;
        if (!task) return Promise.resolve(null);
        const current = task; onState('querying', snapshot());
        watching = execution.pollJob(fetcher, '/api/execution/job?id=' + encodeURIComponent(current.executionId), {...pollOptions, onProgress})
          .then(async job => {
            const result = job.resultExpired === true ? {ok: null, status: 'incomplete', resultExpired: true, error: job.error || '执行证据已过期，请核验原操作。'} : (job.result ?? {ok: null, status: 'incomplete', error: job.error || '任务已结束，但缺少结果证据。'});
            // Only a confirmed terminal query releases the original request association.
            await fetcher.completeExecution?.(current.executionId);
            task = null; saved.write(null); onState('terminal', {executionId: current.executionId, jobStatus: job.status});
            onResult(result, job); return result;
          }).catch(error => {if (task === current) onState('unavailable', {...current, error: error.message || String(error)}); throw error;})
          .finally(() => {watching = null;});
        return watching;
      },
    };
  }
  function createDetailsDialog({dialog, body, status, content, project}) {
    if (!dialog || !status || !content || typeof project !== 'function') throw new TypeError('详情需要 dialog、status、content 和显式字段投影 project');
    const notice = feedback(status);
    let generation = 0, trigger = null, originalOverflow = '', loader = null, key, confirmed = null, hasResult = false;
    const setResult = value => {const projected = project(value); if (typeof projected !== 'string') throw new TypeError('详情投影必须返回允许展示的文本'); content.textContent = projected; confirmed = value; hasResult = true;};
    const focusables = () => [...dialog.querySelectorAll('button,[href],input,select,textarea,[tabindex]')].filter(node => !node.disabled && node.tabIndex !== -1 && (!node.getClientRects || node.getClientRects().length));
    const close = () => {if (dialog.open) dialog.close();};
    dialog.addEventListener('close', () => {generation++; if (body) body.style.overflow = originalOverflow; trigger?.focus?.(); trigger = null;});
    dialog.addEventListener('cancel', event => {event.preventDefault(); close();});
    dialog.addEventListener('keydown', event => {
      if (event.key !== 'Tab' || !dialog.open) return;
      const nodes = focusables(), first = nodes[0], last = nodes.at(-1), active = dialog.ownerDocument.activeElement;
      if (!first) {event.preventDefault(); dialog.focus?.();}
      else if (event.shiftKey && (active === first || !nodes.includes(active))) {event.preventDefault(); last.focus();}
      else if (!event.shiftKey && (active === last || !nodes.includes(active))) {event.preventDefault(); first.focus();}
    });
    dialog.addEventListener('click', event => {if (event.target !== dialog) return; const bounds = dialog.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close();});
    async function retry() {
      if (!dialog.open || !loader) return null;
      const token = ++generation; notice.set('正在读取详情…');
      try {const value = await loader(); if (!dialog.open || token !== generation) return null; setResult(value); notice.set('已读取详情。', {kind: 'success'}); return value;}
      catch (error) {if (dialog.open && token === generation) notice.set('详情读取失败：' + (error.message || String(error)) + (hasResult ? '；已保留先前确认的结果，可重试只读查询。' : '；可重试只读查询。'), {kind: 'warning'}); return null;}
    }
    return {
      setResult, retry, close,
      snapshot: () => ({open: dialog.open, hasResult, result: confirmed}),
      open(opener, options = {}) {
        if (typeof options.key !== 'string' || !options.key) throw new TypeError('详情需要显式记录作用域 key');
        generation++;
        if (!dialog.open) {trigger = opener || dialog.ownerDocument.activeElement; originalOverflow = body?.style.overflow || ''; if (body) body.style.overflow = 'hidden'; dialog.showModal();}
        if (options.key !== key) {key = options.key; confirmed = null; hasResult = false; content.textContent = '尚未取得详情。';}
        if (has(options, 'initial')) setResult(options.initial);
        loader = typeof options.load === 'function' ? options.load : null;
        (focusables()[0] || dialog).focus?.();
        if (loader) return retry();
        notice.set(hasResult ? '显示已确认的结果。' : '尚未取得详情。'); return Promise.resolve(confirmed);
      },
    };
  }
  function createFormContext({pack, page, scope, version = 1, fields, storage, onNotice = () => {}}) {
    if (!Number.isInteger(version) || version < 1 || !fields || !Object.keys(fields).length) throw new TypeError('需要正整数版本和显式字段白名单');
    fields = {...fields};
    for (const node of Object.values(fields)) if (!node || typeof node.value !== 'string') throw new TypeError('保存字段必须提供文本 value');
    let activeScope = scope, previous = {}, pending = new Set(), bindings = [];
    const persistence = () => store(storage, storageKey('form', pack, page, activeScope), onNotice);
    function restore() {
      const record = persistence().read(); previous = {};
      if (record && record.version !== version) {onNotice('保存内容版本已变化，请重新选择当前页面选项。'); return {};}
      if (record?.values && typeof record.values === 'object' && !Array.isArray(record.values)) for (const name of Object.keys(fields)) if (typeof record.values[name] === 'string') {previous[name] = record.values[name]; fields[name].value = record.values[name];}
      return {...previous};
    }
    function save() {
      const values = {};
      for (const [name, node] of Object.entries(fields)) values[name] = pending.has(name) && node.value === '' && has(previous, name) ? previous[name] : node.value;
      previous = values; persistence().write({version, values}); return {...values};
    }
    const dispose = () => {for (const [node, type, handler] of bindings) node.removeEventListener(type, handler); bindings = [];};
    return {
      restore, save, dispose,
      setPending(names) {for (const name of names) if (!has(fields, name)) throw new TypeError('pendingFields 必须属于白名单'); pending = new Set(names);},
      bind() {dispose(); for (const node of Object.values(fields)) {const type = node.tagName === 'SELECT' ? 'change' : 'input'; node.addEventListener(type, save); bindings.push([node, type, save]);}},
      setScope(value) {storageKey('form', pack, page, value); activeScope = value; previous = {}; pending = new Set(); for (const node of Object.values(fields)) node.value = ''; return restore();},
    };
  }
  function createReadGuard(scope = () => '') {
    let generation = 0;
    return {begin: () => ({generation: ++generation, scope: scope()}), current: token => token.generation === generation && token.scope === scope(), invalidate: () => {generation++;}};
  }
  function createReadonlyList({load, scope = () => '', keyOf, rowsOf, cursorOf, onChange = () => {}}) {
    if (![load, keyOf, rowsOf, cursorOf].every(fn => typeof fn === 'function')) throw new TypeError('列表需要显式 load、keyOf、rowsOf、cursorOf 契约');
    const guard = createReadGuard(scope); let controller = null, signature = null;
    let state = {rows: [], cursor: null, loading: false, error: null};
    const snapshot = () => ({...state, rows: [...state.rows]});
    const emit = () => onChange(snapshot());
    return {
      snapshot,
      invalidate() {guard.invalidate(); controller?.abort(); state = {...state, loading: false}; emit();},
      async query({filters = {}, limit = '10', append = false} = {}) {
        const nextSignature = JSON.stringify([scope(), filters, limit]);
        append = append && signature === nextSignature;
        if (!append) state = {rows: [], cursor: null, loading: false, error: null};
        signature = nextSignature; controller?.abort(); controller = new AbortController();
        const token = guard.begin(), previous = append ? [...state.rows] : [], cursor = append ? state.cursor : null;
        state = {...state, loading: true, error: null}; emit();
        try {
          const data = await load({filters: {...filters}, limit, cursor, scope: token.scope, signal: controller.signal});
          if (!guard.current(token)) return {stale: true};
          const rows = rowsOf(data); if (!Array.isArray(rows)) throw new TypeError('rowsOf 必须返回数组');
          const merged = new Map(); for (const row of [...previous, ...rows]) {const key = keyOf(row); if (typeof key !== 'string') throw new TypeError('keyOf 必须返回契约定义的文本唯一键'); merged.set(key, row);}
          state = {rows: [...merged.values()], cursor: cursorOf(data), loading: false, error: null}; emit(); return snapshot();
        } catch (error) {if (!guard.current(token)) return {stale: true}; state = {...state, loading: false, error: error.message || String(error)}; emit(); return snapshot();}
      },
    };
  }
  function formatExactText(value, {trimFractionZeros = false, empty = '—'} = {}) {
    if (value === null || value === undefined || value === '') return empty;
    if (typeof value !== 'string') throw new TypeError('精确展示仅接受已核实的文本；不能恢复 JSON Number 已丢失的精度');
    return trimFractionZeros && /^-?\d+\.\d+$/.test(value) ? value.replace(/0+$/, '').replace(/\.$/, '') : value;
  }
  function presentExact(node, value, options) {node.textContent = formatExactText(value, options); node.title = text(value); return node;}
  function formatTime(value, {timeZone} = {}) {
    if (typeof timeZone !== 'string' || !timeZone) throw new TypeError('必须显式指定 timeZone');
    const formatter = new Intl.DateTimeFormat('en-GB', {timeZone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
    if (typeof value !== 'string' || !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return text(value);
    const date = new Date(value); if (!Number.isFinite(date.getTime())) return value;
    const parts = Object.fromEntries(formatter.formatToParts(date).map(part => [part.type, part.value]));
    return parts.year + '-' + parts.month + '-' + parts.day + ' ' + parts.hour + ':' + parts.minute + ':' + parts.second;
  }
  const copyGenerations = new WeakMap();
  async function copyExact(value, {status, clipboard = root.navigator?.clipboard} = {}) {
    const notice = feedback(status); notice.set('正在复制…');
    const generation = (copyGenerations.get(status) || 0) + 1; copyGenerations.set(status, generation);
    try {if (typeof value !== 'string' || value === '') throw new TypeError('复制值必须是非空文本'); if (!clipboard?.writeText) throw new Error('浏览器未提供剪贴板功能'); await clipboard.writeText(value); if (copyGenerations.get(status) === generation) notice.set('已复制。', {kind: 'success'}); return {ok: true};}
    catch (error) {if (copyGenerations.get(status) === generation) notice.set('复制失败：' + (error.message || String(error)), {kind: 'warning'}); return {ok: false};}
  }
  const api = {feedback, createTaskController, createDetailsDialog, createFormContext, createReadGuard, createReadonlyList, formatExactText, presentExact, formatTime, copyExact};
  root.PlatformWorkbench = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
