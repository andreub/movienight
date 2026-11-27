import { api } from '../api.js';
import { h, fill, icon, poster, genPoster, hueOf, runtime, matchStub, reasonList, errorBox, announce, toast, isTyping } from '../dom.js';
import { filterBar } from '../filters.js';
import { playControl } from '../play.js';
import { go, apiFilters, hasQuickFilters, setFilters, CLEAR_QUICK } from '../state.js';
import { endsAt } from '../clock.js';
import { initialRuntime, rememberRuntime } from '../timecontrol.js';
import { voteButtons, setVote, voteOf } from '../vote.js';
import { setSeen, setSeenVote, isSeen } from '../seen.js';
import { partOf } from './detail.js';
import { serverName } from '../status.js';
import { seenMsg, seenAskMsg, seenVoteMsg, unseenMsg, voteMsg, hideMsg } from '../messages.js';
import { t } from '../i18n.js';

const HINT_KEY = 'movienight.seenHintDismissed';
const HINT_BELOW = 20;

function hintDismissed() {
  try { return localStorage.getItem(HINT_KEY) === '1'; } catch { return false; }
}

export const title = t('tab.tonight');

export function mount(root, route) {
  const bar = filterBar({ time: true, saga: true });
  const stage = h('div', { class: 'tonight__stage' });
  const hint = h('div', { class: 'tonight__hint' });
  root.append(
    h('h1', { class: 'sr-only', text: t('tonight.h1') }),
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
      fill(hint, h('div', { class: 'note note--hint', role: 'region', 'aria-label': t('tonight.tip') },
        icon('eye'),
        h('span', { class: 'note__text', text: t('tonight.hint') }),
        h('a', { class: 'btn btn--ghost btn--sm', href: '#/seen' }, t('seen.title')),
        h('button', {
          type: 'button', class: 'iconbtn note__close', 'aria-label': t('tonight.tipDismiss'),
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
      bar.sagaCount(res.saga_count);
      seen = [...new Set([...seen, res.pick.id])].slice(-60);
      render(mode);
    } catch (err) {
      if (token !== req) return;
      if (err.status === 404 && seen.length) {
        toast(t('tonight.startOver'));
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
    announce(t('tonight.announce', { title: pick.title, n: Math.round(pick.match ?? 0) }));
  }

  function hero(m, mode) {
    const art = m.backdrop_url
      ? h('img', { class: 'pick__backdrop', src: m.backdrop_url, alt: '', decoding: 'async' })
      : h('div', { class: 'pick__backdrop pick__backdrop--gen', 'aria-hidden': 'true' }, h('span', { text: m.title }));
    if (art.tagName === 'IMG') art.addEventListener('error', () => art.replaceWith(h('div', { class: 'pick__backdrop pick__backdrop--gen', 'aria-hidden': 'true' })), { once: true });

    const shuffleBtn = h('button', { type: 'button', class: 'btn btn--ghost', onclick: shuffle, 'aria-keyshortcuts': 'S' },
      icon('shuffle'), t('tonight.shuffle'));
    const seenBtn = h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => { seenBtn.disabled = true; markSeen(m).finally(() => { seenBtn.disabled = false; }); } },
      icon('eye'), t('common.seenIt'));
    return h('article', { class: `pick ${mode === 'shuffle' ? 'is-shuffled' : ''}`, style: { '--h': String(hueOf(m)) }, 'aria-labelledby': 'pick-title' },
      h('div', { class: 'pick__art' }, art),
      h('div', { class: 'pick__inner' },
        poster(m, { label: t('poster.aria', { title: m.title }), eager: true, cls: 'pick__poster' }),
        h('div', { class: 'pick__info' },
          h('div', { class: 'pick__head' },
            h('h2', { class: 'pick__title', id: 'pick-title', text: m.title }),
            matchStub(m.match)),
          meta(m),
          // "Part of X · n of N" for a film in a group (the saga chip's "Next in X" reason says the same: one line only)
          partOf(m.groups?.length ? [m.groups.find((g) => g.key === m.saga?.key) || m.groups[0]] : null, 'pick__part'),
          reasonList(m.groups?.length && m.saga ? m.reasons.filter((r) => !(r.kind === 'trophy' && r.text.startsWith('Next in '))) : m.reasons, 'pick__reasons'),
          h('div', { class: 'pick__actions' },
            playControl(m, 'pick__play'),
            shuffleBtn,
            h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => notTonight(m) }, icon('eyeoff'), t('tonight.notTonight')),
            h('div', { class: 'pick__tail' },
              h('button', { type: 'button', class: 'btn btn--quiet', onclick: () => go({ movie: m.id }) }, icon('info'), t('tonight.details')),
              seenBtn,
              voteButtons(m, { save: voteAndSeen }))))));
  }

  function meta(m) {
    return h('p', { class: 'meta' },
      m.year ? h('span', { text: String(m.year) }) : null,
      runtime(m.runtime) ? h('span', { text: runtime(m.runtime) }) : null,
      endsAt(m.runtime),
      m.genres?.length ? h('span', { text: m.genres.slice(0, 3).join(', ') }) : null,
      m.on_watchlist ? h('span', { class: 'meta__flag' }, icon('watchlist'), t('film.onWatchlist')) : null);
  }

  function alternativesList(list) {
    if (!list.length) return null;
    return h('section', { class: 'alts', 'aria-labelledby': 'alts-title' },
      h('h2', { class: 'alts__title', id: 'alts-title', text: t('tonight.alts') }),
      h('ul', { class: 'alts__list' }, list.map((m) => h('li', null,
        // A card, not a button: its "Part of" line is a link of its own. The title's button stretches over the card.
        h('div', { class: 'alt' },
          poster(m, { cls: 'alt__poster' }),
          h('span', { class: 'alt__body' },
            h('button', { type: 'button', class: 'alt__title', onclick: () => go({ movie: m.id }), text: m.title }),
            h('span', { class: 'alt__meta' }, [m.year, runtime(m.runtime)].filter(Boolean).join(', '), m.runtime ? ', ' : '', endsAt(m.runtime, { prefix: t('clock.ends') })),
            altWhy(m),
            altPart(m)),
          matchStub(m.match, { size: 'sm' }))))));
  }

  // The first reason; the saga's "Next in X" says what the "Part of" line below does, so it gives way (as on the hero).
  function altWhy(m) {
    const r = (m.groups?.length && m.saga ? m.reasons.filter((x) => !(x.kind === 'trophy' && x.text.startsWith('Next in '))) : m.reasons)?.[0];
    return r ? h('span', { class: 'alt__why', text: r.text }) : null;
  }

  // "Part of X · n of N" under a card, one line; the same saga the hero names. Opens it on Complete, not the card.
  function altPart(m) {
    const g = m.groups?.length ? m.groups.find((x) => x.key === m.saga?.key) || m.groups[0] : null;
    const el = partOf(g ? [g] : null, 'alt__part');
    el?.addEventListener('click', (e) => e.stopPropagation());
    return el;
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
    toast(hideMsg(m), {
      action: {
        label: t('common.undo'),
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

  // A vote also marks an unseen film as seen (one request); the seen flow then brings up the next pick.
  // Clearing a vote, or voting on a film already seen, only stores the vote.
  function voteAndSeen(m, vote) {
    if (vote !== 0 && !isSeen(m) && !m.watched) return setSeenVote(m, true, vote, 'hero');
    return setVote(m, vote);
  }

  function seenToast(m, replacementId, undo, { vote, previousVote } = {}) {
    const quick = (close) => h('div', { class: 'toast__votes', role: 'group', 'aria-label': t('msg.likedAsk') },
      [[1, 'thumbup', t('toast.liked')], [2, 'heart', t('vote.love')], [-1, 'thumbdown', t('toast.disliked')]].map(([vote, name, label]) =>
        h('button', {
          type: 'button', class: 'toast__vote', title: label, 'aria-label': label,
          onclick: async () => {
            if (!(await setVote(m, vote))) return;
            close();
            toast(voteMsg(m, vote), { timeout: 3000 });
          },
        }, icon(name))));
    // Already rated (e.g. from the sheet): no need to ask again.
    const voted = vote !== undefined && vote !== 0;
    const rated = voted || voteOf(m) !== 0;
    toast(voted ? seenVoteMsg(m, vote) : rated ? seenMsg(m) : seenAskMsg(m), {
      timeout: rated ? 6000 : 8000,
      extra: rated ? null : quick,
      action: {
        label: t('common.undo'),
        run: async () => {
          // Restore both the seen mark and the vote it was saved with, whatever it was before (possibly none).
          const ok = previousVote === undefined ? await setSeen(m, false, 'undo') : await setSeenVote(m, false, previousVote, 'undo');
          if (!ok) return;
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
    const { movie, seen: nowSeen, was, vote, previousVote } = e.detail;
    const restore = { vote, previousVote };
    if (!data || !nowSeen || was || e.detail.source === 'undo') return;
    if (movie.id === data.pick.id) {
      const previous = data;
      load('shuffle').then(() => {
        const next = data?.pick?.id;
        seenToast(movie, next, () => {
          if (data && data.pick.id === next) { req++; data = previous; render('shuffle'); } else toast(unseenMsg(movie));
        }, restore);
      });
      return;
    }
    const idx = data.alternatives.findIndex((a) => a.id === movie.id);
    if (idx < 0) return;
    replaceAlt(movie, idx, restore);
  }

  // Only the marked slot changes: ask for one film that is not on screen and not shown this session.
  async function replaceAlt(movie, idx, restore) {
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
      else { toast(unseenMsg(movie)); return; }
      renderAlts();
    }, restore);
  }

  function empty() {
    const filtered = hasQuickFilters(filters) || filters.saga;
    return h('div', { class: 'state' },
      h('div', { class: 'state__art', 'aria-hidden': 'true' }, genPoster({ title: t('tonight.nothingPlaying'), year: null })),
      h('p', { class: 'state__title', text: filtered ? t('tonight.empty.filtered') : t('tonight.empty.title') }),
      h('p', { class: 'state__text', text: filtered
        ? t('tonight.empty.filteredText')
        : t('tonight.empty.text', { server: serverName() }) }),
      filtered
        ? h('button', { type: 'button', class: 'btn btn--primary', onclick: () => { rememberRuntime(null); setFilters(CLEAR_QUICK); } }, t('common.clearFilters'))
        : h('a', { class: 'btn btn--primary', href: '#/setup' }, t('common.openSetup')));
  }

  function skeleton() {
    return h('div', { class: 'pick pick--skeleton', 'aria-busy': 'true', 'aria-label': t('tonight.finding') },
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
