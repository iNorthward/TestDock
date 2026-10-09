/** Early theme bootstrap — avoids flash; migrates hidden/legacy themes to default. */
(function () {
  var STORAGE_KEY = 'platform-ui-theme';
  var DEFAULT = 'claude';
  var VISIBLE = { claude: 1, night: 1, studio: 1, console: 1 };

  function resolve(id) {
    return id && VISIBLE[id] ? id : DEFAULT;
  }

  try {
    var stored = localStorage.getItem(STORAGE_KEY);
    var theme = resolve(stored);
    document.documentElement.setAttribute('data-theme', theme);
    if (stored !== theme) localStorage.setItem(STORAGE_KEY, theme);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', DEFAULT);
  }
})();

/** Preload primary UI font from same origin (offline bundle, instant after first visit). */
(function () {
  var link = document.createElement('link');
  link.rel = 'preload';
  link.as = 'font';
  link.type = 'font/woff2';
  link.crossOrigin = 'anonymous';
  link.href = '/assets/fonts/inter-latin.woff2';
  document.head.appendChild(link);
})();
