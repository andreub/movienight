// "Play on Plex": a plain link for one server, a small chooser for several.
import { h, icon } from './dom.js';

const RES = { '4k': '4K', 1080: '1080p', 720: '720p', sd: 'SD' };

export function playControl(movie, cls = '') {
  const sources = movie.plex || [];
  if (!sources.length) {
    return h('button', { type: 'button', class: `btn btn--primary ${cls}`, disabled: true }, icon('play'), 'Not on Plex right now');
  }
  if (sources.length === 1) {
    return h('a', { class: `btn btn--primary ${cls}`, href: sources[0].url, target: '_blank', rel: 'noopener' },
      icon('play'), 'Play on Plex');
  }

  const menuId = `play-${movie.id}-${Math.random().toString(36).slice(2, 7)}`;
  const menu = h('ul', { class: 'menu', id: menuId, hidden: true },
    sources.map((s) => h('li', null, h('a', { class: 'menu__item', href: s.url, target: '_blank', rel: 'noopener', onclick: () => toggle(false) },
      h('span', { text: s.server }),
      s.resolution ? h('span', { class: 'badge', text: RES[s.resolution] || s.resolution }) : null))));
  const button = h('button', {
    type: 'button', class: `btn btn--primary ${cls}`, 'aria-expanded': 'false', 'aria-controls': menuId,
    onclick: () => toggle(menu.hidden),
  }, icon('play'), 'Play on Plex', icon('chevron', 'icon--end'));
  const wrap = h('div', { class: `menu-wrap ${cls}` }, button, menu);

  function onOutside(e) { if (!wrap.contains(e.target)) toggle(false); }
  function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); toggle(false); button.focus(); } }
  function toggle(open) {
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) {
      document.addEventListener('pointerdown', onOutside);
      wrap.addEventListener('keydown', onKey);
      menu.querySelector('a')?.focus();
    } else {
      document.removeEventListener('pointerdown', onOutside);
      wrap.removeEventListener('keydown', onKey);
    }
  }
  return wrap;
}
