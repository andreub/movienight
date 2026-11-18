// HTTP layer. With ?mock=1 (or ?mock=fresh for a first-run setup) requests are
// answered by /static/mock.js instead of the server.

const MOCK = new URLSearchParams(location.search).get('mock');
let mock = null;
let redirected = false;

// A 401 from /api means the login session ended: go to /login once, and come back here afterwards.
function sessionEnded(status) {
  if (status !== 401 || MOCK || redirected) return false;
  redirected = true;
  location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search + location.hash)}`);
  return true;
}

// The page's own version is embedded in index.html (server-rendered). The server stamps /api responses with its
// current one; the first different value means a deploy happened since this page loaded. Missing/unfilled
// values (old server, proxy, mock mode, static preview) disable the check.
const PAGE_VERSION = (() => {
  const v = document.querySelector('meta[name="movienight-version"]')?.content || '';
  return v && !v.startsWith('__') ? v : '';
})();
let announcedVersion = null;

export function checkVersion(headerValue, announce = announceNewVersion) {
  if (MOCK || !PAGE_VERSION || !headerValue || headerValue === PAGE_VERSION || headerValue === announcedVersion) return;
  announcedVersion = headerValue;
  announce();
}

function announceNewVersion() {
  import('/static/js/dom.js').then(({ toast }) => toast('A new version is available.', {
    timeout: 0,
    action: { label: 'Reload', run: () => location.reload() },
  })).catch(() => {});
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

function query(params = {}) {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    for (const v of [].concat(value)) {
      if (v == null || v === '' || v === false) continue;
      qs.append(key, String(v));
    }
  }
  const s = qs.toString();
  return s ? `?${s}` : '';
}

function detailOf(data, status) {
  const d = data?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join('; ');
  return `The server answered with an error (${status}).`;
}

async function request(method, path, { params, json, form } = {}) {
  if (MOCK) {
    mock ??= await import('/static/mock.js');
    const res = await mock.handle(method, path, params || {}, json ?? form, MOCK);
    if (res.status >= 400) throw new ApiError(res.status, detailOf(res.body, res.status));
    return res.body;
  }
  let res;
  try {
    res = await fetch(path + query(params), {
      method,
      headers: json ? { 'Content-Type': 'application/json' } : undefined,
      body: json ? JSON.stringify(json) : form,
    });
  } catch {
    throw new ApiError(0, 'Could not reach MovieNight. Check that the server is running and you are online.');
  }
  checkVersion(res.headers.get('X-MovieNight-Version'));
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    sessionEnded(res.status);
    throw new ApiError(res.status, detailOf(data, res.status));
  }
  return data;
}

export const api = {
  status: () => request('GET', '/api/status'),
  moods: () => request('GET', '/api/moods'),
  tags: () => request('GET', '/api/tags'),
  pick: (params) => request('GET', '/api/pick', { params }),
  recommendations: (params) => request('GET', '/api/recommendations', { params }),
  movie: (id) => request('GET', `/api/movies/${id}`),
  dismiss: (id, days) => request('POST', `/api/movies/${id}/dismiss`, { json: { days } }),
  undismiss: (id) => request('DELETE', `/api/movies/${id}/dismiss`),
  profile: () => request('GET', '/api/profile'),
  feedback: (id, vote) => request('POST', `/api/movies/${id}/feedback`, { json: { vote } }),
  // `vote` (optional): also set the vote in the same request (2 love, 1 up, -1 down, 0 clears it).
  seen: (id, seen, vote) => request('POST', `/api/movies/${id}/seen`, { json: vote === undefined ? { seen } : { seen, vote } }),
  seenCandidates: (params) => request('GET', '/api/seen-candidates', { params }),
  seenMarked: (params) => request('GET', '/api/seen-marked', { params }),
  feedbackSummary: () => request('GET', '/api/feedback'),
  me: () => request('GET', '/api/me'),
  chatStatus: () => request('GET', '/api/chat/status'),
  plexPin: () => request('POST', '/api/plex/pin'),
  plexPinCheck: (id) => request('GET', `/api/plex/pin/${id}`),
  plexLogout: () => request('POST', '/api/plex/logout'),
  plexServer: (id, enabled) => request('PUT', `/api/plex/servers/${encodeURIComponent(id)}`, { json: { enabled } }),
  players: () => request('GET', '/api/players'),
  play: (player_id, server_id, rating_key) => request('POST', '/api/play', { json: { player_id, server_id, rating_key } }),
  sync: (what = 'all') => request('POST', '/api/sync', { json: { what } }),
  letterboxdUser: (username) => request('PUT', '/api/letterboxd/user', { json: { username } }),
  letterboxdImport: (file) => {
    const form = new FormData();
    form.append('file', file);
    return request('POST', '/api/letterboxd/import', { form });
  },
};

let moodsPromise = null;
export function getMoods() {
  moodsPromise ??= api.moods().catch((e) => { moodsPromise = null; throw e; });
  return moodsPromise;
}

// Letterboxd tags that can steer picks, fetched once. Any failure means "no tag chips".
let tagsPromise = null;
export function getTags() {
  tagsPromise ??= api.tags().then((r) => r?.families || []).catch(() => { tagsPromise = null; return []; });
  return tagsPromise;
}

// Plex players that can be remote-controlled, fetched once per page load. Any failure means "none".
let playersPromise = null;
export function getPlayers() {
  playersPromise ??= api.players().then((r) => (Array.isArray(r) ? r : [])).catch(() => []);
  return playersPromise;
}

// AI chat availability, fetched once. Any failure (old backend, network) counts as "off".
let chatPromise = null;
export function getChatStatus() {
  chatPromise ??= api.chatStatus().then((s) => ({ enabled: Boolean(s?.enabled), model: s?.model || '' })).catch(() => ({ enabled: false, model: '' }));
  return chatPromise;
}

function parseSse(raw) {
  let event = 'message';
  const data = [];
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const i = line.indexOf(':');
    const field = i < 0 ? line : line.slice(0, i);
    const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (!data.length) return null;
  try { return { event, data: JSON.parse(data.join('\n')) }; } catch { return null; }
}

// POST /api/chat as a stream of { event, data }. fetch + ReadableStream because EventSource cannot POST.
export async function* chatStream(messages, signal) {
  if (MOCK) {
    mock ??= await import('/static/mock.js');
    yield* mock.chatStream(messages, signal);
    return;
  }
  let res;
  try {
    res = await fetch('/api/chat', {
      method: 'POST', signal,
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ messages }),
    });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError(0, 'Could not reach MovieNight. Check that the server is running and you are online.');
  }
  checkVersion(res.headers.get('X-MovieNight-Version'));
  if (!res.ok) {
    sessionEnded(res.status);
    const data = await res.json().catch(() => null);
    throw new ApiError(res.status, detailOf(data, res.status));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buf += decoder.decode(value, { stream: !done });
      buf = buf.replace(/\r\n/g, '\n');
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const ev = parseSse(buf.slice(0, i));
        buf = buf.slice(i + 2);
        if (ev) yield ev;
      }
      if (done) break;
    }
    const last = buf.trim() ? parseSse(buf) : null;
    if (last) yield last;
  } finally {
    reader.cancel().catch(() => {});
  }
}
