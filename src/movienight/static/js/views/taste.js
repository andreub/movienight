import { api } from '../api.js';
import { h, fill, icon, poster, stars, errorBox, num, ago, fixed, percent } from '../dom.js';
import { t, has } from '../i18n.js';
import { serverName } from '../status.js';
import { genreName, genreList } from '../labels.js';

export const title = t('tab.taste');

export function mount(root) {
  const page = h('div', { class: 'taste' });
  root.append(page);
  let alive = true;

  async function load() {
    fill(page, h('h1', { class: 'page-title', text: t('taste.h1') }),
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
      if (alive) fill(page, h('h1', { class: 'page-title', text: t('taste.h1') }), errorBox(err, load));
    }
  }
  load();

  return { update() {}, destroy() { alive = false; } };
}

function render(p, knows) {
  const s = p.stats || {};
  return [
    h('h1', { class: 'page-title', text: t('taste.h1') }),
    h('p', { class: 'taste__lede' },
      s.hours ? t('taste.lede.hours', { n: s.watched, num: num(s.watched), hours: num(s.hours) }) : t('taste.lede', { n: s.watched, num: num(s.watched) })),
    h('dl', { class: 'stats' },
      stat(t('taste.rated'), num(s.rated)),
      stat(t('taste.avgRating'), s.mean_rating != null ? h('span', null, fixed(s.mean_rating), ' ', stars(Math.round(s.mean_rating * 2) / 2)) : '–'),
      stat(t('taste.liked'), num(s.liked)),
      stat(t('taste.unwatched', { server: serverName() }), num(s.library_unwatched)),
      stat(t('taste.watchlistOn', { server: serverName() }), num(s.watchlist_on_plex))),
    block(t('taste.knows'), knows),
    p.rating_histogram?.length ? block(t('taste.howRate'), histogram(p.rating_histogram, s.mean_rating)) : null,
    h('p', { class: 'taste__note', text: t('taste.barsNote') }),
    h('div', { class: 'taste__cols' },
      affinityBlock(t('taste.genres'), (p.genres || []).map((g) => ({ ...g, name: genreName(g.name) })), 12),
      affinityBlock(t('taste.decades'), [...(p.decades || [])].sort((a, b) => a.decade - b.decade).map((d) => ({ ...d, name: t('taste.decade', { d: d.decade }) })), 12),
      affinityBlock(t('taste.directors'), p.directors, 8),
      affinityBlock(t('taste.actors'), p.cast, 8),
      affinityBlock(t('taste.themes'), p.keywords, 10)),
    p.tags?.length ? block(t('taste.tags'), tagList(p.tags)) : null,
    p.top_rated?.length ? block(t('taste.top'), h('ul', { class: 'top-rated' }, p.top_rated.map((m) => h('li', null,
      poster(m, { label: `${m.title}${m.year ? ` (${m.year})` : ''}` }),
      h('span', { class: 'top-rated__title', text: m.title }),
      stars(m.my_rating))))) : null,
  ];
}

// One line per tag family, its editions (newest first) indented under it when there is more than one tag.
function tagList(families) {
  const line = (g) => [
    t('count.films', { n: g.films, num: num(g.films) }),
    g.mean_rating != null ? t('taste.avgStars', { r: fixed(g.mean_rating) }) : null,
    g.top_genres?.length ? genreList(g.top_genres) : null,
  ].filter(Boolean).join(' · ');
  return h('ul', { class: 'tags' }, families.map((f) => h('li', { class: 'tags__family' },
    h('p', { class: 'tags__row' }, h('span', { class: 'tags__name', text: f.label || f.family }), h('span', { class: 'tags__meta', text: line(f) })),
    f.editions.length > 1 || (f.editions[0] && f.editions[0].tag !== f.family)
      ? h('ul', { class: 'tags__editions' }, f.editions.map((e) => h('li', { class: 'tags__row' },
        h('span', { class: 'tags__name', text: e.edition && f.editions.every((x) => x.edition) ? String(e.edition) : e.tag }),
        h('span', { class: 'tags__meta', text: line(e) }))))
      : null)));
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
      h('span', { class: 'histo__bar', style: { '--v': String(b.count / max) }, title: t('count.films', { n: b.count, num: num(b.count) }) }),
      h('span', { class: 'histo__label', 'aria-hidden': 'true', text: Number.isInteger(b.rating) ? `${b.rating}★` : '' }),
      h('span', { class: 'sr-only', text: t('taste.histoBin', { r: num(b.rating), films: t('count.films', { n: b.count, num: num(b.count) }) }) })))),
    mean != null ? h('p', { class: 'histo__mean', text: t('taste.average', { r: fixed(mean) }) }) : null);
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
        t('count.films', { n: r.count, num: num(r.count) }),
        r.mean_rating != null ? t('taste.avgSuffix', { r: fixed(r.mean_rating) }) : ''),
      h('span', { class: 'sr-only', text: t('taste.affinity', { a: `${a > 0 ? '+' : ''}${fixed(a, 2)}` }) }));
  })));
}

// ---- "How well does it know you?" ----

const pct = percent;
const VERDICTS = { hit: t('verdict.hit'), miss: t('verdict.miss'), ok: t('verdict.ok'), pending: t('verdict.pending') };

function signalWords(rho) {
  if (rho == null) return null;
  if (rho < 0.1) return t('signal.guess');
  if (rho < 0.25) return t('signal.weak');
  if (rho < 0.4) return t('signal.useful');
  return t('signal.strong');
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
    ? (bt.computed_at ? t('taste.tuned.at', { ago: ago(bt.computed_at) }) : t('taste.tuned'))
    : (bt.computed_at ? t('taste.standard.at', { ago: ago(bt.computed_at) }) : t('taste.standard'));
}

function backtestView(bt) {
  if (!bt?.films) {
    return h('p', { class: 'knows__empty', text: t('taste.noCheck') });
  }
  const hit = bt.top_quintile_hit_rate;
  const base = bt.base_rate;
  const rows = [[t('taste.topFifth'), hit, true], [t('taste.random'), base, false]].filter(([, v]) => v != null);
  const signal = signalWords(bt.spearman);
  return h('div', { class: 'knows__test' },
    hit != null && base != null
      ? h('p', { class: 'knows__lede', text: t('taste.hitLede', { hit: pct(hit), base: pct(base) }) })
      : null,
    h('ul', { class: 'bars', 'aria-hidden': 'true' }, rows.map(([name, v, strong]) => h('li', { class: `bars__row ${strong ? 'is-strong' : ''}` },
      h('span', { class: 'bars__name', text: name }),
      h('span', { class: 'bars__track' }, h('span', { class: 'bars__fill', style: { '--v': String(Math.min(1, v)) } })),
      h('span', { class: 'bars__val', text: pct(v) })))),
    chanceWords(hit, base) ? h('p', { class: 'help', text: chanceWords(hit, base) }) : null,
    h('p', { class: 'help', text: `${t('taste.checked', { n: bt.films, num: num(bt.films) })}${signal ? ` ${t('taste.spearman', { rho: fixed(bt.spearman, 2), signal })}` : ''}` }),
    changedWeights(bt));
}

function chanceWords(hit, base) {
  if (hit == null || !base) return '';
  const r = hit / base;
  if (r >= 1.6) return t('chance.clear');
  if (r >= 1.15) return t('chance.better');
  return t('chance.same');
}

function changedWeights(bt) {
  const entries = Object.entries(bt.weights || {}).filter(([k, v]) => bt.default_weights?.[k] != null && bt.default_weights[k] !== v);
  if (!bt.tuned || !entries.length) return null;
  return h('ul', { class: 'keywords knows__weights', 'aria-label': t('taste.weights') },
    entries.map(([k, v]) => h('li', { text: t('taste.weight', { k: has(`taste.part.${k}`) ? t(`taste.part.${k}`) : k, from: num(bt.default_weights[k]), to: num(v) }) })));
}

function votesLine(v) {
  if (!v) return null;
  const total = (v.up || 0) + (v.down || 0) + (v.love || 0);
  const loves = v.love ? t('taste.loves', { n: v.love, num: num(v.love) }) : '';
  return h('p', { class: 'knows__votes' },
    total
      ? t('taste.votes', { up: num(v.up || 0), loves, down: num(v.down || 0) })
      : t('taste.noVotes'));
}

function outcomesView(fb) {
  const list = fb?.outcomes || [];
  if (!list.length) {
    return h('div', { class: 'knows__outcomes' },
      h('h3', { class: 'detail__h', text: t('taste.outcomes') }),
      h('p', { class: 'help', text: t('taste.noOutcomes') }));
  }
  const s = fb.summary || {};
  return h('div', { class: 'knows__outcomes' },
    h('h3', { class: 'detail__h', text: t('taste.outcomes') }),
    s.rated ? h('p', { class: 'help', text: `${t(s.misses ? 'taste.summary.misses' : 'taste.summary', { n: s.count, num: num(s.count), rated: num(s.rated), hits: num(s.hits), misses: num(s.misses) })}${s.mean_rating_of_picks != null && s.user_mean_rating != null ? ` ${t('taste.summary.mean', { picks: fixed(s.mean_rating_of_picks), all: fixed(s.user_mean_rating) })}` : ''}` }) : null,
    h('ul', { class: 'outcomes' }, list.map((o) => h('li', { class: `outcome outcome--${o.verdict}` },
      poster(o, { cls: 'outcome__poster' }),
      h('div', { class: 'outcome__body' },
        h('p', { class: 'outcome__title' }, o.title, o.year ? h('span', { class: 'because__year', text: ` ${o.year}` }) : null),
        h('p', { class: 'outcome__line' },
          t('taste.predicted', { n: Math.round(o.predicted) }),
          o.my_rating != null ? [t('taste.gave'), stars(o.my_rating)] : t('taste.notRated'),
          o.liked ? h('span', { class: 'liked', title: t('taste.liked') }, ' ', icon('heart'), h('span', { class: 'sr-only', text: t('taste.liked') })) : null)),
      h('span', { class: 'verdict', text: VERDICTS[o.verdict] || VERDICTS.pending })))));
}
