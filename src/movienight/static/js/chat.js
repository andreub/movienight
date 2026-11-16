// Conversation state for the Ask view. Lives at module level so a stream keeps running (and the
// thread survives) while the user opens a film or switches tab. Mirrored into sessionStorage.
import { chatStream } from './api.js';

const KEY = 'mn.chat';
const MAX_MESSAGES = 40;
const MAX_CHARS = 4000;

export const chat = {
  messages: [], // { role: 'user' | 'assistant', text, movies: [], error: string | null }
  busy: false,
  tool: null, // transient progress label while a tool runs
  listeners: new Set(),
  controller: null,
};

function emit(type) { for (const fn of chat.listeners) fn(type); }
export function subscribeChat(fn) { chat.listeners.add(fn); return () => chat.listeners.delete(fn); }

function save() {
  try { sessionStorage.setItem(KEY, JSON.stringify(chat.messages)); } catch { /* storage unavailable */ }
}
try {
  const saved = JSON.parse(sessionStorage.getItem(KEY));
  if (Array.isArray(saved)) chat.messages = saved.filter((m) => m && (m.role === 'user' || m.role === 'assistant') && typeof m.text === 'string');
} catch { /* start empty */ }

const MAX_NOTE_FILMS = 10;
const NOTE_RE = /\s*\[Films shown to the user:[^\]]*\]?\s*$/;

// The model must never show its own "Films shown to the user" list (the cards are the display).
export function stripShownNote(text) { return String(text || '').replace(NOTE_RE, ''); }

// Machine-made note naming the cards an assistant message showed, in order, with the id get_movie takes.
export function shownNote(movies) {
  const items = (Array.isArray(movies) ? movies : []).filter((f) => f && f.id != null && f.title).slice(0, MAX_NOTE_FILMS);
  if (!items.length) return '';
  const clean = (t) => String(t).replace(/[\[\]\n;]+/g, ' ').trim().slice(0, 80);
  const list = items.map((f, i) => `${i + 1}. ${clean(f.title)}${f.year ? ` (${f.year})` : ''} #${f.id}`).join('; ');
  return `[Films shown to the user: ${list}]`;
}

// What the server sees: visible text plus the film note, alternating roles, newest 40, ending on the user's turn.
export function historyForServer() {
  const out = [];
  for (const m of chat.messages) {
    const text = (m.role === 'assistant' ? stripShownNote(m.text) : m.text).trim();
    const note = m.role === 'assistant' ? shownNote(m.movies) : '';
    if (!text && !note) continue;
    const last = out[out.length - 1];
    if (last && last.role === m.role) {
      last.text = `${last.text}\n\n${text}`.trim();
      last.note = [last.note, note].filter(Boolean).join(' ');
    } else out.push({ role: m.role, text, note });
  }
  const done = out.map(({ role, text, note }) => {
    const room = Math.max(0, MAX_CHARS - (note ? note.length + 1 : 0)); // trim the text, never the note
    return { role, content: [text.slice(0, room), note].filter(Boolean).join('\n') };
  });
  while (done.length && done[done.length - 1].role !== 'user') done.pop();
  const trimmed = done.slice(-MAX_MESSAGES);
  while (trimmed.length && trimmed[0].role !== 'user') trimmed.shift();
  return trimmed;
}

export function clearChat() {
  stopChat();
  chat.messages = [];
  chat.tool = null;
  save();
  emit('reset');
}

export function stopChat() { chat.controller?.abort(); }

export function retryChat() {
  const last = chat.messages[chat.messages.length - 1];
  if (chat.busy || last?.role !== 'assistant' || !last.error) return;
  chat.messages.pop();
  const prev = chat.messages.pop();
  if (prev?.role === 'user') sendChat(prev.text);
  else emit('reset');
}

export async function sendChat(text) {
  text = text.trim().slice(0, MAX_CHARS);
  if (!text || chat.busy) return;
  const reply = { role: 'assistant', text: '', movies: [], error: null };
  chat.messages.push({ role: 'user', text, movies: [], error: null }, reply);
  chat.busy = true;
  chat.tool = null;
  chat.controller = new AbortController();
  save();
  emit('start');

  try {
    for await (const { event, data } of chatStream(historyForServer(), chat.controller.signal)) {
      if (event === 'text') {
        chat.tool = null;
        reply.text += data.delta || '';
        emit('text');
      } else if (event === 'tool') {
        chat.tool = data.label || 'Working on it…';
        emit('tool');
      } else if (event === 'movies') {
        reply.movies = Array.isArray(data.items) ? data.items : []; // a second set replaces the first
        emit('movies');
      } else if (event === 'error') {
        reply.error = data.detail || 'Something went wrong while answering.';
      } else if (event === 'done') {
        break;
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') reply.error = err.detail || err.message || 'Something went wrong while answering.';
  } finally {
    chat.busy = false;
    chat.tool = null;
    chat.controller = null;
    save();
    emit('end');
  }
}
