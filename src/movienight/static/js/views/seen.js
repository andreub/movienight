// "Seen these?": a dense poster grid of the library films the user most likely knows already.
// One tap marks a film as seen (tap again to undo). Marks are saved at once, with rollback on error.
import { api } from '../api.js';
import { h, fill, icon, poster, num, errorBox, announce } from '../dom.js';
import { setSeen, isSeen } from '../seen.js';

export const title = 'Seen these?';
const PAGE = 60;

export function mount(root) {
  const grid = h('ul', { class: 'triage__grid' });
  const count = h('p', { class: 'triage__count', 'aria-live': 'polite' });
  const more = h('button', { type: 'button', class: 'btn btn--ghost', hidden: true, onclick: () => loadPage() }, 'Load more');
  const status = h('div');

  root.append(h('div', { class: 'triage' },
    h('div', { class: 'triage__top' },
      h('h1', { class: 'page-title', text: 'Seen these?' }),
      h('p', { class: 'lede', text: 'Tap every film you have already watched. They stop showing up in your picks. Most popular first.' }),
      h('div', { class: 'triage__bar' }, count, h('a', { class: 'btn btn--primary btn--sm', href: '#/tonight' }, 'Done'))),
    status, grid,
    h('div', { class: 'triage__end' }, more)));

  const loaded = new Map(); // id -> movie, in list order
  let marked = 0;           // seen_here count across the app (starts from the server, moves with every tap)
  let total = 0;
  let loading = false;
  let alive = true;

  const paintCount = () => { count.textContent = `${num(marked)} marked`; };
  paintCount();
  api.profile().then((p) => { if (alive) { marked += p?.stats?.seen_here ?? 0; paintCount(); } }).catch(() => {});

  // Marked films leave the server's list, so the next page starts after the films that are still unmarked there.
  const nextOffset = () => [...loaded.values()].filter((m) => !isSeen(m)).length;

  function tile(m) {
    const btn = h('button', { type: 'button', class: 'tile', 'aria-pressed': 'false', onclick: () => toggle(m) },
      poster(m),
      h('span', { class: 'tile__check' }, icon('check')),
      h('span', { class: 'tile__title', text: m.title }),
      h('span', { class: 'tile__year', text: m.year ? String(m.year) : '' }));
    btn.setAttribute('aria-label', m.year ? `${m.title}, ${m.year}` : m.title);
    return h('li', { class: 'tile__item', 'data-id': m.id }, btn);
  }

  async function toggle(m) {
    const next = !isSeen(m);
    if (!(await setSeen(m, next, 'triage'))) return;
    announce(next ? `${m.title} marked as seen.` : `${m.title} unmarked.`);
  }

  // Optimistic: the event fires before the request, so the tile and the count flip at once (and back on failure).
  function onSeen(e) {
    const li = grid.querySelector(`[data-id="${e.detail.id}"]`);
    if (!li) return;
    const btn = li.firstElementChild;
    const was = btn.getAttribute('aria-pressed') === 'true';
    if (was === e.detail.seen) return;
    btn.setAttribute('aria-pressed', String(e.detail.seen));
    li.classList.toggle('is-seen', e.detail.seen);
    marked = Math.max(0, marked + (e.detail.seen ? 1 : -1));
    paintCount();
  }
  window.addEventListener('movienight:seen', onSeen);

  async function loadPage() {
    if (loading) return;
    loading = true;
    more.disabled = true;
    try {
      const res = await api.seenCandidates({ limit: PAGE, offset: nextOffset() });
      if (!alive) return;
      fill(status);
      const fresh = res.items.filter((m) => !loaded.has(m.id));
      for (const m of fresh) loaded.set(m.id, m);
      grid.append(...fresh.map(tile));
      total = res.total;
      // `total` counts films still unmarked on the server; the ones on screen are part of it.
      more.hidden = res.items.length === 0 || nextOffset() >= total;
      if (!loaded.size) fill(status, h('div', { class: 'state' },
        h('p', { class: 'state__title', text: 'Nothing left to check' }),
        h('p', { class: 'state__text', text: 'No older, popular films in your library are waiting. Nice work.' })));
    } catch (err) {
      if (alive) fill(status, errorBox(err, () => { fill(status); loadPage(); }));
    } finally {
      loading = false;
      more.disabled = false;
    }
  }
  loadPage();

  return {
    update() {},
    destroy() {
      alive = false;
      window.removeEventListener('movienight:seen', onSeen);
    },
  };
}
