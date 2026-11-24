// Seerr (Overseerr / Jellyseerr) requests for films missing from the library, as each user's own Seerr account.
// The server must have it set up (status.features.seerr); then each user connects their account once, through
// Plex (Setup → Seerr). Statuses and the Request button only show to a connected user; otherwise every helper
// here is a no-op and nothing shows.
// States: none (can be requested), requested (pending or approved in Seerr), available (Seerr has it; the
// library shows it after the next sync).
import { api } from './api.js';
import { getStatus } from './status.js';
import { h, icon, toast, quoted } from './dom.js';

export const seerrEnabled = (s = getStatus()) => Boolean(s?.features?.seerr);

// ---- this user's Seerr account: { enabled, connected, expired, name }
let account = null;
let loading = null;
const listeners = new Set();
export const ACCOUNT_EVENT = 'movienight:seerr-account';

function setAccount(a) {
  account = a;
  for (const fn of listeners) fn(a);
}

export function loadSeerrAccount({ force = false } = {}) {
  if (!force && (account || loading)) return loading || Promise.resolve(account);
  loading = api.seerrAccount()
    .then((a) => { setAccount(a); return a; }, () => account)
    .finally(() => { loading = null; });
  return loading;
}

// Connected (and not expired): statuses and Request are shown.
export const seerrOn = () => Boolean(account?.enabled && account.connected);
async function ready() {
  if (!seerrEnabled() && getStatus()) return false;
  await loadSeerrAccount();
  return seerrOn();
}

// Seerr said the stored session no longer works: show Reconnect wherever the account is shown.
function expired() {
  if (account) setAccount({ ...account, connected: false, expired: true });
}

export async function connectSeerr() {
  try {
    const { url } = await api.seerrConnect();
    location.assign(url); // app.plex.tv, which sends the browser back to /api/seerr/callback
  } catch (err) {
    toast(err.detail || 'Could not reach Plex. Try again.');
  }
}

async function disconnectSeerr() {
  try {
    await api.seerrDisconnect();
  } catch (err) {
    toast(err.detail || 'Could not disconnect Seerr. Try again.');
    return;
  }
  cache.clear();
  setAccount({ ...account, connected: false, expired: false, name: null });
  toast('Seerr disconnected.', { timeout: 3000 });
}

const reconnect = { label: 'Reconnect', run: connectSeerr };

// The Seerr line in Setup: "Connect Seerr", "<name> · Disconnect" or "Session expired · Reconnect Seerr".
export function seerrAccountLine() {
  const el = h('dd', { class: 'seerr-account' });
  const paint = (a) => {
    el.replaceChildren();
    if (!a) { el.textContent = '…'; return; }
    const link = (text, onclick) => h('button', { type: 'button', class: 'linkbtn linkbtn--inline', onclick }, text);
    if (a.connected) el.append(h('span', { text: a.name || 'Connected' }), ' · ', link('Disconnect', disconnectSeerr));
    else if (a.expired) el.append(h('span', { text: 'Session expired' }), ' · ', link('Reconnect Seerr', connectSeerr));
    else el.append(link('Connect Seerr', connectSeerr), h('span', { text: ' to request missing films' }));
  };
  listeners.add(paint);
  paint(account);
  loadSeerrAccount();
  return el;
}

// Back from app.plex.tv: /?seerr=connected | failed. A toast, then the parameter leaves the URL.
export function seerrReturn() {
  const params = new URLSearchParams(location.search);
  const result = params.get('seerr');
  if (result !== 'connected' && result !== 'failed') return;
  params.delete('seerr');
  const qs = params.toString();
  history.replaceState(history.state, '', `${location.pathname}${qs ? `?${qs}` : ''}${location.hash}`);
  if (result === 'connected') {
    loadSeerrAccount({ force: true }).then((a) => toast(a?.name ? `Seerr connected as ${a.name}.` : 'Seerr connected.', { timeout: 4000 }));
  } else {
    toast('Could not connect Seerr. Try again from Setup.', { action: reconnect });
  }
}

// ---- states
// The quiet chip in the panel, and the tiny badge on a Complete poster (none for `none`).
export const CHIP = { requested: 'Requested', available: 'Available soon' };
const BADGE = { requested: 'Requested', available: 'Soon' };
const TITLE = {
  requested: 'Requested in Seerr',
  available: 'Available in Seerr: it shows up here after the next sync',
};

const TTL = 5 * 60e3;
const CHUNK = 200;
const cache = new Map(); // tmdb id -> { state, at }
export const REQUESTED_EVENT = 'movienight:requested';

const known = (id) => {
  const hit = cache.get(id);
  return hit && Date.now() - hit.at < TTL ? hit.state : null;
};
const remember = (id, state) => cache.set(id, { state, at: Date.now() });

// States of these films (Map id -> state), asking only for the ones not known in the last few minutes.
// Failures leave films out.
export async function seerrStatuses(ids) {
  const out = new Map();
  if (!(await ready())) return out;
  const todo = [];
  for (const id of new Set(ids.map(Number).filter((x) => x > 0))) {
    const s = known(id);
    if (s) out.set(id, s); else todo.push(id);
  }
  for (let i = 0; i < todo.length; i += CHUNK) {
    try {
      const r = await api.seerrStatuses(todo.slice(i, i + CHUNK));
      for (const [k, v] of Object.entries(r?.statuses || {})) { remember(Number(k), v); out.set(Number(k), v); }
    } catch (err) {
      if (err.status === 409) expired();
      break; // Seerr unreachable: no badges, no retries now
    }
  }
  return out;
}

export async function seerrState(id) {
  return (await seerrStatuses([id])).get(Number(id)) ?? null;
}

// Request a film; resolves to its new state, or null when Seerr refused (toasted).
export async function requestFilm(f) {
  let r;
  try {
    r = await api.seerrRequest(f.tmdb_id);
  } catch (err) {
    if (err.status === 409) {
      expired();
      toast('Your Seerr session expired.', { action: reconnect });
    } else {
      toast(err.detail || 'Could not request it. Try again.');
    }
    return null;
  }
  remember(f.tmdb_id, r.status);
  window.dispatchEvent(new CustomEvent(REQUESTED_EVENT, { detail: { tmdb_id: f.tmdb_id, status: r.status } }));
  toast(r.status === 'available' ? `${quoted(f)} is already in Seerr.` : `Requested ${quoted(f)}.`, { timeout: 3000 });
  return r.status;
}

// The quiet chip shown in place of the Request button.
export function stateChip(state) {
  return h('span', { class: `reqchip reqchip--${state}`, title: TITLE[state] || null, role: 'status' },
    icon(state === 'available' ? 'check' : 'clock'), CHIP[state] || state);
}

// Badges on missing posters: elements carrying data-seerr="<tmdb id>" (the poster's wrapper) get a small badge
// when the film is requested or available in Seerr. Calls from many cards at once are gathered into one lookup.
let queue = new Set();
let timer = 0;
export function requestBadges(root) {
  if (!seerrEnabled() || !root) return;
  for (const el of root.querySelectorAll('[data-seerr]')) queue.add(el);
  clearTimeout(timer);
  timer = setTimeout(flush, 30);
}

async function flush() {
  const els = [...queue].filter((el) => el.isConnected);
  queue = new Set();
  if (!els.length) return;
  const states = await seerrStatuses(els.map((el) => el.dataset.seerr));
  for (const el of els) paintBadge(el, states.get(Number(el.dataset.seerr)));
}

function paintBadge(el, state) {
  el.querySelector('.cfilm__badge--req')?.remove();
  if (!BADGE[state]) return;
  el.append(h('span', { class: `cfilm__badge cfilm__badge--req cfilm__badge--${state}`, title: TITLE[state], text: BADGE[state] }));
}

// After a request from the panel: the badges of that film, wherever they are.
window.addEventListener(REQUESTED_EVENT, (e) => {
  for (const el of document.querySelectorAll(`[data-seerr="${Number(e.detail.tmdb_id)}"]`)) paintBadge(el, e.detail.status);
});
// Disconnected or expired: the badges go.
listeners.add((a) => {
  if (a?.connected) return;
  for (const el of document.querySelectorAll('.cfilm__badge--req')) el.remove();
});
