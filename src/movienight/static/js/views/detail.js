// Film detail: bottom sheet on phones, side panel on wider screens (one <dialog>). Library films by id (#movie=),
// and any film by TMDB id (#film=, Complete's saga films, in the library or not) as a lighter panel.
import { api } from '../api.js';
import { h, icon, poster, hueOf, runtime, stars, matchStub, reasonList, errorBox, toast, quoted, num } from '../dom.js';
import { playControl } from '../play.js';
import { voteButtons, LOVE } from '../vote.js';
import { setSeen, OUTCOMES } from '../seen.js';
import { parse, go } from '../state.js';
import { serverName } from '../status.js';
import { TV } from '../tvnav.js';

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

// A film by TMDB id, in the library or not (Complete's saga films): the light panel below.
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
    const m = isFilm(id) ? await api.film(id.slice(5)) : await api.movie(id);
    if (token !== req) return;
    const top = body.scrollTop;
    body.replaceChildren(isFilm(id) ? renderFilm(m) : render(m));
    body.scrollTop = quiet ? top : 0;
    const target = (focus && body.querySelector(focus))
      || (TV && isFilm(id) && (body.querySelector('.verdict[aria-pressed="true"]') || body.querySelector('.verdict')))
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
  toast(days === null ? `${quoted(m)} is back in your picks.` : days === 0 ? `${quoted(m)} won't be suggested again.` : `Hid ${quoted(m)} for 30 days.`);
  load();
}

async function markSeen(m, seen) {
  if (!(await setSeen(m, seen, 'sheet'))) return;
  // On Tonight the page itself replaces the film and offers the rating toast.
  if (seen && parse().view === 'tonight') { load(); return; }
  toast(seen ? `Marked ${quoted(m)} as seen.` : `${quoted(m)} is back in your picks.`);
  load();
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
    [`On ${serverName()}`, m.plex?.length ? m.plex.map((p) => p.server + (p.resolution ? ` (${p.resolution === '4k' ? '4K' : p.resolution === 'sd' ? 'SD' : `${p.resolution}p`})` : '')).join(', ') : null],
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
      voteButtons(m),
      !m.watched ? h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => markSeen(m, true) }, icon('eye'), 'Seen it') : null,
      m.letterboxd_url ? h('a', { class: 'btn btn--ghost', href: m.letterboxd_url, target: '_blank', rel: 'noopener' }, 'Letterboxd', icon('external', 'icon--end')) : null),
    m.seen_here ? h('p', { class: 'detail__seen' }, icon('check'), "You've seen this",
      h('button', { type: 'button', class: 'linkbtn', onclick: () => markSeen(m, false) }, 'Undo')) : null,
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

// ---- films by TMDB id (Complete): info from TMDB, how the film was seen, and the four "seen it" verdicts.
// A mark made here (stored as tmdb:<id>) can be undone; a film logged on Letterboxd or played on the media server
// itself can only be unmarked there.
const VIA = { letterboxd: 'Letterboxd' };
const outcomeOf = (f) => {
  if (!f.seen_via.includes('here')) return null;
  const v = f.my_vote ?? 0;
  return OUTCOMES.find((o) => o.vote === (v >= LOVE ? LOVE : v)) || OUTCOMES[1];
};

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
  if (!seen) toast(`${quoted(f)} is no longer marked as seen.`);
  else if (!f.seen) toast(`Marked ${quoted(f)} as seen.`);
  load({ quiet: true, focus });
}

function verdicts(f) {
  const chosen = outcomeOf(f);
  return h('div', { class: 'verdicts', role: 'group', 'aria-label': `${f.title}: have you seen it?` },
    OUTCOMES.map((o) => h('button', {
      type: 'button', class: `verdict verdict--${o.key}`, title: o.label, 'aria-label': o.label,
      'aria-pressed': String(chosen?.key === o.key),
      // the chosen verdict again undoes the mark (as in "Seen these?")
      onclick: () => (chosen?.key === o.key ? saveFilm(f, false, 0, `.verdict--${o.key}`) : saveFilm(f, true, o.vote, `.verdict--${o.key}`)),
    }, icon(o.icon), h('span', { class: 'verdict__label', text: o.short }))));
}

function seenBlock(f) {
  const server = serverName();
  if (!f.seen) {
    return h('section', { class: 'fseen', 'aria-label': 'Have you seen it?' },
      h('h3', { class: 'fseen__h', text: 'Seen it?' }),
      verdicts(f),
      h('p', { class: 'fseen__note', text: f.in_library
        ? 'Your verdict teaches MovieNight your taste.'
        : `Your verdict is kept, and the film counts as seen once it is on ${server}.` }));
  }
  if (f.can_unmark) {
    return h('section', { class: 'fseen', 'aria-label': 'Seen' },
      h('p', { class: 'detail__seen' }, icon('check'), 'You marked this seen'),
      verdicts(f),
      h('div', { class: 'fseen__foot' },
        h('button', { type: 'button', class: 'btn btn--ghost detail__unmark', onclick: () => saveFilm(f, false, 0, '.verdict--seen') },
          icon('eyeoff'), 'Unmark as seen'),
        f.pushed ? h('span', { class: 'fseen__note', text: `Also sets it back to unplayed on ${server}.` }) : null));
  }
  const where = f.seen_via.includes('letterboxd') ? VIA.letterboxd : server;
  return h('section', { class: 'fseen', 'aria-label': 'Seen' },
    h('p', { class: 'detail__seen' }, icon('check'), `Seen on ${where}`,
      f.my_rating != null ? h('span', { class: 'fseen__stars' }, stars(f.my_rating)) : null,
      f.liked ? h('span', { class: 'liked', title: 'Liked' }, icon('heart')) : null),
    h('p', { class: 'fseen__note', text: where === 'Letterboxd'
      ? 'Logged on Letterboxd, so it can only be unmarked there.'
      : `Played on ${server}, so it can only be unmarked there.` }));
}

function renderFilm(f) {
  const art = f.backdrop_url
    ? h('img', { class: 'detail__backdrop', src: f.backdrop_url, alt: '', decoding: 'async' })
    : h('div', { class: 'detail__backdrop detail__backdrop--gen', 'aria-hidden': 'true' });
  const facts = [
    ['Directed by', f.directors?.join(', ')],
    ['Starring', f.cast?.slice(0, 8).join(', ')],
    ['Language', languageName(f.language)],
    ['TMDB rating', f.tmdb_rating ? `${f.tmdb_rating.toFixed(1)} / 10${f.tmdb_votes ? ` from ${num(f.tmdb_votes)} votes` : ''}` : null],
  ].filter(([, v]) => v);
  return h('article', { class: 'detail detail--film', style: { '--h': String(hueOf(f)) } },
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
          f.genres?.length ? h('span', { text: f.genres.join(', ') }) : null,
          h('span', { class: f.in_library ? 'meta__lib' : 'meta__missing', text: f.in_library ? 'In your library' : 'Not in your library' })))),
    seenBlock(f),
    h('div', { class: 'detail__actions' },
      f.movie_id ? h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => go({ movie: f.movie_id }, { replace: true }) }, icon('ticket'), 'Open in library') : null,
      h('a', { class: 'btn btn--ghost', href: f.letterboxd_url, target: '_blank', rel: 'noopener' }, 'Letterboxd', icon('external', 'icon--end'))),
    f.tagline ? h('p', { class: 'detail__tagline', text: f.tagline }) : null,
    f.overview ? h('p', { class: 'detail__overview', text: f.overview }) : null,
    facts.length ? h('dl', { class: 'facts' }, facts.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', null, v)))) : null);
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
