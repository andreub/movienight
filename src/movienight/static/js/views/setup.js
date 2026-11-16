import { api, getChatStatus } from '../api.js';
import { h, fill, icon, num, ago, toast, errorBox } from '../dom.js';
import { subscribe, refreshStatus, STAGES, syncPercent, serverName, serverStatus } from '../status.js';

export const title = 'Setup';

const LETTERBOXD_EXPORT = 'https://letterboxd.com/settings/data/';
const PIN_INTERVAL = 2000;
const PIN_TIMEOUT = 5 * 60 * 1000;

export function mount(root) {
  const intro = h('div', { class: 'setup__intro' });
  const statusError = h('div');
  const lb = letterboxdStep();
  const plex = plexStep();
  const jf = jellyfinStep();       // shown instead of the Plex step when the server selects Jellyfin
  jf.el.hidden = true;
  const sync = syncStep();
  const chatNote = h('p', { class: 'note', hidden: true });
  getChatStatus().then((c) => {
    fill(chatNote, icon(c.enabled ? 'check' : 'info'), h('span', { text: c.enabled ? `AI chat is on${c.model ? ` (${c.model})` : ''}.` : 'AI chat is off (no API key configured).' }));
    chatNote.hidden = false;
  });
  const triage = h('section', { class: 'triage-link', hidden: true, 'aria-labelledby': 'triage-link-title' },
    h('h2', { class: 'triage-link__title', id: 'triage-link-title', text: 'Films you have already seen' }),
    h('p', { class: 'help', text: 'Your Letterboxd history only goes back so far. Quickly mark the classics you know and they stop being suggested.' }),
    h('a', { class: 'btn btn--ghost', href: '#/seen' }, icon('eye'), 'Seen these?'));
  const me = h('p', { class: 'note', hidden: true });
  api.me().then((u) => {
    if (!u?.auth) return;
    fill(me, icon('check'), h('span', { text: u.name ? `Signed in as ${u.name}.` : 'Signed in.' }), h('a', { href: '/logout', text: 'Sign out' }));
    me.hidden = false;
  }).catch(() => {});
  root.append(h('div', { class: 'setup' }, intro, statusError,
    h('ol', { class: 'steps' }, lb.el, plex.el, jf.el, sync.el), triage, chatNote, me));

  let wasReady = null;
  const unsubscribe = subscribe((s, err) => {
    fill(statusError, err ? errorBox(err, refreshStatus) : '');
    if (!s) return;
    if (wasReady !== s.ready) renderIntro(s.ready, wasReady === false, s);
    wasReady = s.ready;
    triage.hidden = !s.ready;
    lb.update(s);
    const isJf = s.media_server === 'jellyfin';
    plex.el.hidden = isJf;
    jf.el.hidden = !isJf;
    if (isJf) jf.update(s); else plex.update(s);
    sync.update(s);
  });

  function renderIntro(ready, justBecameReady, st) {
    const sources = st?.media_server === 'jellyfin' ? 'your Jellyfin server' : 'your Plex servers';
    fill(intro, 
      h('h1', { class: 'page-title', text: ready ? 'Setup' : 'Set up movie night' }),
      ready
        ? h('p', { class: 'lede', text: 'Your sources and sync. Changes here update your picks after the next sync.' })
        : h('p', { class: 'lede', text: `Connect your Letterboxd history and ${sources}. MovieNight then ranks what you can watch right now by how well it fits your taste.` }),
      justBecameReady
        ? h('div', { class: 'banner', role: 'status' }, icon('check'), h('span', { text: "You're all set." }), h('a', { class: 'btn btn--primary', href: '#/tonight' }, "See tonight's pick"))
        : null);
  }

  return {
    update() {},
    destroy() { unsubscribe(); plex.destroy(); },
  };
}

function step(n, title, desc) {
  const badge = h('span', { class: 'step__badge' });
  const content = h('div', { class: 'step__content' });
  const el = h('li', { class: 'step' },
    h('div', { class: 'step__head' },
      h('span', { class: 'step__num', 'aria-hidden': 'true', text: String(n) }),
      h('h2', { class: 'step__title', text: title }),
      badge),
    h('p', { class: 'step__desc', text: desc }),
    content);
  return {
    el, content,
    setDone(done, label) {
      el.classList.toggle('is-done', done);
      fill(badge, done ? icon('check') : '', label);
    },
  };
}

function counts(pairs) {
  return h('dl', { class: 'counts' }, pairs.map(([k, v]) => h('div', null, h('dt', { text: k }), h('dd', { text: num(v) }))));
}

// ---- 1. Letterboxd ----

function letterboxdStep() {
  const s = step(1, 'Letterboxd', 'Your ratings, likes and watchlist teach MovieNight what you enjoy.');
  const summary = h('div');
  const input = h('input', { class: 'input', id: 'lb-user', name: 'username', autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false', required: true });
  let dirty = false;
  input.addEventListener('input', () => { dirty = true; });
  const save = h('button', { class: 'btn btn--ghost', type: 'submit' }, 'Save');
  const form = h('form', { class: 'inline-form', onsubmit: saveUser },
    h('label', { class: 'field__label', for: 'lb-user', text: 'Letterboxd username' }),
    h('div', { class: 'inline-form__row' }, input, save),
    h('p', { class: 'help', text: 'Used to pick up new diary entries automatically from your public RSS feed.' }));

  const file = h('input', { type: 'file', accept: '.zip,application/zip', class: 'sr-only', id: 'lb-file', onchange: () => file.files[0] && upload(file.files[0]) });
  const dropText = h('span', { class: 'drop__text' }, h('strong', { text: 'Drop your export ZIP here' }), h('span', { text: 'or choose a file' }));
  const drop = h('label', { class: 'drop', for: 'lb-file' }, icon('upload', 'drop__icon'), dropText);
  for (const t of ['dragenter', 'dragover']) drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('is-over'); });
  for (const t of ['dragleave', 'drop']) drop.addEventListener(t, () => drop.classList.remove('is-over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) upload(f); });

  s.content.append(summary, form,
    h('div', { class: 'upload' },
      h('p', { class: 'field__label', text: 'Import your full history' }),
      file, drop,
      h('p', { class: 'help' }, 'Download it from ', h('a', { href: LETTERBOXD_EXPORT, target: '_blank', rel: 'noopener', text: 'Letterboxd settings, Data tab' }), '. Upload a new one any time to refresh everything.')));

  async function saveUser(e) {
    e.preventDefault();
    const username = input.value.trim();
    if (!username) return;
    save.disabled = true;
    try {
      await api.letterboxdUser(username);
      dirty = false;
      toast('Letterboxd username saved.');
      refreshStatus();
    } catch (err) { toast(err.detail); }
    save.disabled = false;
  }

  async function upload(f) {
    if (!/\.zip$/i.test(f.name)) { toast('That file is not a ZIP. Upload the export exactly as Letterboxd sends it.'); return; }
    drop.classList.add('is-busy');
    fill(dropText, h('strong', { text: `Importing ${f.name}…` }));
    try {
      const r = await api.letterboxdImport(f);
      toast(`Imported ${num(r.watched)} watched, ${num(r.rated)} rated, ${num(r.liked)} liked and ${num(r.watchlist)} watchlist films.`);
      refreshStatus();
    } catch (err) { toast(err.detail, { timeout: 10000 }); }
    drop.classList.remove('is-busy');
    fill(dropText, h('strong', { text: 'Drop your export ZIP here' }), h('span', { text: 'or choose a file' }));
    file.value = '';
  }

  return {
    el: s.el,
    update(st) {
      const l = st.letterboxd || {};
      const has = (l.watched || 0) > 0;
      s.setDone(has, has ? 'Connected' : 'Needed');
      fill(summary, has
        ? h('div', null,
          counts([['Watched', l.watched], ['Rated', l.rated], ['Liked', l.liked], ['Watchlist', l.watchlist]]),
          h('p', { class: 'help', text: `Last updated ${ago(l.last_sync)}.` }))
        : '');
      if (!dirty && document.activeElement !== input) input.value = l.username || '';
    },
  };
}

// ---- 2. Plex ----

function plexStep() {
  const s = step(2, 'Plex', 'MovieNight only suggests films you can play right now on your servers, including ones shared with you.');
  let poll = null;
  let authWindow = null;

  const connect = h('button', { class: 'btn btn--primary', type: 'button', onclick: startPin }, 'Connect Plex');
  const pinArea = h('div', { class: 'pin', 'aria-live': 'polite' });
  const body = h('div');
  s.content.append(body, pinArea);

  function stopPolling() { clearTimeout(poll); poll = null; }

  async function startPin() {
    // Open the window during the click so popup blockers allow it; point it at Plex once we have the PIN.
    authWindow = window.open('', 'movienight-plex');
    connect.disabled = true;
    fill(pinArea, h('p', { class: 'help', text: 'Asking Plex for a sign-in code…' }));
    let pin;
    try {
      pin = await api.plexPin();
    } catch (err) {
      authWindow?.close();
      connect.disabled = false;
      fill(pinArea, errorBox(err, startPin));
      return;
    }
    if (authWindow && !authWindow.closed) authWindow.location.href = pin.auth_url;
    fill(pinArea, 
      h('p', { class: 'pin__wait' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Waiting for you to sign in to Plex in the other tab…'),
      h('p', { class: 'help' }, 'Code ', h('code', { text: pin.code }), '. Tab didn\'t open? ',
        h('a', { href: pin.auth_url, target: '_blank', rel: 'noopener', text: 'Open Plex sign-in' }), '.'),
      h('button', { class: 'linkbtn', type: 'button', onclick: cancel }, 'Cancel'));
    const deadline = Date.now() + PIN_TIMEOUT;

    const check = async () => {
      try {
        const r = await api.plexPinCheck(pin.id);
        if (r.authorized) {
          stopPolling();
          authWindow?.close();
          fill(pinArea, );
          connect.disabled = false;
          toast('Plex connected. Syncing your libraries now.');
          refreshStatus();
          return;
        }
      } catch (err) {
        if (err.status && err.status < 500) {
          stopPolling();
          connect.disabled = false;
          fill(pinArea, errorBox(err, startPin));
          return;
        }
      }
      if (Date.now() > deadline) {
        stopPolling();
        connect.disabled = false;
        fill(pinArea, h('p', { class: 'help', role: 'alert', text: 'Sign-in timed out after 5 minutes. Press Connect Plex to try again.' }));
        return;
      }
      poll = setTimeout(check, PIN_INTERVAL);
    };
    poll = setTimeout(check, PIN_INTERVAL);
  }

  function cancel() {
    stopPolling();
    connect.disabled = false;
    fill(pinArea, );
  }

  async function logout() {
    if (!confirm('Sign out of Plex? MovieNight forgets your servers and library until you connect again.')) return;
    try {
      await api.plexLogout();
      toast('Signed out of Plex.');
      refreshStatus();
    } catch (err) { toast(err.detail); }
  }

  function serverRow(srv) {
    const toggle = h('button', {
      type: 'button', role: 'switch', class: 'switch', 'aria-checked': String(srv.enabled), 'aria-label': `Use ${srv.name}`,
      onclick: async () => {
        const next = toggle.getAttribute('aria-checked') !== 'true';
        toggle.setAttribute('aria-checked', String(next));
        try {
          await api.plexServer(srv.id, next);
          refreshStatus();
        } catch (err) {
          toggle.setAttribute('aria-checked', String(!next));
          toast(err.detail);
        }
      },
    });
    return h('li', { class: 'server' },
      h('div', { class: 'server__body' },
        h('p', { class: 'server__name', text: srv.name }),
        h('p', { class: 'server__meta' },
          srv.owned ? 'Your server' : 'Shared with you',
          `, ${num(srv.movies)} films`,
          srv.reachable === false ? h('span', { class: 'warn' }, icon('alert'), ' Not reachable right now') : null)),
      toggle);
  }

  return {
    el: s.el,
    update(st) {
      const p = st.plex || {};
      s.setDone(Boolean(p.connected), p.connected ? 'Connected' : 'Needed');
      if (!p.connected) {
        if (!body.contains(connect)) fill(body, connect);
        return;
      }
      stopPolling();
      fill(pinArea, );
      const servers = p.servers || [];
      fill(body, 
        h('p', { class: 'help' },
          p.account ? ['Signed in as ', h('strong', { text: p.account }), '. '] : null,
          `${num(p.movies)} films across ${servers.length} server${servers.length === 1 ? '' : 's'}. Last read ${ago(p.last_sync)}.`),
        servers.length
          ? h('ul', { class: 'servers', 'aria-label': 'Plex servers' }, servers.map(serverRow))
          : h('p', { class: 'help', text: 'No servers found on this Plex account yet.' }),
        h('button', { class: 'linkbtn', type: 'button', onclick: logout }, 'Sign out of Plex'));
    },
    destroy: stopPolling,
  };
}

// ---- 2. Jellyfin (read-only: the connection comes from server variables, not from a form) ----

function jellyfinStep() {
  const s = step(2, 'Jellyfin', 'MovieNight only suggests films you can play right now on your Jellyfin server.');
  const body = h('div');
  s.content.append(body);

  async function syncNow(e) {
    e.currentTarget.disabled = true;
    try {
      await api.sync('jellyfin');
    } catch (err) {
      toast(err.status === 409 ? 'A sync is already running.' : err.detail);
    }
    refreshStatus();
  }

  return {
    el: s.el,
    update(st) {
      const j = st.jellyfin || {};
      s.setDone(Boolean(j.connected), j.connected ? 'Connected' : j.configured ? 'Not connected' : 'Needed');
      fill(body,
        j.connected
          ? h('p', { class: 'help' },
            j.account ? ['Signed in as ', h('strong', { text: j.account }), '. '] : null,
            `${num(j.movies)} films. Last read ${ago(j.last_sync)}.`)
          : null,
        j.problem ? h('div', { class: 'note note--error', role: 'alert' }, icon('alert'), h('span', { text: j.problem })) : null,
        j.configured
          ? (st.sync?.running ? null : h('button', { class: 'btn btn--ghost', type: 'button', onclick: syncNow }, 'Sync now'))
          : h('p', { class: 'help' }, 'Set ', h('code', { text: 'MOVIENIGHT_JELLYFIN_URL' }), ', ',
            h('code', { text: 'MOVIENIGHT_JELLYFIN_USER' }), ' and ', h('code', { text: 'MOVIENIGHT_JELLYFIN_PASSWORD' }),
            ' in the server environment and restart.'));
    },
  };
}

// ---- 3. Sync ----

function syncStep() {
  const s = step(3, 'Sync', 'MovieNight refreshes everything every few hours. Run it now after a big change.');
  const body = h('div');
  s.content.append(body);
  async function start(e) {
    e.currentTarget.disabled = true;
    try {
      await api.sync('all');
    } catch (err) {
      toast(err.status === 409 ? 'A sync is already running.' : err.detail);
    }
    refreshStatus();
  }

  return {
    el: s.el,
    update(st) {
      const sync = st.sync || {};
      const tmdb = st.tmdb || {};
      const pct = syncPercent(sync);
      s.setDone(Boolean(st.ready), st.ready ? 'Ready' : sync.running ? 'Running' : 'Waiting');

      const progress = sync.running
        ? h('div', { class: 'progress' },
          h('p', { class: 'progress__label', id: 'sync-label' }, STAGES[sync.stage] || 'Syncing', pct != null ? h('span', { text: ` ${num(sync.done)} of ${num(sync.total)}` }) : null),
          h('div', {
            class: `progress__track ${pct == null ? 'is-indeterminate' : ''}`, role: 'progressbar', 'aria-labelledby': 'sync-label',
            'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': pct != null ? String(pct) : null,
          }, h('span', { class: 'progress__bar', style: { '--v': `${pct ?? 30}%` } })))
        : h('button', { class: 'btn btn--primary', type: 'button', onclick: start }, 'Sync now');

      fill(body, 
        progress,
        sync.error ? h('div', { class: 'note note--error', role: 'alert' }, icon('alert'), h('span', { text: sync.error })) : null,
        h('dl', { class: 'facts facts--compact' },
          h('div', null, h('dt', { text: serverName(st) }), h('dd', { text: `read ${ago(serverStatus(st).last_sync)}` })),
          h('div', null, h('dt', { text: 'Letterboxd' }), h('dd', { text: `read ${ago(st.letterboxd?.last_sync)}` })),
          serverStatus(st).seen_pushed != null ? h('div', null, h('dt', { text: 'Seen marks' }), h('dd', { text: `${num(serverStatus(st).seen_pushed)} pushed to ${serverName(st)}` })) : null,
          tmdb.configured ? h('div', null, h('dt', { text: 'TMDB' }), h('dd', { text: `details for ${num(tmdb.enriched)} films${tmdb.pending ? `, ${num(tmdb.pending)} to go` : ''}` })) : null),
        tmdb.configured ? null : h('p', { class: 'note' }, icon('info'),
          h('span', null, `TMDB is not set up, so recommendations use ${serverName(st)} metadata only and are less precise. Add `,
            h('code', { text: 'MOVIENIGHT_TMDB_TOKEN' }), ' to the server environment to enable it.')));
    },
  };
}
