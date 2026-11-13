import { api } from '../api.js';
import { h, fill, poster, stars, errorBox, num } from '../dom.js';

export const title = 'Taste';

export function mount(root) {
  const page = h('div', { class: 'taste' });
  root.append(page);
  let alive = true;

  async function load() {
    fill(page, h('h1', { class: 'page-title', text: 'Your taste' }),
      h('div', { class: 'skel skel--title' }), h('div', { class: 'skel skel--block' }));
    try {
      const p = await api.profile();
      if (alive) fill(page, ...render(p));
    } catch (err) {
      if (alive) fill(page, h('h1', { class: 'page-title', text: 'Your taste' }), errorBox(err, load));
    }
  }
  load();

  return { update() {}, destroy() { alive = false; } };
}

function render(p) {
  const s = p.stats || {};
  return [
    h('h1', { class: 'page-title', text: 'Your taste' }),
    h('p', { class: 'taste__lede' },
      `${num(s.watched)} films watched`,
      s.hours ? `, ${num(s.hours)} hours in the dark.` : '.'),
    h('dl', { class: 'stats' },
      stat('Rated', num(s.rated)),
      stat('Average rating', s.mean_rating != null ? h('span', null, s.mean_rating.toFixed(1), ' ', stars(Math.round(s.mean_rating * 2) / 2)) : '–'),
      stat('Liked', num(s.liked)),
      stat('Unwatched on Plex', num(s.library_unwatched)),
      stat('Watchlist films on Plex', num(s.watchlist_on_plex))),
    p.rating_histogram?.length ? block('How you rate', histogram(p.rating_histogram, s.mean_rating)) : null,
    h('p', { class: 'taste__note', text: 'Bars to the right mean you rate films with that trait above your average; to the left, below it.' }),
    h('div', { class: 'taste__cols' },
      affinityBlock('Genres', p.genres, 12),
      affinityBlock('Decades', [...(p.decades || [])].sort((a, b) => a.decade - b.decade).map((d) => ({ ...d, name: `${d.decade}s` })), 12),
      affinityBlock('Directors', p.directors, 8),
      affinityBlock('Actors', p.cast, 8),
      affinityBlock('Themes', p.keywords, 10)),
    p.top_rated?.length ? block('Your highest rated', h('ul', { class: 'top-rated' }, p.top_rated.map((m) => h('li', null,
      poster(m, { label: `${m.title}${m.year ? ` (${m.year})` : ''}` }),
      h('span', { class: 'top-rated__title', text: m.title }),
      stars(m.my_rating))))) : null,
  ];
}

function stat(label, value) {
  return h('div', { class: 'stat' }, h('dt', { text: label }), h('dd', null, value));
}

function block(title, content) {
  return h('section', { class: 'taste__block' }, h('h2', { class: 'section-title', text: title }), content);
}

function histogram(bins, mean) {
  const max = Math.max(1, ...bins.map((b) => b.count));
  return h('div', { class: 'histo' },
    h('ol', { class: 'histo__bars' }, bins.map((b) => h('li', { class: 'histo__bin' },
      h('span', { class: 'histo__bar', style: { '--v': String(b.count / max) }, title: `${b.count} films` }),
      h('span', { class: 'histo__label', 'aria-hidden': 'true', text: Number.isInteger(b.rating) ? `${b.rating}★` : '' }),
      h('span', { class: 'sr-only', text: `${b.rating} stars: ${b.count} films` })))),
    mean != null ? h('p', { class: 'histo__mean', text: `You average ${mean.toFixed(1)} out of 5.` }) : null);
}

function affinityBlock(title, rows, limit) {
  if (!rows?.length) return null;
  const list = rows.slice(0, limit);
  const max = Math.max(0.01, ...list.map((r) => Math.abs(r.affinity ?? 0)));
  return block(title, h('ul', { class: 'aff' }, list.map((r) => {
    const a = r.affinity ?? 0;
    return h('li', { class: 'aff__row' },
      h('span', { class: 'aff__name', text: r.name }),
      h('span', { class: `aff__track ${a < 0 ? 'is-neg' : ''}`, 'aria-hidden': 'true' },
        h('span', { class: 'aff__bar', style: { '--v': String(Math.abs(a) / max) } })),
      h('span', { class: 'aff__meta' },
        `${num(r.count)} film${r.count === 1 ? '' : 's'}`,
        r.mean_rating != null ? `, avg ${r.mean_rating.toFixed(1)}` : ''),
      h('span', { class: 'sr-only', text: `, affinity ${a > 0 ? '+' : ''}${a.toFixed(2)}` }));
  })));
}
