/* ── Agora Design System — base.js ─────────────────────────────────────
   Shared utilities for all templates.
   ──────────────────────────────────────────────────────────────────── */

/* ── Toast notifications ────────────────────────── */
function showToast(msg, type) {
  type = type || 'info';
  let container = document.querySelector('.toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    container.setAttribute('role', 'status');
    container.setAttribute('aria-live', 'polite');
    document.body.appendChild(container);
  }
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add('fade-out');
    setTimeout(() => el.remove(), 300);
  }, 3000);
}

/* ── Tab initialization (ARIA pattern) ──────────── */
function initTabs(tablist) {
  if (!tablist) return;
  const tabs = tablist.querySelectorAll('[role="tab"]');
  const panels = [];
  tabs.forEach(tab => {
    const panel = document.getElementById(tab.getAttribute('aria-controls'));
    if (panel) panels.push(panel);
    tab.addEventListener('click', () => activateTab(tab, tabs, panels));
    tab.addEventListener('keydown', e => {
      let idx = Array.from(tabs).indexOf(tab);
      if (e.key === 'ArrowRight') { idx = (idx + 1) % tabs.length; tabs[idx].focus(); e.preventDefault(); }
      if (e.key === 'ArrowLeft') { idx = (idx - 1 + tabs.length) % tabs.length; tabs[idx].focus(); e.preventDefault(); }
      if (e.key === 'Enter' || e.key === ' ') { activateTab(tab, tabs, panels); e.preventDefault(); }
    });
  });
}

function activateTab(tab, tabs, panels) {
  tabs.forEach(t => { t.setAttribute('aria-selected', 'false'); t.tabIndex = -1; });
  panels.forEach(p => p.hidden = true);
  tab.setAttribute('aria-selected', 'true');
  tab.tabIndex = 0;
  const panel = document.getElementById(tab.getAttribute('aria-controls'));
  if (panel) panel.hidden = false;
}

/* ── Focus trap (for modals) ────────────────────── */
function trapFocus(modal) {
  const focusable = modal.querySelectorAll(
    'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])');
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];

  function handler(e) {
    if (e.key === 'Escape') {
      modal.dispatchEvent(new Event('close'));
      return;
    }
    if (e.key !== 'Tab') return;
    if (e.shiftKey) {
      if (document.activeElement === first) { last.focus(); e.preventDefault(); }
    } else {
      if (document.activeElement === last) { first.focus(); e.preventDefault(); }
    }
  }
  modal.addEventListener('keydown', handler);
  first.focus();
  return () => modal.removeEventListener('keydown', handler);
}

/* ── Screen reader announcer (debounced) ────────── */
let _srTimer = null;
function announce(msg) {
  let el = document.getElementById('sr-announcer');
  if (!el) {
    el = document.createElement('div');
    el.id = 'sr-announcer';
    el.className = 'sr-only';
    el.setAttribute('aria-live', 'assertive');
    el.setAttribute('aria-atomic', 'true');
    document.body.appendChild(el);
  }
  clearTimeout(_srTimer);
  _srTimer = setTimeout(() => { el.textContent = msg; }, 1500);
}

/* ── Make clickable (keyboard-accessible onclick) ── */
function makeClickable(el) {
  if (!el.getAttribute('role')) el.setAttribute('role', 'button');
  if (!el.getAttribute('tabindex')) el.setAttribute('tabindex', '0');
  el.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); el.click(); }
  });
  el.addEventListener('keyup', e => {
    if (e.key === ' ') { e.preventDefault(); el.click(); }
  });
}

/* ── CSS variable helpers ──────────────────────────
   Usage: cssVar('--primary') → '#0891b2'
          COLORS.primary → '#0891b2'
   Templates should use COLORS.x instead of hardcoded hex. ── */
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
const COLORS = new Proxy({}, {
  get(_, prop) {
    const map = {
      primary: '--primary', primaryText: '--primary-text', primaryHover: '--primary-hover',
      success: '--success', warning: '--warning', danger: '--danger', info: '--info',
      cyan: '--cyan', emerald: '--emerald', purple: '--purple', orange: '--orange',
      cyanText: '--cyan-text', emeraldText: '--emerald-text', purpleText: '--purple-text', orangeText: '--orange-text',
      text: '--text', textSecondary: '--text-secondary', dim: '--dim',
      bg: '--bg', surface: '--surface', border: '--border',
    };
    return map[prop] ? cssVar(map[prop]) : '';
  }
});
// Agent color palette (computed at access time via Proxy)
const AGENT_COLORS = new Proxy([], {
  get(_, prop) {
    const list = [COLORS.primary, COLORS.emerald, COLORS.cyan,
                  COLORS.purple, COLORS.orange, COLORS.warning,
                  COLORS.danger, COLORS.success];
    return list[prop];
  }
});

/* ── Markdown renderer (safe) ───────────────────── */
function md(text) {
  // Extract code blocks first — protect from processing
  const codeBlocks = [];
  text = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    codeBlocks.push(`<pre><code>${code.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</code></pre>`);
    return `\x00CB${codeBlocks.length - 1}\x00`;
  });
  // Extract inline code
  const inlineCodes = [];
  text = text.replace(/`([^`]+)`/g, (_, code) => {
    inlineCodes.push(`<code>${code.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</code>`);
    return `\x00IC${inlineCodes.length - 1}\x00`;
  });
  // Escape dangerous HTML tags
  text = text.replace(/<(\/?\s*(?:script|style|iframe|object|embed|form|input|textarea|button|select)[\s>])/gi, '&lt;$1');
  // Tables
  text = text.replace(/((?:^\|.+\|$\n?)+)/gm, (block) => {
    const rows = block.trim().split('\n').filter(r => r.trim());
    if (rows.length < 2) return block;
    const dataRows = rows.filter(r => !/^\|[\s\-:|]+\|$/.test(r));
    if (!dataRows.length) return block;
    const parseRow = (r, tag) => {
      const cells = r.split('|').slice(1, -1).map(c => c.trim());
      return '<tr>' + cells.map(c => `<${tag}>${c}</${tag}>`).join('') + '</tr>';
    };
    let html = '<table><thead>' + parseRow(dataRows[0], 'th') + '</thead>';
    if (dataRows.length > 1) html += '<tbody>' + dataRows.slice(1).map(r => parseRow(r, 'td')).join('') + '</tbody>';
    return '<div class="table-wrap">' + html + '</table></div>\n';
  });
  // Block-level transforms
  text = text
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/^---+$/gm, '<hr>')
    .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/^\d+\. (.+)$/gm, '<li>$1</li>')
    .replace(/(<\/li>)\n+(<li>)/g, '$1$2')
    .replace(/((?:<li>.*<\/li>)+)/g, '<ul>$1</ul>');
  // Inline transforms
  text = text
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  // Ensure complete block elements get their own paragraph.
  // Only target self-contained single-line blocks, not nested internals.
  text = text.replace(/(<h[1-6][^>]*>.+?<\/h[1-6]>)/g, '\n\n$1\n\n');
  text = text.replace(/(<hr>)/g, '\n\n$1\n\n');
  text = text.replace(/\n{3,}/g, '\n\n');
  // Paragraphs: split by double newline, wrap only non-block text in <p>
  const blocks = text.split(/\n{2,}/);
  const blockRe = /^<(h[1-6]|hr|ul|ol|table|blockquote|pre|div|\x00)/i;
  // Also skip blocks that are just closing tags or table internals
  const skipRe = /^<\/(div|table|ul|ol)>$|^<(thead|tbody|tr|th|td)/i;
  text = blocks.map(b => {
    b = b.trim();
    if (!b) return '';
    if (blockRe.test(b) || skipRe.test(b)) return b;
    return '<p>' + b.replace(/\n/g, '<br>') + '</p>';
  }).join('\n');
  // Restore
  text = text.replace(/\x00IC(\d+)\x00/g, (_, i) => inlineCodes[parseInt(i)]);
  text = text.replace(/\x00CB(\d+)\x00/g, (_, i) => codeBlocks[parseInt(i)]);
  return text;
}
