// Film detail: bottom sheet on phones, side panel on wider screens (one <dialog>).
import { api } from '../api.js';
import { h, icon, poster, hueOf, runtime, stars, matchStub, reasonList, errorBox, toast, quoted, num } from '../dom.js';
import { playControl } from '../play.js';
import { voteButtons } from '../vote.js';
import { setSeen } from '../seen.js';
import { parse } from '../state.js';

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

async function load() {
  const token = ++req;
  body.replaceChildren(skeleton());
  try {
    const m = await api.movie(currentId);
    if (token !== req) return;
    body.replaceChildren(render(m));
    body.scrollTop = 0;
    body.querySelector('.sheet__close')?.focus();
  } catch (err) {
    if (token !== req) return;
    body.replaceChildren(closeButton(), errorBox(err, load));
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
    ['On Plex', m.plex?.length ? m.plex.map((p) => p.server + (p.resolution ? ` (${p.resolution === '4k' ? '4K' : p.resolution === 'sd' ? 'SD' : `${p.resolution}p`})` : '')).join(', ') : null],
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
