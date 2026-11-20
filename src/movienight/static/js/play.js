// "Play on Plex" (or Jellyfin): a plain link for one server, a small chooser for several.
// With Plex players on the account (e.g. a TV), the control becomes a menu: "Play on <player>" starts the
// movie there, "Open in Plex Web" keeps the old link. `movie.plex` holds the selected media server's entries.
import { h, icon } from './dom.js';
import { api, getPlayers } from './api.js';
import { serverName } from './status.js';

const RES = { '4k': '4K', 1080: '1080p', 720: '720p', sd: 'SD' };
const LAST_KEY = 'movienight.lastPlayer';

function lastPlayer() {
  try { return localStorage.getItem(LAST_KEY) || ''; } catch { return ''; }
}
function rememberPlayer(id) {
  try { localStorage.setItem(LAST_KEY, id); } catch { /* storage unavailable */ }
}

// MovieNightTV: the Android TV app's WebView (its UA usually says Android too); its Plex links go to the app.
const TV_APP = /MovieNightTV/.test(navigator.userAgent);
const MOBILE = TV_APP || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
  || (navigator.maxTouchPoints > 1 && /Macintosh/.test(navigator.userAgent));

// On mobile Plex entries carry `mobile_url`, a watch.plex.tv universal link: the Plex app opens on the movie's
// page (without the app it is just a web page, so no fallback is needed). The link only opens the app on a real
// user tap on an <a> to another domain, which is why these stay plain links (target=_blank is fine on iOS and Android).
const href = (s) => (MOBILE && s.mobile_url) || s.url;

// In the Android TV app a plex:// deep link opens the film straight in the native Plex app, on the right server
// (it honours the server id and ignores the slug). Jellyfin sources (server id "jf:…") and sources without ids
// keep the web link. The app catches plex: navigations itself, so those links open in place, not in a new window.
function tvHref(s) {
  if (!s.server_id || !s.rating_key || String(s.server_id).startsWith('jf:')) return s.mobile_url || s.url;
  let slug = '';
  try { slug = new URL(s.mobile_url).pathname.split('/').filter(Boolean).pop() || ''; } catch { /* no mobile_url */ }
  return `plex://libraries/${encodeURIComponent(s.server_id)}/movie/${slug || 'movie'}/${encodeURIComponent(s.rating_key)}`;
}
const playHref = (s) => (TV_APP ? tvHref(s) : href(s));
const playTarget = (url) => (url.startsWith('plex:') ? null : '_blank');

export function playControl(movie, cls = '') {
  const sources = movie.plex || [];
  if (!sources.length) {
    return h('button', { type: 'button', class: `btn btn--primary ${cls}`, disabled: true }, icon('play'), `Not on ${serverName()} right now`);
  }
  const holder = h('div', { class: 'play-control', style: { display: 'contents' } }, linkControl(sources, cls));
  // In the TV app we are the TV: Play opens the film in the Plex app, never a cast menu.
  if (TV_APP) return holder;
  // Players are looked up lazily, once per page load; until (or unless) some exist the plain control stays.
  getPlayers().then((players) => {
    if (players.length && sources.some((s) => s.rating_key)) holder.replaceChildren(playerMenu(movie, sources, players, cls));
  });
  return holder;
}

function linkControl(sources, cls) {
  if (sources.length === 1) {
    const url = playHref(sources[0]);
    return h('a', { class: `btn btn--primary ${cls}`, href: url, target: playTarget(url), rel: 'noopener' },
      icon('play'), `Play on ${serverName()}`);
  }
  return menuControl(sources, cls, (toggle) => sources.map((s) => webItem(s, toggle, true)),
    [icon('play'), `Play on ${serverName()}`, icon('chevron', 'icon--end')]);
}

function webItem(s, toggle, withServerName) {
  const url = playHref(s);
  return h('li', null, h('a', { class: 'menu__item', href: url, target: playTarget(url), rel: 'noopener', onclick: () => toggle(false) },
    h('span', { text: withServerName ? s.server : `Open in ${serverName()} Web` }),
    s.resolution ? h('span', { class: 'badge', text: RES[s.resolution] || s.resolution }) : null));
}

function playerMenu(movie, sources, players, cls) {
  const last = lastPlayer();
  const ordered = [...players].sort((a, b) => (b.id === last) - (a.id === last));
  const playable = sources.filter((s) => s.rating_key);
  const several = sources.length > 1;
  let busy = false;
  let button;

  const label = () => [icon('play'), `Play on ${serverName()}`, icon('chevron', 'icon--end')];
  const say = (text) => { button.replaceChildren(icon('play'), text); };

  async function start(player, source, toggle) {
    toggle(false);
    if (busy) return;
    busy = true;
    say(`Starting on ${player.name}…`);
    try {
      await api.play(player.id, source.server_id, source.rating_key);
      rememberPlayer(player.id);
      say(`Playing on ${player.name}`);
    } catch (e) {
      say(e.status === 502 ? `Could not reach ${player.name}` : `Could not start on ${player.name}`);
    }
    setTimeout(() => { busy = false; button.replaceChildren(...label()); }, 4000);
  }

  const wrapAndButton = menuControl(sources, cls, (toggle) => [
    ...ordered.flatMap((p) => playable.map((s) => h('li', null,
      h('button', { type: 'button', class: 'menu__item', onclick: () => start(p, s, toggle) },
        h('span', { text: `Play on ${p.name}${several ? ` (${s.server})` : ''}` }),
        s.resolution ? h('span', { class: 'badge', text: RES[s.resolution] || s.resolution }) : null)))),
    ...sources.map((s) => webItem(s, toggle, false)).map((li, i) => {
      if (several) li.querySelector('span').textContent = `Open in ${serverName()} Web (${sources[i].server})`;
      return li;
    }),
  ], label());
  button = wrapAndButton.querySelector('button.btn');
  return wrapAndButton;
}

// A button that opens a menu (the existing pattern: Escape / outside click close it, focus moves in).
function menuControl(sources, cls, items, content) {
  const menuId = `play-${Math.random().toString(36).slice(2, 9)}`;
  const menu = h('ul', { class: 'menu', id: menuId, hidden: true }, items((open) => toggle(open)));
  const button = h('button', {
    type: 'button', class: `btn btn--primary ${cls}`, 'aria-expanded': 'false', 'aria-controls': menuId,
    onclick: () => toggle(menu.hidden),
  }, content);
  const wrap = h('div', { class: `menu-wrap ${cls}` }, button, menu);

  function onOutside(e) { if (!wrap.contains(e.target)) toggle(false); }
  function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); e.preventDefault(); toggle(false); button.focus(); } }
  function toggle(open) {
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) {
      document.addEventListener('pointerdown', onOutside);
      wrap.addEventListener('keydown', onKey);
      menu.querySelector('a, button')?.focus();
    } else {
      document.removeEventListener('pointerdown', onOutside);
      wrap.removeEventListener('keydown', onKey);
    }
  }
  return wrap;
}
