// HTTP layer. With ?mock=1 (or ?mock=fresh for a first-run setup) requests are
// answered by /static/mock.js instead of the server.

const MOCK = new URLSearchParams(location.search).get('mock');
let mock = null;

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
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, detailOf(data, res.status));
  return data;
}

export const api = {
  status: () => request('GET', '/api/status'),
  moods: () => request('GET', '/api/moods'),
  pick: (params) => request('GET', '/api/pick', { params }),
  recommendations: (params) => request('GET', '/api/recommendations', { params }),
  movie: (id) => request('GET', `/api/movies/${id}`),
  dismiss: (id, days) => request('POST', `/api/movies/${id}/dismiss`, { json: { days } }),
  undismiss: (id) => request('DELETE', `/api/movies/${id}/dismiss`),
  profile: () => request('GET', '/api/profile'),
  plexPin: () => request('POST', '/api/plex/pin'),
  plexPinCheck: (id) => request('GET', `/api/plex/pin/${id}`),
  plexLogout: () => request('POST', '/api/plex/logout'),
  plexServer: (id, enabled) => request('PUT', `/api/plex/servers/${encodeURIComponent(id)}`, { json: { enabled } }),
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
