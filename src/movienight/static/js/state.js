// Route + filter state lives in the URL hash: #/browse?mood=tense&rt=90&wl=1&genre=Horror&q=&sort=year&movie=42
// so a refresh (or a shared link) keeps the view, filters and open film.

export const VIEWS = ['tonight', 'browse', 'ask', 'taste', 'setup'];
export const SORTS = [
  ['match', 'Best match'], ['rating', 'TMDB rating'], ['recent', 'Recently added'], ['year', 'Newest'], ['runtime', 'Shortest'],
];
export const RUNTIMES = [[90, 'Under 90 min'], [120, 'Under 2 h'], [null, 'Any length']];

const toInt = (v) => (v && /^\d+$/.test(v) ? Number(v) : null);

export function parse() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs = ''] = raw.split('?');
  const p = new URLSearchParams(qs);
  return {
    view: VIEWS.includes(path) ? path : 'tonight',
    filters: {
      mood: p.get('mood') || null,
      rt: toInt(p.get('rt')),
      wl: p.get('wl') === '1',
      genres: p.getAll('genre'),
      q: p.get('q') || '',
      sort: SORTS.some(([id]) => id === p.get('sort')) ? p.get('sort') : 'match',
    },
    movie: toInt(p.get('movie')),
  };
}

export function filterQuery(f) {
  const p = new URLSearchParams();
  if (f.mood) p.set('mood', f.mood);
  if (f.rt) p.set('rt', f.rt);
  if (f.wl) p.set('wl', '1');
  for (const g of f.genres) p.append('genre', g);
  if (f.q) p.set('q', f.q);
  if (f.sort !== 'match') p.set('sort', f.sort);
  return p;
}

export function href(view, filters = parse().filters, movie = null) {
  const p = filterQuery(filters);
  if (movie) p.set('movie', movie);
  const s = p.toString();
  return `#/${view}${s ? `?${s}` : ''}`;
}

const listeners = new Set();
let lastHref = null;
function emit() {
  if (location.href === lastHref) return;
  lastHref = location.href;
  const route = parse();
  for (const fn of listeners) fn(route);
}
export function onRoute(fn) { listeners.add(fn); }
window.addEventListener('popstate', emit);
window.addEventListener('hashchange', emit);

// Filter tweaks replace history (Back should not replay every chip); views and films push.
export function go({ view, filters, movie } = {}, { replace = false } = {}) {
  const cur = parse();
  const nextMovie = movie === undefined ? cur.movie : movie;
  const next = href(view ?? cur.view, { ...cur.filters, ...filters }, nextMovie);
  if (next === location.hash) return;
  // Remember entries that opened a film so closing it can simply go Back.
  if (replace) history.replaceState(nextMovie ? history.state : null, '', next);
  else history.pushState({ detail: Boolean(nextMovie && !cur.movie) }, '', next);
  emit();
}
export function start() { emit(); }

export function setFilters(patch) { go({ filters: patch }, { replace: true }); }
export const EMPTY_FILTERS = { mood: null, rt: null, wl: false, genres: [], q: '' };

// Tonight only uses the quick filters; Browse adds genres and search.
export function apiFilters(f, { full = false } = {}) {
  const out = { mood: f.mood, max_runtime: f.rt, watchlist_only: f.wl || null };
  if (full) Object.assign(out, { genre: f.genres, q: f.q });
  return out;
}

export function hasQuickFilters(f) { return Boolean(f.mood || f.rt || f.wl); }
export function hasAnyFilters(f) { return hasQuickFilters(f) || f.genres.length > 0 || Boolean(f.q); }
