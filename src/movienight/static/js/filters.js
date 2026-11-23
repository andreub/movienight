// Quick filters shared by Tonight and Browse: mood, Letterboxd tag, runtime, watchlist only.
import { getMoods, getEras, getTags } from './api.js';
import { h, icon } from './dom.js';
import { RUNTIMES, setFilters, hasQuickFilters, CLEAR_QUICK } from './state.js';
import { timeControl, rememberRuntime } from './timecontrol.js';

let uid = 0;

// `time: true` (Tonight) swaps the length segments for the clock-aware "Done by" control.
export function filterBar({ time = false } = {}) {
  const timeCtl = time ? timeControl() : null;
  const name = `rt-${++uid}`;
  const moods = h('div', { class: 'chips chips--scroll', role: 'group', 'aria-label': 'Mood' });
  const eras = h('div', { class: 'chips chips--scroll', role: 'group', 'aria-label': 'Era' });
  // One chip per tag family ("Like my Sitges"); its editions open in a second short row when picked.
  const editions = h('div', { class: 'chips chips--scroll chips--sub', role: 'group', 'aria-label': 'Tag edition', hidden: true });
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
    onclick: () => { rememberRuntime(null); setFilters(CLEAR_QUICK); },
  }, 'Clear');

  const el = h('div', { class: 'filters' },
    moods,
    eras,
    editions,
    h('div', { class: 'filters__row' },
      timeCtl ? timeCtl.chip : h('fieldset', { class: 'seg' }, h('legend', { class: 'sr-only', text: 'Length' }), runtimeInputs.map(([, l]) => l)),
      watchlist,
      reset),
    timeCtl?.note);

  let state = null;
  let moodButtons = [];
  let eraButtons = [];
  let families = [];
  let familyButtons = [];

  const familyOf = (f) => (f.tagf ? families.find((x) => x.family === f.tagf)
    : f.tag ? families.find((x) => x.editions.some((e) => e.tag === f.tag)) : null);

  Promise.all([getMoods(), getTags()]).then(([list, tags]) => {
    moodButtons = list.map((m) => h('button', {
      type: 'button', class: 'chip', 'aria-pressed': 'false', title: m.description || null, 'data-id': m.id,
      onclick: () => setFilters({ mood: state?.mood === m.id ? null : m.id }),
    }, m.emoji ? h('span', { class: 'chip__emoji', 'aria-hidden': 'true', text: m.emoji }) : null, m.label));
    families = tags;
    familyButtons = tags.map((fam) => h('button', {
      type: 'button', class: 'chip chip--tag', 'aria-pressed': 'false', 'data-family': fam.family,
      title: `Films like the ${fam.films} you tagged ${fam.family} on Letterboxd`,
      onclick: () => setFilters(state && familyOf(state) === fam ? { tag: null, tagf: null } : { tag: null, tagf: fam.family }),
    }, icon('tag'), `Like my ${fam.label}`));
    moods.replaceChildren(...moodButtons, ...familyButtons);
    if (state) sync(state);
  }).catch(() => moods.remove());

  getEras().then((list) => {
    eraButtons = list.map((e) => h('button', {
      type: 'button', class: 'chip', 'aria-pressed': 'false', title: e.description || null, 'data-id': e.id,
      onclick: () => setFilters({ era: state?.era === e.id ? null : e.id }),
    }, e.label));
    eras.replaceChildren(...eraButtons);
    if (state) sync(state);
  }).catch(() => eras.remove());

  function syncEditions(f) {
    const fam = familyOf(f);
    const eds = fam?.editions || [];
    // a family with a single usable tag needs no second row
    if (!fam || eds.length < 2) { editions.hidden = true; editions.replaceChildren(); return; }
    const chip = (label, pressed, patch) => h('button', {
      type: 'button', class: 'chip chip--small', 'aria-pressed': String(pressed), onclick: () => setFilters(patch),
    }, label, h('span', { class: 'chip__count', text: patch.tag ? String(eds.find((e) => e.tag === patch.tag).films) : String(fam.films) }));
    editions.replaceChildren(
      chip('All', !f.tag, { tag: null, tagf: fam.family }),
      ...eds.map((e) => chip(e.edition ? String(e.edition) : e.tag, f.tag === e.tag, { tag: e.tag, tagf: null })));
    editions.hidden = false;
  }

  function sync(f) {
    state = f;
    for (const b of moodButtons) b.setAttribute('aria-pressed', String(b.dataset.id === f.mood));
    for (const b of eraButtons) b.setAttribute('aria-pressed', String(b.dataset.id === f.era));
    const fam = familyOf(f);
    for (const b of familyButtons) b.setAttribute('aria-pressed', String(b.dataset.family === fam?.family));
    syncEditions(f);
    timeCtl?.sync(f);
    for (const [input] of runtimeInputs) input.checked = input.value === String(f.rt ?? '');
    watchlist.setAttribute('aria-pressed', String(f.wl));
    reset.hidden = !hasQuickFilters(f);
  }

  return { el, sync };
}
