import { api } from '../api.js';
import { h, fill, icon, poster, genPoster, hueOf, runtime, matchStub, reasonList, errorBox, announce, toast, isTyping } from '../dom.js';
import { filterBar } from '../filters.js';
import { playControl } from '../play.js';
import { go, apiFilters, hasQuickFilters, setFilters } from '../state.js';

export const title = 'Tonight';

export function mount(root, route) {
  const bar = filterBar();
  const stage = h('div', { class: 'tonight__stage' });
  root.append(
    h('h1', { class: 'sr-only', text: "Tonight's pick" }),
    h('div', { class: 'tonight' }, stage, h('div', { class: 'tonight__filters' }, bar.el)),
  );

  let filters = null;
  let filterKey = '';
  let data = null;
  let seen = [];
  let req = 0;

  async function load(mode) {
    const token = ++req;
    if (mode === 'fresh') seen = [];
    if (data) stage.classList.add('is-busy');
    else fill(stage, skeleton());
    try {
      const res = await api.pick({ ...apiFilters(filters), exclude: seen });
      if (token !== req) return;
      data = res;
      seen = [...new Set([...seen, res.pick.id])].slice(-60);
      render(mode);
    } catch (err) {
      if (token !== req) return;
      if (err.status === 404 && seen.length) {
        toast("You've seen every match for these filters, so we're starting over.");
        load('fresh');
        return;
      }
      data = null;
      stage.classList.remove('is-busy');
      fill(stage, err.status === 404 ? empty() : errorBox(err, () => load('fresh')));
    }
  }

  function render(mode) {
    const { pick, alternatives = [] } = data;
    stage.classList.remove('is-busy');
    fill(stage, hero(pick, mode), alternativesList(alternatives));
    if (mode !== 'fresh' && stage.getBoundingClientRect().top < 0) {
      const smooth = !matchMedia('(prefers-reduced-motion: reduce)').matches;
      stage.scrollIntoView({ block: 'start', behavior: smooth ? 'smooth' : 'auto' });
    }
    announce(`Tonight's pick: ${pick.title}, ${Math.round(pick.match ?? 0)}% match.`);
  }

  function hero(m, mode) {
    const art = m.backdrop_url
      ? h('img', { class: 'pick__backdrop', src: m.backdrop_url, alt: '', decoding: 'async' })
      : h('div', { class: 'pick__backdrop pick__backdrop--gen', 'aria-hidden': 'true' }, h('span', { text: m.title }));
    if (art.tagName === 'IMG') art.addEventListener('error', () => art.replaceWith(h('div', { class: 'pick__backdrop pick__backdrop--gen', 'aria-hidden': 'true' })), { once: true });

    const shuffleBtn = h('button', { type: 'button', class: 'btn btn--ghost', onclick: shuffle, 'aria-keyshortcuts': 'S' },
      icon('shuffle'), 'Shuffle');
    return h('article', { class: `pick ${mode === 'shuffle' ? 'is-shuffled' : ''}`, style: { '--h': String(hueOf(m)) }, 'aria-labelledby': 'pick-title' },
      h('div', { class: 'pick__art' }, art),
      h('div', { class: 'pick__inner' },
        poster(m, { label: `Poster for ${m.title}`, eager: true, cls: 'pick__poster' }),
        h('div', { class: 'pick__info' },
          h('div', { class: 'pick__head' },
            h('h2', { class: 'pick__title', id: 'pick-title', text: m.title }),
            matchStub(m.match)),
          meta(m),
          reasonList(m.reasons, 'pick__reasons'),
          h('div', { class: 'pick__actions' },
            playControl(m, 'pick__play'),
            shuffleBtn,
            h('button', { type: 'button', class: 'btn btn--ghost', onclick: () => notTonight(m) }, icon('eyeoff'), 'Not tonight'),
            h('button', { type: 'button', class: 'btn btn--quiet', onclick: () => go({ movie: m.id }) }, icon('info'), 'Details')))));
  }

  function meta(m) {
    return h('p', { class: 'meta' },
      m.year ? h('span', { text: String(m.year) }) : null,
      runtime(m.runtime) ? h('span', { text: runtime(m.runtime) }) : null,
      m.genres?.length ? h('span', { text: m.genres.slice(0, 3).join(', ') }) : null,
      m.on_watchlist ? h('span', { class: 'meta__flag' }, icon('watchlist'), 'On your watchlist') : null);
  }

  function alternativesList(list) {
    if (!list.length) return null;
    return h('section', { class: 'alts', 'aria-labelledby': 'alts-title' },
      h('h2', { class: 'alts__title', id: 'alts-title', text: 'Also good tonight' }),
      h('ul', { class: 'alts__list' }, list.map((m) => h('li', null,
        h('button', { type: 'button', class: 'alt', onclick: () => go({ movie: m.id }) },
          poster(m, { cls: 'alt__poster' }),
          h('span', { class: 'alt__body' },
            h('span', { class: 'alt__title', text: m.title }),
            h('span', { class: 'alt__meta', text: [m.year, runtime(m.runtime)].filter(Boolean).join(', ') }),
            m.reasons?.[0] ? h('span', { class: 'alt__why', text: m.reasons[0].text }) : null),
          matchStub(m.match, { size: 'sm' }))))));
  }

  function shuffle() {
    if (!data) return load('fresh');
    load('shuffle');
  }

  async function notTonight(m) {
    try {
      await api.dismiss(m.id, 30);
    } catch (err) {
      toast(err.detail);
      return;
    }
    const previous = data;
    toast(`Hid “${m.title}” for 30 days.`, {
      action: {
        label: 'Undo',
        run: async () => {
          try {
            await api.undismiss(m.id);
            req++;
            data = previous;
            render('shuffle');
          } catch (err) { toast(err.detail); }
        },
      },
    });
    load('shuffle');
  }

  function empty() {
    const filtered = hasQuickFilters(filters);
    return h('div', { class: 'state' },
      h('div', { class: 'state__art', 'aria-hidden': 'true' }, genPoster({ title: 'Nothing playing', year: null })),
      h('p', { class: 'state__title', text: filtered ? 'Nothing matches these filters' : 'No picks yet' }),
      h('p', { class: 'state__text', text: filtered
        ? 'Every film that fits is watched or hidden. Loosen a filter to see more.'
        : 'Your Plex library may still be syncing. Check progress in Setup.' }),
      filtered
        ? h('button', { type: 'button', class: 'btn btn--primary', onclick: () => setFilters({ mood: null, rt: null, wl: false }) }, 'Clear filters')
        : h('a', { class: 'btn btn--primary', href: '#/setup' }, 'Open Setup'));
  }

  function skeleton() {
    return h('div', { class: 'pick pick--skeleton', 'aria-busy': 'true', 'aria-label': 'Finding a film' },
      h('div', { class: 'pick__art skel' }),
      h('div', { class: 'pick__inner' },
        h('div', { class: 'pick__poster skel' }),
        h('div', { class: 'pick__info' },
          h('div', { class: 'skel skel--title' }), h('div', { class: 'skel skel--line' }), h('div', { class: 'skel skel--line skel--short' }))));
  }

  function onKey(e) {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || isTyping(e) || document.querySelector('dialog[open]')) return;
    const arrow = e.key === 'ArrowRight' && (e.target === document.body || e.target === root);
    if (e.key === 's' || e.key === 'S' || arrow) { e.preventDefault(); shuffle(); }
  }
  document.addEventListener('keydown', onKey);

  function update(r) {
    filters = r.filters;
    bar.sync(filters);
    const key = JSON.stringify(apiFilters(filters));
    if (key !== filterKey) { filterKey = key; load('fresh'); }
  }
  update(route);

  window.addEventListener('movienight:changed', onChanged);
  function onChanged(e) {
    if (data && e.detail.id === data.pick.id && e.detail.dismissed) load('shuffle');
  }

  return {
    update,
    destroy() {
      req++;
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('movienight:changed', onChanged);
    },
  };
}
