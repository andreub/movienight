// Seerr (Overseerr / Jellyseerr) requests for films missing from the library. Only when the server has it set up
// (status.features.seerr); otherwise every helper here is a no-op and nothing shows.
// States: none (can be requested), pending (requested, waiting for approval), processing (approved, downloading),
// available (Seerr has it; the library shows it after the next sync), blocked (Seerr's blocklist).
import { api } from './api.js';
import { getStatus } from './status.js';
import { h, icon, toast, quoted } from './dom.js';

export const seerrOn = (s = getStatus()) => Boolean(s?.features?.seerr);

// The quiet chip in the panel, and the tiny badge on a Complete poster (none for `none`).
export const CHIP = { pending: 'Requested', processing: 'Processing', available: 'Available soon', blocked: 'Not requestable' };
const BADGE = { pending: 'Requested', processing: 'Processing', available: 'Soon' };
const TITLE = {
  pending: 'Requested in Seerr, waiting for approval',
  processing: 'Approved in Seerr, on its way',
  available: 'Available in Seerr: it shows up here after the next sync',
  blocked: 'Blocklisted in Seerr',
};

const TTL = 5 * 60e3;
const CHUNK = 40; // the server answers at most 40 ids per call
const cache = new Map(); // tmdb id -> { state, at }
export const REQUESTED_EVENT = 'movienight:requested';

const known = (id) => {
  const hit = cache.get(id);
  return hit && Date.now() - hit.at < TTL ? hit.state : null;
};
const remember = (id, state) => cache.set(id, { state, at: Date.now() });

// States of these films (Map id -> state), asking only for the ones not known in the last few minutes, 40 per call.
// Failures leave films out.
export async function seerrStatuses(ids) {
  const out = new Map();
  if (!seerrOn()) return out;
  const todo = [];
  for (const id of new Set(ids.map(Number).filter((x) => x > 0))) {
    const s = known(id);
    if (s) out.set(id, s); else todo.push(id);
  }
  for (let i = 0; i < todo.length; i += CHUNK) {
    try {
      const r = await api.seerrStatus(todo.slice(i, i + CHUNK));
      for (const [k, v] of Object.entries(r?.statuses || {})) { remember(Number(k), v); out.set(Number(k), v); }
    } catch { break; } // Seerr unreachable: no badges, no retries now
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
    toast(err.detail || 'Could not request it. Try again.');
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
    icon(state === 'available' ? 'check' : state === 'blocked' ? 'eyeoff' : 'clock'), CHIP[state] || state);
}

// Badges on missing posters: elements carrying data-seerr="<tmdb id>" (the poster's wrapper) get a small badge
// when the film is requested, on its way or available in Seerr. Calls from many cards at once are gathered into
// one lookup.
let queue = new Set();
let timer = 0;
export function requestBadges(root) {
  if (!seerrOn() || !root) return;
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
