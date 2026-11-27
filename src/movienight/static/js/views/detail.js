// Film detail: bottom sheet on phones, side panel on wider screens (one <dialog>). Library films by id (#movie=),
// and any film by TMDB id (#film=, Complete's saga films, in the library or not) in the same sheet.
import { api } from '../api.js';
import { h, icon, poster, hueOf, runtime, stars, matchStub, reasonList, errorBox, toast, num, fixed } from '../dom.js';
import { playControl } from '../play.js';
import { voteButtons, setVote, voteOf, LOVE } from '../vote.js';
import { setSeen, setSeenVote, isSeen, rememberSeen, OUTCOMES } from '../seen.js';
import { parse, href, showGroup } from '../state.js';
import { serverName } from '../status.js';
import { TV } from '../tvnav.js';
import { seerrEnabled, seerrState, requestFilm, stateChip, CHIP } from '../seerr.js';
import { seenMsg, seenVoteMsg, unseenMsg, voteMsg, hideMsg, neverMsg, unhideMsg, UNMARK_LABEL } from '../messages.js';
import { t, lang } from '../i18n.js';
import { genreList, becauseText } from '../labels.js';

const dialog = document.getElementById('detail');
const body = dialog.querySelector('.sheet__body');
let currentId = null;
let req = 0;
let onRequestClose = () => {};

export function initDetail(requestClose) {
  onRequestClose = requestClose;
  dialog.addEventListener('cancel', (e) => { e.preventDefault(); onRequestClose(); });
  dialog.addEventListener('click', (e) => { if (e.target === dialog) onRequestClose(); });
}

export function openDetail(id) {
  open(id);
}

// A film by TMDB id, in the library or not (Complete's saga films): the same sheet, see renderFilm.
export function openFilm(tmdbId) {
  open(`tmdb:${tmdbId}`);
}

function open(id) {
  if (currentId === id && dialog.open) return;
  currentId = id;
  if (!dialog.open) dialog.showModal();
  load();
}

export function closeDetail() {
  if (!dialog.open) return;
  currentId = null;
  req++;
  dialog.close();
}

const isFilm = (id) => typeof id === 'string' && id.startsWith('tmdb:');

// `focus`: a selector to focus after a reload (the control the user just pressed), else the default.
async function load({ quiet = false, focus = null } = {}) {
  const token = ++req;
  const id = currentId;
  if (!quiet) body.replaceChildren(skeleton());
  try {
    const m = isFilm(id) ? await loadFilm(id.slice(5)) : await api.movie(id);
    if (token !== req) return;
    const top = body.scrollTop;
    body.replaceChildren(isFilm(id) ? renderFilm(m) : render(m));
    body.scrollTop = quiet ? top : 0;
    const target = (focus && body.querySelector(focus))
      || (TV && isFilm(id) && (body.querySelector('.votes__btn[aria-pressed="true"]') || body.querySelector('.votes__btn')))
      || body.querySelector('.sheet__close');
    target?.focus({ preventScroll: quiet });
  } catch (err) {
    if (token !== req) return;
    body.replaceChildren(closeButton(), errorBox(err, () => load()));
  }
}

function closeButton() {
  return h('button', { type: 'button', class: 'sheet__close iconbtn', 'aria-label': t('detail.close'), onclick: () => onRequestClose() }, icon('close'));
}

function hiddenUntil(m) {
  if (!m.dismissed_until) return null;
  const d = new Date(m.dismissed_until);
  if (Number.isNaN(d.getTime()) || d < new Date()) return null;
  return d.getFullYear() >= 9000 ? t('detail.hiddenForGood') : t('detail.hiddenUntil', { date: d.toLocaleDateString(lang, { day: 'numeric', month: 'short' }) });
}

async function setDismissed(m, days) {
  try {
    if (days === null) await api.undismiss(m.id);
    else await api.dismiss(m.id, days);
  } catch (err) {
    toast(err.detail);
    return;
  }
  window.dispatchEvent(new CustomEvent('movienight:changed', { detail: { id: m.id, dismissed: days !== null } }));
  if (days === null) toast(unhideMsg(m));
  else toast(days === 0 ? neverMsg(m) : hideMsg(m), { action: { label: t('common.undo'), run: () => setDismissed(m, null) } });
  load();
}

// A panel opened from Complete closes once an action succeeds, and the saga view re-fetches (it listens to
// `movienight:marked`, which fires only after the server accepted the change, so the refresh sees it).
const fromComplete = () => parse().view === 'complete';

const announceMarked = (detail) => window.dispatchEvent(new CustomEvent('movienight:marked', { detail }));

function marked(detail, message, action) {
  announceMarked(detail);
  if (!fromComplete()) return false;
  onRequestClose();
  closeDetail(); // now, not after the route change: a toast shown inside the open dialog would vanish with it
  if (message) toast(message, action ? { timeout: 6000, action } : undefined);
  return true;
}

async function markSeen(m, seen) {
  if (!(await setSeen(m, seen, 'sheet'))) return;
  window.dispatchEvent(new CustomEvent('movienight:marked', { detail: { id: m.id, seen } }));
  if (!seen) {
    toast(unseenMsg(m));
    load();
    return;
  }
  // Close first (same path as the X button, so the route follows), then toast. On Tonight the page
  // itself replaces the film and offers its own toast through movienight:seen-saved.
  onRequestClose();
  closeDetail(); // now, not after the route change: a toast shown inside the open dialog would vanish with it
  if (parse().view === 'tonight') return;
  toast(seenMsg(m), {
    timeout: 3000,
    action: { label: t('common.undo'), run: async () => { if (await setSeen(m, false, 'undo')) announceMarked({ id: m.id, seen: false }); } },
  });
}

// A vote from the sheet also marks an unseen film as seen (one request). Clearing a vote never un-marks it.
function voteFromSheet(movie, vote) {
  if (vote !== 0 && !isSeen(movie) && !movie.watched) return setSeenVote(movie, true, vote, 'sheet');
  return setVote(movie, vote);
}

function votesControl(m) {
  let wasSeen = false;
  let previousVote = 0;
  const save = (movie, vote) => {
    wasSeen = isSeen(movie) || Boolean(movie.watched);
    previousVote = voteOf(movie);
    return voteFromSheet(movie, vote);
  };
  const onVote = async (vote, done) => {
    const markedSeen = !wasSeen; // the save above also marked it seen when it was not
    if (!(await done)) return; // refused (helpers already rolled back and toasted)
    window.dispatchEvent(new CustomEvent('movienight:marked', { detail: { id: m.id, vote } }));
    if (vote === 0) return; // cleared
    onRequestClose();
    closeDetail(); // now, not after the route change: a toast shown inside the open dialog would vanish with it
    if (!markedSeen) { toast(voteMsg(m, vote), { timeout: 3000 }); return; }
    if (parse().view === 'tonight') return; // Tonight shows its own toast for the seen mark
    toast(seenVoteMsg(m, vote), {
      timeout: 3000,
      action: { label: t('common.undo'), run: async () => { if (await setSeenVote(m, false, previousVote, 'undo')) announceMarked({ id: m.id, seen: false }); } },
    });
  };
  return voteButtons(m, { save, onVote });
}

function render(m) {
  const hidden = hiddenUntil(m);
  const art = m.backdrop_url
    ? h('img', { class: 'detail__backdrop', src: m.backdrop_url, alt: '', decoding: 'async' })
    : h('div', { class: 'detail__backdrop detail__backdrop--gen', 'aria-hidden': 'true' });

  const facts = [
    [t('detail.directed'), m.directors?.join(', ')],
    [t('detail.starring'), m.cast?.slice(0, 8).join(', ')],
    [t('detail.language'), languageName(m.language)],
    [t('detail.countries'), countryNames(m.countries)],
    [t('sort.rating'), m.tmdb_rating != null ? tmdbRating(m) : null],
    [t('detail.yourRating'), m.my_rating != null ? stars(m.my_rating) : null],
    [t('detail.on', { server: serverName() }), sourcesText(m.plex)],
  ].filter(([, v]) => v);

  return h('article', { class: 'detail', style: { '--h': String(hueOf(m)) } },
    closeButton(),
    h('div', { class: 'detail__art' }, art),
    h('header', { class: 'detail__head' },
      poster(m, { label: t('poster.aria', { title: m.title }), eager: true, cls: 'detail__poster' }),
      h('div', { class: 'detail__titles' },
        h('h2', { class: 'detail__title', id: 'detail-title', text: m.title }),
        m.original_title && m.original_title !== m.title ? h('p', { class: 'detail__original', lang: m.language || null, text: m.original_title }) : null,
        h('p', { class: 'meta' },
          m.year ? h('span', { text: String(m.year) }) : null,
          runtime(m.runtime) ? h('span', { text: runtime(m.runtime) }) : null,
          m.genres?.length ? h('span', { text: genreList(m.genres) }) : null)),
      matchStub(m.match)),
    h('div', { class: 'detail__actions' },
      playControl(m),
      !m.watched ? h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => markSeen(m, true) }, icon('eye'), t('common.seenIt')) : null,
      votesControl(m),
      m.letterboxd_url ? h('a', { class: 'btn btn--ghost', href: m.letterboxd_url, target: '_blank', rel: 'noopener' }, 'Letterboxd', icon('external', 'icon--end')) : null),
    m.seen_here ? h('p', { class: 'detail__seen' }, icon('check'), t('detail.youveSeen'),
      h('button', { type: 'button', class: 'linkbtn', onclick: () => markSeen(m, false) }, UNMARK_LABEL)) : null,
    partOf(m.groups),
    m.tagline ? h('p', { class: 'detail__tagline', text: m.tagline }) : null,
    m.overview ? h('p', { class: 'detail__overview', text: m.overview }) : null,
    m.reasons?.length ? section(t('detail.why'), reasonList(m.reasons)) : null,
    m.because?.length ? section(t('detail.because'), h('ul', { class: 'because' }, m.because.map(becauseItem))) : null,
    facts.length ? h('dl', { class: 'facts' }, facts.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', null, v)))) : null,
    m.keywords?.length ? h('ul', { class: 'keywords', 'aria-label': t('detail.keywords') }, m.keywords.slice(0, 16).map((k) => h('li', { text: k }))) : null,
    h('section', { class: 'detail__dismiss', 'aria-label': t('detail.hideFilm') },
      hidden
        ? [h('p', { class: 'detail__hidden' }, icon('eyeoff'), hidden),
          h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => setDismissed(m, null) }, t('detail.showAgain'))]
        : [h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => setDismissed(m, 30) }, icon('eyeoff'), t('detail.hide30')),
          h('button', { type: 'button', class: 'btn btn--danger', onclick: () => setDismissed(m, 0) }, t('detail.never'))]));
}

// ---- films by TMDB id (Complete): the same sheet, with how the film was seen in place of the library's own
// "seen it" state, and no Play for a film that is not in the library. The four "seen it" verdicts are the sheet's
// own controls: "Seen it" (no opinion) and the thumbs/heart (seen, with that vote). A mark made here (stored as
// tmdb:<id>) can be undone; a film logged on Letterboxd or played on the media server itself only there.
const outcomeOf = (f) => {
  if (!f.seen_via.includes('here')) return null;
  const v = f.my_vote ?? 0;
  return OUTCOMES.find((o) => o.vote === (v >= LOVE ? LOVE : v)) || OUTCOMES[1];
};

// A film in the library also gets its Play sources (from the library entry).
// With Seerr set up, its request state is asked for alongside (cached a few minutes; a failure means no button).
async function loadFilm(tmdbId) {
  const [f, request] = await Promise.all([api.film(tmdbId), seerrEnabled() ? seerrState(tmdbId).catch(() => null) : null]);
  if (f.movie_id) f.plex = await api.movie(f.movie_id).then((m) => m.plex || [], () => []);
  f.request = f.in_library ? null : request;
  return f;
}

// Not in the library: "Request" (Seerr) in place of the disabled Play, or the request's state once there is one.
function notInLibrary(f) {
  if (f.request === 'none') {
    return h('button', {
      type: 'button', class: 'btn btn--primary', 'data-act': 'request', title: t('detail.request.title'),
      onclick: async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        const state = await requestFilm(f);
        if (state && currentId === `tmdb:${f.tmdb_id}`) load({ quiet: true, focus: '[data-act="seen"], .votes__btn' });
        else btn.disabled = false;
      },
    }, icon('download'), t('detail.request'));
  }
  if (f.request && CHIP[f.request]) return stateChip(f.request);
  return h('button', { type: 'button', class: 'btn btn--primary', disabled: true }, icon('play'), t('detail.notInLibrary'));
}

let filmBusy = false;
async function saveFilm(f, seen, vote, focus) {
  if (filmBusy) return;
  filmBusy = true;
  try {
    await api.filmSeen(f.tmdb_id, seen, vote);
  } catch (err) {
    toast(err.detail || t('error.save'));
    return;
  } finally {
    filmBusy = false;
  }
  if (f.movie_id) rememberSeen(f.movie_id, seen, vote); // the library film's cached state follows the mark
  window.dispatchEvent(new CustomEvent('movienight:seen', { detail: { id: `tmdb:${f.tmdb_id}`, tmdb_id: f.tmdb_id, seen, source: 'film' } }));
  const message = !seen ? unseenMsg(f) : !f.seen ? (vote ? seenVoteMsg(f, vote) : seenMsg(f)) : vote ? voteMsg(f, vote) : seenMsg(f);
  const undo = { label: t('common.undo'), run: () => undoFilm(f) };
  if (marked({ id: `tmdb:${f.tmdb_id}`, tmdb_id: f.tmdb_id, seen }, message, undo)) return;
  if (!seen) toast(message);
  else if (!f.seen) toast(message);
  load({ quiet: true, focus });
}

// Undo of a panel change: back to the seen state and verdict the film had when the panel loaded.
async function undoFilm(f) {
  const seen = Boolean(f.seen);
  const vote = seen ? (f.my_vote ?? 0) : 0;
  try {
    await api.filmSeen(f.tmdb_id, seen, vote);
  } catch (err) {
    toast(err.detail || t('error.undo'));
    return;
  }
  if (f.movie_id) rememberSeen(f.movie_id, seen, vote);
  window.dispatchEvent(new CustomEvent('movienight:seen', { detail: { id: `tmdb:${f.tmdb_id}`, tmdb_id: f.tmdb_id, seen, source: 'film' } }));
  announceMarked({ id: `tmdb:${f.tmdb_id}`, tmdb_id: f.tmdb_id, seen });
}

// Thumbs up, heart, thumbs down, as in the sheet (.votes): each one marks the film seen with that vote; the chosen
// one again keeps it seen with no opinion.
function filmVotes(f) {
  const chosen = outcomeOf(f);
  const byKey = Object.fromEntries(OUTCOMES.map((o) => [o.key, o]));
  return h('div', { class: 'votes', role: 'group', 'aria-label': t('detail.seenAsk', { title: f.title }) },
    ['up', 'love', 'down'].map((key) => {
      const o = byKey[key];
      const on = chosen?.key === key;
      return h('button', {
        type: 'button', class: `votes__btn votes__btn--${key}`, title: o.label, 'aria-label': o.label, 'aria-pressed': String(on),
        onclick: () => saveFilm(f, true, on ? 0 : o.vote, `.votes__btn--${key}`),
      }, icon(o.icon));
    }));
}

function filmSeenLine(f) {
  const server = serverName();
  if (f.can_unmark) {
    return h('p', { class: 'detail__seen' }, icon('check'), t('detail.youMarked'),
      h('button', {
        type: 'button', class: 'linkbtn', 'data-act': 'unmark', title: f.pushed ? t('detail.unplayed', { server }) : null,
        onclick: () => saveFilm(f, false, 0, '[data-act="seen"]'),
      }, UNMARK_LABEL));
  }
  const where = f.seen_via.includes('letterboxd') ? 'Letterboxd' : server;
  return h('p', { class: 'detail__seen', title: t('detail.seenOn.title', { where }) }, icon('check'), t('detail.seenOn', { where }));
}

function renderFilm(f) {
  const art = f.backdrop_url
    ? h('img', { class: 'detail__backdrop', src: f.backdrop_url, alt: '', decoding: 'async' })
    : h('div', { class: 'detail__backdrop detail__backdrop--gen', 'aria-hidden': 'true' });
  const markable = !f.seen || f.can_unmark;
  const facts = [
    [t('detail.directed'), f.directors?.join(', ')],
    [t('detail.starring'), f.cast?.slice(0, 8).join(', ')],
    [t('detail.language'), languageName(f.language)],
    [t('detail.countries'), countryNames(f.countries)],
    [t('sort.rating'), f.tmdb_rating ? tmdbRating(f) : null],
    [t('detail.yourRating'), f.my_rating != null ? [stars(f.my_rating), f.liked ? h('span', { class: 'liked', title: t('taste.liked') }, ' ', icon('heart')) : null] : null],
    [t('detail.on', { server: serverName() }), sourcesText(f.plex)],
  ].filter(([, v]) => v);

  return h('article', { class: 'detail', style: { '--h': String(hueOf(f)) } },
    closeButton(),
    h('div', { class: 'detail__art' }, art),
    h('header', { class: 'detail__head' },
      poster(f, { label: t('poster.aria', { title: f.title }), eager: true, cls: 'detail__poster' }),
      h('div', { class: 'detail__titles' },
        h('h2', { class: 'detail__title', id: 'detail-title', text: f.title }),
        f.original_title && f.original_title !== f.title ? h('p', { class: 'detail__original', lang: f.language || null, text: f.original_title }) : null,
        h('p', { class: 'meta' },
          f.year ? h('span', { text: String(f.year) }) : null,
          runtime(f.runtime) ? h('span', { text: runtime(f.runtime) }) : null,
          f.genres?.length ? h('span', { text: genreList(f.genres) }) : null))),
    h('div', { class: 'detail__actions' },
      f.in_library ? playControl(f) : notInLibrary(f),
      !f.seen ? h('button', { type: 'button', class: 'btn btn--ghost', 'data-act': 'seen', title: OUTCOMES[1].label,
        onclick: () => saveFilm(f, true, 0, '.votes__btn--up') }, icon('eye'), t('common.seenIt')) : null,
      markable ? filmVotes(f) : null,
      h('a', { class: 'btn btn--ghost', href: f.letterboxd_url, target: '_blank', rel: 'noopener' }, 'Letterboxd', icon('external', 'icon--end'))),
    f.seen ? filmSeenLine(f) : null,
    partOf(f.groups),
    f.tagline ? h('p', { class: 'detail__tagline', text: f.tagline }) : null,
    f.overview ? h('p', { class: 'detail__overview', text: f.overview }) : null,
    facts.length ? h('dl', { class: 'facts' }, facts.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', null, v)))) : null);
}

// "Part of Star Wars · 4 of 11": the started sagas and lists the film belongs to (or, in none, a director's or
// actor's films), best first, as the API sends them. Each line opens that group on Complete.
export function partOf(groups, cls = '') {
  if (!groups?.length) return null;
  const name = (g) => (g.kind === 'director' || g.kind === 'actor' ? t('detail.personFilms', { name: g.name }) : g.name);
  return h('ul', { class: `detail__parts ${cls}`.trim(), 'aria-label': t('detail.partOf') }, groups.map((g) => h('li', null,
    h('a', {
      class: 'detail__part', href: href('complete', parse().filters, null, null, g.key),
      onclick: (e) => { if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; e.preventDefault(); showGroup(g.key); },
    },
    icon('trophy'),
    h('span', { class: 'detail__part-text' }, t('detail.partOfPre'), h('strong', { text: name(g) })),
    h('span', { class: 'detail__part-n', text: t('complete.row.of', { seen: g.seen, total: g.total }) })))));
}

function sourcesText(plex) {
  if (!plex?.length) return null;
  return plex.map((p) => p.server + (p.resolution ? ` (${p.resolution === '4k' ? '4K' : p.resolution === 'sd' ? 'SD' : `${p.resolution}p`})` : '')).join(', ');
}

// TMDB's ISO 3166 codes ("US") as the locale names them ("Estados Unidos")
function countryNames(codes) {
  if (!codes?.length) return null;
  let names = null;
  try { names = new Intl.DisplayNames([lang], { type: 'region' }); } catch { /* old browser: the codes */ }
  return codes.map((c) => { try { return names?.of(c) || c; } catch { return c; } }).join(', ');
}

function languageName(code) {
  if (!code) return null;
  try { return new Intl.DisplayNames([lang], { type: 'language' }).of(code); } catch { return code; }
}

// "7.6 / 10 from 1,234 votes"
function tmdbRating(m) {
  const score = `${fixed(m.tmdb_rating)} / 10`;
  return m.tmdb_votes ? t('detail.tmdbVotes', { score, votes: num(m.tmdb_votes) }) : score;
}

function section(title, content) {
  return h('section', { class: 'detail__section' }, h('h3', { class: 'detail__h', text: title }), content);
}

function becauseItem(b) {
  return h('li', { class: 'because__item' },
    poster(b, { cls: 'because__poster' }),
    h('div', { class: 'because__body' },
      h('p', { class: 'because__title' }, b.title, b.year ? h('span', { class: 'because__year', text: ` ${b.year}` }) : null),
      h('p', { class: 'because__rating' },
        stars(b.my_rating),
        b.liked ? h('span', { class: 'liked', title: t('taste.liked') }, icon('heart'), h('span', { class: 'sr-only', text: t('taste.liked') })) : null,
        b.my_vote === 2 ? h('span', { class: 'because__vote' }, h('span', { class: 'liked' }, icon('heart')), t('detail.loved')) : null,
        b.my_vote === 1 ? h('span', { class: 'because__vote' }, icon('thumbup'), t('detail.liked')) : null),
      b.why ? h('p', { class: 'because__why', text: becauseText(b) }) : null));
}

function skeleton() {
  return h('div', { class: 'detail', 'aria-busy': 'true' },
    closeButton(),
    h('div', { class: 'detail__art skel' }),
    h('div', { class: 'detail__head' },
      h('div', { class: 'detail__poster skel' }),
      h('div', { class: 'detail__titles' }, h('div', { class: 'skel skel--title' }), h('div', { class: 'skel skel--line' }))),
    h('div', { class: 'skel skel--line' }), h('div', { class: 'skel skel--line' }), h('div', { class: 'skel skel--line skel--short' }));
}
