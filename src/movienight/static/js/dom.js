// Small DOM toolkit. All dynamic text goes through text nodes; innerHTML is only
// ever used for the constant SVG icon strings below.

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'style') for (const [p, v] of Object.entries(value)) el.style.setProperty(p, v);
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (key === 'text') el.textContent = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

// replaceChildren that skips empty slots (null/false) instead of printing them.
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
}

const ICONS = {
  ticket: '<path d="M3 7h18v3a2 2 0 0 0 0 4v3H3v-3a2 2 0 0 0 0-4z"/><path d="M9 7v10" stroke-dasharray="2 2"/>',
  grid: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="9" rx="1"/><rect x="3" y="15" width="7" height="6" rx="1"/><rect x="14" y="15" width="7" height="6" rx="1"/>',
  taste: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1L7 17M17 7l2.1-2.1"/>',
  play: '<path d="M7 4l13 8-13 8z" fill="currentColor"/>',
  shuffle: '<path d="M3 6h4l10 12h4M3 18h4l3-3.6M14 9.6L17 6h4M18 3l3 3-3 3M18 15l3 3-3 3"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  eyeoff: '<path d="M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.1 3.9M6.6 6.6C3.9 8.4 2 12 2 12s4 7 10 7a9.6 9.6 0 0 0 4.4-1.1M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  external: '<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M4 20h16"/>',
  check: '<path d="M4 12l5 5L20 6"/>',
  alert: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
  watchlist: '<path d="M6 3h12v18l-6-4-6 4z"/>',
  director: '<path d="M4 9h16v11H4zM4 9l2-5 14 2-1 3M9 4.5L8 9M14 5.5L13 9"/>',
  cast: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-5 4-7 8-7s7 2 8 7"/>',
  genre: '<path d="M3 12V3h9l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
  keyword: '<path d="M5 9h14M5 15h14M10 3L8 21M16 3l-2 18"/>',
  similar: '<circle cx="9" cy="12" r="6"/><circle cx="15" cy="12" r="6"/>',
  acclaimed: '<path d="M12 3l2.8 5.8 6.2.9-4.5 4.4 1 6.3L12 17.4 6.5 20.4l1-6.3L3 9.7l6.2-.9z"/>',
  decade: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  fresh: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18"/>',
  ask: '<path d="M4 5h16v11H9l-5 4z"/><path d="M8.5 9.5h7M8.5 12.5h4"/>',
  send: '<path d="M12 19V5M6 11l6-6 6 6"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  thumbup: '<path d="M7 11v9H4v-9zM7 11l4-8c1.5 0 2.5 1 2.5 2.5V9H19a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.8 20H7"/>',
  thumbdown: '<g transform="rotate(180 12 12)"><path d="M7 11v9H4v-9zM7 11l4-8c1.5 0 2.5 1 2.5 2.5V9H19a2 2 0 0 1 2 2.3l-1.2 7A2 2 0 0 1 17.8 20H7"/></g>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  tag: '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
  heart: '<path d="M12 20s-8-5-8-11a4.5 4.5 0 0 1 8-2.8A4.5 4.5 0 0 1 20 9c0 6-8 11-8 11z" fill="currentColor"/>',
};

export function icon(name, cls = '') {
  const span = document.createElement('span');
  span.className = `icon ${cls}`.trim();
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ICONS.info}</svg>`;
  return span;
}

export function hydrateIcons(root = document) {
  for (const el of root.querySelectorAll('[data-icon]')) el.replaceChildren(icon(el.dataset.icon));
}

// ---- formatting ----

export function runtime(min) {
  if (!min) return null;
  const hrs = Math.floor(min / 60), m = min % 60;
  return hrs ? (m ? `${hrs} h ${m} min` : `${hrs} h`) : `${m} min`;
}

export function stars(rating) {
  if (rating == null) return null;
  const full = Math.floor(rating), half = rating - full >= 0.5;
  return h('span', { class: 'stars', 'aria-label': `${rating} out of 5 stars`, role: 'img' },
    '★'.repeat(full) + (half ? '½' : ''));
}

const nf = new Intl.NumberFormat('en');
export const num = (n) => (n == null ? '–' : nf.format(n));

const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
export function ago(iso) {
  if (!iso) return 'never';
  const secs = (new Date(iso).getTime() - Date.now()) / 1000;
  if (Number.isNaN(secs)) return 'unknown';
  const steps = [[60, 'second'], [3600, 'minute'], [86400, 'hour'], [604800, 'day'], [2629800, 'week'], [31557600, 'month'], [Infinity, 'year']];
  const divs = { second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2629800, year: 31557600 };
  if (Math.abs(secs) < 45) return 'just now';
  const [, unit] = steps.find(([limit]) => Math.abs(secs) < limit);
  return rtf.format(Math.round(secs / divs[unit]), unit);
}

export function matchGrade(match) {
  if (match >= 85) return 'Made for you';
  if (match >= 70) return 'Strong match';
  if (match >= 50) return 'Worth a look';
  return 'Wildcard';
}

// ---- posters ----

function hash(str) {
  let x = 2166136261;
  for (const ch of str) x = Math.imul(x ^ ch.codePointAt(0), 16777619);
  return x >>> 0;
}

// Typographic stand-in poster, coloured from the title so each film keeps its look.
export function genPoster(movie, label) {
  const title = movie.title || 'Untitled';
  const n = hash(`${title}${movie.year ?? ''}`);
  const len = title.length <= 10 ? 's' : title.length <= 22 ? 'm' : 'l';
  return h('div', {
    class: 'poster-gen',
    'data-len': len,
    'data-shape': String(n % 3),
    style: { '--h': String(n % 360), '--x': `${18 + (n >> 9) % 64}%`, '--y': `${12 + (n >> 15) % 40}%` },
    ...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': 'true' }),
  },
  h('span', { class: 'poster-gen__shape' }),
  h('span', { class: 'poster-gen__title', text: title }),
  movie.year ? h('span', { class: 'poster-gen__year', text: String(movie.year) }) : null);
}

export function poster(movie, { label = null, eager = false, cls = '' } = {}) {
  const wrap = h('div', { class: `poster ${cls}`.trim() });
  const fallback = () => wrap.replaceChildren(genPoster(movie, label));
  if (movie.poster_url) {
    const img = h('img', { src: movie.poster_url, alt: label || '', loading: eager ? 'eager' : 'lazy', decoding: 'async' });
    img.addEventListener('error', fallback, { once: true });
    wrap.append(img);
  } else fallback();
  return wrap;
}

export function hueOf(movie) {
  return hash(`${movie.title || 'Untitled'}${movie.year ?? ''}`) % 360;
}

// ---- shared bits ----

export function matchStub(match, { size = 'lg' } = {}) {
  const m = Math.max(0, Math.min(100, Math.round(match ?? 0)));
  return h('div', { class: `stub stub--${size}`, role: 'img', 'aria-label': `${m}% match, ${matchGrade(m)}` },
    h('span', { class: 'stub__num' }, String(m), h('span', { class: 'stub__pct', text: '%' })),
    size === 'lg' ? h('span', { class: 'stub__grade', text: matchGrade(m) }) : null);
}

export function reasonList(reasons = [], cls = '') {
  if (!reasons.length) return null;
  return h('ul', { class: `reasons ${cls}`.trim() },
    reasons.map((r) => h('li', { class: 'reason' }, icon(r.kind), h('span', { text: r.text }))));
}

export function errorBox(err, onRetry) {
  return h('div', { class: 'state state--error', role: 'alert' },
    icon('alert', 'state__icon'),
    h('p', { class: 'state__title', text: 'Something went wrong' }),
    h('p', { class: 'state__text', text: err?.detail || err?.message || 'Unknown error' }),
    onRetry ? h('button', { class: 'btn btn--ghost', type: 'button', onclick: onRetry }, 'Try again') : null);
}

export function announce(text) {
  const el = document.getElementById('announcer');
  el.textContent = '';
  requestAnimationFrame(() => { el.textContent = text; });
}

// A film title in curly quotes for messages, capped so toast buttons stay on screen on a phone.
export function quoted(m, max = 40) {
  const t = String(m.title || 'this film');
  return `“${t.length > max ? `${t.slice(0, max - 1).trimEnd()}…` : t}”`;
}

// `extra(remove)` may return a node (e.g. quick buttons); the toast then wraps onto two rows.
export function toast(message, { action = null, timeout = 6000, extra = null } = {}) {
  const host = document.querySelector('dialog[open] .toasts') || document.querySelector('body > .toasts');
  const remove = () => { el.classList.add('is-leaving'); setTimeout(() => el.remove(), 200); };
  const text = h('span', { class: 'toast__text', text: message });
  const actionBtn = action ? h('button', { class: 'toast__action', type: 'button', onclick: () => { remove(); action.run(); } }, action.label) : null;
  const close = h('button', { class: 'toast__close', type: 'button', 'aria-label': 'Dismiss message', onclick: remove }, icon('close'));
  const extras = extra ? extra(remove) : null;
  const el = extras
    ? h('div', { class: 'toast toast--rich' }, text, close, extras, actionBtn)
    : h('div', { class: 'toast' }, text, actionBtn, close);
  host.append(el);
  if (timeout) setTimeout(remove, timeout); // 0 = stays until dismissed
}

export function isTyping(e) {
  const t = e.target;
  return t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName);
}
