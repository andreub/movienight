// Ask: chat with Claude, which recommends from the Plex library. Text is only ever added as text nodes.
import { getChatStatus } from '../api.js';
import { h, fill, icon, poster, runtime, matchStub } from '../dom.js';
import { chat, subscribeChat, sendChat, stopChat, clearChat, retryChat } from '../chat.js';
import { playControl } from '../play.js';
import { endsAt } from '../clock.js';
import { go } from '../state.js';

export const title = 'Ask';

const STARTERS = [
  'Something tense under 2 hours',
  'A comfort rewatch',
  'Surprise me with a hidden gem',
  'Best thing on my watchlist',
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

  const newChat = h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => { clearChat(); input.focus(); } }, icon('plus'), 'New chat');
  const usageLine = h('p', { class: 'ask__usage', hidden: true });
  const lede = h('div', { class: 'ask__empty' });
  const thread = h('div', { class: 'thread', role: 'log', 'aria-label': 'Conversation', 'aria-live': 'polite' });
  const input = h('textarea', {
    id: 'ask-input', class: 'composer__input', rows: '1', maxlength: '4000', enterkeyhint: 'send',
    placeholder: 'Describe tonight', 'aria-label': 'Message',
  });
  const sendBtn = h('button', { type: 'submit', class: 'composer__btn', 'aria-label': 'Send' }, icon('send'));
  const stopBtn = h('button', { type: 'button', class: 'composer__btn composer__btn--stop', 'aria-label': 'Stop', hidden: true, onclick: stopChat }, icon('stop'));
  const composer = h('form', { class: 'composer', hidden: true },
    h('div', { class: 'composer__inner' }, input, sendBtn, stopBtn));
  const page = h('div', { class: 'ask' },
    h('div', { class: 'ask__top' }, h('h1', { class: 'page-title', text: 'Ask' }), newChat),
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
          h('span', { class: 'pickcard__meta' }, meta, m.runtime ? ', ' : '', endsAt(m.runtime, { prefix: 'ends ' })),
          m.pitch ? h('span', { class: 'pickcard__pitch', text: m.pitch }) : null),
        matchStub(m.match, { size: 'sm' })),
      h('div', { class: 'pickcard__foot' }, playControl(m, 'pickcard__play')));
  }

  function bulbs(label) {
    return h('div', { class: 'working' },
      h('span', { class: 'bulbs', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')),
      h('span', { class: 'working__label', text: label || 'Thinking…' }));
  }

  function botMessage(m, isLast) {
    const text = h('div', { class: 'msg__text' }, richText(m.text));
    const cards = h('ul', { class: 'picks', 'aria-label': 'Recommended films' }, m.movies.map(card));
    cards.hidden = !m.movies.length;
    const status = h('div', { class: 'msg__status' });
    const error = h('div', { class: 'chat-error', role: 'alert', hidden: !m.error },
      icon('alert'), h('span', { text: m.error || '' }),
      isLast && !chat.busy ? h('button', { type: 'button', class: 'linkbtn', onclick: retryChat }, 'Try again') : null);
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
      live.text.replaceChildren(...richText(live.m.text));
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
    h('p', { class: 'ask__lede', text: 'What kind of night is it?' }),
    h('p', { class: 'help', text: 'Describe the mood, the time you have or who is watching. I will pick from what is on your Plex servers.' }),
    h('div', { class: 'starters' }, STARTERS.map((s) => h('button', { type: 'button', class: 'chip', onclick: () => send(s) }, s))));

  // ---- usage (this month's estimated spend) ----
  const money = (n) => `$${Number(n).toFixed(2)}`;
  function paintUsage(u) {
    if (!alive || !u) return;
    const q = `${u.questions} question${u.questions === 1 ? '' : 's'}`;
    usageLine.hidden = false;
    usageLine.classList.toggle('is-exhausted', Boolean(u.exhausted));
    usageLine.textContent = u.exhausted
      ? `Monthly budget used (${money(u.spent_usd)} of ${money(u.budget_usd)}). Ask comes back on the 1st.`
      : `This month: ${money(u.spent_usd)}${u.budget_usd ? ` of ${money(u.budget_usd)}` : ''} \u00b7 ${q}`;
  }
  const refreshUsage = () => getChatStatus().then((c) => paintUsage(c.usage)).catch(() => {});

  // ---- availability ----
  page.classList.add('is-loading');
  getChatStatus().then((c) => {
    if (!alive) return;
    page.classList.remove('is-loading');
    if (!c.enabled) {
      newChat.hidden = true;
      fill(page, h('h1', { class: 'page-title', text: 'Ask' }),
        h('div', { class: 'state' },
          icon('info', 'state__icon state__icon--calm'),
          h('p', { class: 'state__title', text: 'AI chat is off' }),
          h('p', { class: 'state__text', text: 'The server has no API key configured, so Ask is switched off. Everything else works as usual.' }),
          h('a', { class: 'btn btn--ghost', href: '#/tonight' }, "See tonight's pick")));
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
