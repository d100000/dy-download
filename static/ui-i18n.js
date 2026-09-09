// 只翻译界面文案；作品标题、作者名、文案和标签保留原文。
function uiText(message, values = {}) {
  const lang = typeof LANG !== 'undefined' ? LANG : globalThis.__LANG;
  const template = lang === 'en'
    ? (globalThis.__UI_MESSAGES?.en?.[message] || message) : message;
  return String(template).replace(/\{(\w+)\}/g,
    (match, key) => Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : match);
}
function localizeUi(root = document) {
  const nodes = [...root.querySelectorAll('[data-ui]')];
  if (root.matches?.('[data-ui]')) nodes.unshift(root);
  nodes.forEach(node => {
    const translated = uiText(node.getAttribute('data-ui'));
    if (!node.children.length) {
      if (node.textContent !== translated) node.textContent = translated;
    } else {
      for (const text of node.childNodes)
        if (text.nodeType === 3 && text.textContent.trim() === node.getAttribute('data-ui'))
          text.textContent = translated;
    }
  });
}
// Template labels are marked explicitly, so captions and creator names are never translated.
if (typeof document !== 'undefined' && typeof MutationObserver !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => {
    localizeUi();
    new MutationObserver(records => {
      for (const record of records)
        for (const node of record.addedNodes)
          if (node.nodeType === 1) localizeUi(node);
    }).observe(document.body, {childList: true, subtree: true});
  }, {once: true});
}
