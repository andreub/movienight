// Ask: chat with the configured AI model, which recommends from the media server's library. Text is only ever added as text nodes.
import { api, getChatStatus } from '../api.js';
import { h, fill, icon, poster, runtime, matchStub, fixed } from '../dom.js';
import { chat, subscribeChat, sendChat, stopChat, clearChat, retryChat, stripShownNote } from '../chat.js';
import { playControl } from '../play.js';
import { endsAt } from '../clock.js';
import { go } from '../state.js';
import { serverName } from '../status.js';
import { t, lang } from '../i18n.js';

export const title = t('tab.ask');

const STARTERS = [
  t('ask.starter.tense'),
  t('ask.starter.comfort'),
  t('ask.starter.gem'),
  t('ask.starter.watchlist'),
];

// ---- safe rich text: paragraphs, line breaks, simple lists and **bold**, built as DOM nodes ----

function inline(text) {
  return text.split(/(\*\*[^*\n]+\*\*)/).map((part) => (/^\*\*[^*\n]+\*\*$/.test(part) ? h('strong', { text: part.slice(2, -2) }) : part));
}

export function richText(text) {
  const nodes = [];
  let para = null;
  let list = null;
  const flush = () => { para = null; list = null; };
  for (const raw of text.replace(/\r/g, '').split('\n')) {
    const line = raw.trimEnd();
    if (!line.trim()) { flush(); continue; }
    const item = line.match(/^\s*(?:([-*•])|(\d+)[.)])\s+(.*)$/);
    if (item) {
      const ordered = Boolean(item[2]);
      if (!list || list.tagName !== (ordered ? 'OL' : 'UL')) {
        para = null;
        list = h(ordered ? 'ol' : 'ul', { class: 'msg__list' });
        nodes.push(list);
      }
      list.append(h('li', null, inline(item[3])));
    } else {
      list = null;
      if (!para) { para = h('p'); nodes.push(para); } else para.append(h('br'));
      para.append(...inline(line.trim()));
    }
  }
  return nodes;
}

export function mount(root) {
  let alive = true;
  let stick = true; // follow the stream unless the user scrolled up
  let live = null; // refs for the assistant message currently streaming

  const newChat = h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => { clearChat(); input.focus(); } }, icon('plus'), t('ask.new'));
  const usageLine = h('p', { class: 'ask__usage', hidden: true });
  const lede = h('div', { class: 'ask__empty' });
  const thread = h('div', { class: 'thread', role: 'log', 'aria-label': t('ask.thread'), 'aria-live': 'polite' });
  const input = h('textarea', {
    id: 'ask-input', class: 'composer__input', rows: '1', maxlength: '4000', enterkeyhint: 'send',
    placeholder: t('ask.placeholder'), 'aria-label': t('ask.message'),
  });
  const sendBtn = h('button', { type: 'submit', class: 'composer__btn', 'aria-label': t('ask.send') }, icon('send'));
  const stopBtn = h('button', { type: 'button', class: 'composer__btn composer__btn--stop', 'aria-label': t('ask.stop'), hidden: true, onclick: stopChat }, icon('stop'));
  const composer = h('form', { class: 'composer', hidden: true },
    h('div', { class: 'composer__inner' }, input, sendBtn, stopBtn));
  const page = h('div', { class: 'ask' },
    h('div', { class: 'ask__top' }, h('h1', { class: 'page-title', text: t('tab.ask') }), newChat),
    usageLine, lede, thread, composer);
  root.append(page);

  // ---- composer ----
  function grow() {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  }
  input.addEventListener('input', grow);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); composer.requestSubmit(); }
  });
  composer.addEventListener('submit', (e) => {
    e.preventDefault();
    send(input.value);
  });
  function send(text) {
    if (!text.trim() || chat.busy) return;
    input.value = '';
    grow();
    stick = true;
    sendChat(text);
  }

  const heightObserver = new ResizeObserver(() => page.style.setProperty('--composer-h', `${composer.offsetHeight}px`));
  heightObserver.observe(composer);

  // On-screen keyboards shrink the visual viewport but not the layout viewport; lift the composer by the difference.
  const vv = window.visualViewport;
  function onViewport() {
    const kb = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
    document.documentElement.style.setProperty('--kb', `${kb}px`);
    document.documentElement.classList.toggle('kb-open', kb > 80);
    if (kb > 80 && stick) scrollDown();
  }
  vv?.addEventListener('resize', onViewport);
  vv?.addEventListener('scroll', onViewport);

  // ---- scrolling ----
  const atBottom = () => window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 80;
  const onScroll = () => { stick = atBottom(); };
  window.addEventListener('scroll', onScroll, { passive: true });
  let scrollQueued = false;
  function scrollDown() {
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: 'auto' });
    });
  }
  const follow = () => { if (stick) scrollDown(); };

  // ---- rendering ----
  function card(m) {
    const meta = [m.year, runtime(m.runtime)].filter(Boolean).join(', ');
    return h('li', { class: 'pickcard' },
      h('button', { type: 'button', class: 'pickcard__open', onclick: () => go({ movie: m.id }) },
        poster(m, { cls: 'pickcard__poster' }),
        h('span', { class: 'pickcard__body' },
          h('span', { class: 'pickcard__title', text: m.title }),
          h('span', { class: 'pickcard__meta' }, meta, m.runtime ? ', ' : '', endsAt(m.runtime, { prefix: t('clock.ends') })),
          m.pitch ? h('span', { class: 'pickcard__pitch', text: m.pitch }) : null),
        matchStub(m.match, { size: 'sm' })),
      h('div', { class: 'pickcard__foot' }, playControl(m, 'pickcard__play')));
  }

  function bulbs(label) {
    return h('div', { class: 'working' },
      h('span', { class: 'bulbs', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')),
      h('span', { class: 'working__label', text: label || t('ask.thinking') }));
  }

  function botMessage(m, isLast) {
    const text = h('div', { class: 'msg__text' }, richText(stripShownNote(m.text)));
    const cards = h('ul', { class: 'picks', 'aria-label': t('ask.picks') }, m.movies.map(card));
    cards.hidden = !m.movies.length;
    const status = h('div', { class: 'msg__status' });
    const error = h('div', { class: 'chat-error', role: 'alert', hidden: !m.error },
      icon('alert'), h('span', { text: m.error || '' }),
      isLast && !chat.busy ? h('button', { type: 'button', class: 'linkbtn', onclick: retryChat }, t('common.tryAgain')) : null);
    const el = h('article', { class: 'msg msg--bot' }, text, cards, status, error);
    if (isLast) live = { m, text, cards, status, error };
    return el;
  }

  function userMessage(m) {
    return h('article', { class: 'msg msg--user' }, h('div', { class: 'msg__bubble' }, ...m.text.split('\n').flatMap((l, i) => (i ? [h('br'), l] : [l]))));
  }

  function paintStatus() {
    if (!live) return;
    const waiting = chat.busy && !live.m.text && !live.m.movies.length;
    if (chat.busy && (chat.tool || waiting)) fill(live.status, bulbs(chat.tool));
    else fill(live.status);
  }

  function renderAll() {
    live = null;
    const msgs = chat.messages;
    thread.replaceChildren(...msgs.map((m, i) => (m.role === 'user' ? userMessage(m) : botMessage(m, i === msgs.length - 1))));
    lede.hidden = msgs.length > 0;
    newChat.hidden = msgs.length === 0;
    paintStatus();
    paintComposer();
  }

  function paintComposer() {
    input.disabled = chat.busy;
    sendBtn.hidden = chat.busy;
    stopBtn.hidden = !chat.busy;
    composer.classList.toggle('is-busy', chat.busy);
  }

  let textQueued = false;
  function paintText() {
    if (textQueued) return;
    textQueued = true;
    requestAnimationFrame(() => {
      textQueued = false;
      if (!alive || !live) return;
      live.text.replaceChildren(...richText(stripShownNote(live.m.text)));
      paintStatus();
      follow();
    });
  }

  const unsubscribe = subscribeChat((type) => {
    if (!alive) return;
    if (type === 'reset' || type === 'start') { renderAll(); follow(); return; }
    if (type === 'text') paintText();
    else if (type === 'tool') { paintStatus(); follow(); }
    else if (type === 'movies') {
      if (live) { live.cards.replaceChildren(...live.m.movies.map(card)); live.cards.hidden = !live.m.movies.length; }
      paintStatus();
      follow();
    } else if (type === 'end') {
      const wasFocused = document.activeElement === document.body;
      renderAll();
      follow();
      refreshUsage();
      if (wasFocused && matchMedia('(pointer: fine)').matches) input.focus();
    }
  });

  // ---- empty state ----
  fill(lede,
    h('p', { class: 'ask__lede', text: t('ask.lede') }),
    h('p', { class: 'help', text: serverName() === 'Plex' ? t('ask.help.servers', { server: serverName() }) : t('ask.help.server', { server: serverName() }) }),
    h('div', { class: 'starters' }, STARTERS.map((s) => h('button', { type: 'button', class: 'chip', onclick: () => send(s) }, s))));

  // ---- usage (this month's estimated spend) ----
  const money = (n) => new Intl.NumberFormat(lang, { style: 'currency', currency: 'USD' }).format(Number(n));
  const count = (n) => (n >= 1e6 ? `${fixed(n / 1e6)}M` : n >= 1e3 ? `${fixed(n / 1e3)}K` : String(n));
  let provider = null;
  function paintUsage(u) {
    if (!alive || !u) return;
    const parts = [t('ask.questions', { n: u.questions })];
    if (u.tokens) parts.push(t('ask.tokens', { n: count(u.tokens) }));
    // a free OpenAI-compatible tier costs nothing: show what it used, not "$0.00 of $2.00"
    if (!(provider === 'openai' && !u.spent_usd)) parts.unshift(u.budget_usd ? t('ask.spentOf', { spent: money(u.spent_usd), budget: money(u.budget_usd) }) : money(u.spent_usd));
    usageLine.hidden = false;
    usageLine.classList.toggle('is-exhausted', Boolean(u.exhausted));
    usageLine.textContent = u.exhausted
      ? t('ask.exhausted', { spent: money(u.spent_usd), budget: money(u.budget_usd) })
      : t('ask.thisMonth', { parts: parts.join(' \u00b7 ') });
  }
  const refreshUsage = () => api.chatStatus().then((c) => paintUsage(c.usage)).catch(() => {});

  // ---- availability ----
  page.classList.add('is-loading');
  getChatStatus().then((c) => {
    if (!alive) return;
    page.classList.remove('is-loading');
    provider = c.provider || null;
    if (!c.enabled) {
      newChat.hidden = true;
      fill(page, h('h1', { class: 'page-title', text: t('tab.ask') }),
        h('div', { class: 'state' },
          icon('info', 'state__icon state__icon--calm'),
          h('p', { class: 'state__title', text: t('ask.off') }),
          h('p', { class: 'state__text', text: t('ask.offText') }),
          c.problem ? h('p', { class: 'state__text help', text: c.problem }) : null,
          h('a', { class: 'btn btn--ghost', href: '#/tonight' }, t('setup.seePick'))));
      return;
    }
    composer.hidden = false;
    paintUsage(c.usage);
    renderAll();
    if (chat.messages.length) requestAnimationFrame(() => window.scrollTo(0, document.documentElement.scrollHeight));
  });

  return {
    update() {},
    destroy() {
      alive = false;
      unsubscribe();
      heightObserver.disconnect();
      vv?.removeEventListener('resize', onViewport);
      vv?.removeEventListener('scroll', onViewport);
      window.removeEventListener('scroll', onScroll);
      document.documentElement.style.removeProperty('--kb');
      document.documentElement.classList.remove('kb-open');
    },
  };
}
