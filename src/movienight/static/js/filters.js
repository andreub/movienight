// Quick filters shared by Tonight and Browse: mood, runtime, watchlist only.
import { getMoods } from './api.js';
import { h, icon } from './dom.js';
import { RUNTIMES, setFilters, hasQuickFilters } from './state.js';
import { timeControl, rememberRuntime } from './timecontrol.js';

let uid = 0;

// `time: true` (Tonight) swaps the length segments for the clock-aware "Done by" control.
export function filterBar({ time = false } = {}) {
  const timeCtl = time ? timeControl() : null;
  const name = `rt-${++uid}`;
  const moods = h('div', { class: 'chips chips--scroll', role: 'group', 'aria-label': 'Mood' });
  const runtimeInputs = RUNTIMES.map(([value, label]) => {
    const input = h('input', { type: 'radio', name, value: value ?? '', class: 'seg__input', onchange: () => { rememberRuntime(value); setFilters({ rt: value }); } });
    return [input, h('label', { class: 'seg__item' }, input, h('span', { text: label }))];
  });
  const watchlist = h('button', {
    type: 'button', class: 'chip chip--toggle', 'aria-pressed': 'false',
    onclick: () => setFilters({ wl: watchlist.getAttribute('aria-pressed') !== 'true' }),
  }, icon('watchlist'), h('span', { class: 'chip__label', text: 'Watchlist only' }));
  const reset = h('button', {
    type: 'button', class: 'linkbtn', hidden: true,
    onclick: () => { rememberRuntime(null); setFilters({ mood: null, rt: null, wl: false }); },
  }, 'Clear');

  const el = h('div', { class: 'filters' },
    moods,
    h('div', { class: 'filters__row' },
      timeCtl ? timeCtl.chip : h('fieldset', { class: 'seg' }, h('legend', { class: 'sr-only', text: 'Length' }), runtimeInputs.map(([, l]) => l)),
      watchlist,
      reset),
    timeCtl?.note);

  let state = null;
  let moodButtons = [];

  getMoods().then((list) => {
    moodButtons = list.map((m) => h('button', {
      type: 'button', class: 'chip', 'aria-pressed': 'false', title: m.description || null, 'data-id': m.id,
      onclick: () => setFilters({ mood: state?.mood === m.id ? null : m.id }),
    }, m.emoji ? h('span', { class: 'chip__emoji', 'aria-hidden': 'true', text: m.emoji }) : null, m.label));
    moods.replaceChildren(...moodButtons);
    if (state) sync(state);
  }).catch(() => moods.remove());

  function sync(f) {
    state = f;
    for (const b of moodButtons) b.setAttribute('aria-pressed', String(b.dataset.id === f.mood));
    timeCtl?.sync(f);
    for (const [input] of runtimeInputs) input.checked = input.value === String(f.rt ?? '');
    watchlist.setAttribute('aria-pressed', String(f.wl));
    reset.hidden = !hasQuickFilters(f);
  }

  return { el, sync };
}
