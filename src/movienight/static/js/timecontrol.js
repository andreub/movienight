// "Done by" control for Tonight. It replaces the old length segments: one chip that sets
// max_runtime from the clock (finish times) or directly (under 90 min / under 2 h / no limit).
// The choice lives in the URL (rt), and is remembered for the session so it survives navigation.
import { h, runtime } from './dom.js';
import { dropdown } from './dropdown.js';
import { RUNTIMES, setFilters } from './state.js';
import { endOptions, clockText, fitsText, minutesUntil, weeknightDefault } from './clock.js';
import { t } from './i18n.js';

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

export function timeControl() {
  const note = h('p', { class: 'timenote', 'aria-live': 'polite', hidden: true });
  let current = null;

  function pick(opt) {
    setChoice(opt.kind === 'end' ? { kind: 'end', endAt: opt.endAt, rt: opt.rt } : opt.kind === 'cap' ? { kind: 'cap', rt: opt.rt } : { kind: 'none' });
    setFilters({ rt: opt.rt ?? null });
  }

  const item = (opt, text, hint, on) => dd.item(text, hint, on, () => pick(opt));

  function build() {
    const rt = current?.rt ?? null;
    const saved = getChoice();
    const ends = endOptions();
    const savedEndOn = saved?.kind === 'end' && saved.rt === rt;
    return h('ul', null,
      h('li', { class: 'menu__label', text: t('time.doneBy') }),
      ends.map((o) => item(o, o.label, t('time.upTo', { rt: runtime(o.rt) }), savedEndOn && saved.endAt === o.endAt)),
      h('li', { class: 'menu__label', text: t('time.cap') }),
      RUNTIMES.filter(([v]) => v).map(([v, text]) => item({ kind: 'cap', rt: v }, text, null, !savedEndOn && rt === v)),
      item({ kind: 'none', rt: null }, t('time.noRush'), t('time.anyLength'), rt == null));
  }

  const dd = dropdown({ iconName: 'clock', build });
  const { chip, button, label } = dd;

  function sync(f) {
    current = f;
    const rt = f.rt;
    const saved = getChoice();
    const asEnd = rt != null && saved?.kind === 'end' && saved.rt === rt;
    const capLabel = RUNTIMES.find(([v]) => v === rt)?.[1];
    label.textContent = rt == null ? t('time.noRush') : asEnd ? t('time.doneByAt', { time: clockText(new Date(saved.endAt)) }) : capLabel || t('time.upToCap', { rt: runtime(rt) });
    button.classList.toggle('is-active', rt != null);

    note.replaceChildren();
    note.hidden = rt == null;
    if (rt != null) {
      const auto = asEnd && saved.auto;
      note.append(auto ? `${t('time.weeknight', { fits: fitsText(rt) })} ` : `${fitsText(rt)[0].toUpperCase()}${fitsText(rt).slice(1)}. `);
      if (auto) note.append(h('button', { type: 'button', class: 'linkbtn linkbtn--inline', onclick: () => { pick({ kind: 'none', rt: null }); button.focus(); } }, t('common.remove')));
    }
  }

  return { chip, note, sync, clear: () => rememberRuntime(null) };
}
