// Film detail: bottom sheet on phones, side panel on wider screens (one <dialog>). Library films by id (#movie=),
// and any film by TMDB id (#film=, Complete's saga films, in the library or not) in the same sheet.
import { api } from '../api.js';
import { h, icon, poster, hueOf, runtime, stars, matchStub, reasonList, errorBox, toast, num } from '../dom.js';
import { playControl } from '../play.js';
import { voteButtons, setVote, voteOf, LOVE } from '../vote.js';
import { setSeen, setSeenVote, isSeen, OUTCOMES } from '../seen.js';
import { parse } from '../state.js';
import { serverName } from '../status.js';
import { TV } from '../tvnav.js';
import { seenMsg, seenVoteMsg, unseenMsg, voteMsg, hideMsg, neverMsg, unhideMsg, UNMARK_LABEL } from '../messages.js';

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
  return h('button', { type: 'button', class: 'sheet__close iconbtn', 'aria-label': 'Close', onclick: () => onRequestClose() }, icon('close'));
}

function hiddenUntil(m) {
  if (!m.dismissed_until) return null;
  const d = new Date(m.dismissed_until);
  if (Number.isNaN(d.getTime()) || d < new Date()) return null;
  return d.getFullYear() >= 9000 ? 'Hidden for good' : `Hidden until ${d.toLocaleDateString('en', { day: 'numeric', month: 'short' })}`;
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
  else toast(days === 0 ? neverMsg(m) : hideMsg(m), { action: { label: 'Undo', run: () => setDismissed(m, null) } });
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
  if (parse().view === 'tonight') return;
  toast(seenMsg(m), {
    timeout: 3000,
    action: { label: 'Undo', run: async () => { if (await setSeen(m, false, 'undo')) announceMarked({ id: m.id, seen: false }); } },
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
    if (!markedSeen) { toast(voteMsg(m, vote), { timeout: 3000 }); return; }
    if (parse().view === 'tonight') return; // Tonight shows its own toast for the seen mark
    toast(seenVoteMsg(m, vote), {
      timeout: 3000,
      action: { label: 'Undo', run: async () => { if (await setSeenVote(m, false, previousVote, 'undo')) announceMarked({ id: m.id, seen: false }); } },
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
    ['Directed by', m.directors?.join(', ')],
    ['Starring', m.cast?.slice(0, 8).join(', ')],
    ['Language', languageName(m.language)],
    ['Countries', m.countries?.join(', ')],
    ['TMDB rating', m.tmdb_rating != null ? `${m.tmdb_rating.toFixed(1)} / 10${m.tmdb_votes ? ` from ${num(m.tmdb_votes)} votes` : ''}` : null],
    ['Your rating', m.my_rating != null ? stars(m.my_rating) : null],
    [`On ${serverName()}`, sourcesText(m.plex)],
  ].filter(([, v]) => v);

  return h('article', { class: 'detail', style: { '--h': String(hueOf(m)) } },
    closeButton(),
    h('div', { class: 'detail__art' }, art),
    h('header', { class: 'detail__head' },
      poster(m, { label: `Poster for ${m.title}`, eager: true, cls: 'detail__poster' }),
      h('div', { class: 'detail__titles' },
        h('h2', { class: 'detail__title', id: 'detail-title', text: m.title }),
        m.original_title && m.original_title !== m.title ? h('p', { class: 'detail__original', lang: m.language || null, text: m.original_title }) : null,
        h('p', { class: 'meta' },
          m.year ? h('span', { text: String(m.year) }) : null,
          runtime(m.runtime) ? h('span', { text: runtime(m.runtime) }) : null,
          m.genres?.length ? h('span', { text: m.genres.join(', ') }) : null)),
      matchStub(m.match)),
    h('div', { class: 'detail__actions' },
      playControl(m),
      !m.watched ? h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => markSeen(m, true) }, icon('eye'), 'Seen it') : null,
      votesControl(m),
      m.letterboxd_url ? h('a', { class: 'btn btn--ghost', href: m.letterboxd_url, target: '_blank', rel: 'noopener' }, 'Letterboxd', icon('external', 'icon--end')) : null),
    m.seen_here ? h('p', { class: 'detail__seen' }, icon('check'), "You've seen this",
      h('button', { type: 'button', class: 'linkbtn', onclick: () => markSeen(m, false) }, UNMARK_LABEL)) : null,
    m.tagline ? h('p', { class: 'detail__tagline', text: m.tagline }) : null,
    m.overview ? h('p', { class: 'detail__overview', text: m.overview }) : null,
    m.reasons?.length ? section('Why it fits you', reasonList(m.reasons)) : null,
    m.because?.length ? section('Because you watched', h('ul', { class: 'because' }, m.because.map(becauseItem))) : null,
    facts.length ? h('dl', { class: 'facts' }, facts.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', null, v)))) : null,
    m.keywords?.length ? h('ul', { class: 'keywords', 'aria-label': 'Keywords' }, m.keywords.slice(0, 16).map((k) => h('li', { text: k }))) : null,
    h('section', { class: 'detail__dismiss', 'aria-label': 'Hide this film' },
      hidden
        ? [h('p', { class: 'detail__hidden' }, icon('eyeoff'), hidden),
          h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => setDismissed(m, null) }, 'Show it again')]
        : [h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => setDismissed(m, 30) }, icon('eyeoff'), 'Hide for 30 days'),
          h('button', { type: 'button', class: 'btn btn--danger', onclick: () => setDismissed(m, 0) }, 'Never suggest it')]));
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
async function loadFilm(tmdbId) {
  const f = await api.film(tmdbId);
  if (f.movie_id) f.plex = await api.movie(f.movie_id).then((m) => m.plex || [], () => []);
  return f;
}

let filmBusy = false;
async function saveFilm(f, seen, vote, focus) {
  if (filmBusy) return;
  filmBusy = true;
  try {
    await api.filmSeen(f.tmdb_id, seen, vote);
  } catch (err) {
    toast(err.detail || 'Could not save that. Try again.');
    return;
  } finally {
    filmBusy = false;
  }
  window.dispatchEvent(new CustomEvent('movienight:seen', { detail: { id: `tmdb:${f.tmdb_id}`, tmdb_id: f.tmdb_id, seen, source: 'film' } }));
  const message = !seen ? unseenMsg(f) : !f.seen ? (vote ? seenVoteMsg(f, vote) : seenMsg(f)) : vote ? voteMsg(f, vote) : seenMsg(f);
  const undo = { label: 'Undo', run: () => undoFilm(f) };
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
    toast(err.detail || 'Could not undo that. Try again.');
    return;
  }
  window.dispatchEvent(new CustomEvent('movienight:seen', { detail: { id: `tmdb:${f.tmdb_id}`, tmdb_id: f.tmdb_id, seen, source: 'film' } }));
  announceMarked({ id: `tmdb:${f.tmdb_id}`, tmdb_id: f.tmdb_id, seen });
}

// Thumbs up, heart, thumbs down, as in the sheet (.votes): each one marks the film seen with that vote; the chosen
// one again keeps it seen with no opinion.
function filmVotes(f) {
  const chosen = outcomeOf(f);
  const byKey = Object.fromEntries(OUTCOMES.map((o) => [o.key, o]));
  return h('div', { class: 'votes', role: 'group', 'aria-label': `${f.title}: seen it?` },
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
    return h('p', { class: 'detail__seen' }, icon('check'), 'You marked this seen',
      h('button', {
        type: 'button', class: 'linkbtn', 'data-act': 'unmark', title: f.pushed ? `Also sets it back to unplayed on ${server}` : null,
        onclick: () => saveFilm(f, false, 0, '[data-act="seen"]'),
      }, UNMARK_LABEL));
  }
  const where = f.seen_via.includes('letterboxd') ? 'Letterboxd' : server;
  return h('p', { class: 'detail__seen', title: `Seen on ${where}, so it can only be unmarked there` }, icon('check'), `Seen on ${where}`);
}

function renderFilm(f) {
  const art = f.backdrop_url
    ? h('img', { class: 'detail__backdrop', src: f.backdrop_url, alt: '', decoding: 'async' })
    : h('div', { class: 'detail__backdrop detail__backdrop--gen', 'aria-hidden': 'true' });
  const markable = !f.seen || f.can_unmark;
  const facts = [
    ['Directed by', f.directors?.join(', ')],
    ['Starring', f.cast?.slice(0, 8).join(', ')],
    ['Language', languageName(f.language)],
    ['Countries', f.countries?.join(', ')],
    ['TMDB rating', f.tmdb_rating ? `${f.tmdb_rating.toFixed(1)} / 10${f.tmdb_votes ? ` from ${num(f.tmdb_votes)} votes` : ''}` : null],
    ['Your rating', f.my_rating != null ? [stars(f.my_rating), f.liked ? h('span', { class: 'liked', title: 'Liked' }, ' ', icon('heart')) : null] : null],
    [`On ${serverName()}`, sourcesText(f.plex)],
  ].filter(([, v]) => v);

  return h('article', { class: 'detail', style: { '--h': String(hueOf(f)) } },
    closeButton(),
    h('div', { class: 'detail__art' }, art),
    h('header', { class: 'detail__head' },
      poster(f, { label: `Poster for ${f.title}`, eager: true, cls: 'detail__poster' }),
      h('div', { class: 'detail__titles' },
        h('h2', { class: 'detail__title', id: 'detail-title', text: f.title }),
        f.original_title && f.original_title !== f.title ? h('p', { class: 'detail__original', lang: f.language || null, text: f.original_title }) : null,
        h('p', { class: 'meta' },
          f.year ? h('span', { text: String(f.year) }) : null,
          runtime(f.runtime) ? h('span', { text: runtime(f.runtime) }) : null,
          f.genres?.length ? h('span', { text: f.genres.join(', ') }) : null))),
    h('div', { class: 'detail__actions' },
      f.in_library ? playControl(f)
        : h('button', { type: 'button', class: 'btn btn--primary', disabled: true }, icon('play'), 'Not in your library'),
      !f.seen ? h('button', { type: 'button', class: 'btn btn--ghost', 'data-act': 'seen', title: OUTCOMES[1].label,
        onclick: () => saveFilm(f, true, 0, '.votes__btn--up') }, icon('eye'), 'Seen it') : null,
      markable ? filmVotes(f) : null,
      h('a', { class: 'btn btn--ghost', href: f.letterboxd_url, target: '_blank', rel: 'noopener' }, 'Letterboxd', icon('external', 'icon--end'))),
    f.seen ? filmSeenLine(f) : null,
    f.tagline ? h('p', { class: 'detail__tagline', text: f.tagline }) : null,
    f.overview ? h('p', { class: 'detail__overview', text: f.overview }) : null,
    facts.length ? h('dl', { class: 'facts' }, facts.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', null, v)))) : null);
}

function sourcesText(plex) {
  if (!plex?.length) return null;
  return plex.map((p) => p.server + (p.resolution ? ` (${p.resolution === '4k' ? '4K' : p.resolution === 'sd' ? 'SD' : `${p.resolution}p`})` : '')).join(', ');
}

function languageName(code) {
  if (!code) return null;
  try { return new Intl.DisplayNames(['en'], { type: 'language' }).of(code); } catch { return code; }
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
        b.liked ? h('span', { class: 'liked', title: 'Liked' }, icon('heart'), h('span', { class: 'sr-only', text: 'Liked' })) : null,
        b.my_vote === 2 ? h('span', { class: 'because__vote' }, h('span', { class: 'liked' }, icon('heart')), 'You loved it') : null,
        b.my_vote === 1 ? h('span', { class: 'because__vote' }, icon('thumbup'), 'You liked it') : null),
      b.why ? h('p', { class: 'because__why', text: b.why }) : null));
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
