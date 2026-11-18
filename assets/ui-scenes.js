(function () {
  'use strict';

  const GUIDE = '/docs/guides/统一UI场景规范.md';
  const KINDS = ['现成组件', '组合采用', '流程配方'];
  const LABELS = ['可复用组件与实现起点', '平台默认行为', 'Pack 需补的内容', '定向验收'];
  const pageTypes = [
    ['workspace', '首页与工作区', '导航、主题、深链接和侧栏', 'A01 A02 A03 A04 A05 A06 A07 A10 F07', '共享工作区外壳、主题切换、文档阅读器'],
    ['list', '查询列表与详情', '列表筛选、分页、详情和关联记录', 'B01 B02 B03 A09 B04 B05 B06 B07 B13 A02', 'createReadonlyList、createReadGuard、createDetailsDialog、presentExact'],
    ['configuration', '表单与配置编辑', '草稿、级联选择、版本冲突和权限', 'B12 B08 B09 B10 B11 C01 C03 C04 C05 C06 C08 F05', '原生表单控件、createFormContext、createReadGuard、feedback'],
    ['resources', '资源池与场景准备', '测试身份、资源分配和多步准备', 'C02 C01 C08 F04 D04 B09 D14', 'createReadonlyList、createDetailsDialog、feedback、createTaskController'],
    ['execution', '执行与批量任务', '确认范围、跟踪进度、恢复原任务与部分失败', 'D01 D02 D03 D04 D08 D13 D14 F02 A07 A08', '平台案例面板、createTaskController、lookupReceipt、feedback'],
    ['monitoring', '运行监控与诊断', '服务启停、预检、压测、日志及回调', 'D05 D06 D07 D08 D09 D10 D11 D12 E11 A08', '平台洞察、createReadonlyList、createDetailsDialog、feedback'],
    ['orders', '收银台与订单处理', '支付方式、限时操作、凭证和运营队列', 'E01 E02 E03 E04 E05 E06 E07 E08 E11 E12 C05 C06 C08 D02', 'createFormContext、createDetailsDialog、presentExact、copyExact、feedback'],
    ['review', '批次复核与文件', '原批次、回执差异、独立状态与恢复', 'E10 E09 E08 D02 B04 A09 B01 B02 B13 A02', 'createReadonlyList、createDetailsDialog、feedback、原生文件控件'],
    ['activities', '活动、奖励与关系', '活动规则、历史档位、奖励与分成', 'F01 C07 F02 F03 B07 B09 F05', 'createReadonlyList、createDetailsDialog、presentExact、formatTime'],
    ['reports', '报表、文档与历史', '阅读、比较、基线与离线快照', 'F06 A05 B05 B06 B07 D08 D10 D11 D12 E12', '文档阅读器、createReadonlyList、createDetailsDialog、copyExact'],
  ].map(([id, title, description, scenes, components]) => ({id, title, description, components, scenes: scenes.split(' ')}));

  // The five-column tables in the platform guide are the source of truth.
  function parseGuide(markdown) {
    const scenes = new Map();
    let group = null;
    for (const line of markdown.split(/\r?\n/)) {
      if (line.startsWith('## ')) {
        const heading = line.match(/^## ([A-F])\s+(.+)$/u);
        group = heading ? {code: heading[1], title: heading[2]} : null;
      }
      if (!group || !line.startsWith('| **')) continue;
      const cells = line.trim().split('|').slice(1, -1).map(cell => cell.trim());
      const entry = cells[0]?.match(/^\*\*([A-F]\d{2})\*\* (.+)$/u);
      const adoption = cells[1]?.match(/^\*\*(.+?)\*\*。(.+)$/u);
      if (cells.length !== 5 || cells.some(cell => !cell) || !entry || !adoption ||
          !entry[1].startsWith(group.code) || !KINDS.includes(adoption[1]) || scenes.has(entry[1])) {
        throw new Error('场景规范表格格式无效');
      }
      const [, id, title] = entry;
      scenes.set(id, {id, title, group: group.title, kind: adoption[1], facts: [adoption[2], ...cells.slice(2)]});
    }
    const required = new Set(pageTypes.flatMap(page => page.scenes));
    if (!scenes.size || [...required].some(id => !scenes.has(id))) throw new Error('场景规范缺少页面选型所需的条目');
    return scenes;
  }

  function findScenes(scenes, page, query) {
    const items = page ? page.scenes.map(id => scenes.get(id)) : [...scenes.values()];
    const terms = normalize(query).split(/\s+/u).filter(Boolean);
    return items.filter(scene => {
      const searchable = normalize([scene.id, scene.title, scene.group, scene.kind, ...scene.facts].join(' '));
      return terms.every(term => searchable.includes(term));
    });
  }

  function normalize(value) {
    return value.normalize('NFKC').toLowerCase().trim();
  }

  function formatSelection(scene) {
    return [scene.id + ' ' + scene.title + '（' + scene.kind + '）',
      ...scene.facts.map((fact, i) => LABELS[i] + '：' + fact.replace(/`/g, ''))].join('\n');
  }

  function setFact(node, fact) {
    fact.split(/`([^`]+)`/u).forEach((value, i) => {
      if (i % 2) {
        const code = document.createElement('code');
        code.textContent = value;
        node.append(code);
      } else {
        node.append(value);
      }
    });
  }

  function mount(root) {
    const get = id => root.querySelector('#' + id);
    const pageInput = get('scene-page-type');
    const search = get('scene-search');
    const clear = get('scene-clear');
    const retry = get('scene-retry');
    const results = get('scene-results');
    const status = get('scene-status');
    const list = get('scene-list');
    const details = get('scene-detail');
    const title = get('scene-title');
    let copyStatus = get('scene-copy-status');
    let scenes, selected;

    for (const page of pageTypes) pageInput.add(new Option(page.title, page.id));
    get('scene-guide').href = GUIDE;

    function select(scene, focus = false) {
      if (selected !== scene) {
        selected = scene;
        const newStatus = copyStatus.cloneNode(false);
        newStatus.textContent = '';
        newStatus.hidden = true;
        copyStatus.replaceWith(newStatus);
        copyStatus = newStatus;
        get('scene-meta').textContent = scene.group + ' / ' + scene.kind;
        title.textContent = scene.id + ' ' + scene.title;
        get('scene-facts').replaceChildren(...scene.facts.map((fact, index) => {
          const entry = document.createElement('div');
          const label = document.createElement('dt');
          const value = document.createElement('dd');
          label.textContent = LABELS[index];
          setFact(value, fact);
          entry.append(label, value);
          return entry;
        }));
      }
      for (const button of list.querySelectorAll('button')) {
        button.setAttribute('aria-pressed', String(button.dataset.scene === scene.id));
      }
      if (focus) title.focus();
    }

    function writeLocation() {
      const url = new URL(location.href);
      for (const [key, value] of [['page', pageInput.value], ['q', search.value.trim()], ['scene', results.hidden ? '' : selected.id]]) {
        if (value) url.searchParams.set(key, value);
        else url.searchParams.delete(key);
      }
      history.replaceState(null, '', url);
    }

    function render(requestedId) {
      const page = pageTypes.find(item => item.id === pageInput.value);
      const visible = findScenes(scenes, page, search.value);
      get('scene-page-description').textContent = page?.description || '按页面类型查看推荐场景，也可搜索全部条目。';
      const recommendations = get('scene-page-components');
      recommendations.hidden = !page;
      recommendations.querySelector('span').textContent = page?.components || '';
      clear.disabled = !page && !search.value;
      status.textContent = visible.length
        ? '显示 ' + visible.length + ' 类场景 · 共 ' + scenes.size + ' 类'
        : '没有匹配的场景，请更换关键词或清除筛选。';
      results.hidden = visible.length === 0;
      list.replaceChildren(...visible.map(scene => {
        const item = document.createElement('li');
        const button = document.createElement('button');
        const id = document.createElement('span');
        id.className = 'scenes-id';
        id.textContent = scene.id;
        button.type = 'button';
        button.dataset.scene = scene.id;
        button.append(id, document.createTextNode(scene.title));
        button.addEventListener('click', () => {
          select(scene, true);
          writeLocation();
        });
        item.append(button);
        return item;
      }));
      if (visible.length) select(visible.find(item => item.id === requestedId) || visible[0]);
      details.hidden = results.hidden;
      writeLocation();
    }

    function readLocation() {
      const params = new URLSearchParams(location.search);
      pageInput.value = pageTypes.some(page => page.id === params.get('page')) ? params.get('page') : '';
      search.value = (params.get('q') || '').slice(0, 120);
      render(params.get('scene'));
      list.querySelector('[aria-pressed="true"]')?.scrollIntoView({block: 'nearest', inline: 'nearest', container: 'nearest'});
    }

    async function load() {
      const retryFocused = document.activeElement === retry;
      retry.hidden = true;
      status.textContent = '正在读取场景规范…';
      try {
        const path = GUIDE.slice(1);
        const response = await fetch('/api/document?' + new URLSearchParams({path}));
        if (!response.ok) throw new Error('无法读取场景规范');
        const payload = await response.json();
        if (payload.ok !== true || typeof payload.data?.content !== 'string') throw new Error('场景规范响应格式无效');
        scenes = parseGuide(payload.data.content);
        pageInput.disabled = search.disabled = false;
        readLocation();
        if (retryFocused) pageInput.focus();
      } catch (error) {
        status.textContent = '场景暂不可用。' + error.message + '。请重试或刷新页面。';
        retry.hidden = false;
        if (retryFocused) retry.focus();
      }
    }

    pageInput.addEventListener('change', () => render());
    search.addEventListener('input', () => render(selected?.id));
    clear.addEventListener('click', () => {
      pageInput.value = search.value = '';
      render();
      search.focus();
    });
    get('scene-copy').addEventListener('click', () => window.PlatformWorkbench.copyExact(formatSelection(selected), {status: copyStatus}));
    retry.addEventListener('click', load);
    load();
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = {pageTypes, parseGuide, findScenes, formatSelection};
  else mount(document.getElementById('ui-scenes'));
})();
