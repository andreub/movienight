// "Complete" on the Android TV app (html.tv): one saga per screen, in the look of Tonight. A full-screen hero with
// the backdrop of the next film to watch, the saga's name, progress and "why" hint, and the whole saga as one
// poster row in release order. D-pad: Left/Right walk the films, OK opens the film's side panel (the detail sheet
// with Play for one in the library; for a seen or missing one, its info, the four verdicts and Unmark),
// Down/Up move to the next/previous saga with a slide. Up on the first saga leaves for the tabs (tvnav.js).
// Browsers never load this: complete.js hands over only when tvnav.js detected the TV app.
import { api } from '../api.js';
import { h, fill, icon, poster, hueOf, errorBox, announce } from '../dom.js';
import { openSagaFilm } from './complete.js';

const KIND_LABEL = { collection: 'Saga', franchise: 'Franchise', series: 'Series' };
const STATE_TEXT = { available: 'In your library · OK to open', seen: 'Seen · OK for details', missing: 'Not in your library · OK to mark seen' };

export function mountTv(root) {
  let alive = true;
  let groups = [];
  let index = 0;
  let screen = null;
  let lastFilm = null; // the film focused last: kept across reloads and given focus back when the panel closes
  const stage = h('div', { class: 'ctv__stage' });
  const dots = h('ol', { class: 'ctv__dots', 'aria-hidden': 'true' });
  const el = h('section', { class: 'ctv', 'aria-roledescription': 'carousel', 'aria-label': 'Sagas you have started' },
    h('h1', { class: 'sr-only', text: 'Complete' }), stage, dots);
  root.append(el);

  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;

  function backdrop(g) {
    const next = nextFilm(g);
    const gen = () => h('div', { class: 'pick__backdrop pick__backdrop--gen', 'aria-hidden': 'true' },
      h('span', { text: next?.title || g.name }));
    if (!g.backdrop_url) return gen();
    const img = h('img', { class: 'pick__backdrop', src: g.backdrop_url, alt: '', decoding: 'async' });
    img.addEventListener('error', () => img.replaceWith(gen()), { once: true });
    return img;
  }

  // The film to watch next: the first one in the library, else the first unseen one.
  function nextFilm(g) {
    return g.films.find((f) => f.state === 'available') || g.films.find((f) => f.state !== 'seen') || g.films[0];
  }

  function tile(f, caption) {
    const label = `${f.title}${f.year ? ` (${f.year})` : ''}, ${STATE_TEXT[f.state].split(' · ')[0].toLowerCase()}`;
    const btn = h('button', {
      type: 'button', class: 'ctv__film', 'aria-label': label,
      onclick: () => openSagaFilm(f),
      onfocus: () => {
        lastFilm = f.tmdb_id;
        fill(caption,
          h('span', { class: 'ctv__cap-title', text: f.title }),
          f.year ? h('span', { text: String(f.year) }) : null,
          h('span', { class: `ctv__cap-state ctv__cap-state--${f.state}`, text: STATE_TEXT[f.state] }));
        const smooth = !reduced();
        btn.closest('li').scrollIntoView({ block: 'nearest', inline: 'center', behavior: smooth ? 'smooth' : 'auto' });
      },
    },
    h('span', { class: 'ctv__art' },
      poster(f, { eager: true }),
      f.state === 'seen' ? h('span', { class: 'cfilm__seen', 'aria-hidden': 'true' }, icon('check')) : null,
      f.state === 'available' ? h('span', { class: 'cfilm__badge', text: 'In library' }) : null));
    return h('li', { class: `ctv__item cfilm--${f.state}` }, btn);
  }

  function build(g, i) {
    const caption = h('p', { class: 'ctv__caption', 'aria-hidden': 'true' });
    const row = h('ul', { class: 'ctv__row', 'aria-label': `${g.name}, in release order` }, g.films.map((f) => tile(f, caption)));
    const pct = g.total ? Math.round((g.seen / g.total) * 100) : 0;
    const after = groups[i + 1];
    return h('article', {
      class: 'ctv__screen', style: { '--h': String(hueOf(nextFilm(g) || g)) },
      'aria-roledescription': 'slide', 'aria-label': `${g.name}, ${i + 1} of ${groups.length}`, 'data-key': g.key,
    },
    h('div', { class: 'pick__art ctv__backdrop' }, backdrop(g)),
    h('div', { class: 'ctv__inner' },
      h('p', { class: 'ctv__kicker' },
        h('span', { text: KIND_LABEL[g.kind] || 'Saga' }),
        h('span', { text: `${i + 1} of ${groups.length}` })),
      h('h2', { class: 'ctv__title', text: g.name }),
      h('div', { class: 'ctv__meta' },
        h('div', {
          class: 'cprog__bar ctv__bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(g.total),
          'aria-valuenow': String(g.seen), 'aria-label': `${g.seen} of ${g.total} seen`,
        }, h('span', { class: 'cprog__fill', style: { width: `${pct}%` } })),
        h('span', { class: 'ctv__count' }, h('strong', { text: String(g.seen) }), ` of ${g.total} seen`,
          g.available ? h('span', { class: 'cprog__avail', text: ` · ${g.available} in library` }) : null),
        g.reason ? h('span', { class: 'ctv__why', text: g.reason }) : null),
      caption,
      row),
    h('p', { class: 'ctv__hint', 'aria-hidden': 'true' },
      after ? [icon('chevron'), `Next: ${after.name}`] : null));
  }

  function paintDots() {
    fill(dots, groups.map((g, i) => h('li', { class: i === index ? 'is-current' : null })));
  }

  // Show saga `i`; `dir` is 1 (down, next), -1 (up, previous) or 0 (first paint, no slide).
  function show(i, dir = 0, focusKey = null) {
    const g = groups[i];
    if (!g) return;
    index = i;
    const next = build(g, i);
    const old = screen;
    screen = next;
    if (old && dir && !reduced()) {
      old.classList.add(dir > 0 ? 'is-leaving-up' : 'is-leaving-down');
      next.classList.add(dir > 0 ? 'is-entering-up' : 'is-entering-down');
      stage.append(next);
      const done = () => old.remove();
      old.addEventListener('animationend', done, { once: true });
      setTimeout(done, 700);
    } else {
      fill(stage, next);
    }
    paintDots();
    const tiles = [...next.querySelectorAll('.ctv__film')];
    const start = focusKey != null ? g.films.findIndex((f) => f.tmdb_id === focusKey) : g.films.indexOf(nextFilm(g));
    tiles[Math.max(0, start)]?.focus({ preventScroll: true });
    announce(`${g.name}. ${g.seen} of ${g.total} seen.${g.reason ? ` ${g.reason}.` : ''}`);
  }

  function onKey(e) {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || document.querySelector('dialog[open]')) return;
    const film = document.activeElement?.closest?.('.ctv__film');
    if (!film || !screen?.contains(film)) return;
    const tiles = [...screen.querySelectorAll('.ctv__film')];
    const at = tiles.indexOf(film);
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const to = tiles[at + (e.key === 'ArrowRight' ? 1 : -1)];
      if (to) to.focus({ preventScroll: true });
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (index + 1 < groups.length) show(index + 1, 1);
    } else if (e.key === 'ArrowUp' && index > 0) {
      e.preventDefault();
      show(index - 1, -1);
    }
    // Up on the first saga: not handled here, so tvnav.js moves focus to the tabs.
  }
  document.addEventListener('keydown', onKey);

  async function load() {
    const focusKey = document.activeElement?.closest?.('.ctv') || document.querySelector('dialog[open]') ? lastFilm : null;
    const key = groups[index]?.key;
    if (!groups.length) fill(stage, h('div', { class: 'ctv__screen ctv__screen--skeleton pick pick--skeleton', 'aria-busy': 'true' }, h('div', { class: 'pick__art skel' })));
    try {
      const r = await api.completions({});
      if (!alive) return;
      groups = r.groups;
      if (!groups.length) {
        fill(dots);
        fill(stage, h('div', { class: 'state ctv__empty' },
          h('p', { class: 'state__title', text: r.completed.length || r.hidden_count ? 'All caught up' : 'Nothing started yet' }),
          h('p', { class: 'state__text', text: 'Watch one film of a saga, franchise or trilogy and it shows up here.' })));
        return;
      }
      const keep = groups.findIndex((g) => g.key === key);
      show(keep >= 0 ? keep : 0, 0, keep >= 0 ? focusKey : null);
    } catch (err) {
      if (alive) fill(stage, errorBox(err, load));
    }
  }

  // A film marked (or unmarked) seen in the panel changes the counts: refresh, staying on the same saga and film.
  const onSeen = () => load();
  window.addEventListener('movienight:seen', onSeen);
  // Closing the panel (Back) gives focus back to the film it was opened from, even after a refresh.
  const dialog = document.getElementById('detail');
  const onClose = () => {
    if (!alive || !screen) return;
    const i = groups[index]?.films.findIndex((f) => f.tmdb_id === lastFilm) ?? -1;
    const tile = screen.querySelectorAll('.ctv__film')[i];
    if (tile) setTimeout(() => tile.focus({ preventScroll: true }), 0);
  };
  dialog?.addEventListener('close', onClose);
  load();

  return {
    update() {},
    destroy() {
      alive = false;
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('movienight:seen', onSeen);
      dialog?.removeEventListener('close', onClose);
    },
  };
}
