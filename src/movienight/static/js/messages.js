// One wording per outcome for the confirmation toasts (seen, unseen, vote, hide), so Tonight, Browse, the
// library sheet and the film panel never drift apart. Titles go through `quoted` (curly quotes, capped).
import { quoted } from './dom.js';
import { t } from './i18n.js';

// What the vote says, as a short sentence with no film in it ("You liked it.").
export function voteNote(vote) {
  return vote > 1 ? t('vote.note.love') : vote > 0 ? t('vote.note.up') : t('vote.note.down');
}

export const seenMsg = (m) => t('msg.seen', { title: quoted(m) });
export const seenAskMsg = (m) => `${seenMsg(m)} ${t('msg.likedAsk')}`;
export const seenVoteMsg = (m, vote) => `${seenMsg(m)} ${voteNote(vote)}`;
export const unseenMsg = (m) => t('msg.unseen', { title: quoted(m) });
export const voteMsg = (m, vote) => `${t('msg.noted', { title: quoted(m) })} ${voteNote(vote)}`;
export const hideMsg = (m) => t('msg.hid', { title: quoted(m) });
export const neverMsg = (m) => t('msg.never', { title: quoted(m) });
export const unhideMsg = (m) => t('msg.unhide', { title: quoted(m) });

// The link beside "You've seen this" / "You marked this seen", the same in the sheet and the film panel.
export const UNMARK_LABEL = t('common.undo');
