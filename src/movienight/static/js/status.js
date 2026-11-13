// Shared /api/status store. Polls every 2 s only while a sync is running.
import { api } from './api.js';

let current = null;
let error = null;
let timer = null;
const subs = new Set();

export const getStatus = () => current;

export function subscribe(fn) {
  subs.add(fn);
  if (current || error) fn(current, error);
  return () => subs.delete(fn);
}

export async function refreshStatus() {
  clearTimeout(timer);
  try {
    current = await api.status();
    error = null;
  } catch (e) {
    error = e;
  }
  for (const fn of subs) fn(current, error);
  if (current?.sync?.running) timer = setTimeout(refreshStatus, 2000);
  return current;
}

export const STAGES = {
  plex: 'Reading your Plex libraries',
  letterboxd: 'Reading your Letterboxd history',
  tmdb: 'Fetching film details from TMDB',
  profile: 'Updating your taste profile',
};

export function syncPercent(sync) {
  return sync?.total ? Math.min(100, Math.round((sync.done / sync.total) * 100)) : null;
}
