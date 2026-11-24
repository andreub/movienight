// One wording per outcome for the confirmation toasts (seen, unseen, vote, hide), so Tonight, Browse, the
// library sheet and the film panel never drift apart. Titles go through `quoted` (curly quotes, capped).
import { quoted } from './dom.js';

// What the vote says, as a short sentence with no film in it ("You liked it.").
export function voteNote(vote) {
  return vote > 1 ? 'A favourite.' : vote > 0 ? 'You liked it.' : 'Less like it.';
}

export const seenMsg = (m) => `Marked ${quoted(m)} as seen.`;
export const seenAskMsg = (m) => `${seenMsg(m)} Did you like it?`;
export const seenVoteMsg = (m, vote) => `${seenMsg(m)} ${voteNote(vote)}`;
export const unseenMsg = (m) => `Unmarked ${quoted(m)}. It can come back in your picks.`;
export const voteMsg = (m, vote) => `Noted: ${quoted(m)}. ${voteNote(vote)}`;
export const hideMsg = (m) => `Hid ${quoted(m)} for 30 days.`;
export const neverMsg = (m) => `${quoted(m)} won't be suggested again.`;
export const unhideMsg = (m) => `${quoted(m)} is back in your picks.`;

// The link beside "You've seen this" / "You marked this seen", the same in the sheet and the film panel.
export const UNMARK_LABEL = 'Undo';
