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

// What the server sees: visible text only, alternating roles, newest 40, ending on the user's turn.
export function historyForServer() {
  const out = [];
  for (const m of chat.messages) {
    const content = m.text.trim().slice(0, MAX_CHARS);
    if (!content) continue;
    const last = out[out.length - 1];
    if (last && last.role === m.role) last.content = `${last.content}\n\n${content}`.slice(0, MAX_CHARS);
    else out.push({ role: m.role, content });
  }
  while (out.length && out[out.length - 1].role !== 'user') out.pop();
  const trimmed = out.slice(-MAX_MESSAGES);
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
