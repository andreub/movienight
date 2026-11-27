// UI language: English (`en`) or Spanish (Spain) (`es-ES`). One JSON catalogue per language under /static/i18n/,
// loaded once before any view renders (top-level await: every module that imports `t` waits for it).
// The choice comes from the device (localStorage) or else the browser: `es*` gives es-ES, anything else en.
// A key missing from the chosen catalogue falls back to English. Plural entries are objects keyed by
// Intl.PluralRules categories ({ "one": "…", "other": "…" }), picked by `vars.n`.

export const LANGS = [['en', 'English'], ['es-ES', 'Español']];
const KEY = 'movienight.lang';

// The first preference that is Spanish or English decides (so ['ca', 'es'] gives Spanish); none of them, English.
export function pickLang(languages = []) {
  const first = languages.find((l) => /^(es|en)\b/i.test(l || ''));
  return first && /^es/i.test(first) ? 'es-ES' : 'en';
}

function stored() {
  try { const v = localStorage.getItem(KEY); return LANGS.some(([id]) => id === v) ? v : null; } catch { return null; }
}

export const lang = stored() || pickLang(navigator.languages?.length ? [...navigator.languages] : [navigator.language]);

async function load(id) {
  try {
    const res = await fetch(`/static/i18n/${id}.json`);
    return res.ok ? await res.json() : {};
  } catch { return {}; }
}

const [EN, LOCAL] = await Promise.all([load('en'), lang === 'en' ? null : load(lang)]);
document.documentElement.lang = lang;

const rules = new Intl.PluralRules(lang);

function lookup(cat, key, vars) {
  const v = cat?.[key];
  if (v == null) return null;
  if (typeof v === 'string') return v;
  const n = Number(vars?.n);
  return v[rules.select(Number.isFinite(n) ? n : 0)] ?? v.other ?? null;
}

export function t(key, vars) {
  const s = lookup(LOCAL, key, vars) ?? lookup(EN, key, vars);
  if (s == null) {
    console.warn(`i18n: missing key ${key}`);
    return '';
  }
  return vars ? s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] == null ? m : String(vars[k]))) : s;
}

// Remember the choice on this device and reload, so every view (and the strings built at load) switch at once.
export function setLang(id) {
  try { localStorage.setItem(KEY, id); } catch { /* storage unavailable */ }
  location.reload();
}

// Static markup (index.html): data-i18n sets the text, data-i18n-aria-label / data-i18n-title the attributes.
export function translateDom(root = document) {
  for (const el of root.querySelectorAll('[data-i18n]')) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll('[data-i18n-aria-label]')) el.setAttribute('aria-label', t(el.dataset.i18nAriaLabel));
  for (const el of root.querySelectorAll('[data-i18n-title]')) el.setAttribute('title', t(el.dataset.i18nTitle));
}
