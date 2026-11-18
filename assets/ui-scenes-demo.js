(function () {
  'use strict';

  const batches = [
    {id: 'RK-101', name: '包装纸箱', quantity: 120},
    {id: 'RK-102', name: '防护缓冲垫', quantity: 80},
    {id: 'RK-103', name: '标签卷', quantity: 40},
    {id: 'RK-104', name: '周转箱', quantity: 36},
    {id: 'RK-105', name: '密封袋', quantity: 200},
  ];
  const PAGE_SIZE = 3;
  const STORAGE_KEY = 'testdock.ui-scenes.review-demo.v1';

  function makeReceipt(batch, sample) {
    return {batchId: sample === 'wrong' ? 'RK-999' : batch.id, version: 1,
      quantity: batch.quantity + (sample === 'difference' ? -1 : 0)};
  }

  function compareReceipt(batch, receipt) {
    if (!receipt || typeof receipt.batchId !== 'string' || receipt.version !== 1 ||
        !Number.isSafeInteger(receipt.quantity) || receipt.quantity < 0) {
      throw new Error('回单格式无效，请下载当前批次的 JSON 模板。');
    }
    if (receipt.batchId !== batch.id) throw new Error('回单批次 ' + receipt.batchId + ' 与当前批次不符。');
    return receipt.quantity - batch.quantity;
  }

  function restore(storage) {
    try {
      const saved = JSON.parse(storage.getItem(STORAGE_KEY));
      const records = new Map();
      for (const batch of batches) {
        const row = saved?.records?.[batch.id];
        if (typeof row?.id === 'string' && /^REV-[0-9a-f-]{36}$/.test(row.id) && typeof row.confirmed === 'boolean') {
          records.set(batch.id, {id: row.id, confirmed: row.confirmed});
        }
      }
      return {records, selected: batches.find(batch => batch.id === saved?.selected) || null};
    } catch (_) {
      return {records: new Map(), selected: null};
    }
  }

  function mount() {
    const get = id => document.getElementById(id);
    const ui = window.PlatformWorkbench;
    const sample = get('receipt-sample');
    const file = get('receipt-file');
    const lose = get('lose-response');
    const compare = get('compare-receipt');
    const register = get('register-review');
    const lookup = get('lookup-review');
    const listRows = get('batch-rows');
    const notice = ui.feedback(get('review-status'));
    const listNotice = ui.feedback(get('batch-status'));
    const selectionButtons = new Map();
    let storage;
    try { storage = window.sessionStorage; } catch (_) {}
    let {records, selected} = restore(storage);
    let busy = false;
    let comparison = null;
    let activeSearch = '';

    function persist() {
      try {
        storage.setItem(STORAGE_KEY, JSON.stringify({selected: selected?.id, records: Object.fromEntries(records)}));
        return true;
      } catch (_) {
        return false;
      }
    }

    function controls() {
      const current = selected && records.get(selected.id);
      get('review-batch').textContent = selected ? selected.id + ' · ' + selected.name + ' · 回单版本 1' : '请先选择入库批次。';
      get('comparison-result').textContent = comparison?.text || (current ? '一致（登记记录）' : '未核对');
      const registration = register.hasAttribute('aria-busy') ? '登记中' : current?.confirmed ? '已登记' : '待确认';
      get('registration-result').textContent = current ? registration + ' · ' + current.id : '未登记';
      for (const [id, button] of selectionButtons) {
        button.setAttribute('aria-pressed', String(id === selected?.id));
        button.disabled = busy;
      }
      get('download-template').disabled = !selected || busy;
      sample.disabled = file.disabled = !selected || busy;
      compare.disabled = !selected || busy || (!sample.value && !file.files.length);
      register.disabled = !selected || busy || Boolean(current) || comparison?.difference !== 0;
      lose.disabled = register.disabled;
      lookup.disabled = busy || !current || current.confirmed;
    }

    function resetPreview() {
      comparison = null;
      notice.set('');
      controls();
    }

    function choose(batch) {
      if (busy || selected === batch) return;
      selected = batch;
      sample.value = file.value = '';
      lose.checked = false;
      resetPreview();
      persist();
      get('review-title').focus();
    }

    const dialog = ui.createDetailsDialog({
      dialog: get('batch-details'), body: document.body, status: get('batch-details-status'),
      content: get('batch-details-content'),
      project: batch => [batch.id + ' · ' + batch.name, '应收数量：' + batch.quantity + ' 件', '回单版本：1'].join('\n'),
    });
    get('close-batch-details').addEventListener('click', dialog.close);

    const list = ui.createReadonlyList({
      keyOf: batch => batch.id, rowsOf: result => result.rows, cursorOf: result => result.cursor,
      async load({filters, cursor}) {
        const matches = batches.filter(batch => (batch.id + ' ' + batch.name).toLowerCase().includes(filters.search));
        const start = cursor ?? 0;
        const end = start + PAGE_SIZE;
        return {rows: matches.slice(start, end), cursor: end < matches.length ? end : null};
      },
      onChange({rows, cursor, loading}) {
        get('batch-more').disabled = loading || cursor === null;
        if (loading) return;
        listNotice.set(rows.length ? '已显示 ' + rows.length + ' 个批次。' : '没有匹配的批次。');
        selectionButtons.clear();
        listRows.replaceChildren(...rows.map(batch => {
          const row = document.createElement('tr');
          for (const text of [batch.id, batch.name, batch.quantity + ' 件']) {
            const cell = row.insertCell();
            cell.textContent = text;
          }
          const actions = document.createElement('div');
          const select = document.createElement('button');
          select.className = 'btn';
          select.type = 'button';
          select.textContent = '选择批次';
          select.dataset.batch = batch.id;
          select.setAttribute('aria-label', '选择批次 ' + batch.id);
          select.addEventListener('click', () => choose(batch));
          selectionButtons.set(batch.id, select);
          const detail = document.createElement('button');
          detail.className = 'btn';
          detail.type = 'button';
          detail.textContent = '详情';
          detail.setAttribute('aria-label', batch.id + ' 详情');
          detail.addEventListener('click', () => dialog.open(detail, {key: batch.id, initial: batch}));
          actions.className = 'scenes-actions demo-row-actions';
          actions.append(select, detail);
          row.insertCell().append(actions);
          return row;
        }));
        controls();
      },
    });

    function query(append) {
      return list.query({filters: {search: activeSearch}, append});
    }

    get('batch-filter').addEventListener('submit', event => {
      event.preventDefault();
      activeSearch = get('batch-search').value.trim().toLowerCase();
      query(false);
    });
    get('batch-more').addEventListener('click', () => query(true));
    sample.addEventListener('change', () => { file.value = ''; resetPreview(); });
    file.addEventListener('change', () => { if (file.files.length) sample.value = ''; resetPreview(); });

    get('download-template').addEventListener('click', () => {
      const documentText = JSON.stringify(makeReceipt(selected, 'match'), null, 2);
      const url = URL.createObjectURL(new Blob([documentText], {type: 'application/json'}));
      const link = document.createElement('a');
      link.download = selected.id + '-回单.json';
      link.href = url;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      notice.set('已发起下载当前批次的回单模板。');
    });

    compare.addEventListener('click', async () => {
      if (!selected || compare.disabled) return;
      busy = true;
      controls();
      compare.setAttribute('aria-busy', 'true');
      try {
        let receipt;
        if (file.files.length) {
          const localFile = file.files[0];
          if (!localFile.name.toLowerCase().endsWith('.json') || localFile.size > 16 * 1024) {
            throw new Error('请选择不超过 16 KB 的 JSON 回单。');
          }
          try { receipt = JSON.parse(await localFile.text()); }
          catch (_) { throw new Error('无法读取 JSON 回单。'); }
        } else {
          receipt = makeReceipt(selected, sample.value);
        }
        const difference = compareReceipt(selected, receipt);
        comparison = {difference, text: difference === 0 ? '数量一致' :
          '有差异 · 应收 ' + selected.quantity + ' 件，回单 ' + receipt.quantity + ' 件'};
        notice.set(difference === 0 ? '回单数量与批次一致。' : '请更正回单后再核对。', {kind: difference ? 'warning' : 'success'});
      } catch (error) {
        comparison = {text: '核对未完成'};
        notice.set(error.message, {kind: 'warning'});
      } finally {
        busy = false;
        compare.removeAttribute('aria-busy');
        controls();
      }
    });

    register.addEventListener('click', () => {
      if (register.disabled || busy) return;
      const loseResponse = lose.checked;
      busy = true;
      const record = {id: 'REV-' + crypto.randomUUID(), confirmed: false};
      records.set(selected.id, record);
      const stored = persist();
      register.setAttribute('aria-busy', 'true');
      controls();
      notice.set('正在登记本批次复核…');
      setTimeout(() => {
        if (!loseResponse) {
          record.confirmed = true;
          persist();
        }
        busy = false;
        register.removeAttribute('aria-busy');
        controls();
        notice.set(loseResponse
          ? '登记结果待确认，请查询原复核。' + (stored ? '' : '浏览器无法保存此记录，请在当前页面查询。')
          : '复核已登记。库存入账仍未执行。', {kind: loseResponse ? 'warning' : 'success'});
        if (document.activeElement === document.body && loseResponse) lookup.focus();
      }, 400);
    });

    lookup.addEventListener('click', () => {
      const record = records.get(selected?.id);
      if (lookup.disabled || !record) return;
      record.confirmed = true;
      persist();
      controls();
      notice.set('已核对原登记 ' + record.id + '。库存入账仍未执行。', {kind: 'success'});
    });

    controls();
    query(false);
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {makeReceipt, compareReceipt, restore};
  else mount();
})();
