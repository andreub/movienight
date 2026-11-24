import { hydrateIcons } from './dom.js';
import { initTvNav } from './tvnav.js';
import { onRoute, start, go, href, parse } from './state.js';
import { getChatStatus } from './api.js';
import { subscribe, refreshStatus, syncPercent } from './status.js';
import { seerrReturn } from './seerr.js';
import { initDetail, openDetail, openFilm, closeDetail } from './views/detail.js';
import * as tonight from './views/tonight.js';
import * as browse from './views/browse.js';
import * as ask from './views/ask.js';
import * as taste from './views/taste.js';
import * as setup from './views/setup.js';
import * as seenView from './views/seen.js';
import * as complete from './views/complete.js';

const VIEWS = { tonight, browse, complete, ask, taste, setup, seen: seenView };
const main = document.getElementById('main');
const tabs = [...document.querySelectorAll('.tab')];
const pill = document.querySelector('.sync-pill');
const pillText = pill.querySelector('.sync-pill__text');

let current = { name: null, instance: null };
let status = null;

hydrateIcons();
initTvNav();
seerrReturn(); // back from connecting Seerr through Plex (?seerr=connected|failed)

// The Ask tab only exists when the server has an AI key configured.
const askTab = tabs.find((t) => t.dataset.view === 'ask');
getChatStatus().then((c) => { askTab.hidden = !c.enabled; });

initDetail(() => {
  // Back closes the sheet when we opened it ourselves; otherwise just drop the param.
  if (history.state?.detail) history.back();
  else go({ movie: null }, { replace: true });
});

// The Complete tab exists unless the server turned it off (MOVIENIGHT_COMPLETIONIST=0).
const completeTab = tabs.find((t) => t.dataset.view === 'complete');
const completionist = (s) => s?.features?.completionist !== false;

function render(route) {
  if (status && !status.ready && ['tonight', 'browse', 'complete', 'seen'].includes(route.view)) {
    go({ view: 'setup', movie: null }, { replace: true });
    return;
  }
  if (route.view === 'complete' && status && !completionist(status)) {
    go({ view: 'tonight', movie: null }, { replace: true });
    return;
  }
  if (route.view !== current.name) {
    const firstRender = current.name === null;
    current.instance?.destroy();
    main.replaceChildren();
    main.dataset.view = route.view;
    current = { name: route.view, instance: VIEWS[route.view].mount(main, route) };
    document.title = `${VIEWS[route.view].title} · MovieNight`;
    if (!firstRender) {
      window.scrollTo(0, 0);
      main.focus({ preventScroll: true });
    }
  } else {
    current.instance.update(route);
  }

  for (const tab of tabs) {
    tab.href = href(tab.dataset.view, route.filters);
    if (tab.dataset.view === route.view) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  }

  if (route.movie) openDetail(route.movie);
  else if (route.film) openFilm(route.film);
  else closeDetail();
}

subscribe((s) => {
  const wasReady = status?.ready;
  status = s;
  if (!s) return;
  completeTab.hidden = !completionist(s);
  const running = s.sync?.running;
  pill.hidden = !running;
  if (running) {
    const pct = syncPercent(s.sync);
    pillText.textContent = pct != null ? `Syncing ${pct}%` : 'Syncing';
  }
  // Before the first render, start() below takes care of it.
  if (current.name && wasReady !== s.ready) render(parse());
});

// Status decides between onboarding and the app, so wait for it (briefly) before the first render.
await Promise.race([refreshStatus(), new Promise((r) => setTimeout(r, 1500))]);
onRoute(render);
start();
