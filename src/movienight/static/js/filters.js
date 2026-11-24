// Quick filters shared by Tonight and Browse: mood, Letterboxd tag, runtime, watchlist only; on Tonight also
// "Complete a saga" (`saga: true`), shown only while it has a film to offer (see sagaCount).
import { getMoods, getEras, getTags } from './api.js';
import { h, icon } from './dom.js';
import { RUNTIMES, EMPTY_FILTERS, apiFilters, setFilters, hasQuickFilters, CLEAR_QUICK } from './state.js';
import { dropdown } from './dropdown.js';
import { timeControl, rememberRuntime } from './timecontrol.js';

let uid = 0;

// `time: true` (Tonight) swaps the length segments for the clock-aware "Done by" control.
// `saga: true` (Tonight) adds the "Complete a saga" toggle beside Watchlist only.
export function filterBar({ time = false, saga = false } = {}) {
  const timeCtl = time ? timeControl() : null;
  const name = `rt-${++uid}`;
  const moods = h('div', { class: 'chips chips--scroll', role: 'group', 'aria-label': 'Mood' });
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
  // The next film of each saga the user has started, in the library. Hidden until Tonight learns there is one.
  const sagaChip = saga ? h('button', {
    type: 'button', class: 'chip chip--toggle', 'aria-pressed': 'false', hidden: true,
    title: 'The next film of each saga you have started',
    onclick: () => setFilters({ saga: sagaChip.getAttribute('aria-pressed') !== 'true' }),
  }, icon('trophy'), h('span', { class: 'chip__label', text: 'Complete a saga' })) : null;
  let sagaFilms = 0;
  const reset = h('button', {
    type: 'button', class: 'linkbtn', hidden: true,
    onclick: () => { rememberRuntime(null); setFilters(CLEAR_QUICK); },
  }, 'Clear');

  // Era: a compact menu beside the length control. Hidden until the list loads (and removed if it fails or is empty).
  let eraList = [];
  const eraCtl = dropdown({
    iconName: 'calendar',
    build: () => h('ul', null,
      eraCtl.item('Any era', null, !state?.era, () => setFilters({ era: null })),
      eraOptions().map((e) => eraCtl.item(e.label, null, state?.era === e.id, () => setFilters({ era: e.id })))),
  });
  eraCtl.chip.hidden = true;
  eraCtl.label.textContent = 'Any era';

  const el = h('div', { class: 'filters' },
    moods,
    editions,
    h('div', { class: 'filters__row' },
      timeCtl ? timeCtl.chip : h('fieldset', { class: 'seg' }, h('legend', { class: 'sr-only', text: 'Length' }), runtimeInputs.map(([, l]) => l)),
      eraCtl.chip,
      watchlist,
      sagaChip,
      reset),
    timeCtl?.note);

  let state = null;
  let moodButtons = [];
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

  // The list depends on the other filters and on what is still unseen: refetch on either change.
  // The selected era stays listed even when nothing is left in it, so it can be seen and cleared.
  const eraOptions = () => (state?.era && !eraList.some((e) => e.id === state.era)
    ? [...eraList, { id: state.era, label: eraLabel(state.era) }] : eraList);
  const eraLabel = (id) => eraList.find((e) => e.id === id)?.label || String(id);
  let erasKey = null, erasSeq = 0, erasLoaded = false;
  function showEras() {
    eraCtl.chip.hidden = !(eraList.length || state?.era);
    eraCtl.label.textContent = state?.era ? eraLabel(state.era) : 'Any era';
  }
  function loadEras(force) {
    const { era, ...params } = apiFilters(state || EMPTY_FILTERS);
    const key = JSON.stringify(params);
    if (!force && key === erasKey) return;
    erasKey = key;
    const seq = ++erasSeq;
    getEras(params).then((list) => {
      if (seq !== erasSeq) return;
      eraList = list || []; erasLoaded = true;
      showEras();
    }).catch(() => { if (seq === erasSeq && !erasLoaded) eraCtl.chip.remove(); });
  }
  const EVENTS = ['movienight:seen-saved', 'movienight:marked', 'movienight:changed'];
  function onChange() {
    if (el.isConnected) loadEras(true);
    else for (const ev of EVENTS) window.removeEventListener(ev, onChange);
  }
  for (const ev of EVENTS) window.addEventListener(ev, onChange);
  loadEras();

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
    showEras();
    loadEras();
    eraCtl.button.classList.toggle('is-active', !!f.era);
    const fam = familyOf(f);
    for (const b of familyButtons) b.setAttribute('aria-pressed', String(b.dataset.family === fam?.family));
    syncEditions(f);
    timeCtl?.sync(f);
    for (const [input] of runtimeInputs) input.checked = input.value === String(f.rt ?? '');
    watchlist.setAttribute('aria-pressed', String(f.wl));
    if (sagaChip) {
      sagaChip.setAttribute('aria-pressed', String(f.saga));
      sagaChip.hidden = !f.saga && !sagaFilms;
    }
    reset.hidden = !(hasQuickFilters(f) || (sagaChip && f.saga));
  }

  // How many films "Complete a saga" would offer under the other filters (null: unknown, keep the chip as it is).
  function sagaCount(n) {
    if (!sagaChip || n == null) return;
    sagaFilms = n;
    sagaChip.hidden = !state?.saga && !n;
  }

  return { el, sync, sagaCount };
}
