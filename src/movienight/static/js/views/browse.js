import { api } from '../api.js';
import { h, icon, poster, runtime, matchStub, errorBox, num } from '../dom.js';
import { filterBar } from '../filters.js';
import { go, apiFilters, setFilters, hasAnyFilters, SORTS, EMPTY_FILTERS } from '../state.js';

export const title = 'Browse';
const PAGE = 24;

export function mount(root, route) {
  const bar = filterBar();
  const search = h('input', {
    type: 'search', id: 'browse-q', class: 'input input--search', placeholder: 'Search titles', autocomplete: 'off', enterkeyhint: 'search',
  });
  const sort = h('select', { id: 'browse-sort', class: 'input input--select', onchange: () => setFilters({ sort: sort.value }) },
    SORTS.map(([id, label]) => h('option', { value: id, text: label })));
  const genres = h('div', { class: 'chips chips--scroll', role: 'group', 'aria-label': 'Genres' });
  const count = h('p', { class: 'browse__count', 'aria-live': 'polite' });
  const grid = h('ul', { class: 'grid' });
  const more = h('button', { type: 'button', class: 'btn btn--ghost', hidden: true, onclick: () => loadPage() }, 'Load more');
  const sentinel = h('div', { class: 'browse__end' }, more);
  const results = h('div', { class: 'browse__results' }, count, grid, sentinel);

  root.append(h('div', { class: 'browse' },
    h('div', { class: 'browse__top' },
      h('h1', { class: 'page-title', text: 'Browse' }),
      h('div', { class: 'browse__tools' },
        h('label', { class: 'field field--search' }, h('span', { class: 'sr-only', text: 'Search titles' }), icon('search'), search),
        h('label', { class: 'field field--sort' }, h('span', { class: 'field__label', text: 'Sort' }), sort))),
    bar.el,
    genres,
    results));

  let debounce;
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => setFilters({ q: search.value.trim() }), 300);
  });

  let filters = null;
  let key = '';
  let offset = 0;
  let total = 0;
  let req = 0;
  let loading = false;

  const io = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && !loading && offset < total) loadPage();
  }, { rootMargin: '600px' });
  io.observe(sentinel);

  function params() {
    return { ...apiFilters(filters, { full: true }), sort: filters.sort, limit: PAGE, offset };
  }

  async function reload() {
    const token = ++req;
    offset = 0;
    total = 0;
    more.hidden = true;
    grid.replaceChildren(...Array.from({ length: 8 }, () => h('li', { class: 'card card--skeleton', 'aria-hidden': 'true' }, h('div', { class: 'poster skel' }), h('div', { class: 'skel skel--line' }))));
    count.textContent = 'Loading films…';
    loading = true;
    try {
      const res = await api.recommendations(params());
      if (token !== req) return;
      total = res.total;
      offset = res.items.length;
      renderFacets(res.facets);
      grid.replaceChildren(...res.items.map(card));
      if (!res.items.length) results.replaceChildren(count, emptyState(), grid, sentinel);
      else results.querySelector('.state')?.remove();
      count.textContent = total === 1 ? '1 film' : `${num(total)} films`;
      more.hidden = offset >= total;
    } catch (err) {
      if (token !== req) return;
      grid.replaceChildren();
      count.textContent = '';
      results.querySelector('.state')?.remove();
      count.after(errorBox(err, reload));
    } finally {
      if (token === req) loading = false;
    }
  }

  async function loadPage() {
    if (loading || offset >= total) return;
    const token = req;
    loading = true;
    more.disabled = true;
    more.textContent = 'Loading…';
    try {
      const res = await api.recommendations(params());
      if (token !== req) return;
      grid.append(...res.items.map(card));
      offset += res.items.length;
      total = res.total;
      if (!res.items.length) total = offset;
    } catch (err) {
      if (token === req) results.append(errorBox(err, () => { results.querySelector('.state--error')?.remove(); loadPage(); }));
    } finally {
      if (token === req) {
        loading = false;
        more.disabled = false;
        more.textContent = 'Load more';
        more.hidden = offset >= total;
      }
    }
  }

  function renderFacets(facets) {
    const list = [...(facets?.genres || [])];
    for (const g of filters.genres) if (!list.some((x) => x.name === g)) list.push({ name: g, count: 0 });
    genres.replaceChildren(...list.map((g) => {
      const on = filters.genres.includes(g.name);
      return h('button', {
        type: 'button', class: 'chip chip--small', 'aria-pressed': String(on),
        onclick: () => setFilters({ genres: on ? filters.genres.filter((x) => x !== g.name) : [...filters.genres, g.name] }),
      }, g.name, h('span', { class: 'chip__count', text: num(g.count) }));
    }));
  }

  function card(m) {
    return h('li', { class: 'card', 'data-id': m.id },
      h('button', { type: 'button', class: 'card__btn', onclick: () => go({ movie: m.id }) },
        h('span', { class: 'card__art' },
          poster(m),
          matchStub(m.match, { size: 'sm' }),
          m.on_watchlist ? h('span', { class: 'card__flag', title: 'On your watchlist' }, icon('watchlist'), h('span', { class: 'sr-only', text: 'On your watchlist' })) : null),
        h('span', { class: 'card__title', text: m.title }),
        h('span', { class: 'card__meta', text: [m.year, runtime(m.runtime)].filter(Boolean).join(', ') })));
  }

  function emptyState() {
    return h('div', { class: 'state' },
      h('p', { class: 'state__title', text: hasAnyFilters(filters) ? 'Nothing matches these filters' : 'No films to show yet' }),
      h('p', { class: 'state__text', text: hasAnyFilters(filters)
        ? 'Try another mood, a longer runtime or fewer genres.'
        : 'Once Plex has synced, every unwatched film shows up here, ranked for you.' }),
      hasAnyFilters(filters)
        ? h('button', { type: 'button', class: 'btn btn--primary', onclick: () => { search.value = ''; setFilters(EMPTY_FILTERS); } }, 'Reset filters')
        : h('a', { class: 'btn btn--primary', href: '#/setup' }, 'Open Setup'));
  }

  function onChanged(e) {
    const li = grid.querySelector(`[data-id="${e.detail.id}"]`);
    if (li) li.classList.toggle('is-hidden', e.detail.dismissed);
  }
  window.addEventListener('movienight:changed', onChanged);

  function update(r) {
    filters = r.filters;
    bar.sync(filters);
    sort.value = filters.sort;
    if (document.activeElement !== search) search.value = filters.q;
    const next = JSON.stringify(params());
    const k = next.replace(/"offset":\d+/, '');
    if (k !== key) { key = k; reload(); }
  }
  update(route);

  return {
    update,
    destroy() {
      req++;
      io.disconnect();
      clearTimeout(debounce);
      window.removeEventListener('movienight:changed', onChanged);
    },
  };
}
