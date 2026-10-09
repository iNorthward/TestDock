/**
 * PLATFORM dynamic UI theme switcher.
 * Register themes in ALL_THEMES; set hidden: true to keep CSS but hide from picker.
 */
(function (global) {
  'use strict';

  var STORAGE_KEY = 'platform-ui-theme';
  var DEFAULT = 'claude';
  var selectSeq = 0;

  var ALL_THEMES = [
    { id: 'claude', label: 'Claude', desc: '暖 cream · 赤陶', swatch: 'linear-gradient(135deg, #faf9f5 50%, #cc785c 50%)' },
    { id: 'night', label: '暗色控制台', desc: '深色底 · 暖陶强调', swatch: 'linear-gradient(135deg, #131211 50%, #d4916f 50%)' },
    { id: 'studio', label: '文档工作台', desc: '轻量阅读 · 柔和', swatch: 'linear-gradient(135deg, #fbfbfc 50%, #0d9488 50%)' },
    { id: 'console', label: '工程控制台', desc: '高密度 · 锌灰强调', swatch: 'linear-gradient(135deg, #f4f4f5 50%, #52525b 50%)' },
    { id: 'cursor', label: 'Cursor', desc: '编辑感 · 橙', swatch: 'linear-gradient(135deg, #f7f7f4 50%, #f54e00 50%)', hidden: true },
    { id: 'meta', label: 'Meta', desc: '科技零售 · Meta Blue', swatch: 'linear-gradient(135deg, #ffffff 50%, #0064e0 50%)', hidden: true },
    { id: 'nike', label: 'Nike', desc: '运动零售 · 极简黑白', swatch: 'linear-gradient(135deg, #ffffff 50%, #111111 50%)', hidden: true },
  ];

  function findTheme(id) {
    for (var i = 0; i < ALL_THEMES.length; i++) {
      if (ALL_THEMES[i].id === id) return ALL_THEMES[i];
    }
    return null;
  }

  function isRegistered(id) {
    return !!findTheme(id);
  }

  function isVisible(id) {
    var theme = findTheme(id);
    return !!(theme && !theme.hidden);
  }

  function visibleThemes() {
    return ALL_THEMES.filter(function (t) { return !t.hidden; });
  }

  function resolveTheme(id) {
    if (id && isVisible(id)) return id;
    return DEFAULT;
  }

  function getTheme() {
    try {
      var stored = localStorage.getItem(STORAGE_KEY);
      return resolveTheme(stored);
    } catch (e) { /* private mode */ }
    return DEFAULT;
  }

  function syncSelects(id) {
    document.querySelectorAll('.theme-picker__select').forEach(function (sel) {
      if (sel.value !== id) sel.value = id;
    });
  }

  function applyTheme(id) {
    id = resolveTheme(id);
    document.documentElement.setAttribute('data-theme', id);
    try { localStorage.setItem(STORAGE_KEY, id); } catch (e) { /* ignore */ }
    syncSelects(id);
    document.dispatchEvent(new CustomEvent('platform-theme-change', { detail: { theme: id } }));
  }

  function mount(container) {
    if (!container) return;
    var current = getTheme();
    var picker = document.createElement('div');
    picker.className = 'theme-picker';

    var selectId = 'platform-theme-select-' + (++selectSeq);
    var select = document.createElement('select');
    select.id = selectId;
    select.className = 'theme-picker__select';
    select.setAttribute('aria-label', '界面风格');

    visibleThemes().forEach(function (theme) {
      var opt = document.createElement('option');
      opt.value = theme.id;
      opt.textContent = theme.label;
      opt.title = theme.desc;
      select.appendChild(opt);
    });

    select.value = current;
    select.addEventListener('change', function () { applyTheme(select.value); });

    picker.appendChild(select);
    container.innerHTML = '';
    container.appendChild(picker);
  }

  global.PLATFORMTheme = {
    ALL_THEMES: ALL_THEMES,
    THEMES: visibleThemes(),
    STORAGE_KEY: STORAGE_KEY,
    DEFAULT: DEFAULT,
    getTheme: getTheme,
    setTheme: applyTheme,
    resolveTheme: resolveTheme,
    mount: mount,
    register: function (theme) {
      if (theme && theme.id && !isRegistered(theme.id)) {
        ALL_THEMES.push(theme);
      }
    },
  };

  function autoMount() {
    document.querySelectorAll('[data-theme-picker], #theme-picker').forEach(function (el) {
      if (el.querySelector('.theme-picker')) return;
      mount(el);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoMount);
  } else {
    autoMount();
  }
})(typeof window !== 'undefined' ? window : this);
