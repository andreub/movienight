// D-pad navigation for the Android TV app, whose WebView user agent carries "MovieNightTV".
// Only there: adds `tv` to <html> (bigger focus ring in CSS) and moves focus with the arrow keys to the nearest
// focusable element in that direction. Every other client gets nothing from this module.
import { isTyping } from './dom.js';

export const TV = /MovieNightTV/.test(navigator.userAgent);

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), '
  + 'textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
const DIRS = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' };
const TEXT_TYPES = new Set(['text', 'search', 'email', 'url', 'tel', 'password', 'number', '']);

export function initTvNav() {
  if (!TV) return;
  document.documentElement.classList.add('tv');
  // Window, bubble phase: the views' own arrow handlers (e.g. Tonight's shuffle) run first and win.
  window.addEventListener('keydown', onKey);
}

function onKey(e) {
  const dir = DIRS[e.key];
  if (!dir || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
  if (isTyping(e) && !leavesField(e.target, dir)) return;
  const from = document.activeElement;
  const scope = scopeOf(from);
  const items = candidates(scope).filter((el) => el !== from);
  const next = isItem(from, scope) ? nearest(from.getBoundingClientRect(), items, dir) : firstVisible(items);
  if (scope === document.body && scrollStep(next, dir)) { e.preventDefault(); return; }
  if (!next) return; // nothing that way: let the WebView scroll the page
  e.preventDefault();
  next.focus({ preventScroll: true });
  next.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

// Single-line fields and radio groups keep Left/Right (caret, choice); Up/Down leave them. Checkboxes and plain
// buttons-as-inputs leave on every arrow. Selects leave on Up/Down; textareas when the caret is on the first/last line; sliders keep every arrow.
function leavesField(el, dir) {
  if (el.tagName === 'SELECT') return dir === 'up' || dir === 'down'; // OK still opens the list
  if (el.tagName === 'TEXTAREA') return atEdge(el, dir);
  if (el.readOnly && (el.tagName === 'TEXTAREA' || TEXT_TYPES.has(el.type))) return true; // no caret to move
  if (el.tagName !== 'INPUT') return false;
  if (el.type === 'checkbox' || el.type === 'file') return true;
  return (TEXT_TYPES.has(el.type) || el.type === 'radio') && (dir === 'up' || dir === 'down');
}

// Pages that opt in with data-tv-scroll (the Ask thread, mostly plain text): Up/Down scroll the page a step while
// there is more to read, rather than jumping to a control off screen or to one pinned to the bottom edge
// (data-tv-end, the chat composer), which is reached only once the page is scrolled to its end.
export function scrollStep(next, dir) {
  if ((dir !== 'up' && dir !== 'down') || !document.querySelector('[data-tv-scroll]')) return false;
  const down = dir === 'down';
  const max = document.documentElement.scrollHeight - window.innerHeight;
  if (down ? window.scrollY >= max - 2 : window.scrollY <= 2) return false;
  if (next && !next.closest('[data-tv-end]')) {
    const r = next.getBoundingClientRect();
    const end = document.querySelector('[data-tv-end]:not([hidden])');
    const bottom = end ? Math.min(window.innerHeight, end.getBoundingClientRect().top) : window.innerHeight;
    if (r.top >= 0 && r.bottom <= bottom) return false; // in view: just move to it
  }
  window.scrollBy({ top: (down ? 1 : -1) * Math.round(window.innerHeight * 0.6), behavior: 'smooth' });
  return true;
}

function atEdge(el, dir) {
  if (dir !== 'up' && dir !== 'down') return false;
  if (el.selectionStart !== el.selectionEnd) return false;
  const before = el.value.slice(0, el.selectionStart);
  return dir === 'up' ? !before.includes('\n') : !el.value.slice(el.selectionStart).includes('\n');
}

// An open modal dialog traps focus; an open menu keeps it among its items.
function scopeOf(from) {
  const menu = from?.closest?.('.menu:not([hidden])');
  if (menu) return menu;
  return document.querySelector('dialog[open]') || document.body;
}

function isItem(el, scope) {
  return el && el !== document.body && scope.contains(el) && el.matches(FOCUSABLE) && visible(el);
}

function candidates(scope) {
  return [...scope.querySelectorAll(FOCUSABLE)].filter(visible);
}

function visible(el) {
  if (el.closest('[hidden], [inert]')) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false; // also skips sr-only inputs
  return getComputedStyle(el).visibility !== 'hidden';
}

function firstVisible(items) {
  const vh = window.innerHeight;
  const onScreen = items.filter((el) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < vh; });
  const main = document.getElementById('main');
  return onScreen.find((el) => main?.contains(el)) || onScreen[0] || items[0] || null;
}

// Lowest score wins: gap along the direction plus twice the sideways offset (0 when the two overlap sideways).
// Left/Right only move within the same band (sideways overlap), so the end of a row stays put instead of jumping
// to the top bar; Up/Down may move diagonally. Controls laid over a card (votes, seen toggle) are skipped, so
// the arrows walk card to card; Up is the one way into them.
function nearest(cur, items, dir) {
  const horizontal = dir === 'left' || dir === 'right';
  const sign = dir === 'down' || dir === 'right' ? 1 : -1;
  const cx = (r) => r.left + r.width / 2;
  const cy = (r) => r.top + r.height / 2;
  let best = null;
  let bestScore = Infinity;
  for (const el of items) {
    const r = el.getBoundingClientRect();
    if (dir !== 'up' && inside(r, cur)) continue;
    const along = horizontal ? (cx(r) - cx(cur)) * sign : (cy(r) - cy(cur)) * sign;
    if (along <= 1) continue;
    const gap = horizontal
      ? Math.max(0, sign > 0 ? r.left - cur.right : cur.left - r.right)
      : Math.max(0, sign > 0 ? r.top - cur.bottom : cur.top - r.bottom);
    const side = horizontal
      ? Math.max(0, r.top - cur.bottom, cur.top - r.bottom)
      : Math.max(0, r.left - cur.right, cur.left - r.right);
    if (horizontal && side > 0) continue;
    const sideCenter = horizontal ? Math.abs(cy(r) - cy(cur)) : Math.abs(cx(r) - cx(cur));
    const score = gap + side * 2 + sideCenter * 0.01 + along * 0.001;
    if (score < bestScore) { bestScore = score; best = el; }
  }
  return best;
}

function inside(r, outer) {
  return r.left >= outer.left - 1 && r.right <= outer.right + 1 && r.top >= outer.top - 1 && r.bottom <= outer.bottom + 1;
}
