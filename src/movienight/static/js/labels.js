// Labels the server sends as stable keys plus params, worded here in the UI language. Each comes with the server's
// English text too, which is shown when the catalogue has no entry for the key (a newer server, say).
//   reasons      {kind, key, params, text}      catalogue reason.<key>
//   because      {why_key, why_params, why}     because.<key>
//   group "why"  {reason_key, reason_params, reason}   creason.<key>
//   moods/eras   by id                          mood.<id>.label / .description, era.<id> / era.decade
//   genres       TMDB's English genre names     genre.<name>
//   chat tools   {name, label}                  ask.tool.<name>
//   errors       {code, params, detail}         apiError.<code>
import { t, has, lang } from './i18n.js';

const compact = new Intl.NumberFormat(lang, { notation: 'compact', maximumFractionDigits: 1 });
const one = new Intl.NumberFormat(lang, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const list = (() => {
  try { return new Intl.ListFormat(lang, { style: 'long', type: 'conjunction' }); } catch { return null; }
})();

export const genreName = (name) => (name && has(`genre.${name}`) ? t(`genre.${name}`) : name);
export const genreList = (names = [], n = Infinity) => names.slice(0, n).map(genreName).join(', ');
const listOf = (xs) => (list ? list.format(xs) : xs.join(', '));
// Spanish names decades "los 90" but "los 2000": two digits before 2000 (English keeps "1990s").
const decadeWord = (d) => (/^es/.test(lang) && Number(d) < 2000 ? String(d).slice(2) : String(d));

// params as the catalogue wants them: genres translated, numbers in the locale's format
function words(params = {}) {
  const out = { ...params };
  if (out.genre) out.genre = genreName(out.genre);
  if (Array.isArray(out.keywords)) out.keywords = listOf(out.keywords);
  if (typeof out.rating === 'number') out.rating = one.format(out.rating);
  if (typeof out.votes === 'number') out.votes = compact.format(out.votes);
  if (out.decade != null) out.decade = decadeWord(out.decade);
  if (typeof out.budget === 'number') out.budget = new Intl.NumberFormat(lang, { style: 'currency', currency: 'USD' }).format(out.budget);
  return out;
}

function keyed(prefix, key, params, fallback) {
  return key && has(`${prefix}.${key}`) ? t(`${prefix}.${key}`, words(params)) : (fallback || '');
}

export const reasonText = (r) => keyed('reason', r?.key, r?.params, r?.text);
export const becauseText = (b) => keyed('because', b?.why_key, b?.why_params, b?.why);
export const groupReason = (g) => keyed('creason', g?.reason_key, g?.reason_params, g?.reason);
export const toolLabel = (ev) => (ev?.name && has(`ask.tool.${ev.name}`) ? t(`ask.tool.${ev.name}`) : ev?.label || t('ask.working'));
export const errorText = (data) => keyed('apiError', data?.code, data?.params, typeof data?.detail === 'string' ? data.detail : '');

export const moodLabel = (m) => (has(`mood.${m.id}.label`) ? t(`mood.${m.id}.label`) : m.label);
export const moodDesc = (m) => (has(`mood.${m.id}.description`) ? t(`mood.${m.id}.description`) : m.description);

const DECADE = /^(\d{4})s$/;
export function eraLabel(e) {
  const d = DECADE.exec(e.id || '');
  if (d) return t('era.decade', { decade: decadeWord(d[1]) });
  return has(`era.${e.id}`) ? t(`era.${e.id}`) : e.label || String(e.id);
}
