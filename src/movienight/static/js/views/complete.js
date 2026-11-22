// "Complete": sagas, franchises and series the user has started, discovered from what they watched, as a single
// column of roomy cards. Posters fill the card's width (CSS grid, measured with a ResizeObserver) so covers stay
// large. Sagas show the whole group in release order: seen films dimmed with a check, films in the library in
// full colour with the accent ring, missing ones greyed; any film opens its detail panel (missing and seen ones
// can be marked seen with a verdict, or unmarked), so it is clear where the
// gaps fall. When a saga does not fit in a few rows, long runs of seen films fold into a "✓ 7 seen" tile (tap
// opens it); unseen films and their neighbours always stay. "Show all" appears only if it still overflows.
// Groups without an order list unseen films first. Finished and hidden groups stay folded at the bottom.
import { api } from '../api.js';
import { go } from '../state.js';
import { h, fill, icon, poster, errorBox, toast, announce, quoted } from '../dom.js';
import { TV } from '../tvnav.js';
import { mountTv } from './completetv.js';

export const title = 'Complete';

const KINDS = [
  [null, 'All'], ['collection', 'Sagas'], ['franchise', 'Franchises'], ['series', 'Series'],
];

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
  const first = run[0].year;
  const last = run[run.length - 1].year;
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
export function sagaLayout(films, cap, open = new Set()) {
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
  return { items: items.slice(0, cap), cut: items.length - cap };
}

// Unordered groups: unseen films first (the API already ranks them), then seen ones when expanded.
function listLayout(films, cap, all) {
  const unseen = films.filter((f) => f.state !== 'seen');
  const items = (all ? [...unseen, ...films.filter((f) => f.state === 'seen')] : unseen).map((film) => ({ film }));
  if (all || items.length <= cap) return { items, cut: 0 };
  return { items: items.slice(0, cap), cut: items.length - cap };
}

// How many rows a card may fill before folding: sagas get 3 (a whole trilogy-of-trilogies fits unfolded);
// unordered groups 2, or 3 when the card is wide.
const rowsFor = (cols, ordered) => (ordered || cols >= 6 ? 3 : 2);

export function mount(root) {
  if (TV) return mountTv(root); // the Android TV app: one saga per screen (completetv.js)
  let kind = null;
  let alive = true;
  let token = 0;
  let data = null;
  const expanded = new Set(); // keys shown in full ("Show all"), kept across reloads
  const observers = new Set();

  const chips = KINDS.map(([id, label]) => h('button', {
    type: 'button', class: 'chip chip--small', 'aria-pressed': String(id === kind), 'data-kind': id || '',
    onclick: () => { kind = id; paintChips(); load(); },
  }, h('span', { text: label }), h('span', { class: 'chip__count' })));
  const list = h('ul', { class: 'cgroups' });
  const status = h('div');
  const folds = h('div', { class: 'cfolds' });

  root.append(h('div', { class: 'complete' },
    h('div', { class: 'complete__top' },
      h('h1', { class: 'page-title', text: 'Complete' }),
      h('p', { class: 'lede', text: 'Sagas, franchises and series you have started. New releases and the ones you rated highest come first.' }),
      h('div', { class: 'chips chips--scroll complete__kinds', role: 'group', 'aria-label': 'Kind of group' }, chips)),
    status, list, folds));

  function paintChips() {
    for (const c of chips) {
      const id = c.dataset.kind || null;
      c.setAttribute('aria-pressed', String(id === kind));
      const n = data?.counts ? (id ? data.counts[id] : Object.values(data.counts).reduce((a, b) => a + b, 0)) : null;
      c.querySelector('.chip__count').textContent = n == null ? '' : String(n);
      c.hidden = Boolean(id) && data?.counts && !data.counts[id] && id !== kind;
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
      const { items, cut } = g.ordered ? sagaLayout(g.films, cap, open) : listLayout(g.films, cap, expanded.has(g.key));
      fill(grid, items.map((it) => (it.fold ? foldTile(it.fold, () => { open.add(it.start); render(true); }) : film(it.film))));
      const all = expanded.has(g.key);
      moreBtn.hidden = !cut && !all;
      if (all) moreBtn.textContent = 'Show less';
      else if (g.ordered) moreBtn.textContent = `Show all ${g.total}`;
      else moreBtn.textContent = `+${cut} more · show all`;
      moreBtn.setAttribute('aria-expanded', String(all));
    }

    const ro = new ResizeObserver(() => render());
    ro.observe(grid);
    observers.add(ro);

    const li = h('li', { class: 'cgroup', 'data-key': g.key },
      h('div', { class: 'cgroup__head' },
        h('h2', { class: 'cgroup__name', text: g.name }),
        g.reason ? h('span', { class: 'cgroup__why', text: g.reason }) : null,
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
      data = r;
      paint();
    } catch (err) {
      if (alive && t === token) { fill(list); fill(status, errorBox(err, load)); }
    }
  }

  // A film marked seen (detail sheet) changes the counts: refresh quietly.
  const onSeen = () => load();
  window.addEventListener('movienight:seen', onSeen);
  paintChips();
  load();

  return {
    update() {},
    destroy() { alive = false; disconnect(); window.removeEventListener('movienight:seen', onSeen); },
  };
}
