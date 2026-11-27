import { api, getChatStatus } from '../api.js';
import { h, fill, icon, num, ago, toast, errorBox } from '../dom.js';
import { subscribe, refreshStatus, STAGES, syncPercent, serverName, serverStatus } from '../status.js';
import { seerrAccountLine } from '../seerr.js';
import { t, lang, LANGS, setLang } from '../i18n.js';

export const title = t('tab.setup');

const LETTERBOXD_EXPORT = 'https://letterboxd.com/settings/data/';
const PIN_INTERVAL = 2000;
const PIN_TIMEOUT = 5 * 60 * 1000;

// TMDB asks for this exact notice and its logo wherever its data is used.
function credits() {
  return h('footer', { class: 'credits' },
    h('a', { href: 'https://www.themoviedb.org', target: '_blank', rel: 'noopener', 'aria-label': 'The Movie Database (TMDB)' },
      h('img', { src: '/static/tmdb.svg', alt: 'TMDB', width: 120, height: 16 })),
    h('p', { class: 'help', text: t('setup.tmdbNotice') }),
    h('p', { class: 'help', text: t('setup.trademarks') }),
    h('p', { class: 'help' }, `${t('setup.free')} `,
      h('a', { href: 'https://github.com/andreub/movienight', target: '_blank', rel: 'noopener', text: t('setup.source') })));
}

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
    fill(chatNote, icon(c.enabled ? 'check' : 'info'), h('span', { text: c.enabled ? (c.model ? t('setup.chatOnModel', { model: c.model }) : t('setup.chatOn')) : t('setup.chatOff') }));
    chatNote.hidden = false;
  });
  const triage = h('section', { class: 'triage-link', hidden: true, 'aria-labelledby': 'triage-link-title' },
    h('h2', { class: 'triage-link__title', id: 'triage-link-title', text: t('setup.triage.title') }),
    h('p', { class: 'help', text: t('setup.triage.text') }),
    h('a', { class: 'btn btn--ghost', href: '#/seen' }, icon('eye'), t('seen.title')));
  const me = h('p', { class: 'note', hidden: true });
  api.me().then((u) => {
    if (!u?.auth) return;
    fill(me, icon('check'), h('span', { text: u.name ? t('setup.signedInAs', { name: u.name }) : t('setup.signedIn') }), h('a', { href: '/logout', text: t('setup.signOut') }));
    me.hidden = false;
  }).catch(() => {});
  root.append(h('div', { class: 'setup' }, intro, statusError,
    h('ol', { class: 'steps' }, lb.el, plex.el, jf.el, sync.el), triage, chatNote, me, credits()));

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
    fill(intro, 
      h('div', { class: 'setup__title' }, h('h1', { class: 'page-title', text: ready ? t('tab.setup') : t('setup.h1.new') }), langSwitch()),
      ready
        ? h('p', { class: 'lede', text: t('setup.lede') })
        : h('p', { class: 'lede', text: st?.media_server === 'jellyfin' ? t('setup.lede.new.jellyfin') : t('setup.lede.new.plex') }),
      justBecameReady
        ? h('div', { class: 'banner', role: 'status' }, icon('check'), h('span', { text: t('setup.allSet') }), h('a', { class: 'btn btn--primary', href: '#/tonight' }, t('setup.seePick')))
        : null);
  }

  return {
    update() {},
    destroy() { unsubscribe(); plex.destroy(); },
  };
}

// English / Español, beside the page title (the existing small segmented control). Remembered on this device.
function langSwitch() {
  return h('fieldset', { class: 'seg seg--small setup__lang' }, h('legend', { class: 'sr-only', text: t('setup.language') }),
    LANGS.map(([id, name]) => h('label', { class: 'seg__item' },
      h('input', { type: 'radio', name: 'lang', value: id, class: 'seg__input', checked: id === lang, lang: id, onchange: () => setLang(id) }),
      h('span', { lang: id, text: name }))));
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
  const s = step(1, 'Letterboxd', t('setup.lb.desc'));
  const summary = h('div');
  const input = h('input', { class: 'input', id: 'lb-user', name: 'username', autocomplete: 'off', autocapitalize: 'none', spellcheck: 'false', required: true });
  let dirty = false;
  input.addEventListener('input', () => { dirty = true; });
  const save = h('button', { class: 'btn btn--ghost', type: 'submit' }, t('common.save'));
  const form = h('form', { class: 'inline-form', onsubmit: saveUser },
    h('label', { class: 'field__label', for: 'lb-user', text: t('setup.lb.user') }),
    h('div', { class: 'inline-form__row' }, input, save),
    h('p', { class: 'help', text: t('setup.lb.userHelp') }));

  const file = h('input', { type: 'file', accept: '.zip,application/zip', class: 'sr-only', id: 'lb-file', onchange: () => file.files[0] && upload(file.files[0]) });
  const dropText = h('span', { class: 'drop__text' }, h('strong', { text: t('setup.lb.drop') }), h('span', { text: t('setup.lb.choose') }));
  const drop = h('label', { class: 'drop', for: 'lb-file' }, icon('upload', 'drop__icon'), dropText);
  for (const t of ['dragenter', 'dragover']) drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('is-over'); });
  for (const t of ['dragleave', 'drop']) drop.addEventListener(t, () => drop.classList.remove('is-over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) upload(f); });

  s.content.append(summary, form,
    h('div', { class: 'upload' },
      h('p', { class: 'field__label', text: t('setup.lb.import') }),
      file, drop,
      h('p', { class: 'help' }, t('setup.lb.download'), h('a', { href: LETTERBOXD_EXPORT, target: '_blank', rel: 'noopener', text: t('setup.lb.settings') }), t('setup.lb.downloadEnd'))));

  async function saveUser(e) {
    e.preventDefault();
    const username = input.value.trim();
    if (!username) return;
    save.disabled = true;
    try {
      await api.letterboxdUser(username);
      dirty = false;
      toast(t('setup.lb.saved'));
      refreshStatus();
    } catch (err) { toast(err.detail); }
    save.disabled = false;
  }

  async function upload(f) {
    if (!/\.zip$/i.test(f.name)) { toast(t('setup.lb.notZip')); return; }
    drop.classList.add('is-busy');
    fill(dropText, h('strong', { text: t('setup.lb.importing', { name: f.name }) }));
    try {
      const r = await api.letterboxdImport(f);
      toast(t('setup.lb.imported', { watched: num(r.watched), rated: num(r.rated), liked: num(r.liked), watchlist: num(r.watchlist) }));
      refreshStatus();
    } catch (err) { toast(err.detail, { timeout: 10000 }); }
    drop.classList.remove('is-busy');
    fill(dropText, h('strong', { text: t('setup.lb.drop') }), h('span', { text: t('setup.lb.choose') }));
    file.value = '';
  }

  return {
    el: s.el,
    update(st) {
      const l = st.letterboxd || {};
      const has = (l.watched || 0) > 0;
      s.setDone(has, has ? t('setup.connected') : t('setup.needed'));
      fill(summary, has
        ? h('div', null,
          counts([[t('setup.lb.watched'), l.watched], [t('taste.rated'), l.rated], [t('taste.liked'), l.liked], [t('setup.lb.watchlist'), l.watchlist]]),
          h('p', { class: 'help', text: t('setup.lastUpdated', { ago: ago(l.last_sync) }) }))
        : '');
      if (!dirty && document.activeElement !== input) input.value = l.username || '';
    },
  };
}

// ---- 2. Plex ----

function plexStep() {
  const s = step(2, 'Plex', t('setup.plex.desc'));
  let poll = null;
  let authWindow = null;

  const connect = h('button', { class: 'btn btn--primary', type: 'button', onclick: startPin }, t('setup.plex.connect'));
  const pinArea = h('div', { class: 'pin', 'aria-live': 'polite' });
  const body = h('div');
  s.content.append(body, pinArea);

  let alive = true;
  function stopPolling() { clearTimeout(poll); poll = null; }

  async function startPin() {
    // Open the window during the click so popup blockers allow it; point it at Plex once we have the PIN.
    authWindow = window.open('', 'movienight-plex');
    connect.disabled = true;
    fill(pinArea, h('p', { class: 'help', text: t('setup.plex.asking') }));
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
      h('p', { class: 'pin__wait' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), t('setup.plex.waiting')),
      h('p', { class: 'help' }, t('setup.plex.code'), h('code', { text: pin.code }), t('setup.plex.noTab'),
        h('a', { href: pin.auth_url, target: '_blank', rel: 'noopener', text: t('setup.plex.open') }), '.'),
      h('button', { class: 'linkbtn', type: 'button', onclick: cancel }, t('common.cancel')));
    const deadline = Date.now() + PIN_TIMEOUT;

    const check = async () => {
      try {
        const r = await api.plexPinCheck(pin.id);
        if (!alive) return;
        if (r.authorized) {
          stopPolling();
          authWindow?.close();
          fill(pinArea, );
          connect.disabled = false;
          toast(t('setup.plex.connected'));
          refreshStatus();
          return;
        }
      } catch (err) {
        if (!alive) return;
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
        fill(pinArea, h('p', { class: 'help', role: 'alert', text: t('setup.plex.timeout') }));
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
    if (!confirm(t('setup.plex.confirmOut'))) return;
    try {
      await api.plexLogout();
      toast(t('setup.plex.signedOut'));
      refreshStatus();
    } catch (err) { toast(err.detail); }
  }

  function serverRow(srv) {
    const toggle = h('button', {
      type: 'button', role: 'switch', class: 'switch', 'aria-checked': String(srv.enabled), 'aria-label': t('setup.plex.use', { name: srv.name }),
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
          srv.owned ? t('setup.plex.yours') : t('setup.plex.shared'),
          `, ${t('count.films', { n: srv.movies, num: num(srv.movies) })}`,
          srv.reachable === false ? h('span', { class: 'warn' }, icon('alert'), ` ${t('setup.plex.unreachable')}`) : null)),
      toggle);
  }

  return {
    el: s.el,
    update(st) {
      const p = st.plex || {};
      s.setDone(Boolean(p.connected), p.connected ? t('setup.connected') : t('setup.needed'));
      if (!p.connected) {
        if (!body.contains(connect)) fill(body, connect);
        return;
      }
      stopPolling();
      fill(pinArea, );
      const servers = p.servers || [];
      fill(body, 
        h('p', { class: 'help' },
          p.account ? [t('setup.signedInAsPre'), h('strong', { text: p.account }), t('setup.signedInAsPost')] : null,
          t('setup.plex.summary', { films: t('count.films', { n: p.movies, num: num(p.movies) }), servers: t('count.servers', { n: servers.length, num: num(servers.length) }), ago: ago(p.last_sync) })),
        servers.length
          ? h('ul', { class: 'servers', 'aria-label': t('setup.plex.servers') }, servers.map(serverRow))
          : h('p', { class: 'help', text: t('setup.plex.noServers') }),
        h('button', { class: 'linkbtn', type: 'button', onclick: logout }, t('setup.plex.signOut')));
    },
    destroy() { alive = false; stopPolling(); },
  };
}

// ---- 2. Jellyfin (read-only: the connection comes from server variables, not from a form) ----

function jellyfinStep() {
  const s = step(2, 'Jellyfin', t('setup.jf.desc'));
  const body = h('div');
  s.content.append(body);

  async function syncNow(e) {
    e.currentTarget.disabled = true;
    try {
      await api.sync('jellyfin');
    } catch (err) {
      toast(err.status === 409 ? t('setup.sync.running') : err.detail);
    }
    refreshStatus();
  }

  return {
    el: s.el,
    update(st) {
      const j = st.jellyfin || {};
      s.setDone(Boolean(j.connected), j.connected ? t('setup.connected') : j.configured ? t('setup.notConnected') : t('setup.needed'));
      fill(body,
        j.connected
          ? h('p', { class: 'help' },
            j.account ? [t('setup.signedInAsPre'), h('strong', { text: j.account }), t('setup.signedInAsPost')] : null,
            t('setup.jf.summary', { films: t('count.films', { n: j.movies, num: num(j.movies) }), ago: ago(j.last_sync) }))
          : null,
        j.problem ? h('div', { class: 'note note--error', role: 'alert' }, icon('alert'), h('span', { text: j.problem })) : null,
        j.configured
          ? (st.sync?.running ? null : h('button', { class: 'btn btn--ghost', type: 'button', onclick: syncNow }, t('setup.sync.now')))
          : h('p', { class: 'help' }, t('setup.jf.set'), h('code', { text: 'MOVIENIGHT_JELLYFIN_URL' }), ', ',
            h('code', { text: 'MOVIENIGHT_JELLYFIN_USER' }), t('setup.jf.and'), h('code', { text: 'MOVIENIGHT_JELLYFIN_PASSWORD' }),
            t('setup.jf.restart')));
    },
  };
}

// ---- 3. Sync ----

function syncStep() {
  const s = step(3, t('setup.sync'), t('setup.sync.desc'));
  const body = h('div');
  s.content.append(body);
  let seerrLine = null; // this user's Seerr account (made once, kept across re-renders)
  async function start(e) {
    e.currentTarget.disabled = true;
    try {
      await api.sync('all');
    } catch (err) {
      toast(err.status === 409 ? t('setup.sync.running') : err.detail);
    }
    refreshStatus();
  }

  return {
    el: s.el,
    update(st) {
      const sync = st.sync || {};
      const tmdb = st.tmdb || {};
      const pct = syncPercent(sync);
      s.setDone(Boolean(st.ready), st.ready ? t('setup.sync.ready') : sync.running ? t('setup.sync.runningBadge') : t('setup.sync.waiting'));

      const progress = sync.running
        ? h('div', { class: 'progress' },
          h('p', { class: 'progress__label', id: 'sync-label' }, STAGES[sync.stage] || t('sync.pill'), pct != null ? h('span', { text: ` ${t('setup.sync.of', { done: num(sync.done), total: num(sync.total) })}` }) : null),
          h('div', {
            class: `progress__track ${pct == null ? 'is-indeterminate' : ''}`, role: 'progressbar', 'aria-labelledby': 'sync-label',
            'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': pct != null ? String(pct) : null,
          }, h('span', { class: 'progress__bar', style: { '--v': `${pct ?? 30}%` } })))
        : h('button', { class: 'btn btn--primary', type: 'button', onclick: start }, t('setup.sync.now'));

      fill(body, 
        progress,
        sync.error ? h('div', { class: 'note note--error', role: 'alert' }, icon('alert'), h('span', { text: sync.error })) : null,
        h('dl', { class: 'facts facts--compact' },
          h('div', null, h('dt', { text: serverName(st) }), h('dd', { text: t('setup.sync.read', { ago: ago(serverStatus(st).last_sync) }) })),
          h('div', null, h('dt', { text: 'Letterboxd' }), h('dd', { text: t('setup.sync.read', { ago: ago(st.letterboxd?.last_sync) }) })),
          serverStatus(st).seen_pushed != null ? h('div', null, h('dt', { text: t('setup.sync.seenMarks') }), h('dd', { text: t('setup.sync.pushed', { n: num(serverStatus(st).seen_pushed), server: serverName(st) }) })) : null,
          tmdb.configured ? h('div', null, h('dt', { text: 'TMDB' }), h('dd', { text: tmdb.pending ? t('setup.sync.tmdbPending', { n: tmdb.enriched, num: num(tmdb.enriched), pending: num(tmdb.pending) }) : t('setup.sync.tmdb', { n: tmdb.enriched, num: num(tmdb.enriched) }) })) : null,
          st.features?.seerr ? h('div', null, h('dt', { text: 'Seerr' }), seerrLine ??= seerrAccountLine()) : null),
        tmdb.configured ? null : h('p', { class: 'note' }, icon('info'),
          h('span', null, t('setup.tmdbOff', { server: serverName(st) }),
            h('code', { text: 'MOVIENIGHT_TMDB_TOKEN' }), t('setup.tmdbOffEnd'))));
    },
  };
}
