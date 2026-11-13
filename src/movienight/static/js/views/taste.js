import { api } from '../api.js';
import { h, fill, icon, poster, stars, errorBox, num, ago } from '../dom.js';

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
      if (!alive) return;
      const knows = h('div', { class: 'knows' }, h('div', { class: 'skel skel--block' }));
      fill(page, ...render(p, knows));
      api.feedbackSummary()
        .then((fb) => { if (alive) fill(knows, ...knowsYou(fb, p.model)); })
        .catch((err) => { if (alive) fill(knows, ...(p.model?.films ? knowsYou(null, p.model) : [errorBox(err, load)])); });
    } catch (err) {
      if (alive) fill(page, h('h1', { class: 'page-title', text: 'Your taste' }), errorBox(err, load));
    }
  }
  load();

  return { update() {}, destroy() { alive = false; } };
}

function render(p, knows) {
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
    block('How well does it know you?', knows),
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

// ---- "How well does it know you?" ----

const pct = (x) => `${Math.round(x * 100)}%`;
const VERDICTS = { hit: 'Hit', miss: 'Miss', ok: 'Close', pending: 'Not rated yet' };

function signalWords(rho) {
  if (rho == null) return null;
  if (rho < 0.1) return 'barely better than a guess';
  if (rho < 0.25) return 'a weak signal';
  if (rho < 0.4) return 'a useful signal';
  return 'a strong signal';
}

function knowsYou(fb, fallbackModel) {
  const bt = fb?.backtest ?? fallbackModel ?? null;
  return [
    backtestView(bt),
    tuning(fb?.backtest) ? h('p', { class: 'taste__note', text: tuning(fb?.backtest) }) : null,
    votesLine(fb?.votes),
    outcomesView(fb),
  ];
}

function tuning(bt) {
  if (!bt?.films) return '';
  return bt.tuned
    ? `Its scoring weights were tuned automatically to fit your history${bt.computed_at ? `, last checked ${ago(bt.computed_at)}` : ''}.`
    : `It is using the standard scoring weights${bt.computed_at ? ` (last checked ${ago(bt.computed_at)})` : ''}. They tune themselves once the check finds a better fit.`;
}

function backtestView(bt) {
  if (!bt?.films) {
    return h('p', { class: 'knows__empty', text: 'No accuracy check yet. It runs after a sync, once you have enough rated films to test against.' });
  }
  const hit = bt.top_quintile_hit_rate;
  const base = bt.base_rate;
  const rows = [['MovieNight’s top fifth', hit, true], ['Picking at random', base, false]].filter(([, v]) => v != null);
  const signal = signalWords(bt.spearman);
  return h('div', { class: 'knows__test' },
    hit != null && base != null
      ? h('p', { class: 'knows__lede', text: `Of the films it would have ranked in its top fifth, you rated ${pct(hit)} among your favourites. Picking at random would give ${pct(base)}.` })
      : null,
    h('ul', { class: 'bars', 'aria-hidden': 'true' }, rows.map(([name, v, strong]) => h('li', { class: `bars__row ${strong ? 'is-strong' : ''}` },
      h('span', { class: 'bars__name', text: name }),
      h('span', { class: 'bars__track' }, h('span', { class: 'bars__fill', style: { '--v': String(Math.min(1, v)) } })),
      h('span', { class: 'bars__val', text: pct(v) })))),
    chanceWords(hit, base) ? h('p', { class: 'help', text: chanceWords(hit, base) }) : null,
    h('p', { class: 'help', text: `Checked against ${num(bt.films)} films you have already seen.${signal ? ` Across all of them its ranking agrees with yours at ${bt.spearman.toFixed(2)}, ${signal}. Zero would be pure chance; one would be a perfect match.` : ''}` }),
    changedWeights(bt));
}

function chanceWords(hit, base) {
  if (hit == null || !base) return '';
  const r = hit / base;
  if (r >= 1.6) return 'That is clearly better than chance.';
  if (r >= 1.15) return 'That is better than chance, though not by a huge margin. It should sharpen as you rate more films.';
  return 'That is about what chance would give, so treat its picks as suggestions for now. It should improve as you rate more films.';
}

function changedWeights(bt) {
  const entries = Object.entries(bt.weights || {}).filter(([k, v]) => bt.default_weights?.[k] != null && bt.default_weights[k] !== v);
  if (!bt.tuned || !entries.length) return null;
  return h('ul', { class: 'keywords knows__weights', 'aria-label': 'Tuned weights' },
    entries.map(([k, v]) => h('li', { text: `${k} ${bt.default_weights[k]} to ${v}` })));
}

function votesLine(v) {
  if (!v) return null;
  const total = (v.up || 0) + (v.down || 0);
  return h('p', { class: 'knows__votes' },
    total
      ? `You have given ${num(v.up || 0)} thumbs up and ${num(v.down || 0)} thumbs down. Each one nudges what it suggests next.`
      : 'No thumbs yet. Use the thumbs on a pick to tell it what to show more or less of.');
}

function outcomesView(fb) {
  const list = fb?.outcomes || [];
  if (!list.length) {
    return h('div', { class: 'knows__outcomes' },
      h('h3', { class: 'detail__h', text: 'Its picks, after you watched' }),
      h('p', { class: 'help', text: 'Nothing to compare yet. Watch something it suggested and rate it on Letterboxd; it shows up here.' }));
  }
  const s = fb.summary || {};
  return h('div', { class: 'knows__outcomes' },
    h('h3', { class: 'detail__h', text: 'Its picks, after you watched' }),
    s.rated ? h('p', { class: 'help', text: `Of ${num(s.count)} films it suggested that you watched, you rated ${num(s.rated)}: ${num(s.hits)} landed${s.misses ? `, ${num(s.misses)} missed` : ''}.${s.mean_rating_of_picks != null && s.user_mean_rating != null ? ` You gave its picks ${s.mean_rating_of_picks.toFixed(1)} on average, against ${s.user_mean_rating.toFixed(1)} for everything else.` : ''}` }) : null,
    h('ul', { class: 'outcomes' }, list.map((o) => h('li', { class: `outcome outcome--${o.verdict}` },
      poster(o, { cls: 'outcome__poster' }),
      h('div', { class: 'outcome__body' },
        h('p', { class: 'outcome__title' }, o.title, o.year ? h('span', { class: 'because__year', text: ` ${o.year}` }) : null),
        h('p', { class: 'outcome__line' },
          `Predicted ${Math.round(o.predicted)}`,
          o.my_rating != null ? [' → you gave ', stars(o.my_rating)] : ' and you have not rated it yet',
          o.liked ? h('span', { class: 'liked', title: 'Liked' }, ' ', icon('heart'), h('span', { class: 'sr-only', text: 'Liked' })) : null)),
      h('span', { class: 'verdict', text: VERDICTS[o.verdict] || VERDICTS.pending })))));
}
