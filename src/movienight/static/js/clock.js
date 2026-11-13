// Everything that depends on the wall clock: "ends at 23:47" labels and finish-time options.
import { h, runtime } from './dom.js';

const fmt = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
export const clockText = (date) => fmt.format(date);

const MIN = 60000;

// Whole minutes from now until `end`, rounded down to 5 so the label reads "1 h 50", not "1 h 53".
export function minutesUntil(end, now = new Date()) {
  return Math.max(0, Math.floor((end.getTime() - now.getTime()) / MIN / 5) * 5);
}

// A small "Ends at 23:47" label that keeps itself current while the page stays open.
export function endsAt(min, { prefix = 'Ends at ', cls = 'ends' } = {}) {
  if (!min) return null;
  const el = h('span', { class: cls, 'data-ends': String(min), 'data-prefix': prefix, title: 'If you press play now' });
  el.textContent = prefix + clockText(new Date(Date.now() + min * MIN));
  return el;
}
setInterval(() => {
  for (const el of document.querySelectorAll('[data-ends]')) {
    el.textContent = el.dataset.prefix + clockText(new Date(Date.now() + Number(el.dataset.ends) * MIN));
  }
}, 30000);

// Finish times worth offering right now: the next two half-hours that leave at least 80 minutes, and midnight.
export function endOptions(now = new Date()) {
  const first = new Date(now.getTime() + 80 * MIN);
  const exact = first.getSeconds() === 0 && first.getMilliseconds() === 0;
  first.setSeconds(0, 0);
  if (!exact) first.setMinutes(first.getMinutes() + 1);
  if (first.getMinutes() % 30) first.setMinutes(first.getMinutes() < 30 ? 30 : 60);
  const times = [first, new Date(first.getTime() + 30 * MIN)];

  const midnight = new Date(now);
  midnight.setHours(24, 0, 0, 0);
  const away = midnight.getTime() - now.getTime();
  if (away >= 80 * MIN && away <= 6 * 60 * MIN && !times.some((t) => t.getTime() === midnight.getTime())) times.push(midnight);

  return times.sort((a, b) => a - b).map((endAt) => ({
    kind: 'end',
    endAt: endAt.getTime(),
    rt: minutesUntil(endAt, now),
    label: endAt.getHours() === 0 && endAt.getMinutes() === 0 ? 'Midnight' : clockText(endAt),
  }));
}

export const fitsText = (rt) => `fits films up to ${runtime(rt)}`;

// Weeknights (Sun to Thu) after 21:30: suggest finishing 2 h 15 from now, but not past 00:30.
export function weeknightDefault(now = new Date()) {
  const day = now.getDay();
  const minutesOfDay = now.getHours() * 60 + now.getMinutes();
  if (day > 4 || minutesOfDay < 21 * 60 + 30) return null;
  const cap = new Date(now);
  cap.setHours(24, 30, 0, 0);
  const end = new Date(Math.min(now.getTime() + 135 * MIN, cap.getTime()));
  const rt = minutesUntil(end, now);
  return rt >= 80 ? { kind: 'end', endAt: end.getTime(), rt, auto: true } : null;
}
