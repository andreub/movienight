// "Seen these?": a dense poster grid of library films, each with a four-way verdict:
// didn't like it / just seen / liked it / a favourite. One tap marks the film seen and sets the vote together
// (one request); tapping the chosen one again undoes both. Two lists: films to check (most popular first)
// and films already marked (newest mark first), so earlier marks can be rated afterwards.
import { api } from '../api.js';
import { h, fill, icon, poster, num, stars, errorBox, announce } from '../dom.js';
import { setSeenVote, isSeen, OUTCOMES } from '../seen.js';
import { voteOf, LOVE } from '../vote.js';

export const title = 'Seen these?';
const PAGE = 60;


const TABS = {
  check: {
    label: 'To check',
    lede: 'Seen one of these? Tap how it went. Seen films stop showing up in your picks, and your verdict teaches it your taste. Most popular first.',
    load: (params) => api.seenCandidates(params),
    // Marked films leave this list on the server, so the next page starts after the ones still unmarked.
    offset: (loaded) => loaded.filter((m) => !isSeen(m)).length,
    empty: ['Nothing left to check', 'No older, popular films in your library are waiting. Nice work.'],
  },
  marked: {
    label: 'Already marked',
    lede: 'Films you marked as seen, newest first. Rate the ones you remember so they count towards your taste.',
    load: (params) => api.seenMarked(params),
    offset: (loaded) => loaded.filter((m) => isSeen(m)).length,
    empty: ['Nothing marked yet', 'Films you mark as seen show up here, so you can rate them later.'],
  },
};

const outcomeOf = (m) => {
  if (!isSeen(m)) return null;
  const v = voteOf(m);
  return OUTCOMES.find((o) => o.vote === (v >= LOVE ? LOVE : v)) || OUTCOMES[1];
};

export function mount(root) {
  let tab = 'check';
  const grid = h('ul', { class: 'triage__grid' });
  const count = h('p', { class: 'triage__count', 'aria-live': 'polite' });
  const more = h('button', { type: 'button', class: 'btn btn--ghost', hidden: true, onclick: () => loadPage() }, 'Load more');
  const status = h('div');
  const lede = h('p', { class: 'lede' });
  const tabBtns = Object.entries(TABS).map(([id, t]) => h('button', {
    type: 'button', class: 'chip chip--small', 'aria-pressed': 'false', 'data-tab': id, onclick: () => switchTo(id),
  }, t.label));

  root.append(h('div', { class: 'triage' },
    h('div', { class: 'triage__top' },
      h('h1', { class: 'page-title', text: 'Seen these?' }),
      lede,
      h('div', { class: 'chips triage__tabs', role: 'group', 'aria-label': 'Which films' }, tabBtns),
      h('div', { class: 'triage__bar' }, count)),
    status, grid,
    h('div', { class: 'triage__end' }, more)));

  let loaded = new Map(); // id -> movie, in list order
  let marked = 0;         // seen_here count across the app (starts from the server, moves with every tap)
  let loading = 0;        // request token: a tab switch makes older responses stale
  let alive = true;

  const paintCount = () => { count.textContent = `${num(marked)} marked`; };
  paintCount();
  api.profile().then((p) => { if (alive) { marked += p?.stats?.seen_here ?? 0; paintCount(); } }).catch(() => {});

  function rateGroup(m) {
    const btns = OUTCOMES.map((o) => h('button', {
      type: 'button', class: `rate__btn rate__btn--${o.key}`, title: o.label, 'aria-label': o.label, 'aria-pressed': 'false',
      onclick: () => choose(m, o),
    }, icon(o.icon)));
    return h('div', { class: 'rate', role: 'group', 'aria-label': `${m.title}: have you seen it?` }, btns);
  }

  function tile(m) {
    const verdict = h('span', { class: 'tile__verdict' });
    const li = h('li', { class: 'tile__item', 'data-id': m.id },
      h('div', { class: 'tile' },
        h('div', { class: 'tile__poster' },
          poster(m),
          h('span', { class: 'tile__badge', 'aria-hidden': 'true' })),
        rateGroup(m),
        h('span', { class: 'tile__title', text: m.title }),
        h('span', { class: 'tile__year' },
          m.year ? String(m.year) : '',
          m.my_rating != null ? h('span', { class: 'tile__lb', title: 'Your Letterboxd rating counts, not this verdict' }, ' ', stars(m.my_rating)) : null,
          verdict)));
    paint(li, m);
    return li;
  }

  function paint(li, m) {
    const o = outcomeOf(m);
    const t = li.querySelector('.tile');
    t.dataset.outcome = o ? o.key : '';
    for (const b of li.querySelectorAll('.rate__btn')) b.setAttribute('aria-pressed', String(Boolean(o) && b.classList.contains(`rate__btn--${o.key}`)));
    const badge = li.querySelector('.tile__badge');
    if (o) badge.replaceChildren(icon(o.icon)); else badge.replaceChildren();
    li.querySelector('.tile__verdict').textContent = o ? ` · ${o.short}` : '';
  }

  // One request per film at a time, so a rollback can never land on top of a later choice.
  const busy = new Set();
  async function choose(m, o) {
    if (busy.has(m.id)) return;
    busy.add(m.id);
    const undo = outcomeOf(m)?.key === o.key;
    try {
      const ok = await setSeenVote(m, !undo, undo ? 0 : o.vote, 'triage');
      if (ok) announce(undo ? `${m.title} unmarked.` : `${m.title}: ${o.short.toLowerCase()}.`);
    } finally {
      busy.delete(m.id);
    }
  }

  // Optimistic: the events fire before the request, so the tile and the count flip at once (and back on failure).
  function onSeen(e) {
    const li = grid.querySelector(`[data-id="${e.detail.id}"]`);
    const m = loaded.get(e.detail.id);
    if (li && m) paint(li, m);
  }
  function onSeenCount(e) {
    if (e.detail.was === e.detail.seen) return;      // re-rating a film already marked
    marked = Math.max(0, marked + (e.detail.seen ? 1 : -1));
    paintCount();
  }
  window.addEventListener('movienight:seen', onSeenCount);
  window.addEventListener('movienight:seen', onSeen);
  window.addEventListener('movienight:vote', onSeen);

  function switchTo(id) {
    tab = id;
    for (const b of tabBtns) b.setAttribute('aria-pressed', String(b.dataset.tab === id));
    lede.textContent = TABS[id].lede;
    loaded = new Map();
    fill(grid);
    fill(status);
    more.hidden = true;
    loadPage();
  }

  async function loadPage() {
    const token = ++loading;
    const t = TABS[tab];
    more.disabled = true;
    try {
      const res = await t.load({ limit: PAGE, offset: t.offset([...loaded.values()]) });
      if (!alive || token !== loading) return;
      fill(status);
      const fresh = res.items.filter((m) => !loaded.has(m.id));
      for (const m of fresh) loaded.set(m.id, m);
      grid.append(...fresh.map(tile));
      // `total` counts the films still in this list on the server; the ones on screen are part of it.
      more.hidden = res.items.length === 0 || t.offset([...loaded.values()]) >= res.total;
      if (!loaded.size) fill(status, h('div', { class: 'state' },
        h('p', { class: 'state__title', text: t.empty[0] }),
        h('p', { class: 'state__text', text: t.empty[1] })));
    } catch (err) {
      if (alive && token === loading) fill(status, errorBox(err, () => { fill(status); loadPage(); }));
    } finally {
      if (token === loading) more.disabled = false;
    }
  }
  switchTo('check');

  return {
    update() {},
    destroy() {
      alive = false;
      window.removeEventListener('movienight:seen', onSeenCount);
      window.removeEventListener('movienight:seen', onSeen);
      window.removeEventListener('movienight:vote', onSeen);
    },
  };
}
