// "Complete" on the Android TV app (html.tv): one saga per screen, in the look of Tonight. A full-screen hero with
// the backdrop of the next film to watch, the saga's name, progress and "why" hint, and the whole saga as one
// poster row in release order. D-pad: Left/Right walk the films, OK opens the film's side panel (the detail sheet
// with Play for one in the library; for a seen or missing one, its info, the four verdicts and Unmark),
// Down/Up move to the next/previous saga with a slide. Up on the first saga leaves for the tabs (tvnav.js).
// The TV app always gets this layout. In a browser it is the "Screen" layout of Complete (`web: true`), without the
// TV mode of tvnav.js: a click opens a film, the arrows beside the dots, the wheel (one saga per gesture) and a
// vertical swipe change saga, and the arrow keys and Enter work as on the remote while nothing else has the keys.
// The kind chips (All, Sagas, ..., Directors, Actors) sit at the top, beside the layout toggle in a browser: a chip
// starts over at the first group of its kind, and the browser remembers it. On the TV, Up on the first group
// reaches them (dimmed until then), Left/Right walk them, OK applies one and Down goes back to the films.
// Directors and actors have no order: their row lists the films to see first, then the seen ones.
import { api } from '../api.js';
import { h, fill, icon, poster, hueOf, errorBox, announce, isTyping } from '../dom.js';
import { openSagaFilm, KINDS, storedKind, storeKind, chipKind, kindOfKey } from './complete.js';

const KIND_LABEL = { collection: 'Saga', franchise: 'Franchise', series: 'Saga', list: 'List', director: 'Director', actor: 'Actor' };
const STATE_TEXT = { available: 'In your library · OK to open', seen: 'Seen · OK for details', missing: 'Not in your library · OK to mark seen' };
const STATE_TEXT_WEB = { available: 'In your library · Enter to open', seen: 'Seen · Enter for details', missing: 'Not in your library · Enter to mark seen' };

const DOTS = 9;
const ARROWS = new Set(['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']);

// `web`: the browser's Screen layout; `toolbar`: a node laid over the top-left corner (the layout toggle).
// `target`: a group key to open on (a film's "Part of X" line), with its kind (or All) picked for this visit.
export function mountTv(root, { web = false, toolbar = null, target = null } = {}) {
  let shownTarget = target;
  let alive = true;
  let everything = []; // every group in progress, all kinds
  let groups = []; // those of the chosen kind
  let counts = null; // kind -> groups in progress
  let kind = storedKind();
  let index = 0;
  let screen = null;
  let lastFilm = null; // the film focused last: kept across reloads and given focus back when the panel closes
  const stage = h('div', { class: 'ctv__stage' });
  const dots = h('ol', { class: 'ctv__dots', 'aria-hidden': 'true' });
  const navBtn = (dir, label) => h('button', {
    type: 'button', class: `ctv__step ctv__step--${dir > 0 ? 'next' : 'prev'}`, 'aria-label': label, title: label,
    onclick: () => step(dir),
  }, icon('chevron'));
  const prevBtn = web ? navBtn(-1, 'Previous saga') : null;
  const nextBtn = web ? navBtn(1, 'Next saga') : null;
  const nav = web ? h('div', { class: 'ctv__nav' }, prevBtn, dots, nextBtn) : dots;
  const chips = KINDS.map(([id, label]) => h('button', {
    type: 'button', class: 'chip chip--small', 'aria-pressed': String(id === kind), 'data-kind': id || '',
    onclick: () => pick(id),
  }, h('span', { text: label }), h('span', { class: 'chip__count' })));
  const kinds = h('div', { class: 'chips ctv__kinds', role: 'group', 'aria-label': 'Kind of group' }, chips);
  const el = h('section', { class: `ctv${web ? ' ctv--web' : ''}`, 'aria-roledescription': 'carousel', 'aria-label': 'Sagas you have started' },
    h('h1', { class: 'sr-only', text: 'Complete' }), stage, nav, h('div', { class: 'ctv__toolbar' }, toolbar, kinds));
  root.append(el);

  const ofKind = (g) => !kind || chipKind(g.kind) === kind;
  const shownChips = () => chips.filter((c) => !c.hidden);

  function paintChips() {
    for (const c of chips) {
      const id = c.dataset.kind || null;
      c.setAttribute('aria-pressed', String(id === kind));
      const n = counts ? (id ? counts[id] || 0 : everything.length) : null;
      c.querySelector('.chip__count').textContent = n == null ? '' : String(n);
      c.hidden = Boolean(id) && Boolean(counts) && !counts[id] && id !== kind;
    }
  }

  // A chip: the first group of that kind, focus left on the chip (Down goes to the films).
  function pick(id) {
    if (id === kind) return;
    kind = id;
    storeKind(id);
    groups = everything.filter(ofKind);
    paintChips();
    if (groups.length) show(0, 0, null, false);
    else empty();
  }

  function empty() {
    fill(dots);
    screen = null;
    if (prevBtn) nav.hidden = true;
    const label = KINDS.find(([id]) => id === kind)?.[1].toLowerCase();
    fill(stage, h('div', { class: 'state ctv__empty' },
      h('p', { class: 'state__title', text: everything.length && kind ? `No ${label} in progress` : 'Nothing started yet' }),
      h('p', { class: 'state__text', text: 'Watch one film of a saga, franchise or trilogy, or a few by the same director or actor, and it shows up here.' })));
  }

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

  const stateText = web ? STATE_TEXT_WEB : STATE_TEXT;

  // Centres a tile in its poster row by scrolling only the row sideways: scrollIntoView would also scroll the
  // hero (and the page) vertically while the next saga slides in.
  function centre(li) {
    const row = li?.parentElement;
    if (!row) return;
    const r = li.getBoundingClientRect();
    const box = row.getBoundingClientRect();
    const left = row.scrollLeft + (r.left - box.left) - (row.clientWidth - r.width) / 2;
    row.scrollTo({ left: Math.max(0, left), behavior: reduced() ? 'auto' : 'smooth' });
  }

  function tile(f, caption) {
    const label = `${f.title}${f.year ? ` (${f.year})` : ''}, ${STATE_TEXT[f.state].split(' · ')[0].toLowerCase()}`;
    const btn = h('button', {
      type: 'button', class: 'ctv__film', 'aria-label': label,
      onclick: () => { lastFilm = f.tmdb_id; openSagaFilm(f); },
      onfocus: () => {
        lastFilm = f.tmdb_id;
        fill(caption,
          h('span', { class: 'ctv__cap-title', text: f.title }),
          f.year ? h('span', { text: String(f.year) }) : null,
          h('span', { class: `ctv__cap-state ctv__cap-state--${f.state}`, text: stateText[f.state] }));
        centre(btn.closest('li'));
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
    const row = h('ul', { class: 'ctv__row', 'aria-label': g.ordered ? `${g.name}, in release order` : `${g.name}: still to watch, then seen` },
      g.films.map((f) => tile(f, caption)));
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
        g.reason ? h('span', { class: 'ctv__why', text: g.reason, title: g.reason, 'aria-label': g.reason }) : null),
      caption,
      row),
    h('p', { class: 'ctv__hint', 'aria-hidden': 'true' },
      after ? [icon('chevron'), `Next: ${after.name}`] : null));
  }

  // At most DOTS dots: a window around the current saga, its outer two shrinking when more lie beyond.
  function paintDots() {
    const n = groups.length;
    const start = Math.max(0, Math.min(index - Math.floor(DOTS / 2), n - DOTS));
    const end = Math.min(n, start + DOTS);
    const cls = (i) => {
      if (i === index) return 'is-current';
      if ((i === start && start > 0) || (i === end - 1 && end < n)) return 'is-edge';
      if ((i === start + 1 && start > 0) || (i === end - 2 && end < n)) return 'is-near-edge';
      return null;
    };
    fill(dots, groups.slice(start, end).map((g, k) => h('li', { class: cls(start + k) })));
    if (prevBtn) { nav.hidden = groups.length < 2; prevBtn.disabled = index <= 0; nextBtn.disabled = index >= groups.length - 1; }
  }

  // Next (1) or previous (-1) saga, if there is one.
  function step(dir) {
    const to = index + dir;
    if (!screen || to < 0 || to >= groups.length) return false;
    show(to, dir);
    return true;
  }

  // Show saga `i`; `dir` is 1 (down, next), -1 (up, previous) or 0 (first paint, no slide). `focus`: give the
  // film focus (not when a chip was just picked).
  function show(i, dir = 0, focusKey = null, focus = true) {
    const g = groups[i];
    if (!g) return;
    index = i;
    const next = build(g, i);
    const old = screen;
    screen = next;
    if (old && dir && !reduced()) {
      old.inert = true; // leaving: out of reach of focus, clicks and the scroll of its row
      old.classList.add(dir > 0 ? 'is-leaving-up' : 'is-leaving-down');
      next.classList.add(dir > 0 ? 'is-entering-up' : 'is-entering-down');
      stage.append(next);
      const done = () => { if (old.isConnected) old.remove(); };
      old.addEventListener('animationend', done, { once: true });
      setTimeout(done, 700);
      // only the leaving screen and the new one: drop any older one still fading out after fast steps
      for (const s of [...stage.children]) if (s !== old && s !== next) s.remove();
    } else {
      fill(stage, next);
    }
    paintDots();
    el.scrollTop = 0; // never left scrolled (overflow: clip in CSS; this covers engines without it)
    const tiles = [...next.querySelectorAll('.ctv__film')];
    const start = focusKey != null ? g.films.findIndex((f) => f.tmdb_id === focusKey) : g.films.indexOf(nextFilm(g));
    if (focus) tiles[Math.max(0, start)]?.focus({ preventScroll: true });
    else lastFilm = g.films[Math.max(0, start)]?.tmdb_id ?? null;
    announce(`${g.name}. ${g.seen} of ${g.total} seen.${g.reason ? ` ${g.reason}.` : ''}`);
  }

  // In a browser the keys also work when focus is nowhere in particular (the page, the hero itself, or the arrows
  // beside the dots for the arrow keys), so a click on the backdrop does not strand them; never in a field, the
  // layout toggle, the tabs or an open dialog.
  function webTarget(e) {
    if (!web || !screen || isTyping(e)) return null;
    const a = document.activeElement;
    const idle = !a || a === document.body || a.id === 'main' || a === el;
    const onNav = Boolean(a?.closest?.('.ctv__nav')) && e.key !== 'Enter';
    if (!idle && !onNav) return null;
    const tiles = [...screen.querySelectorAll('.ctv__film')];
    const i = groups[index]?.films.findIndex((f) => f.tmdb_id === lastFilm) ?? -1;
    return tiles[i] || tiles[groups[index]?.films.indexOf(nextFilm(groups[index]))] || tiles[0] || null;
  }

  // The film to go back to from the chips: the one focused last, else the next one to watch.
  function currentTile() {
    if (!screen) return null;
    const tiles = [...screen.querySelectorAll('.ctv__film')];
    const g = groups[index];
    const i = g?.films.findIndex((f) => f.tmdb_id === lastFilm) ?? -1;
    return tiles[i] || tiles[g?.films.indexOf(nextFilm(g))] || tiles[0] || null;
  }

  // On a chip: Left/Right walk the chips, Down returns to the films; OK (Enter) is the button's own click and Up is
  // left to tvnav.js (the tabs).
  function chipKey(e, chip) {
    const shown = shownChips();
    const at = shown.indexOf(chip);
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      shown[at + (e.key === 'ArrowRight' ? 1 : -1)]?.focus({ preventScroll: true });
    } else if (e.key === 'ArrowDown') {
      const t = currentTile();
      if (t) { e.preventDefault(); t.focus({ preventScroll: true }); }
    }
  }

  function onKey(e) {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || document.querySelector('dialog[open]')) return;
    const chip = document.activeElement?.closest?.('.ctv__kinds .chip');
    if (chip && kinds.contains(chip)) { chipKey(e, chip); return; }
    let film = document.activeElement?.closest?.('.ctv__film');
    if (!film || !screen?.contains(film)) {
      if (!ARROWS.has(e.key) && e.key !== 'Enter') return;
      film = webTarget(e);
      if (!film) return;
      if (e.key === 'Enter') { e.preventDefault(); film.click(); return; }
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); film.focus({ preventScroll: true }); return; }
    }
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
    } else if (e.key === 'ArrowUp') {
      // Up on the first group: the kind chips (on the current one); Up again leaves for the tabs (tvnav.js)
      const on = chips.find((c) => c.getAttribute('aria-pressed') === 'true' && !c.hidden) || shownChips()[0];
      if (on) { e.preventDefault(); on.focus({ preventScroll: true }); }
    }
  }
  document.addEventListener('keydown', onKey);

  // Wheel and trackpad (browser only): one saga per gesture, up and down alike, wherever focus is. Listened to on
  // the window while mounted and taken whenever the pointer is over the layout (only an open dialog stops it), so
  // it never depends on what was clicked last. A gesture ends after a short quiet spell, or when the deltas grow
  // again after the momentum tail of a trackpad flick died down; mostly horizontal scrolls are left to the row.
  let wheelAcc = 0;
  let wheelLocked = 0; // time the current gesture changed saga; 0 = not locked
  let wheelLast = 0; // |delta| of the previous event
  let wheelQuiet = 0;
  function overLayout(e) {
    if (e.target instanceof Node && el.contains(e.target)) return true;
    const r = el.getBoundingClientRect();
    return e.clientX >= r.left && e.clientX < r.right && e.clientY >= r.top && e.clientY < r.bottom;
  }
  function onWheel(e) {
    if (!alive || e.ctrlKey || !overLayout(e) || document.querySelector('dialog[open]')) return;
    const dy = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1);
    if (Math.abs(e.deltaX) > Math.abs(dy)) {
      // sideways: the poster row scrolls; anywhere else it must not scroll the page or swipe back in history
      if (!(e.target instanceof Element && e.target.closest('.ctv__screen:not([inert]) .ctv__row'))) e.preventDefault();
      return;
    }
    e.preventDefault();
    const size = Math.abs(dy);
    clearTimeout(wheelQuiet);
    wheelQuiet = setTimeout(() => { wheelLocked = 0; wheelAcc = 0; wheelLast = 0; }, 240);
    const fresh = wheelLocked && performance.now() - wheelLocked > 350 && size > wheelLast * 1.5 + 4;
    wheelLast = size;
    if (wheelLocked && !fresh) return;
    if (fresh) { wheelLocked = 0; wheelAcc = 0; }
    wheelAcc += dy;
    if (Math.abs(wheelAcc) < 24) return;
    wheelLocked = performance.now();
    step(wheelAcc > 0 ? 1 : -1);
    wheelAcc = 0;
  }
  // Touch (phones): a mostly vertical swipe changes saga; sideways swipes scroll the poster row.
  let touch = null;
  const onTouchStart = (e) => { touch = e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null; };
  const onTouchEnd = (e) => {
    if (!touch || !e.changedTouches.length) return;
    const dx = e.changedTouches[0].clientX - touch.x;
    const dy = e.changedTouches[0].clientY - touch.y;
    touch = null;
    if (Math.abs(dy) > 50 && Math.abs(dy) > Math.abs(dx) * 1.5) step(dy < 0 ? 1 : -1);
  };
  if (web) {
    window.addEventListener('wheel', onWheel, { passive: false });
    el.addEventListener('touchstart', onTouchStart, { passive: true });
    el.addEventListener('touchend', onTouchEnd, { passive: true });
  }

  async function load() {
    const focusKey = document.activeElement?.closest?.('.ctv') || document.querySelector('dialog[open]') ? lastFilm : null;
    const want = target;
    target = null;
    const key = want || groups[index]?.key;
    if (!groups.length) fill(stage, h('div', { class: 'ctv__screen ctv__screen--skeleton pick pick--skeleton', 'aria-busy': 'true' }, h('div', { class: 'pick__art skel' })));
    try {
      const r = await api.completions({});
      if (!alive) return;
      everything = r.groups;
      counts = {};
      for (const g of everything) counts[chipKind(g.kind)] = (counts[chipKind(g.kind)] || 0) + 1;
      if (kind && !counts[kind]) { kind = null; storeKind(null); } // a remembered kind with nothing left: All
      if (want && kind && kind !== kindOfKey(want) && everything.some((g) => g.key === want)) kind = null; // not stored
      groups = everything.filter(ofKind);
      paintChips();
      if (!groups.length) {
        if (!everything.length) {
          fill(dots);
          screen = null;
          if (prevBtn) nav.hidden = true;
          fill(stage, h('div', { class: 'state ctv__empty' },
            h('p', { class: 'state__title', text: r.completed.length || r.hidden_count ? 'All caught up' : 'Nothing started yet' }),
            h('p', { class: 'state__text', text: 'Watch one film of a saga, franchise or trilogy, or a few by the same director or actor, and it shows up here.' })));
        } else empty();
        return;
      }
      const keep = groups.findIndex((g) => g.key === key);
      show(keep >= 0 ? keep : 0, 0, keep >= 0 && !want ? focusKey : null);
    } catch (err) {
      if (alive) fill(stage, errorBox(err, load));
    }
  }

  // A film marked (or unmarked) seen in the panel changes the counts: refresh, staying on the same saga and film.
  const onSeen = () => load();
  window.addEventListener('movienight:marked', onSeen);
  // Closing the panel (Back) gives focus back to the film it was opened from, even after a refresh.
  const dialog = document.getElementById('detail');
  const onClose = () => {
    if (!alive || !screen) return;
    const i = groups[index]?.films.findIndex((f) => f.tmdb_id === lastFilm) ?? -1;
    const tile = screen.querySelectorAll('.ctv__film')[i];
    if (tile) setTimeout(() => tile.focus({ preventScroll: true }), 0);
  };
  dialog?.addEventListener('close', onClose);
  paintChips();
  load();

  return {
    update(route) {
      if (!route?.group || route.group === shownTarget) return;
      shownTarget = target = route.group;
      load();
    },
    destroy() {
      alive = false;
      clearTimeout(wheelQuiet);
      window.removeEventListener('wheel', onWheel);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('movienight:marked', onSeen);
      dialog?.removeEventListener('close', onClose);
    },
  };
}
