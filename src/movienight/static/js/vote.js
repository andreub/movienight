// Votes on a film: thumbs down (-1), thumbs up (1) and love (2, a favourite: counts more than a thumb up).
// Optimistic: the button flips at once and rolls back with a toast if the server refuses.
// Every group for the same film (hero, sheet, card, "Seen these?") stays in step through a window event.
import { api } from './api.js';
import { h, icon, toast } from './dom.js';

export const LOVE = 2;
const votes = new Map(); // last known vote per film, so re-rendered groups agree with what the user just did

export function voteOf(movie) {
  return votes.has(movie.id) ? votes.get(movie.id) : movie.my_vote ?? 0;
}

export async function setVote(movie, vote) {
  const before = voteOf(movie);
  announceVote(movie.id, vote);
  try {
    await api.feedback(movie.id, vote);
    movie.my_vote = vote || null;
    return true;
  } catch (err) {
    announceVote(movie.id, before);
    toast(err.detail || 'Could not save your vote. Try again.');
    return false;
  }
}

export function announceVote(id, vote) {
  votes.set(id, vote);
  window.dispatchEvent(new CustomEvent('movienight:vote', { detail: { id, vote } }));
}

// `onVote(vote, done)` runs after the optimistic flip; `done` resolves true once the server accepted it.
// `love` adds the heart button. Without it (compact cards) a loved film shows a heart on its thumbs-up button,
// and tapping that clears the vote.
export function voteButtons(movie, { compact = false, love = !compact, onVote = null } = {}) {
  let vote = voteOf(movie);
  const make = (value, name, iconName, label) => h('button', {
    type: 'button', class: `votes__btn votes__btn--${name}`, title: label, 'aria-label': label, 'aria-pressed': 'false',
    onclick: (e) => {
      e.stopPropagation();
      const active = vote === value || (!love && value === 1 && vote === LOVE);
      const next = active ? 0 : value;
      const done = setVote(movie, next);
      onVote?.(next, done);
    },
  }, icon(iconName));
  const up = make(1, 'up', 'thumbup', 'More like this');
  const fav = love ? make(LOVE, 'love', 'heart', 'A favourite') : null;
  const down = make(-1, 'down', 'thumbdown', 'Less like this');
  const el = h('div', { class: `votes ${compact ? 'votes--compact' : ''}`.trim(), role: 'group', 'aria-label': `Rate ${movie.title}` }, up, fav, down);

  let upIcon = 'thumbup';
  const paint = () => {
    up.setAttribute('aria-pressed', String(vote === 1 || (!love && vote === LOVE)));
    down.setAttribute('aria-pressed', String(vote === -1));
    fav?.setAttribute('aria-pressed', String(vote === LOVE));
    if (!love) {
      const want = vote === LOVE ? 'heart' : 'thumbup';
      if (want !== upIcon) {
        upIcon = want;
        up.replaceChildren(icon(want));
        const label = want === 'heart' ? 'A favourite. Tap to clear' : 'More like this';
        up.title = label;
        up.setAttribute('aria-label', label);
      }
      up.classList.toggle('votes__btn--love', vote === LOVE);
    }
  };
  function onVoteEvent(e) {
    if (!el.isConnected && !el.dataset.fresh) { window.removeEventListener('movienight:vote', onVoteEvent); return; }
    if (e.detail.id === movie.id) { vote = e.detail.vote; paint(); }
  }
  // The group is created before it is attached, so give it one tick before treating "not connected" as gone.
  el.dataset.fresh = '1';
  setTimeout(() => delete el.dataset.fresh, 0);
  window.addEventListener('movienight:vote', onVoteEvent);
  paint();
  return el;
}
