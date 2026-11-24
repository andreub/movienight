// Route + filter state lives in the URL hash: #/browse?mood=tense&era=1990s&rt=90&wl=1&genre=Horror&q=&sort=year&movie=42
// (`film=603` instead of `movie` opens a film by TMDB id, in the library or not: Complete's saga films)
// (plus tag=sitges2025 or tagf=sitges: "films like the ones I tagged", see /api/tags; saga=1: Tonight's "Complete a
// saga", the next film of each started saga; group=collection-10 opens Complete on that group)
// so a refresh (or a shared link) keeps the view, filters and open film.

export const VIEWS = ['tonight', 'browse', 'complete', 'ask', 'taste', 'setup', 'seen'];
export const SORTS = [
  ['match', 'Best match'], ['rating', 'TMDB rating'], ['recent', 'Recently added'], ['year', 'Newest'], ['runtime', 'Shortest'],
];
export const RUNTIMES = [[90, 'Under 90 min'], [120, 'Under 2 h'], [null, 'Any length']];

const toInt = (v) => (v && /^\d+$/.test(v) ? Number(v) : null);

export function parse() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [path, qs = ''] = raw.split('?');
  const p = new URLSearchParams(qs);
  // old links used mood=recent|classic; those are eras now
  const legacy = ['recent', 'classic'].includes(p.get('mood')) ? p.get('mood') : null;
  return {
    view: VIEWS.includes(path) ? path : 'tonight',
    filters: {
      mood: legacy ? null : p.get('mood') || null,
      era: p.get('era') || legacy,
      tag: p.get('tag') || null,
      tagf: p.get('tag') ? null : p.get('tagf') || null,
      rt: toInt(p.get('rt')),
      wl: p.get('wl') === '1',
      genres: p.getAll('genre'),
      q: p.get('q') || '',
      sort: SORTS.some(([id]) => id === p.get('sort')) ? p.get('sort') : 'match',
      saga: p.get('saga') === '1',
    },
    movie: toInt(p.get('movie')),
    film: p.get('movie') ? null : toInt(p.get('film')),
    group: /^[a-z]+-[a-z0-9-]+$/.test(p.get('group') || '') ? p.get('group') : null,
  };
}

export function filterQuery(f) {
  const p = new URLSearchParams();
  if (f.mood) p.set('mood', f.mood);
  if (f.era) p.set('era', f.era);
  if (f.tag) p.set('tag', f.tag);
  else if (f.tagf) p.set('tagf', f.tagf);
  if (f.rt) p.set('rt', f.rt);
  if (f.wl) p.set('wl', '1');
  for (const g of f.genres) p.append('genre', g);
  if (f.q) p.set('q', f.q);
  if (f.sort !== 'match') p.set('sort', f.sort);
  if (f.saga) p.set('saga', '1');
  return p;
}

// `group`: a Complete group to open (only on the Complete view).
export function href(view, filters = parse().filters, movie = null, film = null, group = null) {
  const p = filterQuery(filters);
  if (group && view === 'complete') p.set('group', group);
  if (movie) p.set('movie', movie);
  else if (film) p.set('film', film);
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
// `movie` (library id) and `film` (TMDB id) share the detail sheet: opening one closes the other, and
// `movie: null` closes either.
// `group` opens a group on Complete (kept while the view stays Complete, dropped on any other view).
export function go({ view, filters, movie, film, group } = {}, { replace = false } = {}) {
  const cur = parse();
  let nextMovie = movie === undefined ? cur.movie : movie;
  let nextFilm = film === undefined ? cur.film : film;
  if (movie) nextFilm = null;
  else if (film) nextMovie = null;
  else if (movie === null && film === undefined) nextFilm = null;
  const next = href(view ?? cur.view, { ...cur.filters, ...filters }, nextMovie, nextFilm, group === undefined ? cur.group : group);
  if (next === location.hash) return;
  const open = Boolean(nextMovie || nextFilm);
  // Remember entries that opened a film so closing it can simply go Back.
  if (replace) history.replaceState(open ? history.state : null, '', next);
  else history.pushState({ detail: open && !(cur.movie || cur.film) }, '', next);
  emit();
}
export function start() { emit(); }

export function setFilters(patch) { go({ filters: patch }, { replace: true }); }
export const EMPTY_FILTERS = { mood: null, era: null, tag: null, tagf: null, rt: null, wl: false, genres: [], q: '', saga: false };
export const CLEAR_QUICK = { mood: null, era: null, tag: null, tagf: null, rt: null, wl: false, saga: false };

// Tonight only uses the quick filters, plus "Complete a saga" (`saga`); Browse adds genres and search instead.
export function apiFilters(f, { full = false } = {}) {
  const out = { mood: f.mood, era: f.era, tag: f.tag, tag_family: f.tag ? null : f.tagf, max_runtime: f.rt, watchlist_only: f.wl || null };
  if (full) Object.assign(out, { genre: f.genres, q: f.q });
  else out.complete = f.saga || null;
  return out;
}

export function hasQuickFilters(f) { return Boolean(f.mood || f.era || f.tag || f.tagf || f.rt || f.wl); }
export function hasAnyFilters(f) { return hasQuickFilters(f) || f.genres.length > 0 || Boolean(f.q); }

// A film's "Part of X" line: open that group on Complete. When Complete is already showing (maybe that very group,
// scrolled away from), the URL may not change, so the view is also told directly.
export const GROUP_EVENT = 'movienight:group';
export function showGroup(key) {
  const was = parse().view === 'complete';
  go({ view: 'complete', movie: null, film: null, group: key });
  if (was) window.dispatchEvent(new CustomEvent(GROUP_EVENT, { detail: key }));
}
