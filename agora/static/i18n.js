/* ── Agora i18n — per-language file translation system ─────────────────
   Each language lives in /static/lang/{code}.json
   Usage:
     HTML:  <span data-t="key">Fallback</span>
            <input data-t-placeholder="key" placeholder="Fallback">
            <button data-t-title="key" title="Fallback">
     JS:    t('key')  or  t('key', {n: 5})  for interpolation ({n} in string)
   ──────────────────────────────────────────────────────────────────── */

const I18N = (() => {
  const AVAILABLE = ['en', 'tr'];
  const LABELS = { en: 'English', tr: 'Turkce' };

  let _lang = localStorage.getItem('agora-lang') || 'en';
  let _strings = {};   // current language
  let _fallback = {};   // English fallback
  let _ready = false;
  const _callbacks = [];

  async function _load(code) {
    const res = await fetch('/static/lang/' + code + '.json');
    return res.json();
  }

  // Bootstrap: load current lang + English fallback in parallel
  (async () => {
    try {
      const loads = [_load(_lang)];
      if (_lang !== 'en') loads.push(_load('en'));
      const [current, fallback] = await Promise.all(loads);
      _strings = current;
      _fallback = fallback || current;
      _ready = true;
      document.documentElement.lang = _lang;
      applyDOM();
      _callbacks.forEach(fn => fn());
      _callbacks.length = 0;
    } catch (e) {
      console.warn('[i18n] Failed to load language files:', e);
    }
  })();

  function onReady(fn) {
    if (_ready) fn();
    else _callbacks.push(fn);
  }

  function getLang() { return _lang; }

  async function setLang(lang) {
    _lang = lang;
    localStorage.setItem('agora-lang', lang);
    document.documentElement.lang = lang;
    try {
      _strings = await _load(lang);
      if (lang !== 'en' && !Object.keys(_fallback).length) {
        _fallback = await _load('en');
      }
      if (lang === 'en') _fallback = _strings;
    } catch (e) {
      console.warn('[i18n] Failed to load', lang, e);
    }
    applyDOM();
    // Update language selector if present
    const sel = document.getElementById('langSelect');
    if (sel) sel.value = lang;
  }

  function t(key, vars) {
    let s = _strings[key];
    if (s === undefined) s = _fallback[key];
    if (s === undefined) return key;
    if (vars) {
      Object.keys(vars).forEach(k => {
        s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), vars[k]);
      });
    }
    return s;
  }

  function applyDOM() {
    if (!_ready) return;

    // data-t → textContent
    document.querySelectorAll('[data-t]').forEach(el => {
      const key = el.getAttribute('data-t');
      const val = t(key);
      if (val !== key) el.textContent = val;
    });
    // data-t-placeholder
    document.querySelectorAll('[data-t-placeholder]').forEach(el => {
      const key = el.getAttribute('data-t-placeholder');
      const val = t(key);
      if (val !== key) el.placeholder = val;
    });
    // data-t-title
    document.querySelectorAll('[data-t-title]').forEach(el => {
      const key = el.getAttribute('data-t-title');
      const val = t(key);
      if (val !== key) el.title = val;
    });
    // data-t-html → innerHTML (use sparingly)
    document.querySelectorAll('[data-t-html]').forEach(el => {
      const key = el.getAttribute('data-t-html');
      const val = t(key);
      if (val !== key) el.innerHTML = val;
    });
    // data-t-aria → aria-label
    document.querySelectorAll('[data-t-aria]').forEach(el => {
      const key = el.getAttribute('data-t-aria');
      const val = t(key);
      if (val !== key) el.setAttribute('aria-label', val);
    });
  }

  return { t, setLang, getLang, applyDOM, onReady, AVAILABLE, LABELS };
})();

// Global shorthand
function t(key, vars) { return I18N.t(key, vars); }
