// "Complete": sagas, franchises and series the user has started, discovered from what they watched, as a single
// column of roomy cards. Posters fill the card's width (CSS grid, measured with a ResizeObserver) so covers stay
// large. Sagas show the whole group in release order: seen films dimmed with a check, films in the library in
// full colour with the accent ring, missing ones greyed; any film opens its detail panel (missing and seen ones
// can be marked seen with a verdict, or unmarked), so it is clear where the
// gaps fall. When a saga does not fit in a few rows, long runs of seen films fold into a "✓ 7 seen" tile (tap
// opens it); unseen films and their neighbours always stay. "Show all" appears only if it still overflows.
// Directors, actors and canonical lists (Best Picture winners, a critics' poll: no order) list unseen films first, then the seen ones folded into one "✓ N seen" tile, with
// the photo of the person on the card. Kind chips filter both layouts. Finished and hidden groups stay folded at the bottom.
// Two layouts: this "List" and "Screen" (completetv.js, one saga per screen). A toggle in the header switches
// between them in place and the browser remembers the choice; the TV app always gets Screen, with no toggle.
import { api } from '../api.js';
import { go } from '../state.js';
import { h, fill, icon, poster, hueOf, errorBox, toast, announce, quoted } from '../dom.js';
import { TV } from '../tvnav.js';
import { mountTv } from './completetv.js';

export const title = 'Complete';

// The kind chips, shared with the Screen layout. A chip whose kind has no group is hidden (unless it is the current one).
export const KINDS = [
  [null, 'All'], ['collection', 'Sagas'], ['franchise', 'Franchises'], ['list', 'Lists'],
  ['director', 'Directors'], ['actor', 'Actors'],
];
export const PEOPLE = new Set(['director', 'actor']);

const KIND_KEY = 'movienight.completeKind';

// The chosen kind chip, remembered by the browser for both layouts (null = All).
export function storedKind() {
  try {
    const k = localStorage.getItem(KIND_KEY);
    return KINDS.some(([id]) => id && id === chipKind(k)) ? chipKind(k) : null;
  } catch { return null; }
}

export function storeKind(kind) {
  try { if (kind) localStorage.setItem(KIND_KEY, kind); else localStorage.removeItem(KIND_KEY); } catch { /* storage unavailable */ }
}

// How many groups a chip stands for, from the API's `counts` (All = every kind).
// Keyword-detected series ("Cornetto Trilogy") are sagas to the user: they share the Sagas chip.
export const chipKind = (kind) => (kind === 'series' ? 'collection' : kind);

export function kindCount(counts, id) {
  if (!counts) return null;
  if (!id) return Object.values(counts).reduce((a, b) => a + b, 0);
  return id === 'collection' ? (counts.collection || 0) + (counts.series || 0) : counts[id] || 0;
}

// A director's or actor's face (TMDB w185), or their initials on a colour of their own when TMDB has none.
export function face(g, cls = '') {
  const initials = (g.name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase();
  const gen = () => h('span', { class: 'cface__gen', style: { '--h': String(hueOf({ title: g.name })) }, text: initials });
  const wrap = h('span', { class: `cface ${cls}`.trim(), 'aria-hidden': 'true' });
  if (g.profile_url) {
    const img = h('img', { src: g.profile_url, alt: '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('error', () => img.replaceWith(gen()), { once: true });
    wrap.append(img);
  } else wrap.append(gen());
  return wrap;
}

const pct = (g) => (g.total ? Math.round((g.seen / g.total) * 100) : 0);

function progress(g) {
  return h('div', { class: 'cprog' },
    h('div', {
      class: 'cprog__bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(g.total),
      'aria-valuenow': String(g.seen), 'aria-label': `${g.name}: ${g.seen} of ${g.total} seen`,
    }, h('span', { class: 'cprog__fill', style: { width: `${pct(g)}%` } })),
    h('span', { class: 'cprog__label' },
      h('strong', { text: String(g.seen) }), ` of ${g.total} seen`,
      g.available ? h('span', { class: 'cprog__avail', text: ` · ${g.available} in library` }) : null));
}

const STATE_LABEL = { available: 'in your library', seen: 'seen', missing: 'not in your library' };

// Opens a film: one in the library to watch gets the full detail sheet (Play lives there); any other one (seen,
// or missing from the library) the light panel by TMDB id, to mark it seen with a verdict or undo a mark.
export function openSagaFilm(f) {
  if (f.state === 'available' && f.movie_id) go({ movie: f.movie_id });
  else go({ film: f.tmdb_id });
}

// One film tile; every one opens a panel.
function film(f) {
  const label = `${f.title}${f.year ? ` (${f.year})` : ''}, ${STATE_LABEL[f.state]}`;
  const art = h('div', { class: 'cfilm__art' },
    poster(f, { label }),
    f.state === 'available' ? h('span', { class: 'cfilm__badge', text: 'In library' }) : null,
    f.state === 'seen' ? h('span', { class: 'cfilm__seen', 'aria-hidden': 'true' }, icon('check')) : null);
  return h('li', { class: `cfilm cfilm--${f.state}` },
    h('button', { type: 'button', class: 'cfilm__btn', 'aria-label': label, title: label, onclick: () => openSagaFilm(f) }, art));
}

// A run of seen films folded into one tile the size of a poster.
function foldTile(run, onOpen) {
  const ys = run.map((f) => f.year).filter(Boolean); // filmographies list seen films newest first
  const first = ys.length ? Math.min(...ys) : null;
  const last = ys.length ? Math.max(...ys) : null;
  const years = first && last ? (first === last ? String(first) : `${first}–${last}`) : '';
  return h('li', { class: 'cfilm cfilm--fold' },
    h('button', {
      type: 'button', class: 'cfold-tile', onclick: onOpen,
      'aria-label': `${run.length} seen films${years ? `, ${years}` : ''}. Show them`,
    },
    h('span', { class: 'cfold-tile__check' }, icon('check')),
    h('span', { class: 'cfold-tile__n', text: String(run.length) }),
    h('span', { class: 'cfold-tile__label', text: 'seen' }),
    years ? h('span', { class: 'cfold-tile__years', text: years }) : null));
}

// The tiles of an ordered group for `cap` slots: every film when it fits; otherwise runs of seen films folded
// (keeping the neighbours of unseen films, then without them), and cut at `cap` only as a last resort.
// `open` holds the start index of runs the user unfolded; once one is open the card no longer truncates.
// `pin` (a tmdb id: the film the card's New/Recent reason names) always stays on show, even when cutting.
export function sagaLayout(films, cap, open = new Set(), pin = null) {
  const all = films.map((f, i) => ({ film: f, i }));
  if (films.length <= cap && !open.size) return { items: all, cut: 0 };
  let items = all;
  for (const neighbours of [true, false]) {
    const keep = films.map((f) => f.state !== 'seen');
    if (neighbours) films.forEach((f, i) => { if (f.state !== 'seen') { if (i > 0) keep[i - 1] = true; if (i + 1 < films.length) keep[i + 1] = true; } });
    items = [];
    let run = [];
    const flush = () => {
      if (run.length >= 2 && !open.has(run[0])) items.push({ fold: run.map((i) => films[i]), start: run[0] });
      else for (const i of run) items.push({ film: films[i], i });
      run = [];
    };
    films.forEach((f, i) => { if (keep[i]) { flush(); items.push({ film: f, i }); } else run.push(i); });
    flush();
    if (items.length <= cap || open.size) return { items, cut: 0 };
  }
  const shown = items.slice(0, cap);
  const at = items.findIndex((it) => it.film && it.film.tmdb_id === pin);
  if (at >= cap) shown[cap - 1] = items[at];
  return { items: shown, cut: items.length - cap };
}

// Unordered groups (filmographies): unseen films first (the API already ranks them), then the seen ones folded
// into one "✓ N seen" tile at the end (unfolded with `seenOpen`). When that overflows `cap`, the tile stays last
// and the unseen films are cut; once the seen ones are open, or with `all`, nothing is cut.
export function listLayout(films, cap, all = false, seenOpen = false) {
  const unseen = films.filter((f) => f.state !== 'seen').map((film) => ({ film }));
  const seen = films.filter((f) => f.state === 'seen');
  const tail = seen.length >= 2 && !seenOpen && !all ? [{ fold: seen, start: 'seen' }] : seen.map((film) => ({ film }));
  const items = [...unseen, ...tail];
  if (all || seenOpen || items.length <= cap) return { items, cut: 0 };
  const room = Math.max(1, cap - tail.length);
  return { items: [...unseen.slice(0, room), ...tail], cut: unseen.length - room };
}

// How many rows a card may fill before folding: sagas get 3 (a whole trilogy-of-trilogies fits unfolded);
// unordered groups 2, or 3 when the card is wide.
const rowsFor = (cols, ordered) => (ordered || cols >= 6 ? 3 : 2);

const LAYOUT_KEY = 'movienight.completeLayout';
const LAYOUTS = [['list', 'List', 'list'], ['screen', 'Screen', 'screen']];

function storedLayout() {
  try { return localStorage.getItem(LAYOUT_KEY) === 'screen' ? 'screen' : 'list'; } catch { return 'list'; }
}

function storeLayout(layout) {
  try { localStorage.setItem(LAYOUT_KEY, layout); } catch { /* storage unavailable */ }
}

// The List / Screen segmented control.
function layoutToggle(layout, onPick) {
  const items = LAYOUTS.map(([value, label, glyph]) => {
    const input = h('input', {
      type: 'radio', name: 'complete-layout', value, class: 'seg__input', checked: value === layout,
      onchange: () => onPick(value),
    });
    return h('label', { class: 'seg__item' }, input, h('span', {}, icon(glyph), label));
  });
  return h('fieldset', { class: 'seg seg--small complete__layout' }, h('legend', { class: 'sr-only', text: 'Layout' }), items);
}

export function mount(root) {
  if (TV) return mountTv(root); // the Android TV app: always one saga per screen, no toggle
  let inner = null;
  function render(layout, { refocus = false } = {}) {
    inner?.destroy();
    root.replaceChildren();
    const toggle = layoutToggle(layout, (next) => { storeLayout(next); render(next, { refocus: true }); });
    inner = layout === 'screen' ? mountTv(root, { web: true, toolbar: toggle }) : mountList(root, toggle);
    if (refocus) toggle.querySelector('input:checked')?.focus({ preventScroll: true });
  }
  render(storedLayout());
  return {
    update(route) { inner.update(route); },
    destroy() { inner.destroy(); },
  };
}

function mountList(root, toggle) {
  let kind = storedKind();
  let alive = true;
  let token = 0;
  let data = null;
  const expanded = new Set(); // keys shown in full ("Show all"), kept across reloads
  const observers = new Set();

  const chips = KINDS.map(([id, label]) => h('button', {
    type: 'button', class: 'chip chip--small', 'aria-pressed': String(id === kind), 'data-kind': id || '',
    onclick: () => { kind = id; storeKind(id); paintChips(); load(); },
  }, h('span', { text: label }), h('span', { class: 'chip__count' })));
  const list = h('ul', { class: 'cgroups' });
  const status = h('div');
  const folds = h('div', { class: 'cfolds' });

  root.append(h('div', { class: 'complete' },
    h('div', { class: 'complete__top' },
      h('div', { class: 'complete__head' }, h('h1', { class: 'page-title', text: 'Complete' }), toggle),
      h('p', { class: 'lede', text: 'Sagas, franchises and series you have started, the great lists you have dipped into, then the directors and actors you keep coming back to. New releases and the ones you rated highest come first.' }),
      h('div', { class: 'chips chips--scroll complete__kinds', role: 'group', 'aria-label': 'Kind of group' }, chips)),
    status, list, folds));

  function paintChips() {
    for (const c of chips) {
      const id = c.dataset.kind || null;
      c.setAttribute('aria-pressed', String(id === kind));
      const n = kindCount(data?.counts, id);
      c.querySelector('.chip__count').textContent = n == null ? '' : String(n);
      c.hidden = Boolean(id) && data?.counts && !kindCount(data.counts, id) && id !== kind;
    }
  }

  function card(g) {
    const grid = h('ul', { class: 'cgrid', 'aria-label': g.ordered ? `${g.name}, in release order` : `Still to watch in ${g.name}` });
    const moreBtn = h('button', { type: 'button', class: 'cmore', hidden: true, onclick: () => { toggleAll(); } });
    const open = new Set();
    let cols = 0;

    const toggleAll = () => {
      if (expanded.has(g.key)) expanded.delete(g.key); else expanded.add(g.key);
      render(true);
    };

    function render(force = false) {
      const style = getComputedStyle(grid);
      const min = parseFloat(style.getPropertyValue('--tile-min')) || 112;
      const gap = parseFloat(style.columnGap) || 12;
      const width = grid.clientWidth;
      if (!width) return;
      const c = Math.max(1, Math.floor((width + gap) / (min + gap)));
      if (c === cols && !force) return;
      cols = c;
      const cap = expanded.has(g.key) ? Infinity : c * rowsFor(c, g.ordered);
      const { items, cut } = g.ordered ? sagaLayout(g.films, cap, open, g.reason_film) : listLayout(g.films, cap, expanded.has(g.key), open.has('seen'));
      fill(grid, items.map((it) => (it.fold ? foldTile(it.fold, () => { open.add(it.start); render(true); }) : film(it.film))));
      const all = expanded.has(g.key);
      moreBtn.hidden = !cut && !all;
      moreBtn.textContent = all ? 'Show less' : `Show all ${g.total}`;
      moreBtn.setAttribute('aria-expanded', String(all));
    }

    const person = PEOPLE.has(g.kind);

    const ro = new ResizeObserver(() => render());
    ro.observe(grid);
    observers.add(ro);

    const li = h('li', { class: 'cgroup', 'data-key': g.key },
      h('div', { class: `cgroup__head${person ? ' cgroup__head--person' : ''}` },
        person ? face(g, 'cgroup__face') : null,
        person
          ? h('div', { class: 'cgroup__who' },
            h('span', { class: 'cgroup__kind', text: g.kind === 'director' ? 'Director' : 'Actor' }),
            h('h2', { class: 'cgroup__name', text: g.name }))
          : h('h2', { class: 'cgroup__name', text: g.name }),
        g.reason ? h('span', { class: 'cgroup__why', text: g.reason, title: g.reason, 'aria-label': g.reason }) : null,
        h('button', { type: 'button', class: 'cgroup__hide', 'aria-label': `Hide ${g.name}`, title: 'Hide', onclick: () => hide(g, li) }, icon('close'))),
      progress(g),
      grid,
      h('div', { class: 'cgroup__foot' }, moreBtn));
    return li;
  }

  function row(g, { action } = {}) {
    return h('li', { class: 'crow' },
      h('span', { class: 'crow__icon' }, icon(g.complete ? 'trophy' : 'eyeoff')),
      h('span', { class: 'crow__name', text: g.name }),
      h('span', { class: 'crow__meta', text: g.complete ? `All ${g.total} seen` : `${g.seen} of ${g.total}` }),
      action || null);
  }

  async function hide(g, li) {
    li.classList.add('is-leaving');
    try {
      await api.hideCompletion(g.key);
    } catch (err) {
      li.classList.remove('is-leaving');
      toast(err.detail || 'Could not hide it.');
      return;
    }
    announce(`${g.name} hidden.`);
    toast(`Hid ${quoted({ title: g.name })}.`, {
      action: { label: 'Undo', run: () => api.unhideCompletion(g.key).then(load).catch((e) => toast(e.detail || 'Could not undo.')) },
    });
    load();
  }

  function folded(label, items, { onOpen } = {}) {
    const body = h('ul', { class: 'crows' }, items);
    const d = h('details', { class: 'cfold' }, h('summary', { class: 'cfold__sum' }, icon('chevron', 'cfold__chev'), h('span', { text: label })), body);
    if (onOpen) d.addEventListener('toggle', () => { if (d.open) onOpen(body); }, { once: true });
    return d;
  }

  function disconnect() {
    for (const ro of observers) ro.disconnect();
    observers.clear();
  }

  function paint() {
    paintChips();
    disconnect();
    const groups = data.groups;
    fill(list, groups.map(card));
    if (!groups.length) {
      const any = data.completed.length || data.hidden_count;
      fill(status, h('div', { class: 'state' },
        h('p', { class: 'state__title', text: any ? 'All caught up' : 'Nothing started yet' }),
        h('p', { class: 'state__text', text: any
          ? 'Every saga you have started here is finished or hidden.'
          : 'Watch one film of a saga, franchise or trilogy and it shows up here, with what is left to see.' })));
    } else fill(status);
    const parts = [];
    if (data.completed.length) parts.push(folded(`Completed (${data.completed.length})`, data.completed.map((g) => row(g))));
    if (data.hidden_count) {
      parts.push(folded(`Show hidden (${data.hidden_count})`, [], {
        onOpen: async (body) => {
          try {
            const r = await api.completions({ kind, hidden: true });
            fill(body, r.hidden.map((g) => row(g, {
              action: h('button', { type: 'button', class: 'linkbtn', onclick: () => api.unhideCompletion(g.key).then(load).catch((e) => toast(e.detail || 'Could not unhide it.')) }, 'Unhide'),
            })));
          } catch (err) { fill(body, errorBox(err)); }
        },
      }));
    }
    fill(folds, parts);
  }

  async function load() {
    const t = ++token;
    if (!data) fill(list, Array.from({ length: 3 }, () => h('li', { class: 'cgroup cgroup--skeleton', 'aria-hidden': 'true' },
      h('div', { class: 'skel skel--line skel--short' }), h('div', { class: 'skel skel--line' }),
      h('ul', { class: 'cgrid cgrid--skeleton' }, Array.from({ length: 8 }, () => h('li', { class: 'cfilm' }, h('div', { class: 'poster skel' })))))));
    try {
      const r = await api.completions({ kind });
      if (!alive || t !== token) return;
      if (kind && !kindCount(r.counts, kind)) { // a remembered kind with no groups left: back to All
        kind = null;
        storeKind(null);
        return load();
      }
      data = r;
      paint();
    } catch (err) {
      if (alive && t === token) { fill(list); fill(status, errorBox(err, load)); }
    }
  }

  // A film marked, voted or unmarked in the panel (`movienight:marked`, fired once the server accepted it) changes the counts: refresh quietly.
  const onSeen = () => load();
  window.addEventListener('movienight:marked', onSeen);
  paintChips();
  load();

  return {
    update() {},
    destroy() { alive = false; disconnect(); window.removeEventListener('movienight:marked', onSeen); },
  };
}
