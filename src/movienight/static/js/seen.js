// "Seen it": marks a film as already watched (stored here, not on Letterboxd). Optimistic: every control for the
// same film flips at once through a window event and rolls back with a toast if the server refuses.
import { api } from './api.js';
import { h, icon, toast } from './dom.js';
import { voteOf, announceVote } from './vote.js';

const states = new Map(); // last known state per film, so re-rendered controls agree with what the user just did

export function isSeen(movie) {
  return states.has(movie.id) ? states.get(movie.id) : Boolean(movie.seen_here);
}

// `was` is the state before this change, so listeners can tell a real flip from a repeat.
function announce(id, seen, source, was) {
  states.set(id, seen);
  window.dispatchEvent(new CustomEvent('movienight:seen', { detail: { id, seen, was, source } }));
}

export async function setSeen(movie, seen, source = '') {
  const before = isSeen(movie);
  announce(movie.id, seen, source, before);
  try {
    await api.seen(movie.id, seen);
    movie.seen_here = seen;
    return true;
  } catch (err) {
    announce(movie.id, before, source, seen);
    toast(err.detail || 'Could not save that. Try again.');
    return false;
  }
}

// Seen + vote in one request (one transaction on the server). Both flip at once and both roll back on failure,
// so a half state is never shown as saved. `vote` 0 clears the vote; unmarking passes seen=false, vote=0.
export async function setSeenVote(movie, seen, vote, source = '') {
  const before = { seen: isSeen(movie), vote: voteOf(movie) };
  announce(movie.id, seen, source, before.seen);
  announceVote(movie.id, vote);
  try {
    await api.seen(movie.id, seen, vote);
    movie.seen_here = seen;
    movie.my_vote = vote || null;
    return true;
  } catch (err) {
    announce(movie.id, before.seen, source, seen);
    announceVote(movie.id, before.vote);
    toast(err.detail || 'Could not save that. Try again.');
    return false;
  }
}

// Compact round toggle for poster cards. Never opens the card.
export function seenToggle(movie) {
  let seen = isSeen(movie);
  const btn = h('button', {
    type: 'button', class: 'seenbtn', 'aria-label': `I've seen ${movie.title}`, title: seen ? 'Seen. Tap to undo' : 'Seen it',
    onclick: (e) => { e.stopPropagation(); setSeen(movie, !seen, 'card'); },
  }, icon('eye'));
  const paint = () => {
    btn.setAttribute('aria-pressed', String(seen));
    btn.title = seen ? 'Seen. Tap to undo' : 'Seen it';
  };
  function onEvent(e) {
    if (!btn.isConnected && !btn.dataset.fresh) { window.removeEventListener('movienight:seen', onEvent); return; }
    if (e.detail.id === movie.id) { seen = e.detail.seen; paint(); }
  }
  btn.dataset.fresh = '1';
  setTimeout(() => delete btn.dataset.fresh, 0);
  window.addEventListener('movienight:seen', onEvent);
  paint();
  return btn;
}
