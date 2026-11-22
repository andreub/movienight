// "Complete": sagas, franchises and curated trilogies the user has started, as a single column of roomy cards.
// A card shows its progress ("4 of 9 seen") and at most five unseen films: the ones in the library first (full
// colour, tap opens the detail sheet), then missing ones (greyed). Seen films are only counted, never drawn,
// until "+N more · show all" expands the whole list in saga order. Finished and hidden groups stay folded.
import { api } from '../api.js';
import { go } from '../state.js';
import { h, fill, icon, poster, errorBox, toast, announce, quoted } from '../dom.js';

export const title = 'Complete';

const KINDS = [
  [null, 'All'], ['collection', 'Sagas'], ['franchise', 'Franchises'], ['curated', 'Trilogies'],
];

const pct = (g) => (g.total ? Math.round((g.seen / g.total) * 100) : 0);

function progress(g) {
  return h('div', { class: 'cprog' },
    h('div', {
      class: 'cprog__bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(g.total),
      'aria-valuenow': String(g.seen), 'aria-label': `${g.name}: ${g.seen} of ${g.total} seen`,
    }, h('span', { class: 'cprog__fill', style: { width: `${pct(g)}%` } })),
    h('span', { class: 'cprog__label' },
      h('strong', { text: String(g.seen) }), ` of ${g.total} seen`,
      g.available ? h('span', { class: 'cprog__avail', text: ` · ${g.available} in library` }) : null));
}

// One film of a strip or of the full list. Available films open the detail sheet; the others are not links.
function film(f, { caption = false } = {}) {
  const label = `${f.title}${f.year ? ` (${f.year})` : ''}${f.state === 'available' ? ', in your library' : f.state === 'seen' ? ', seen' : ', not in your library'}`;
  const art = h('div', { class: 'cfilm__art' },
    poster(f, { label: caption ? null : label }),
    f.state === 'available' ? h('span', { class: 'cfilm__badge', text: 'In library' }) : null,
    f.state === 'seen' ? h('span', { class: 'cfilm__seen', title: 'Seen' }, icon('check')) : null);
  const cap = caption ? h('span', { class: 'cfilm__cap' },
    h('span', { class: 'cfilm__title', text: f.title }),
    f.year ? h('span', { class: 'cfilm__year', text: String(f.year) }) : null) : null;
  const body = f.state === 'available' && f.movie_id
    ? h('button', { type: 'button', class: 'cfilm__btn', 'aria-label': caption ? null : label, title: f.title, onclick: () => go({ movie: f.movie_id }) }, art, cap)
    : h('div', { class: 'cfilm__btn', title: f.title }, art, cap);
  return h('li', { class: `cfilm cfilm--${f.state}` }, body);
}

export function mount(root) {
  let kind = null;
  let alive = true;
  let token = 0;
  let data = null;
  const expanded = new Set(); // keys whose full list is open (kept across reloads)

  const chips = KINDS.map(([id, label]) => h('button', {
    type: 'button', class: 'chip chip--small', 'aria-pressed': String(id === kind), 'data-kind': id || '',
    onclick: () => { kind = id; paintChips(); load(); },
  }, h('span', { text: label }), h('span', { class: 'chip__count' })));
  const list = h('ul', { class: 'cgroups' });
  const status = h('div');
  const folds = h('div', { class: 'cfolds' });

  root.append(h('div', { class: 'complete' },
    h('div', { class: 'complete__top' },
      h('h1', { class: 'page-title', text: 'Complete' }),
      h('p', { class: 'lede', text: 'Sagas and trilogies you have started. The ones you can finish from your library come first.' }),
      h('div', { class: 'chips chips--scroll complete__kinds', role: 'group', 'aria-label': 'Kind of group' }, chips)),
    status, list, folds));

  function paintChips() {
    for (const c of chips) {
      const id = c.dataset.kind || null;
      c.setAttribute('aria-pressed', String(id === kind));
      const n = data?.counts ? (id ? data.counts[id] : Object.values(data.counts).reduce((a, b) => a + b, 0)) : null;
      c.querySelector('.chip__count').textContent = n == null ? '' : String(n);
      c.hidden = Boolean(id) && data?.counts && !data.counts[id] && id !== kind;
    }
  }

  function card(g) {
    const all = h('div', { class: 'cgroup__all', hidden: true });
    const strip = h('ul', { class: 'cstrip', 'aria-label': `Still to watch in ${g.name}` }, g.films_preview.map((f) => film(f)));
    let open = false;
    const moreBtn = h('button', { type: 'button', class: 'cmore', 'aria-expanded': 'false', onclick: () => toggle() });
    const paintMore = () => {
      moreBtn.textContent = open ? 'Show less' : g.more ? `+${g.more} more · show all` : 'Show all';
      moreBtn.setAttribute('aria-expanded', String(open));
    };
    async function toggle(force) {
      open = force ?? !open;
      paintMore();
      strip.hidden = open;
      all.hidden = !open;
      if (open) expanded.add(g.key); else expanded.delete(g.key);
      if (!open || all.dataset.loaded) return;
      fill(all, h('ul', { class: 'cfull' }, Array.from({ length: Math.min(g.total, 10) }, () => h('li', { class: 'cfilm' }, h('div', { class: 'poster skel' })))));
      try {
        const full = await api.completion(g.key);
        if (!alive) return;
        all.dataset.loaded = '1';
        fill(all, h('ul', { class: 'cfull', 'aria-label': `Every film in ${g.name}` }, full.films.map((f) => film(f, { caption: true }))));
      } catch (err) {
        fill(all, errorBox(err, () => { delete all.dataset.loaded; toggle(true); }));
      }
    }
    paintMore();
    const li = h('li', { class: 'cgroup', 'data-key': g.key },
      h('div', { class: 'cgroup__head' },
        h('h2', { class: 'cgroup__name', text: g.name }),
        h('button', { type: 'button', class: 'cgroup__hide', 'aria-label': `Hide ${g.name}`, title: 'Hide', onclick: () => hide(g, li) }, icon('close'))),
      progress(g),
      h('div', { class: 'cgroup__films' }, strip, all, h('div', { class: 'cgroup__foot' }, moreBtn)));
    if (expanded.has(g.key)) toggle(true);
    return li;
  }

  function row(g, { action } = {}) {
    return h('li', { class: 'crow' },
      h('span', { class: 'crow__icon' }, icon(g.complete ? 'trophy' : 'eyeoff')),
      h('span', { class: 'crow__name', text: g.name }),
      h('span', { class: 'crow__meta', text: g.complete ? `All ${g.total} seen` : `${g.seen} of ${g.total}` }),
      action || null);
  }

  async function hide(g, li) {
    li.classList.add('is-leaving');
    try {
      await api.hideCompletion(g.key);
    } catch (err) {
      li.classList.remove('is-leaving');
      toast(err.detail || 'Could not hide it.');
      return;
    }
    announce(`${g.name} hidden.`);
    toast(`Hid ${quoted({ title: g.name })}.`, {
      action: { label: 'Undo', run: () => api.unhideCompletion(g.key).then(load).catch((e) => toast(e.detail || 'Could not undo.')) },
    });
    load();
  }

  function folded(label, items, { onOpen } = {}) {
    const body = h('ul', { class: 'crows' }, items);
    const d = h('details', { class: 'cfold' }, h('summary', { class: 'cfold__sum' }, icon('chevron', 'cfold__chev'), h('span', { text: label })), body);
    if (onOpen) d.addEventListener('toggle', () => { if (d.open) onOpen(body); }, { once: true });
    return d;
  }

  function paint() {
    paintChips();
    const groups = data.groups;
    fill(list, groups.map(card));
    if (!groups.length) {
      const any = data.completed.length || data.hidden_count;
      fill(status, h('div', { class: 'state' },
        h('p', { class: 'state__title', text: any ? 'All caught up' : 'Nothing started yet' }),
        h('p', { class: 'state__text', text: any
          ? 'Every saga you have started here is finished or hidden.'
          : 'Watch one film of a saga or trilogy and it shows up here, with what is left to see.' })));
    } else fill(status);
    const parts = [];
    if (data.completed.length) parts.push(folded(`Completed (${data.completed.length})`, data.completed.map((g) => row(g))));
    if (data.hidden_count) {
      parts.push(folded(`Show hidden (${data.hidden_count})`, [], {
        onOpen: async (body) => {
          try {
            const r = await api.completions({ kind, hidden: true });
            fill(body, r.hidden.map((g) => row(g, {
              action: h('button', { type: 'button', class: 'linkbtn', onclick: () => api.unhideCompletion(g.key).then(load).catch((e) => toast(e.detail || 'Could not unhide it.')) }, 'Unhide'),
            })));
          } catch (err) { fill(body, errorBox(err)); }
        },
      }));
    }
    fill(folds, parts);
  }

  async function load() {
    const t = ++token;
    if (!data) fill(list, Array.from({ length: 3 }, () => h('li', { class: 'cgroup cgroup--skeleton', 'aria-hidden': 'true' },
      h('div', { class: 'skel skel--line skel--short' }), h('div', { class: 'skel skel--line' }),
      h('div', { class: 'cgroup__films' }, h('ul', { class: 'cstrip' }, Array.from({ length: 5 }, () => h('li', { class: 'cfilm' }, h('div', { class: 'poster skel' }))))))));
    try {
      const r = await api.completions({ kind });
      if (!alive || t !== token) return;
      data = r;
      paint();
    } catch (err) {
      if (alive && t === token) { fill(list); fill(status, errorBox(err, load)); }
    }
  }

  // A film marked seen (detail sheet) changes the counts: refresh quietly.
  const onSeen = () => load();
  window.addEventListener('movienight:seen', onSeen);
  paintChips();
  load();

  return {
    update() {},
    destroy() { alive = false; window.removeEventListener('movienight:seen', onSeen); },
  };
}
