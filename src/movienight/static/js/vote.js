// Thumbs up / down on a film. Optimistic: the button flips at once and rolls back with a toast if the server refuses.
// Every group for the same film (hero, sheet, card) stays in step through a window event.
import { api } from './api.js';
import { h, icon, toast } from './dom.js';

const votes = new Map(); // last known vote per film, so re-rendered groups agree with what the user just did

export async function setVote(movie, vote) {
  const before = votes.has(movie.id) ? votes.get(movie.id) : movie.my_vote ?? 0;
  announce(movie.id, vote);
  try {
    await api.feedback(movie.id, vote);
    movie.my_vote = vote || null;
    return true;
  } catch (err) {
    announce(movie.id, before);
    toast(err.detail || 'Could not save your vote. Try again.');
    return false;
  }
}

function announce(id, vote) {
  votes.set(id, vote);
  window.dispatchEvent(new CustomEvent('movienight:vote', { detail: { id, vote } }));
}

// `onVote(vote, done)` runs after the optimistic flip; `done` resolves true once the server accepted it.
export function voteButtons(movie, { compact = false, onVote = null } = {}) {
  let vote = votes.has(movie.id) ? votes.get(movie.id) : movie.my_vote ?? 0;
  const make = (value, name, label) => h('button', {
    type: 'button', class: `votes__btn votes__btn--${name}`, title: label, 'aria-label': label, 'aria-pressed': 'false',
    onclick: (e) => {
      e.stopPropagation();
      const next = vote === value ? 0 : value;
      const done = setVote(movie, next);
      onVote?.(next, done);
    },
  }, icon(name === 'up' ? 'thumbup' : 'thumbdown'));
  const up = make(1, 'up', 'More like this');
  const down = make(-1, 'down', 'Less like this');
  const el = h('div', { class: `votes ${compact ? 'votes--compact' : ''}`.trim(), role: 'group', 'aria-label': `Rate ${movie.title}` }, up, down);

  const paint = () => {
    up.setAttribute('aria-pressed', String(vote === 1));
    down.setAttribute('aria-pressed', String(vote === -1));
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
