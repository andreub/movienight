import { hydrateIcons } from './dom.js';
import { onRoute, start, go, href, parse } from './state.js';
import { subscribe, refreshStatus, syncPercent } from './status.js';
import { initDetail, openDetail, closeDetail } from './views/detail.js';
import * as tonight from './views/tonight.js';
import * as browse from './views/browse.js';
import * as taste from './views/taste.js';
import * as setup from './views/setup.js';

const VIEWS = { tonight, browse, taste, setup };
const main = document.getElementById('main');
const tabs = [...document.querySelectorAll('.tab')];
const pill = document.querySelector('.sync-pill');
const pillText = pill.querySelector('.sync-pill__text');

let current = { name: null, instance: null };
let status = null;

hydrateIcons();

initDetail(() => {
  // Back closes the sheet when we opened it ourselves; otherwise just drop the param.
  if (history.state?.detail) history.back();
  else go({ movie: null }, { replace: true });
});

function render(route) {
  if (status && !status.ready && (route.view === 'tonight' || route.view === 'browse')) {
    go({ view: 'setup', movie: null }, { replace: true });
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
  else closeDetail();
}

subscribe((s) => {
  const wasReady = status?.ready;
  status = s;
  if (!s) return;
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
