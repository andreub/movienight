// "Done by" control for Tonight. It replaces the old length segments: one chip that sets
// max_runtime from the clock (finish times) or directly (under 90 min / under 2 h / no limit).
// The choice lives in the URL (rt), and is remembered for the session so it survives navigation.
import { h, icon, runtime } from './dom.js';
import { RUNTIMES, setFilters } from './state.js';
import { endOptions, clockText, fitsText, minutesUntil, weeknightDefault } from './clock.js';

const KEY = 'mn.time';
let memory = null;

export function getChoice() {
  try { return JSON.parse(sessionStorage.getItem(KEY)) ?? memory; } catch { return memory; }
}
export function setChoice(choice) {
  memory = choice;
  try { sessionStorage.setItem(KEY, JSON.stringify(choice)); } catch { /* storage unavailable */ }
}
// Used by every place that sets or clears the length, so a later default never fights the user.
export const rememberRuntime = (rt) => setChoice(rt ? { kind: 'cap', rt } : { kind: 'none' });

// Called once when Tonight opens. Returns the max_runtime to apply (or null to leave the URL alone).
// A filter already in the URL always wins.
export function initialRuntime(filters, now = new Date()) {
  if (filters.rt != null) return null;
  const saved = getChoice();
  if (saved?.kind === 'none') return null;
  if (saved?.kind === 'cap') return saved.rt;
  if (saved?.kind === 'end') {
    const rt = minutesUntil(new Date(saved.endAt), now);
    if (rt >= 40) { setChoice({ ...saved, rt }); return rt; }
    setChoice(null);
  }
  if (filters.mood || filters.wl) return null;
  const auto = weeknightDefault(now);
  if (!auto) return null;
  setChoice(auto);
  return auto.rt;
}

let uid = 0;

export function timeControl() {
  const menuId = `time-menu-${++uid}`;
  const label = h('span', { class: 'chip__label' });
  const button = h('button', {
    type: 'button', class: 'chip chip--time', 'aria-haspopup': 'true', 'aria-expanded': 'false', 'aria-controls': menuId,
    onclick: () => toggle(menu.hidden),
  }, icon('clock'), label, icon('chevron', 'icon--end'));
  const menu = h('div', { class: 'menu menu--down menu--time', id: menuId, hidden: true });
  const chip = h('div', { class: 'menu-wrap timectl' }, button, menu);
  const note = h('p', { class: 'timenote', 'aria-live': 'polite', hidden: true });
  let current = null;

  function pick(opt) {
    setChoice(opt.kind === 'end' ? { kind: 'end', endAt: opt.endAt, rt: opt.rt } : opt.kind === 'cap' ? { kind: 'cap', rt: opt.rt } : { kind: 'none' });
    toggle(false);
    button.focus();
    setFilters({ rt: opt.rt ?? null });
  }

  function item(opt, text, hint, on) {
    return h('li', null, h('button', { type: 'button', class: 'menu__item', 'aria-pressed': String(on), onclick: () => pick(opt) },
      h('span', { text }), hint ? h('span', { class: 'menu__hint', text: hint }) : null));
  }

  function build() {
    const rt = current?.rt ?? null;
    const saved = getChoice();
    const ends = endOptions();
    const savedEndOn = saved?.kind === 'end' && saved.rt === rt;
    menu.replaceChildren(h('ul', null,
      h('li', { class: 'menu__label', text: 'Done by' }),
      ends.map((o) => item(o, o.label, `up to ${runtime(o.rt)}`, savedEndOn && saved.endAt === o.endAt)),
      h('li', { class: 'menu__label', text: 'Or cap the length' }),
      RUNTIMES.filter(([v]) => v).map(([v, text]) => item({ kind: 'cap', rt: v }, text, null, !savedEndOn && rt === v)),
      item({ kind: 'none', rt: null }, 'No rush', 'any length', rt == null)));
  }

  function onOutside(e) { if (!chip.contains(e.target)) toggle(false); }
  function onKey(e) { if (e.key === 'Escape') { e.stopPropagation(); toggle(false); button.focus(); } }
  function toggle(open) {
    if (open) build();
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) {
      document.addEventListener('pointerdown', onOutside);
      chip.addEventListener('keydown', onKey);
      (menu.querySelector('[aria-pressed="true"]') ?? menu.querySelector('button'))?.focus();
    } else {
      document.removeEventListener('pointerdown', onOutside);
      chip.removeEventListener('keydown', onKey);
    }
  }

  function sync(f) {
    current = f;
    const rt = f.rt;
    const saved = getChoice();
    const asEnd = rt != null && saved?.kind === 'end' && saved.rt === rt;
    const capLabel = RUNTIMES.find(([v]) => v === rt)?.[1];
    label.textContent = rt == null ? 'No rush' : asEnd ? `Done by ${clockText(new Date(saved.endAt))}` : capLabel || `Up to ${runtime(rt)}`;
    button.classList.toggle('is-active', rt != null);

    note.replaceChildren();
    note.hidden = rt == null;
    if (rt != null) {
      const auto = asEnd && saved.auto;
      note.append(auto ? `Suggested for a weeknight: ${fitsText(rt)}. ` : `${fitsText(rt)[0].toUpperCase()}${fitsText(rt).slice(1)}. `);
      if (auto) note.append(h('button', { type: 'button', class: 'linkbtn linkbtn--inline', onclick: () => pick({ kind: 'none', rt: null }) }, 'Remove'));
    }
  }

  return { chip, note, sync, clear: () => rememberRuntime(null) };
}
