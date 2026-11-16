import { api } from '../api.js';
import { h, fill, icon, poster, genPoster, hueOf, runtime, matchStub, reasonList, errorBox, announce, toast, isTyping } from '../dom.js';
import { filterBar } from '../filters.js';
import { playControl } from '../play.js';
import { go, apiFilters, hasQuickFilters, setFilters } from '../state.js';
import { endsAt } from '../clock.js';
import { initialRuntime, rememberRuntime } from '../timecontrol.js';
import { voteButtons, setVote } from '../vote.js';
import { setSeen } from '../seen.js';

const HINT_KEY = 'movienight.seenHintDismissed';
const HINT_BELOW = 20;

function hintDismissed() {
  try { return localStorage.getItem(HINT_KEY) === '1'; } catch { return false; }
}

export const title = 'Tonight';

export function mount(root, route) {
  const bar = filterBar({ time: true });
  const stage = h('div', { class: 'tonight__stage' });
  const hint = h('div', { class: 'tonight__hint' });
  root.append(
    h('h1', { class: 'sr-only', text: "Tonight's pick" }),
    h('div', { class: 'tonight' }, hint, stage, h('div', { class: 'tonight__filters' }, bar.el)),
  );

  let filters = null;
  let filterKey = '';
  let data = null;
  let seen = [];
  let req = 0;
  let alive = true;
  // A smart default (or the remembered choice) is applied right after mount; hold the first load until it lands.
  const startRt = initialRuntime(route.filters);
  let holdLoad = startRt != null;
  if (holdLoad) setTimeout(() => { if (alive) setFilters({ rt: startRt }); }, 0);

  // Nudge towards the triage screen until the user has marked a few films, unless dismissed.
  if (!hintDismissed()) {
    api.profile().then((p) => {
      if (!alive || hintDismissed() || (p?.stats?.seen_here ?? 0) >= HINT_BELOW) return;
      fill(hint, h('div', { class: 'note note--hint', role: 'region', 'aria-label': 'Tip' },
        icon('eye'),
        h('span', { class: 'note__text', text: 'Your history starts in 2021, so classics you saw long ago can show up. Mark the ones you have seen and they stop coming back.' }),
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/seen' }, 'Seen these?'),
        h('button', {
          type: 'button', class: 'iconbtn note__close', 'aria-label': 'Dismiss tip',
          onclick: () => { try { localStorage.setItem(HINT_KEY, '1'); } catch { /* private mode */ } fill(hint); },
        }, icon('close'))));
    }).catch(() => {});
  }

  async function load(mode) {
    const token = ++req;
    if (mode === 'fresh') seen = [];
    if (data) stage.classList.add('is-busy');
    else fill(stage, skeleton());
    try {
      const res = await api.pick({ ...apiFilters(filters), exclude: seen });
      if (token !== req) return;
      data = res;
      seen = [...new Set([...seen, res.pick.id])].slice(-60);
      render(mode);
    } catch (err) {
      if (token !== req) return;
      if (err.status === 404 && seen.length) {
        toast("You've seen every match for these filters, so we're starting over.");
        load('fresh');
        return;
      }
      data = null;
      stage.classList.remove('is-busy');
      fill(stage, err.status === 404 ? empty() : errorBox(err, () => load('fresh')));
    }
  }

  function render(mode) {
    const { pick, alternatives = [] } = data;
    stage.classList.remove('is-busy');
    fill(stage, hero(pick, mode), alternativesList(alternatives));
    if (mode !== 'fresh' && stage.getBoundingClientRect().top < 0) {
      const smooth = !matchMedia('(prefers-reduced-motion: reduce)').matches;
      stage.scrollIntoView({ block: 'start', behavior: smooth ? 'smooth' : 'auto' });
    }
    announce(`Tonight's pick: ${pick.title}, ${Math.round(pick.match ?? 0)}% match.`);
  }

  function hero(m, mode) {
    const art = m.backdrop_url
      ? h('img', { class: 'pick__backdrop', src: m.backdrop_url, alt: '', decoding: 'async' })
      : h('div', { class: 'pick__backdrop pick__backdrop--gen', 'aria-hidden': 'true' }, h('span', { text: m.title }));
    if (art.tagName === 'IMG') art.addEventListener('error', () => art.replaceWith(h('div', { class: 'pick__backdrop pick__backdrop--gen', 'aria-hidden': 'true' })), { once: true });

    const shuffleBtn = h('button', { type: 'button', class: 'btn btn--ghost', onclick: shuffle, 'aria-keyshortcuts': 'S' },
      icon('shuffle'), 'Shuffle');
    const seenBtn = h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => { seenBtn.disabled = true; markSeen(m).finally(() => { seenBtn.disabled = false; }); } },
      icon('eye'), 'Seen it');
    return h('article', { class: `pick ${mode === 'shuffle' ? 'is-shuffled' : ''}`, style: { '--h': String(hueOf(m)) }, 'aria-labelledby': 'pick-title' },
      h('div', { class: 'pick__art' }, art),
      h('div', { class: 'pick__inner' },
        poster(m, { label: `Poster for ${m.title}`, eager: true, cls: 'pick__poster' }),
        h('div', { class: 'pick__info' },
          h('div', { class: 'pick__head' },
            h('h2', { class: 'pick__title', id: 'pick-title', text: m.title }),
            matchStub(m.match)),
          meta(m),
          reasonList(m.reasons, 'pick__reasons'),
          h('div', { class: 'pick__actions' },
            playControl(m, 'pick__play'),
            shuffleBtn,
            seenBtn,
            h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => notTonight(m) }, icon('eyeoff'), 'Not tonight'),
            h('div', { class: 'pick__tail' },
              h('button', { type: 'button', class: 'btn btn--quiet', onclick: () => go({ movie: m.id }) }, icon('info'), 'Details'),
              voteButtons(m, { onVote: (vote, done) => { if (vote === -1) lessLikeThis(m, done); } }))))));
  }

  function meta(m) {
    return h('p', { class: 'meta' },
      m.year ? h('span', { text: String(m.year) }) : null,
      runtime(m.runtime) ? h('span', { text: runtime(m.runtime) }) : null,
      endsAt(m.runtime),
      m.genres?.length ? h('span', { text: m.genres.slice(0, 3).join(', ') }) : null,
      m.on_watchlist ? h('span', { class: 'meta__flag' }, icon('watchlist'), 'On your watchlist') : null);
  }

  function alternativesList(list) {
    if (!list.length) return null;
    return h('section', { class: 'alts', 'aria-labelledby': 'alts-title' },
      h('h2', { class: 'alts__title', id: 'alts-title', text: 'Also good tonight' }),
      h('ul', { class: 'alts__list' }, list.map((m) => h('li', null,
        h('button', { type: 'button', class: 'alt', onclick: () => go({ movie: m.id }) },
          poster(m, { cls: 'alt__poster' }),
          h('span', { class: 'alt__body' },
            h('span', { class: 'alt__title', text: m.title }),
            h('span', { class: 'alt__meta' }, [m.year, runtime(m.runtime)].filter(Boolean).join(', '), m.runtime ? ', ' : '', endsAt(m.runtime, { prefix: 'ends ' })),
            m.reasons?.[0] ? h('span', { class: 'alt__why', text: m.reasons[0].text }) : null),
          matchStub(m.match, { size: 'sm' }))))));
  }

  function shuffle() {
    if (!data) return load('fresh');
    load('shuffle');
  }

  async function notTonight(m) {
    try {
      await api.dismiss(m.id, 30);
    } catch (err) {
      toast(err.detail);
      return;
    }
    const previous = data;
    toast(`Hid “${m.title}” for 30 days.`, {
      action: {
        label: 'Undo',
        run: async () => {
          try {
            await api.undismiss(m.id);
            req++;
            data = previous;
            render('shuffle');
          } catch (err) { toast(err.detail); }
        },
      },
    });
    load('shuffle');
  }

  // "Seen it" (hero, sheet or card): the film leaves the page and a new one takes its slot (see onSeenSaved).
  async function markSeen(m) {
    await setSeen(m, true, 'hero');
  }

  function seenToast(m, replacementId, undo) {
    const quick = (close) => h('div', { class: 'toast__votes', role: 'group', 'aria-label': 'Did you like it?' },
      [[1, 'thumbup', 'I liked it'], [2, 'heart', 'A favourite'], [-1, 'thumbdown', "I didn't like it"]].map(([vote, name, label]) =>
        h('button', {
          type: 'button', class: 'toast__vote', title: label, 'aria-label': label,
          onclick: async () => {
            if (!(await setVote(m, vote))) return;
            close();
            toast(vote > 1 ? 'Noted: a favourite.' : vote > 0 ? 'Noted: more like this.' : 'Noted: less like this.', { timeout: 3000 });
          },
        }, icon(name))));
    toast(`Marked “${m.title}” as seen. Did you like it?`, {
      timeout: 8000,
      extra: quick,
      action: {
        label: 'Undo',
        run: async () => {
          if (!(await setSeen(m, false, 'undo'))) return;
          undo();
        },
      },
    });
  }

  function renderAlts() {
    const old = stage.querySelector('.alts');
    const next = alternativesList(data.alternatives || []);
    if (old && next) old.replaceWith(next);
    else if (old) old.remove();
    else if (next) stage.append(next);
  }

  function onSeenSaved(e) {
    const { movie, seen: nowSeen, was } = e.detail;
    if (!data || !nowSeen || was || e.detail.source === 'undo') return;
    if (movie.id === data.pick.id) {
      const previous = data;
      load('shuffle').then(() => {
        const next = data?.pick?.id;
        seenToast(movie, next, () => {
          if (data && data.pick.id === next) { req++; data = previous; render('shuffle'); } else toast('Unmarked. It can come back in your picks.');
        });
      });
      return;
    }
    const idx = data.alternatives.findIndex((a) => a.id === movie.id);
    if (idx < 0) return;
    replaceAlt(movie, idx);
  }

  // Only the marked slot changes: ask for one film that is not on screen and not shown this session.
  async function replaceAlt(movie, idx) {
    const token = req;
    const dataAtStart = data;
    const onScreen = [dataAtStart.pick.id, ...dataAtStart.alternatives.map((a) => a.id)];
    let repl = null;
    try {
      const res = await api.pick({ ...apiFilters(filters), exclude: [...new Set([...seen, ...onScreen])] });
      repl = res.pick;
    } catch { /* no replacement: the slot just closes */ }
    if (token !== req || data !== dataAtStart) return;
    const at = data.alternatives.findIndex((a) => a.id === movie.id);
    if (at < 0) return;
    if (repl && !onScreen.includes(repl.id)) {
      data.alternatives[at] = repl;
      seen = [...new Set([...seen, repl.id])].slice(-60);
    } else {
      repl = null;
      data.alternatives.splice(at, 1);
    }
    renderAlts();
    const replId = repl?.id;
    seenToast(movie, replId, () => {
      if (!data || data.pick.id === movie.id) return;
      const cur = replId ? data.alternatives.findIndex((a) => a.id === replId) : -1;
      if (cur >= 0) data.alternatives[cur] = movie;
      else if (!replId && data.alternatives.length < 6) data.alternatives.splice(Math.min(at, data.alternatives.length), 0, movie);
      else { toast('Unmarked. It can come back in your picks.'); return; }
      renderAlts();
    });
  }

  // A thumbs-down is feedback, not a hide: the film ranks lower and the next pick comes up.
  async function lessLikeThis(m, done) {
    if (!(await done)) return;
    const previous = data;
    toast(`Noted. “${m.title}” will rank lower.`, {
      action: {
        label: 'Undo',
        run: async () => {
          if (!(await setVote(m, 0))) return;
          req++;
          data = previous;
          render('shuffle');
        },
      },
    });
    load('shuffle');
  }

  function empty() {
    const filtered = hasQuickFilters(filters);
    return h('div', { class: 'state' },
      h('div', { class: 'state__art', 'aria-hidden': 'true' }, genPoster({ title: 'Nothing playing', year: null })),
      h('p', { class: 'state__title', text: filtered ? 'Nothing matches these filters' : 'No picks yet' }),
      h('p', { class: 'state__text', text: filtered
        ? 'Every film that fits is watched or hidden. Loosen a filter to see more.'
        : 'Your Plex library may still be syncing. Check progress in Setup.' }),
      filtered
        ? h('button', { type: 'button', class: 'btn btn--primary', onclick: () => { rememberRuntime(null); setFilters({ mood: null, rt: null, wl: false }); } }, 'Clear filters')
        : h('a', { class: 'btn btn--primary', href: '#/setup' }, 'Open Setup'));
  }

  function skeleton() {
    return h('div', { class: 'pick pick--skeleton', 'aria-busy': 'true', 'aria-label': 'Finding a film' },
      h('div', { class: 'pick__art skel' }),
      h('div', { class: 'pick__inner' },
        h('div', { class: 'pick__poster skel' }),
        h('div', { class: 'pick__info' },
          h('div', { class: 'skel skel--title' }), h('div', { class: 'skel skel--line' }), h('div', { class: 'skel skel--line skel--short' }))));
  }

  function onKey(e) {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || isTyping(e) || document.querySelector('dialog[open]')) return;
    const arrow = e.key === 'ArrowRight' && (e.target === document.body || e.target === root);
    if (e.key === 's' || e.key === 'S' || arrow) { e.preventDefault(); shuffle(); }
  }
  document.addEventListener('keydown', onKey);

  function update(r) {
    filters = r.filters;
    bar.sync(filters);
    if (holdLoad) {
      if (filters.rt == null) { if (!data) fill(stage, skeleton()); return; }
      holdLoad = false;
    }
    const key = JSON.stringify(apiFilters(filters));
    if (key !== filterKey) { filterKey = key; load('fresh'); }
  }
  update(route);

  window.addEventListener('movienight:changed', onChanged);
  window.addEventListener('movienight:seen-saved', onSeenSaved);
  function onChanged(e) {
    if (data && e.detail.id === data.pick.id && e.detail.dismissed) load('shuffle');
  }

  return {
    update,
    destroy() {
      alive = false;
      req++;
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('movienight:changed', onChanged);
      window.removeEventListener('movienight:seen-saved', onSeenSaved);
    },
  };
}
