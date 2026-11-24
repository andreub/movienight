// Fixture backend for ?mock=1 (ready library), ?mock=fresh (first-run setup) and ?mock=jellyfin (Jellyfin
// as the media server, read-only card in Setup).
// Posters/backdrops are null on purpose so the generated fallbacks get exercised.

const FILMS = [
  ['Drive', 2011, 100, ['Crime', 'Drama'], 'Nicolas Winding Refn', ['Ryan Gosling', 'Carey Mulligan', 'Bryan Cranston'], 7.6, 'A quiet stunt driver who moonlights as a getaway driver gets pulled into a robbery that goes wrong.'],
  ['Arrival', 2016, 116, ['Science Fiction', 'Drama', 'Mystery'], 'Denis Villeneuve', ['Amy Adams', 'Jeremy Renner', 'Forest Whitaker'], 7.6, 'A linguist is recruited to talk with visitors whose language bends the way she experiences time.'],
  ['Parasite', 2019, 132, ['Comedy', 'Thriller', 'Drama'], 'Bong Joon Ho', ['Song Kang-ho', 'Choi Woo-shik', 'Park So-dam'], 8.5, 'A struggling family schemes its way into the employ of a wealthy household, until a secret in the basement changes everything.'],
  ['Mad Max: Fury Road', 2015, 120, ['Action', 'Adventure', 'Science Fiction'], 'George Miller', ['Tom Hardy', 'Charlize Theron', 'Nicholas Hoult'], 7.6, 'In a desert wasteland, a road warrior and a rebel commander flee a tyrant across a two-hour chase.'],
  ['Spirited Away', 2001, 125, ['Animation', 'Family', 'Fantasy'], 'Hayao Miyazaki', ['Rumi Hiiragi', 'Miyu Irino', 'Mari Natsuki'], 8.5, 'A girl wanders into a bathhouse for spirits and must work there to free her parents.', 'ja'],
  ['The Thing', 1982, 109, ['Horror', 'Science Fiction', 'Mystery'], 'John Carpenter', ['Kurt Russell', 'Wilford Brimley', 'Keith David'], 8.1, 'An Antarctic research crew discovers a shape-shifting organism that can imitate any of them.'],
  ['Alien', 1979, 117, ['Horror', 'Science Fiction'], 'Ridley Scott', ['Sigourney Weaver', 'Tom Skerritt', 'John Hurt'], 8.2, 'The crew of a commercial towing ship answers a distress call and brings something back on board.'],
  ['Heat', 1995, 170, ['Crime', 'Drama', 'Action'], 'Michael Mann', ['Al Pacino', 'Robert De Niro', 'Val Kilmer'], 7.9, 'A meticulous thief and an obsessive detective circle each other across Los Angeles.'],
  ['Before Sunrise', 1995, 101, ['Romance', 'Drama'], 'Richard Linklater', ['Ethan Hawke', 'Julie Delpy'], 7.7, 'Two strangers meet on a train and spend one night walking and talking through Vienna.'],
  ['Paddington 2', 2017, 104, ['Comedy', 'Family', 'Adventure'], 'Paul King', ['Ben Whishaw', 'Hugh Grant', 'Sally Hawkins'], 7.6, 'A polite bear takes odd jobs to buy a pop-up book and ends up framed for its theft.'],
  ['Get Out', 2017, 104, ['Horror', 'Mystery', 'Thriller'], 'Jordan Peele', ['Daniel Kaluuya', 'Allison Williams', 'Catherine Keener'], 7.6, 'A weekend visit to his girlfriend’s parents turns stranger and more sinister by the hour.'],
  ['Whiplash', 2014, 107, ['Drama', 'Music'], 'Damien Chazelle', ['Miles Teller', 'J.K. Simmons'], 8.4, 'A young jazz drummer is pushed to breaking point by a ferocious conservatory teacher.'],
  ['Moonlight', 2016, 111, ['Drama'], 'Barry Jenkins', ['Trevante Rhodes', 'Mahershala Ali', 'Naomie Harris'], 7.4, 'Three chapters in the life of a young man growing up in Miami.'],
  ['Hereditary', 2018, 127, ['Horror', 'Mystery', 'Drama'], 'Ari Aster', ['Toni Collette', 'Alex Wolff', 'Milly Shapiro'], 7.3, 'After a family matriarch dies, her daughter’s household begins to unravel in terrifying ways.'],
  ['Inception', 2010, 148, ['Action', 'Science Fiction', 'Adventure'], 'Christopher Nolan', ['Leonardo DiCaprio', 'Joseph Gordon-Levitt', 'Elliot Page'], 8.4, 'A thief who steals secrets from dreams is hired to plant an idea instead.'],
  ['Knives Out', 2019, 130, ['Comedy', 'Crime', 'Mystery'], 'Rian Johnson', ['Daniel Craig', 'Ana de Armas', 'Chris Evans'], 7.8, 'A famous crime novelist dies after his birthday party, and every relative has a motive.'],
  ['Portrait of a Lady on Fire', 2019, 122, ['Drama', 'Romance', 'History'], 'Céline Sciamma', ['Noémie Merlant', 'Adèle Haenel'], 8.1, 'On an island in 18th-century Brittany, a painter is commissioned to secretly paint a reluctant bride.', 'fr', 'Portrait de la jeune fille en feu'],
  ['The Grand Budapest Hotel', 2014, 99, ['Comedy', 'Drama'], 'Wes Anderson', ['Ralph Fiennes', 'Tony Revolori', 'Saoirse Ronan'], 8.0, 'A legendary concierge and his lobby boy are caught up in the theft of a priceless painting.'],
  ['Zodiac', 2007, 157, ['Crime', 'Drama', 'Mystery'], 'David Fincher', ['Jake Gyllenhaal', 'Mark Ruffalo', 'Robert Downey Jr.'], 7.5, 'A cartoonist becomes obsessed with identifying a serial killer who taunts San Francisco.'],
  ['Amélie', 2001, 122, ['Comedy', 'Romance'], 'Jean-Pierre Jeunet', ['Audrey Tautou', 'Mathieu Kassovitz'], 7.9, 'A shy Parisian waitress decides to quietly change the lives of the people around her.', 'fr', 'Le Fabuleux Destin d’Amélie Poulain'],
  ['Rear Window', 1954, 112, ['Mystery', 'Thriller'], 'Alfred Hitchcock', ['James Stewart', 'Grace Kelly'], 8.2, 'A photographer stuck in a wheelchair starts to suspect a neighbour of murder.'],
  ['Seven Samurai', 1954, 207, ['Action', 'Drama', 'Adventure'], 'Akira Kurosawa', ['Toshiro Mifune', 'Takashi Shimura'], 8.5, 'A village hires seven masterless samurai to defend it against bandits.', 'ja', 'Shichinin no samurai'],
  ['Eternal Sunshine of the Spotless Mind', 2004, 108, ['Romance', 'Drama', 'Science Fiction'], 'Michel Gondry', ['Jim Carrey', 'Kate Winslet'], 8.1, 'After a painful breakup, a man undergoes a procedure to erase his ex from memory.'],
  ['Lost in Translation', 2003, 102, ['Drama', 'Romance', 'Comedy'], 'Sofia Coppola', ['Bill Murray', 'Scarlett Johansson'], 7.4, 'Two sleepless Americans strike up an unlikely friendship in a Tokyo hotel.'],
  ['Prisoners', 2013, 153, ['Crime', 'Thriller', 'Drama'], 'Denis Villeneuve', ['Hugh Jackman', 'Jake Gyllenhaal', 'Viola Davis'], 8.1, 'When his daughter goes missing, a father takes matters into his own hands.'],
  ['Annihilation', 2018, 115, ['Science Fiction', 'Horror', 'Mystery'], 'Alex Garland', ['Natalie Portman', 'Jennifer Jason Leigh', 'Tessa Thompson'], 6.4, 'A biologist joins an expedition into a shimmering zone where nature follows new rules.'],
  ['In the Mood for Love', 2000, 98, ['Drama', 'Romance'], 'Wong Kar-wai', ['Tony Leung Chiu-wai', 'Maggie Cheung'], 8.1, 'Two neighbours in 1960s Hong Kong discover their spouses are having an affair.', 'zh', '花樣年華'],
  ['Everything Everywhere All at Once', 2022, 140, ['Action', 'Comedy', 'Science Fiction'], 'Daniel Kwan', ['Michelle Yeoh', 'Ke Huy Quan', 'Jamie Lee Curtis'], 7.8, 'A laundromat owner in the middle of a tax audit discovers she can borrow skills from other universes.'],
  ['Past Lives', 2023, 106, ['Drama', 'Romance'], 'Celine Song', ['Greta Lee', 'Teo Yoo', 'John Magaro'], 7.8, 'Two childhood friends reunite in New York decades after one of them emigrated.'],
  ['Some Like It Hot', 1959, 121, ['Comedy', 'Romance', 'Crime'], 'Billy Wilder', ['Marilyn Monroe', 'Tony Curtis', 'Jack Lemmon'], 8.2, 'Two musicians who witnessed a mob hit hide out in an all-female band.'],
  ['The Long Quiet Weekend at the Lighthouse Keeper’s Cottage', 2024, null, ['Drama'], 'Anna Lind', ['Mia Holm'], null, null],
  ['Salt Line', 2026, 109, ['Thriller', 'Crime', 'Drama'], 'Marit Voss', ['Elena Marsh', 'Tomas Reyes', 'Idris Okoye'], 7.2, 'A harbour inspector who finds a sealed container that should not exist has one night to decide who to tell.', 'en', null, '2026-09-18'],
];

const MOODS = [
  { id: 'light', label: 'Something light', emoji: '🍿', description: 'Comedy, romance, feel-good', test: (m) => hasAny(m, ['Comedy', 'Family', 'Animation']) },
  { id: 'tense', label: 'Edge of the seat', emoji: '😬', description: 'Thrillers and crime', test: (m) => hasAny(m, ['Thriller', 'Crime']) },
  { id: 'scary', label: 'Scare me', emoji: '👻', description: 'Horror', test: (m) => hasAny(m, ['Horror']) },
  { id: 'mind-bending', label: 'Mind-bending', emoji: '🌀', description: 'Science fiction and puzzles', test: (m) => hasAny(m, ['Science Fiction', 'Mystery']) },
  { id: 'epic', label: 'Something epic', emoji: '🏔️', description: 'Big, long adventures', test: (m) => hasAny(m, ['Adventure', 'Action']) || (m.runtime || 0) > 140 },
  { id: 'heartfelt', label: 'Heartfelt', emoji: '💛', description: 'Drama and romance', test: (m) => hasAny(m, ['Romance', 'Drama']) },
  { id: 'short', label: 'Short one', emoji: '⏱️', description: 'Under 95 minutes', test: (m) => m.runtime && m.runtime < 100 },
  { id: 'just-arrived', label: 'Just arrived', emoji: '🆕', description: 'Added to your library in the last 30 days', test: (m) => Date.now() - Date.parse(m.added_at) <= 30 * 86400000 },
];

// Eras (GET /api/eras): Recent, decades from 1980 (newest first), Classic.
const isRecent = (m) => {
  const now = new Date(), c = new Date(); c.setMonth(c.getMonth() - 18);
  if (m.release_date) { const d = new Date(m.release_date); return d >= c && d <= now; }
  return m.year >= c.getFullYear() && m.year <= now.getFullYear();
};
const eraTest = (id) => {
  if (id === 'recent') return isRecent;
  if (id === 'classic') return (m) => m.year <= 1979;
  const d = /^(\d{3})0s$/.test(id || '') ? Number(id.slice(0, 4)) : null;
  return d && d >= 1980 ? (m) => m.year >= d && m.year <= d + 9 : () => false;
};
function eras(p = {}) {
  const movies = filter({ ...p, era: null, mood: ['recent', 'classic'].includes(p.mood) ? null : p.mood });
  const out = [];
  if (movies.some(isRecent)) out.push({ id: 'recent', label: 'Fresh releases', description: 'Released in the last 18 months' });
  const decades = [...new Set(movies.filter((m) => m.year >= 1980).map((m) => Math.floor(m.year / 10) * 10))].sort((a, b) => b - a);
  for (const d of decades) out.push({ id: `${d}s`, label: `${d}s`, description: `Released in the ${d}s` });
  if (movies.some((m) => m.year <= 1979)) out.push({ id: 'classic', label: 'Classic', description: 'Before 1980' });
  return out;
}

// Letterboxd diary tags (GET /api/tags): a festival with editions, and a tag without a year.
const TAGS = [
  { family: 'fest', label: 'Fest', films: 21, matched: 21, editions: [
    { tag: 'fest2025', edition: 2025, films: 8, matched: 8 }, { tag: 'fest2024', edition: 2024, films: 7, matched: 7 }, { tag: 'fest2023', edition: 2023, films: 6, matched: 6 }] },
  { family: 'date night', label: 'Date night', films: 5, matched: 5, editions: [{ tag: 'date night', edition: null, films: 5, matched: 5 }] },
];
const TAG_TESTS = {
  fest: (m) => hasAny(m, ['Horror', 'Mystery', 'Science Fiction', 'Thriller']),
  fest2025: (m) => hasAny(m, ['Horror', 'Science Fiction']),
  fest2024: (m) => hasAny(m, ['Mystery', 'Thriller']),
  fest2023: (m) => hasAny(m, ['Horror']),
  'date night': (m) => hasAny(m, ['Romance']),
};
const TAG_STATS = [
  { family: 'fest', label: 'Fest', films: 21, matched: 21, mean_rating: 3.6, top_genres: ['Horror', 'Thriller', 'Science Fiction'], editions: [
    { tag: 'fest2025', edition: 2025, films: 8, matched: 8, mean_rating: 3.8, top_genres: ['Horror', 'Science Fiction', 'Drama'] },
    { tag: 'fest2024', edition: 2024, films: 7, matched: 7, mean_rating: 3.4, top_genres: ['Thriller', 'Mystery', 'Horror'] },
    { tag: 'fest2023', edition: 2023, films: 6, matched: 6, mean_rating: 3.5, top_genres: ['Horror', 'Comedy', 'Fantasy'] }] },
  { family: 'date night', label: 'Date night', films: 5, matched: 5, mean_rating: 3.9, top_genres: ['Romance', 'Comedy', 'Drama'], editions: [
    { tag: 'date night', edition: null, films: 5, matched: 5, mean_rating: 3.9, top_genres: ['Romance', 'Comedy', 'Drama'] }] },
  { family: 'rewatch', label: 'Rewatch', films: 2, matched: 2, mean_rating: 4.5, top_genres: ['Drama'], editions: [
    { tag: 'rewatch', edition: null, films: 2, matched: 2, mean_rating: 4.5, top_genres: ['Drama'] }] },
];

const hasAny = (m, gs) => m.genres.some((g) => gs.includes(g));
const params = new URLSearchParams(location.search);
const FRESH = params.get('mock') === 'fresh';
const NOFB = params.get('mock') === 'nofb'; // no accuracy check or outcomes yet
const NOCHAT = params.get('mock') === 'nochat'; // AI chat switched off
const FREECHAT = params.get('mock') === 'free'; // AI chat on a free OpenAI-compatible tier (price 0)
const JELLYFIN = params.get('mock') === 'jellyfin'; // Jellyfin is the media server
const NOCOMPLETE = params.get('mock') === 'nocomplete'; // MOVIENIGHT_COMPLETIONIST=0: no Complete tab
const JF_BASE = 'https://jellyfin.example.com';
const votes = new Map(); // id -> -1 | 1 | 2 (love)
const seenSet = new Set(); // films marked "seen it" in this session
const seenAt = new Map(); // id -> when it was marked (for the "Already marked" list, newest first)

// Extra well-known films, only for the "Seen these?" triage screen (not part of Browse or Tonight).
const CLASSICS = ['The Matrix', 'Fargo', 'The Fellowship of the Ring', 'Pulp Fiction', 'Jurassic Park', 'Forrest Gump', 'Goodfellas', 'Alien', 'Back to the Future', 'The Shining',
  'Gladiator', 'Se7en', 'Casablanca', 'Titanic', 'Toy Story', 'The Silence of the Lambs', 'Braveheart', 'Die Hard', 'Ghostbusters', 'The Princess Bride',
  'Heat', 'Léon: The Professional', 'Psycho', 'Star Wars', 'Raiders of the Lost Ark', 'The Godfather', 'Blade Runner', 'Terminator 2', 'Amélie', 'The Truman Show'];
const triage = Array.from({ length: 84 }, (_, i) => {
  const base = CLASSICS[i % CLASSICS.length];
  return {
    id: 500 + i, title: i < CLASSICS.length ? base : `${base} ${['II', 'III', 'Returns', 'Redux'][i % 4]}`, year: 1975 + ((i * 7) % 48), runtime: 90 + (i % 5) * 12,
    genres: ['Drama'], directors: [], cast: [], poster_url: null, backdrop_url: null, tmdb_votes: 90000 - i * 900, match: 60, reasons: [],
    on_watchlist: false, watched: false, my_rating: null, liked: false, dismissed_until: null, plex: [],
  };
}).filter((m) => m.year <= new Date().getFullYear() - 3);
// A few films marked seen earlier (no verdict yet, one thumb up), for the "Already marked" list.
if (!FRESH) triage.slice(40, 46).forEach((m, i) => { seenSet.add(m.id); seenAt.set(m.id, Date.now() - (i + 1) * 86400e3); });
if (!FRESH) votes.set(triage[41].id, 1);

function reasonsFor(m, i) {
  const r = [];
  if (m.on_watchlist) r.push({ kind: 'watchlist', text: 'On your Letterboxd watchlist' });
  if (i % 3 === 0) r.push({ kind: 'director', text: `You rate ${m.directors[0]}'s films 4.2 on average` });
  if (i % 4 === 1) r.push({ kind: 'similar', text: 'Fans of Heat (you gave it ★★★★½) also love this' });
  if (i % 2 === 0) r.push({ kind: 'cast', text: `Stars ${m.cast[0]}, one of your most-watched actors` });
  if ((m.tmdb_rating || 0) >= 8.1) r.push({ kind: 'acclaimed', text: 'Widely acclaimed: one of the best-rated films on your servers' });
  if (r.length < 2) r.push({ kind: 'genre', text: `${m.genres[0]} is among your favourite genres` });
  if (m.year < 1980) r.push({ kind: 'decade', text: `You rate films from the ${Math.floor(m.year / 10) * 10}s highly` });
  return r.slice(0, 4);
}

// Only these were added in the last 30 days (days ago); every other film was added at least 40 days ago.
const JUST_ARRIVED = { 'Salt Line': 2, 'Arrival': 6, 'Heat': 11, 'Get Out': 19 };

const movies = FILMS.map(([title, year, runtime, genres, director, cast, rating, overview, language = 'en', original, release_date = null], i) => {
  const id = 100 + i;
  const m = {
    id, title, original_title: original || title, year, release_date, runtime, tagline: i % 5 === 0 ? 'Some nights call for exactly this.' : null, overview,
    genres, directors: [director], cast, language, countries: [{ en: 'US', ja: 'JP', fr: 'FR', zh: 'HK' }[language]],
    poster_url: null, backdrop_url: null,
    tmdb_rating: rating, tmdb_votes: rating ? 4000 + ((i * 7919) % 30000) : null, tmdb_id: 1000 + i,
    letterboxd_url: `https://letterboxd.com/tmdb/${1000 + i}`,
    match: Math.max(4, Math.round(97 - i * 2.9 - (i % 3) * 2)),
    on_watchlist: i % 4 === 2, watched: false, my_rating: null, liked: false, dismissed_until: null,
    added_at: new Date(Date.now() - (title in JUST_ARRIVED ? JUST_ARRIVED[title] : 40 + i * 3) * 86400000).toISOString(),
    plex: JELLYFIN
      ? [{ server: 'Den', server_id: 'jf:f00dcafe', resolution: i % 3 === 0 ? '4k' : '1080', url: `${JF_BASE}/web/#/details?id=item${id}&serverId=f00dcafe` }]
      : [{ server: 'Home', server_id: 'home1', resolution: i % 3 === 0 ? '4k' : '1080', url: `https://app.plex.tv/desktop/#!/server/home1/details?key=%2Flibrary%2Fmetadata%2F${id}`, mobile_url: `https://watch.plex.tv/movie/mock-film-${id}` }],
  };
  if (!JELLYFIN && i % 5 === 1) m.plex.push({ server: 'Lucía’s Library', server_id: 'shared2', resolution: '720', url: `https://app.plex.tv/desktop/#!/server/shared2/details?key=%2Flibrary%2Fmetadata%2F${id + 5000}`, mobile_url: `https://watch.plex.tv/movie/mock-film-${id}` });
  m.reasons = reasonsFor(m, i);
  return m;
});

const dismissed = new Map();
const plexState = { connected: !FRESH, pinPolls: 0 };
const lbState = FRESH ? { watched: 0, rated: 0, liked: 0, watchlist: 0, username: '' } : { watched: 289, rated: 240, liked: 9, watchlist: 118, username: 'filmfan' };
let syncStarted = null;
let synced = !FRESH;
const servers = [
  { id: 'home1', name: 'Home', owned: true, movies: 1184, enabled: true, reachable: true },
  { id: 'shared2', name: 'Lucía’s Library', owned: false, movies: 512, enabled: true, reachable: true },
  { id: 'cabin3', name: 'Cabin NAS', owned: true, movies: 88, enabled: false, reachable: false },
];
const SYNC_MS = 12000;

function syncInfo() {
  if (!syncStarted) return { running: false, stage: 'idle', done: 0, total: 0, error: null };
  const t = Date.now() - syncStarted;
  if (t > SYNC_MS) { syncStarted = null; synced = true; return syncInfo(); }
  const stages = [['plex', 1696], ['letterboxd', 289], ['tmdb', 1400], ['profile', 0]];
  const idx = Math.min(3, Math.floor((t / SYNC_MS) * 4));
  const frac = (t % (SYNC_MS / 4)) / (SYNC_MS / 4);
  const [stage, total] = stages[idx];
  return { running: true, stage, done: Math.round(total * frac), total, error: null };
}

const isDismissed = (m) => dismissed.has(m.id) && dismissed.get(m.id) > Date.now();
function view(m) {
  return { ...m, watched: seenSet.has(m.id), seen_here: seenSet.has(m.id), my_vote: votes.get(m.id) || null, dismissed_until: isDismissed(m) ? new Date(dismissed.get(m.id)).toISOString() : null };
}

function filter(p, pool = movies) {
  const genres = [].concat(p.genre || []);
  const q = (p.q || '').toLowerCase();
  const legacy = ['recent', 'classic'].includes(p.mood) ? p.mood : null;
  const mood = legacy ? null : MOODS.find((x) => x.id === p.mood);
  const era = p.era || legacy;
  const eraT = era ? eraTest(era) : null;
  const tagTest = TAG_TESTS[p.tag || p.tag_family];
  return pool.filter((m) => (p.include_dismissed || !isDismissed(m))
    && (p.include_watched || !seenSet.has(m.id))
    && (!mood || mood.test(m))
    && (!eraT || eraT(m))
    && (!tagTest || tagTest(m))
    && (!p.max_runtime || (m.runtime && m.runtime <= p.max_runtime))
    && (!p.watchlist_only || m.on_watchlist)
    && genres.every((g) => m.genres.includes(g))
    && (!q || m.title.toLowerCase().includes(q)));
}

const SORTERS = {
  match: (a, b) => b.match - a.match,
  rating: (a, b) => (b.tmdb_rating || 0) - (a.tmdb_rating || 0),
  recent: (a, b) => b.added_at.localeCompare(a.added_at),
  year: (a, b) => b.year - a.year,
  runtime: (a, b) => (a.runtime || 999) - (b.runtime || 999),
};

function because(m) {
  return [
    { title: 'Heat', year: 1995, my_rating: 4.5, liked: true, poster_url: null, why: 'Similar crime drama you loved' },
    { title: m.directors[0] === 'Denis Villeneuve' ? 'Sicario' : 'Collateral', year: 2004, my_rating: 4, liked: false, poster_url: null, why: m.directors[0] === 'Denis Villeneuve' ? 'Same director' : 'Same mood and pacing' },
    { title: 'Memories of Murder', year: 2003, my_rating: 5, liked: true, poster_url: null, why: 'Fans of both overlap a lot' },
    { title: 'Thief', year: 1981, my_rating: null, liked: false, my_vote: 2, poster_url: null, why: 'Same director (Michael Mann)' },
  ];
}

function profile() {
  const hist = [1, 2, 3, 5, 9, 18, 30, 52, 44, 28].map((count, i) => ({ rating: (i + 1) / 2, count }));
  return {
    model: NOFB ? { films: 0, spearman: null, top_quintile_hit_rate: null, base_rate: null, tuned: false }
      : { films: 230, spearman: 0.31, top_quintile_hit_rate: 0.55, base_rate: 0.3, tuned: true },
    stats: { watched: 289, rated: 240, mean_rating: 3.4, liked: 9, hours: 512, library_unwatched: 900, watchlist_on_plex: 14, seen_here: seenSet.size },
    rating_histogram: hist,
    genres: [['Crime', 34, 3.9, 0.62], ['Mystery', 28, 3.8, 0.48], ['Science Fiction', 41, 3.7, 0.4], ['Thriller', 39, 3.6, 0.3], ['Drama', 120, 3.5, 0.12], ['Animation', 12, 3.5, 0.08], ['Romance', 26, 3.3, -0.05], ['Comedy', 52, 3.1, -0.22], ['Action', 47, 3.0, -0.31], ['Family', 9, 2.7, -0.55]]
      .map(([name, count, mean_rating, affinity]) => ({ name, count, mean_rating, affinity })),
    directors: [['Denis Villeneuve', 5, 4.4, 0.9], ['David Fincher', 6, 4.2, 0.82], ['Bong Joon Ho', 4, 4.3, 0.78], ['Michael Mann', 3, 4.1, 0.6], ['Christopher Nolan', 7, 3.8, 0.42], ['John Carpenter', 4, 3.7, 0.33], ['Wes Anderson', 5, 3.4, 0.05], ['Ridley Scott', 6, 3.1, -0.2]]
      .map(([name, count, mean_rating, affinity]) => ({ name, count, mean_rating, affinity })),
    cast: [['Jake Gyllenhaal', 6, 4.0, 0.7], ['Song Kang-ho', 4, 4.2, 0.68], ['Amy Adams', 5, 3.8, 0.45], ['Oscar Isaac', 5, 3.7, 0.4], ['Tilda Swinton', 6, 3.6, 0.31], ['Ryan Gosling', 7, 3.5, 0.22], ['Tom Hanks', 8, 3.2, -0.08], ['Adam Sandler', 3, 2.2, -0.7]]
      .map(([name, count, mean_rating, affinity]) => ({ name, count, mean_rating, affinity })),
    decades: [[1950, 6, 3.9, 0.45], [1970, 11, 3.8, 0.4], [1980, 19, 3.6, 0.22], [1990, 30, 3.7, 0.3], [2000, 64, 3.4, 0.02], [2010, 108, 3.3, -0.06], [2020, 51, 3.2, -0.12], [1960, 4, 3.5, 0.1]]
      .map(([decade, count, mean_rating, affinity]) => ({ decade, count, mean_rating, affinity })),
    keywords: [['heist', 9, 0.7], ['serial killer', 11, 0.6], ['time loop', 4, 0.55], ['neo-noir', 8, 0.5], ['dystopia', 12, 0.3], ['coming of age', 14, -0.15], ['superhero', 10, -0.6]]
      .map(([name, count, affinity]) => ({ name, count, affinity })),
    top_rated: ['Heat', 'Memories of Murder', 'Zodiac', 'Mulholland Drive', 'Sicario', 'The Handmaiden', 'Children of Men', 'Oldboy', 'Arrival', 'Prisoners', 'Spirited Away', 'Collateral']
      .map((title, i) => ({ title, year: 1995 + i * 2, my_rating: i < 5 ? 5 : 4.5, liked: i < 3, poster_url: null })),
    tags: TAG_STATS,
  };
}

function feedback() {
  const up = [...votes.values()].filter((v) => v === 1).length;
  const down = [...votes.values()].filter((v) => v === -1).length;
  const love = [...votes.values()].filter((v) => v === 2).length;
  const outcomes = NOFB ? [] : [
    ['Prisoners', 2013, 87, 4.5, true, '2026-11-06', 'hit'],
    ['Annihilation', 2018, 74, 2.5, false, '2026-11-03', 'miss'],
    ['Zodiac', 2007, 91, 4, true, '2026-10-29', 'hit'],
    ['Knives Out', 2019, 68, 3.5, false, '2026-10-21', 'ok'],
    ['Moonlight', 2016, 63, null, false, null, 'pending'],
  ].map(([title, year, predicted, my_rating, liked, watched_date, verdict]) => ({ title, year, poster_url: null, predicted, my_rating, liked, watched_date, verdict }));
  return {
    outcomes,
    summary: NOFB ? { count: 0, rated: 0, hits: 0, misses: 0, mean_rating_of_picks: null, user_mean_rating: 3.4 }
      : { count: 5, rated: 4, hits: 2, misses: 1, mean_rating_of_picks: 3.6, user_mean_rating: 3.4 },
    votes: { up: up + (NOFB ? 0 : 5), down: down + (NOFB ? 0 : 2), love: love + (NOFB ? 0 : 1) },
    backtest: NOFB ? { films: 0, spearman: null, top_quintile_hit_rate: null, base_rate: null, weights: {}, default_weights: {}, tuned: false, computed_at: null }
      : { films: 230, spearman: 0.31, top_quintile_hit_rate: 0.55, base_rate: 0.3, weights: { genre: 1.2, director: 0.9, cast: 1 }, default_weights: { genre: 1, director: 1, cast: 1 }, tuned: true, computed_at: new Date(Date.now() - 3 * 86400e3).toISOString() },
  };
}

// ---- completionist (GET /api/completions): a 30-film saga with a few gaps, the Spider-Man franchise (four
// collections merged), a keyword series (the Cornetto trilogy), a 3-film saga, finished sagas and a hidden one.
// Every group is ordered: `films` is the whole group in release order. Films marked 'a' are in the library.
const SAGA_TITLES = ['Dawn', 'Ember', 'The Long Night', 'Ashfall', 'Crown of Salt', 'Iron Tide', 'The Hollow King', 'Starfall', 'Ravenmoor',
  'The Glass Road', 'Thornwood', 'Winterhold', 'The Drowned Bell', 'Sunspear', 'Ninefold', 'The Pale Gate', 'Brightwater', 'Ironbark',
  'The Last Lantern', 'Stormcrow', 'The Quiet Sea', 'Gloamreach', 'The Ninth Banner', 'Silverfall', 'Embermoor', 'The Wandering Keep',
  'Duskmantle', 'The Second Sun', 'Highwatch', 'Homecoming'];
const SAGA_STATES = 'ssssssssssss' + 'm' + 'sssssss' + 'a' + 'sssss' + 'm' + 'a' + 'mm'; // 30: a few gaps late in the saga
const COMPLETIONS = [
  { key: 'collection-9100', kind: 'collection', name: 'The Long Night Chronicles', score: 1.9, reason: 'You rated \u26054.4',
    films: SAGA_TITLES.map((t, i) => [`The Long Night: ${t}`, 1988 + i, SAGA_STATES[i], 4000 + ((i * 3719) % 9000)]) },
  { key: 'franchise-spider-man', kind: 'franchise', name: 'Spider-Man', score: 4.1, reason: 'New 2026', films: [
    ['Spider-Man', 2002, 's', 19000], ['Spider-Man 2', 2004, 'p', 15000], ['Spider-Man 3', 2007, 'a', 14000], ['The Amazing Spider-Man', 2012, 'm', 18000],
    ['The Amazing Spider-Man 2', 2014, 'm', 13000], ['Spider-Man: Homecoming', 2017, 'h', 21000], ['Spider-Man: Into the Spider-Verse', 2018, 's', 16000],
    ['Spider-Man: Far From Home', 2019, 'm', 16000], ['Spider-Man: No Way Home', 2021, 's', 20000], ['Spider-Man: Across the Spider-Verse', 2023, 'a', 7000],
    ['Spider-Man: Brand New Day', 2026, 'm', 900]] },
  { key: 'series-357318', kind: 'series', name: 'Cornetto Trilogy', score: 2.3, reason: 'You rated \u26054.5', films: [
    ['Shaun of the Dead', 2004, 's', 8000], ['Hot Fuzz', 2007, 's', 7600], ['The World’s End', 2013, 'a', 4300]] },
  { key: 'collection-53159', kind: 'collection', name: 'Before', score: 0.9, reason: 'In your library', films: [
    ['Before Sunrise', 1995, 's', 4200], ['Before Sunset', 2004, 'a', 3400], ['Before Midnight', 2013, 'm', 2800]] },
  { key: 'collection-2344', kind: 'collection', name: 'The Matrix', score: 0.6, reason: 'Recent 2021', films: [
    ['The Matrix', 1999, 's', 26000], ['The Matrix Reloaded', 2003, 's', 11000], ['The Matrix Revolutions', 2003, 'm', 9800], ['The Matrix Resurrections', 2021, 'm', 6200]] },
  { key: 'collection-263', kind: 'collection', name: 'The Dark Knight', films: [
    ['Batman Begins', 2005, 's', 21000], ['The Dark Knight', 2008, 's', 33000], ['The Dark Knight Rises', 2012, 's', 23000]] },
  { key: 'collection-10194', kind: 'collection', name: 'Toy Story', films: [
    ['Toy Story', 1995, 's', 18000], ['Toy Story 2', 1999, 's', 14000], ['Toy Story 3', 2010, 's', 15000], ['Toy Story 4', 2019, 's', 9800]] },
  { key: 'collection-295', kind: 'collection', name: 'Pirates of the Caribbean', films: [
    ['The Curse of the Black Pearl', 2003, 's', 21000], ['Dead Man’s Chest', 2006, 'm', 16000], ['At World’s End', 2007, 'm', 14000]] },
  // filmographies (made-up people): no order of their own, so films come as the API sends them: unseen first (in
  // the library, then missing by relevance), then the seen ones, newest first
  { key: 'director-9001', kind: 'director', name: 'Mara Okonkwo', score: 3.2, reason: 'New 2026', films: [
    ['Paper Lanterns', 2004, 'a', 3100], ['The Cartographer', 2009, 'm', 6400], ['Low Tide', 2026, 'm', 2100], ['Northbound', 2023, 's', 5200],
    ['Salt and Iron', 2019, 's', 8800], ['The Quiet Year', 2015, 'h', 4700], ['Glasshouse', 2012, 's', 7300], ['First Light', 2001, 's', 2500]] },
  { key: 'director-9002', kind: 'director', name: 'Tomas Lindqvist', score: 1.4, reason: 'You rated \u26054.3', films: [
    ['Winter Orchard', 1998, 'm', 1900], ['The Ferryman', 1994, 'm', 1200], ['Harbour Lights', 2011, 's', 3600], ['Undertow', 2006, 's', 4100],
    ['A Room in Lisbon', 2002, 'p', 2800]] },
  { key: 'actor-9003', kind: 'actor', name: 'Celia Moreau', score: 2.1, reason: 'In your library', films: [
    ['Night Market', 2021, 'a', 3900], ['The Understudy', 2017, 'a', 2600], ['Blue Hour', 2024, 'm', 5100], ['Silver Coast', 2013, 'm', 4400],
    ['The Long Goodbye Club', 2010, 'm', 1700], ['Fault Lines', 2008, 'm', 1300], ['Ninety Days', 2022, 's', 6100], ['Caravan', 2019, 's', 7200],
    ['The Audition', 2015, 's', 3300], ['Sea Glass', 2011, 's', 2900]] },
  { key: 'actor-9004', kind: 'actor', name: 'Dev Raman', score: 0.8, films: [
    ['Counterweight', 2016, 'm', 8200], ['The Signal', 2012, 'm', 2400], ['Overpass', 2020, 's', 9100], ['Monsoon Run', 2018, 's', 4500],
    ['Quiet Engines', 2014, 's', 3800], ['Rooftops', 2009, 's', 2200]] },
];
// canonical lists (made-up titles): an Oscars-like list of 98 winners and a Sight and Sound-like poll of 100 ranked
// films (ties share a rank). Unordered like filmographies: completionGroup() sorts them the way the API does.
const LIST_W1 = ['Silent', 'Golden', 'Last', 'Broken', 'Distant', 'Crimson', 'Hidden', 'Restless', 'Burning', 'Endless', 'Quiet', 'Wild'];
const LIST_W2 = ['River', 'Empire', 'Harvest', 'Letter', 'Garden', 'Frontier', 'Station', 'Season', 'Promise', 'Mirror', 'Voyage', 'Kingdom'];
const POLL_W1 = ['Portrait', 'Song', 'Night', 'Memory', 'Chronicle', 'Story', 'Passion', 'Ballad', 'Journey', 'Shadow'];
const POLL_W2 = ['a Stranger', 'the Sea', 'Two Rivers', 'Winter', 'the Valley', 'a Small Town', 'Lost Time', 'the Bridge', 'Smoke', 'the Moon'];
COMPLETIONS.push(
  { key: 'list-oscars-best-picture', kind: 'list', name: 'Oscars \u00b7 Best Picture', score: 1.2, reason: 'Recent \u00b7 2025',
    films: Array.from({ length: 98 }, (_, i) => {
      const n = (i * 5) % 144;
      return [`The ${LIST_W1[n % 12]} ${LIST_W2[Math.floor(n / 12)]}`, 1928 + i, i % 3 === 0 ? 's' : i % 11 === 5 ? 'a' : 'm', 400 + ((i * 2311) % 20000)];
    }) },
  { key: 'list-sight-and-sound-2022', kind: 'list', name: 'Sight and Sound \u00b7 Greatest Films 2022', score: 0.7, reason: 'You rated \u26054.1',
    films: Array.from({ length: 100 }, (_, i) => [`${POLL_W1[i % 10]} of ${POLL_W2[Math.floor(i / 10)]}`, 1924 + ((i * 37) % 98),
      i % 5 === 0 ? 's' : i % 13 === 4 ? 'a' : 'm', 200 + ((i * 1777) % 15000), i % 7 === 3 ? i : i + 1]) },
);
const PEOPLE_KINDS = new Set(['director', 'actor']);
const UNORDERED = new Set(['director', 'actor', 'list']);
// "All": sagas, then lists, then people
const blockOf = (kind) => (PEOPLE_KINDS.has(kind) ? 2 : kind === 'list' ? 1 : 0);
// ?mock=1&sagas=N pads the list with generated trilogies up to N groups (long lists: the Screen layout's dots).
for (let n = COMPLETIONS.length; n < Math.min(200, Number(params.get('sagas')) || 0); n += 1) {
  COMPLETIONS.push({ key: `collection-${70000 + n}`, kind: 'collection', name: `Filler Saga ${n}`, score: 0.1, films: [
    [`Filler ${n}: One`, 1990 + (n % 30), 's', 1000], [`Filler ${n}: Two`, 1992 + (n % 30), 'm', 800], [`Filler ${n}: Three`, 1994 + (n % 30), 'a', 600]] });
}
const hiddenGroups = new Set(FRESH ? [] : ['collection-295']);
const completionMovies = [];
// How a seen saga film was seen: 's' Letterboxd, 'p' played on Plex itself, 'h' marked seen here (can be undone).
// localSeen: tmdb id -> vote of the marks made here (GET/POST /api/films/:id).
const localSeen = new Map();
const sagaFilms = new Map(); // tmdb id -> [film, group]
let nextSagaId = 800;
for (const g of COMPLETIONS) {
  g.films = g.films.map(([title, year, st, votes, rank], i) => {
    const via = { s: 'letterboxd', p: 'server', h: 'here' }[st] || null;
    const f = { tmdb_id: 90000 + nextSagaId, title, year, release_date: `${year}-06-01`, backdrop_url: null, state: via ? 'seen' : { a: 'available', m: 'missing' }[st], vote_count: votes, vote_average: 6.4 + ((i * 7) % 20) / 10, poster_url: null };
    if (via) f.via = via;
    if (rank) f.rank = rank;
    if (via === 'here') localSeen.set(f.tmdb_id, 1);
    sagaFilms.set(f.tmdb_id, [f, g]);
    if (f.state === 'available') {
      f.movie_id = nextSagaId;
      completionMovies.push({ ...movies[1], id: nextSagaId, title, original_title: title, year, tmdb_id: f.tmdb_id, genres: ['Adventure'], directors: ['Saga Director'],
        cast: ['Lead Actor'], overview: `Part of ${g.name}.`, tagline: null, match: 70 - (i % 5) * 4, on_watchlist: false, reasons: [] });
    }
    nextSagaId += 1;
    return f;
  });
}
// Seerr (set up in every mock but ?mock=fresh; &seerr=0 turns it off): a few missing films already requested,
// on their way or available there; any other one can be requested (it then waits for approval).
const SEERR = !FRESH && params.get('seerr') !== '0';
const seerrStates = new Map();
for (const [title, state] of [['The Amazing Spider-Man', 'pending'], ['Spider-Man: Far From Home', 'processing'],
  ['The Matrix Resurrections', 'available'], ['Low Tide', 'pending']]) {
  for (const [id, [f]] of sagaFilms) if (f.title === title) seerrStates.set(id, state);
}
// The state a saga film has now, after the marks made here.
function sagaState(f) {
  if (f.via === 'here') return localSeen.has(f.tmdb_id) ? 'seen' : 'missing';
  if (f.state !== 'seen' && (localSeen.has(f.tmdb_id) || seenSet.has(f.movie_id))) return 'seen';
  return f.state;
}
// 'New 2026' / 'Recent 2021' in the data: the API names the newest unseen film of that year in the reason and, for
// filmographies, puts it first (even when it is not in the library).
function recency(g, films) {
  const m = /^(New|Recent) (\d{4})$/.exec(g.reason || '');
  const f = m && films.filter((x) => x.state !== 'seen' && x.year === Number(m[2])).pop();
  if (!f) return { reason: g.reason ?? null, reason_film: null };
  if (PEOPLE_KINDS.has(g.kind)) films.unshift(...films.splice(films.indexOf(f), 1));
  return { reason: `${m[1]} ${m[2]}: ${f.title}`, reason_film: f.tmdb_id };
}
function completionGroup(g) {
  let films = g.films.map(({ via, ...f }) => ({ ...f, state: sagaState({ via, ...f }) }));
  if (g.kind === 'list') { // as the API sends unordered groups: library, then missing by appeal, then seen newest first
    const appeal = (f) => Math.log1p(f.vote_count) * f.vote_average;
    films = [...films.filter((f) => f.state === 'available'), ...films.filter((f) => f.state === 'missing').sort((a, b) => appeal(b) - appeal(a)),
      ...films.filter((f) => f.state === 'seen').sort((a, b) => b.year - a.year)];
  }
  const avail = films.filter((f) => f.state === 'available');
  const seen = films.filter((f) => f.state === 'seen').length;
  const next = avail[0];
  return { key: g.key, kind: g.kind, name: g.name, seen, total: films.length, available: avail.length,
    missing: films.filter((f) => f.state === 'missing').length, complete: seen === films.length, ordered: !UNORDERED.has(g.kind),
    ...(PEOPLE_KINDS.has(g.kind) ? { person_id: Number(g.key.split('-')[1]), profile_url: null } : {}),
    next: next ? { movie_id: next.movie_id, title: next.title, year: next.year } : null, films, hidden: hiddenGroups.has(g.key),
    score: g.score ?? 0, ...recency(g, films), backdrop_url: null };
}
function sagaFilm(id) {
  const [f, g] = sagaFilms.get(id) || [];
  if (!f) return null;
  const state = sagaState(f);
  const via = state !== 'seen' ? [] : f.via && f.via !== 'here' ? [f.via] : ['here'];
  return {
    tmdb_id: f.tmdb_id, movie_id: f.movie_id || null, title: f.title, original_title: f.title, year: f.year,
    runtime: 96 + (f.tmdb_id % 47), tagline: '', genres: ['Adventure', 'Action'], directors: ['Saga Director'], cast: ['Lead Actor', 'Second Lead'],
    overview: `Part of ${g.name}. A chapter that picks up where the last one left off, with old friends, new enemies and more at stake.`,
    language: 'en', countries: ['US'], poster_url: null, backdrop_url: null, tmdb_rating: f.vote_average, tmdb_votes: f.vote_count,
    letterboxd_url: `https://letterboxd.com/tmdb/${f.tmdb_id}`, in_library: Boolean(f.movie_id), seen: via.length > 0, seen_via: via,
    can_unmark: via[0] === 'here', pushed: false, my_vote: via[0] === 'here' ? localSeen.get(f.tmdb_id) || null : null,
    my_rating: via[0] === 'letterboxd' ? 4 : null, liked: false,
  };
}

function completions(p) {
  const all = FRESH ? [] : COMPLETIONS.map(completionGroup);
  // sagas first, then canonical lists, then directors and actors; each block by score
  all.sort((a, b) => blockOf(a.kind) - blockOf(b.kind) || b.score - a.score || a.name.localeCompare(b.name));
  const visible = all.filter((g) => !g.hidden);
  const pick = (g) => !p.kind || g.kind === p.kind || (p.kind === 'collection' && g.kind === 'series');
  const counts = { collection: 0, franchise: 0, series: 0, list: 0, director: 0, actor: 0 };
  for (const g of visible) counts[g.kind] += 1;
  return {
    groups: visible.filter((g) => pick(g) && !g.complete),
    completed: visible.filter((g) => pick(g) && g.complete),
    hidden: p.hidden ? all.filter((g) => g.hidden && pick(g)) : [],
    hidden_count: all.filter((g) => g.hidden).length,
    counts,
  };
}

// Elsewhere in the app, as the API does it: the groups a film is part of (its detail's "Part of X · n of N": two
// sagas or lists at most, a person's films only when there is none), Tonight's "Complete a saga" (the next film of
// each saga in progress that is in the library) and the chat's list_completions tool.
const SAGA_KINDS = new Set(['collection', 'franchise', 'series']);
const summary = (g) => ({ key: g.key, name: g.name, kind: g.kind, seen: g.seen, total: g.total });
function ranked() {
  if (FRESH || NOCOMPLETE) return [];
  return COMPLETIONS.map(completionGroup).filter((g) => !g.hidden)
    .sort((a, b) => blockOf(a.kind) - blockOf(b.kind) || b.score - a.score || a.name.localeCompare(b.name));
}
function groupsOf(tmdbId) {
  if (NOCOMPLETE) return undefined;
  const found = ranked().filter((g) => g.films.some((f) => f.tmdb_id === tmdbId));
  const main = found.filter((g) => !PEOPLE_KINDS.has(g.kind)).slice(0, 2);
  return (main.length ? main : found.filter((g) => PEOPLE_KINDS.has(g.kind)).slice(0, 1)).map(summary);
}
function sagaNext() {
  const out = new Map();
  for (const g of ranked()) {
    if (SAGA_KINDS.has(g.kind) && !g.complete && g.next?.movie_id && !out.has(g.next.movie_id)) out.set(g.next.movie_id, summary(g));
  }
  return out;
}
function sagaView(m, nexts) {
  const g = nexts?.get(m.id);
  const v = { ...view(m), groups: groupsOf(m.tmdb_id) };
  return g ? { ...v, saga: g, reasons: [{ kind: 'trophy', text: `Next in ${g.name} · ${g.seen} of ${g.total} seen` }, ...v.reasons.slice(0, 3)] } : v;
}

const ok = (body, status = 200) => ({ status, body });
const err = (status, detail) => ({ status, body: { detail } });
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

export async function handle(method, path, p, body) {
  await wait(path === '/api/status' ? 120 : 350 + Math.random() * 300);
  const route = `${method} ${path.replace(/\/\d+(?=\/|$)/g, '/:id').replace(/^(\/api\/plex\/servers\/)[^/]+$/, '$1:id')}`;
  const id = Number((path.match(/\/(\d+)(?:\/|$)/) || [])[1]);

  if (NOCOMPLETE && path.startsWith('/api/completions')) return err(404, 'Not Found');
  const comp = path.match(/^\/api\/completions\/([a-z0-9-]+)(\/hide)?$/);
  if (comp) {
    const g = COMPLETIONS.find((x) => x.key === comp[1]);
    if (!g || FRESH) return err(404, 'Group not found');
    if (!comp[2] && method === 'GET') return ok(completionGroup(g));
    if (comp[2] && method === 'POST') { hiddenGroups.add(g.key); return ok({ ok: true }); }
    if (comp[2] && method === 'DELETE') { hiddenGroups.delete(g.key); return ok({ ok: true }); }
  }
  if (route === 'GET /api/completions') return ok(completions(p));
  if (route === 'GET /api/films/:id') { const f = sagaFilm(id); return f ? ok({ ...f, groups: groupsOf(f.tmdb_id) }) : err(404, 'Film not found'); }
  if (route === 'POST /api/films/:id/seen') {
    const f = sagaFilm(id);
    if (!f) return err(404, 'Film not found');
    if (!body.seen && f.seen && !f.can_unmark) return err(409, `Seen on ${f.seen_via[0] === 'letterboxd' ? 'Letterboxd' : 'Plex'}: it can only be unmarked there`);
    if (body.seen) localSeen.set(id, body.vote ?? localSeen.get(id) ?? 0); else localSeen.delete(id);
    return ok({ ok: true });
  }

  if (path.startsWith('/api/seerr/') && !SEERR) return err(404, 'Not Found');
  if (route === 'GET /api/seerr/status') {
    const ids = String(p.tmdb || '').split(',').map(Number).filter((x) => x > 0).slice(0, 40);
    return ok({ statuses: Object.fromEntries(ids.map((x) => [String(x), seerrStates.get(x) || 'none'])) });
  }
  if (route === 'POST /api/seerr/request/:id') {
    if (!seerrStates.has(id)) seerrStates.set(id, 'pending');
    return ok({ status: seerrStates.get(id) });
  }

  switch (route) {
    case 'GET /api/status': {
      const sync = syncInfo();
      const lbReady = lbState.watched > 0;
      if (JELLYFIN) {
        return ok({
          media_server: 'jellyfin',
          letterboxd: { ...lbState, last_sync: lbReady ? new Date(Date.now() - 2 * 3600e3).toISOString() : null },
          plex: { connected: false, account: null, movies: 0, last_sync: null, servers: [], seen_pushed: null },
          jellyfin: { configured: true, connected: true, account: 'movie-fan', movies: 1696, last_sync: synced ? new Date(Date.now() - 2 * 3600e3).toISOString() : null, seen_pushed: null, problem: null },
          tmdb: { configured: true, enriched: 1400, pending: 12 },
          sync: sync.running && sync.stage === 'plex' ? { ...sync, stage: 'jellyfin' } : sync,
          ready: lbReady && synced,
          features: { completionist: !NOCOMPLETE, seerr: SEERR },
        });
      }
      return ok({
        media_server: 'plex',
        letterboxd: { ...lbState, last_sync: lbReady ? new Date(Date.now() - 2 * 3600e3).toISOString() : null },
        plex: plexState.connected
          ? { connected: true, account: 'movie-fan', movies: 1696, last_sync: synced ? new Date(Date.now() - 2 * 3600e3).toISOString() : null, servers }
          : { connected: false, account: null, movies: 0, last_sync: null, servers: [] },
        tmdb: { configured: !FRESH, enriched: 1400, pending: 12 },
        sync,
        ready: plexState.connected && lbReady && synced,
        features: { completionist: !NOCOMPLETE, seerr: SEERR },
      });
    }
    case 'GET /api/moods': return ok(MOODS.map(({ test, ...m }) => m));
    case 'GET /api/eras': return ok(eras(p));
    case 'GET /api/tags': return ok({ min_films: 3, families: FRESH ? [] : TAGS });
    case 'GET /api/recommendations': {
      const list = filter(p).sort(SORTERS[p.sort] || SORTERS.match);
      const counts = {};
      for (const m of list) for (const g of m.genres) counts[g] = (counts[g] || 0) + 1;
      const offset = Number(p.offset) || 0, limit = Number(p.limit) || 24;
      return ok({
        items: list.slice(offset, offset + limit).map(view),
        total: list.length,
        facets: { genres: Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([name, count]) => ({ name, count })), min_year: 1954, max_year: 2024 },
      });
    }
    case 'GET /api/pick': {
      const exclude = [].concat(p.exclude || []).map(Number);
      const nexts = sagaNext();
      const sagaFilms = filter(p, completionMovies).filter((m) => nexts.has(m.id));
      const list = (p.complete ? sagaFilms : filter(p)).filter((m) => !exclude.includes(m.id)).sort(SORTERS.match);
      if (!list.length) return err(404, 'No film matches these filters.');
      const top = list.slice(0, 6);
      const pick = top[Math.floor(Math.random() * top.length)];
      const shown = p.complete ? nexts : null;
      return ok({ pick: sagaView(pick, shown), alternatives: list.filter((m) => m !== pick).slice(0, 3).map((m) => sagaView(m, shown)),
        ...(NOCOMPLETE ? {} : { saga_count: sagaFilms.length }) });
    }
    case 'GET /api/movies/:id': {
      const m = movies.find((x) => x.id === id) || completionMovies.find((x) => x.id === id);
      if (!m) return err(404, 'Movie not found.');
      return ok({ ...view(m), keywords: ['neo-noir', 'night', 'los angeles', 'loneliness', 'heist'], because: because(m), groups: groupsOf(m.tmdb_id) });
    }
    case 'POST /api/movies/:id/dismiss':
      dismissed.set(id, body.days === 0 ? Date.parse('9999-12-31') : Date.now() + body.days * 86400e3);
      return ok({ ok: true });
    case 'DELETE /api/movies/:id/dismiss':
      dismissed.delete(id);
      return ok({ ok: true });
    case 'POST /api/movies/:id/feedback':
      if (![-1, 0, 1, 2].includes(body.vote)) return err(422, 'Invalid vote');
      if (body.vote) votes.set(id, body.vote); else votes.delete(id);
      return ok({ ok: true });
    case 'POST /api/movies/:id/seen':
      if (![...movies, ...triage, ...completionMovies].some((x) => x.id === id)) return err(404, 'Movie not found.');
      if (body.vote !== undefined && body.vote !== null && ![-1, 0, 1, 2].includes(body.vote)) return err(422, 'Invalid vote');
      if (body.seen) { if (!seenSet.has(id)) seenAt.set(id, Date.now()); seenSet.add(id); } else { seenSet.delete(id); seenAt.delete(id); }
      if (body.vote !== undefined && body.vote !== null) { if (body.vote) votes.set(id, body.vote); else votes.delete(id); }
      return ok({ ok: true });
    case 'GET /api/seen-marked': {
      const list = [...movies, ...triage].filter((m) => seenSet.has(m.id)).sort((a, b) => seenAt.get(b.id) - seenAt.get(a.id));
      const offset = Number(p.offset) || 0, limit = Number(p.limit) || 60;
      return ok({ items: list.slice(offset, offset + limit).map(view), total: list.length });
    }
    case 'GET /api/seen-candidates': {
      const list = triage.filter((m) => !seenSet.has(m.id)).sort((a, b) => b.tmdb_votes - a.tmdb_votes);
      const offset = Number(p.offset) || 0, limit = Number(p.limit) || 60;
      return ok({ items: list.slice(offset, offset + limit).map(view), total: list.length });
    }
    case 'GET /api/feedback':
      return FRESH ? err(409, 'Import your Letterboxd history first.') : ok(feedback());
    case 'GET /api/me':
      return ok({ auth: false, name: null });
    case 'GET /api/chat/status':
      return ok({
        enabled: !NOCHAT, provider: FREECHAT ? 'openai' : 'anthropic', model: FREECHAT ? 'openai/gpt-oss-120b' : 'claude-sonnet-5',
        problem: NOCHAT ? 'No API key is configured (ANTHROPIC_API_KEY).' : null,
        usage: params.get('mock') === 'budget'
          ? { month: '2026-11', spent_usd: 2.01, budget_usd: 2, questions: 61, tokens: 912000, model: 'claude-sonnet-5', exhausted: true }
          : FREECHAT
            ? { month: '2026-11', spent_usd: 0, budget_usd: 2, questions: 9, tokens: 48700, model: 'openai/gpt-oss-120b', exhausted: false }
            : { month: '2026-11', spent_usd: 0.0123, budget_usd: 2, questions: 14, tokens: 23400, model: 'claude-sonnet-5', exhausted: false },
      });
    case 'GET /api/profile':
      return lbState.watched ? ok(profile()) : err(409, 'Import your Letterboxd history first.');
    case 'POST /api/plex/pin':
      plexState.pinPolls = 0;
      return ok({ id: 555, code: 'MN4K', auth_url: 'about:blank#mock-plex-sign-in' });
    case 'GET /api/plex/pin/:id':
      if (++plexState.pinPolls < 3) return ok({ authorized: false });
      plexState.connected = true;
      syncStarted = Date.now();
      return ok({ authorized: true });
    case 'POST /api/plex/logout':
      plexState.connected = false;
      synced = false;
      return ok({ ok: true });
    case 'PUT /api/plex/servers/:id': {
      const s = servers.find((x) => x.id === decodeURIComponent(path.split('/').pop()));
      if (s) s.enabled = body.enabled;
      return ok({ ok: true });
    }
    case 'GET /api/players':
      return ok(JELLYFIN ? [] : [{ id: 'tv1', name: 'Living Room TV', product: 'Chromecast' }]);
    case 'POST /api/play':
      return ok(null, 204);
    case 'POST /api/sync':
      if (syncInfo().running) return err(409, 'A sync is already running.');
      syncStarted = Date.now();
      return ok({ started: true }, 202);
    case 'PUT /api/letterboxd/user':
      lbState.username = body.username;
      return ok({ ok: true });
    case 'POST /api/letterboxd/import':
      await wait(1200);
      Object.assign(lbState, { watched: 289, rated: 240, liked: 9, watchlist: 118 });
      return ok({ watched: 289, rated: 240, liked: 9, watchlist: 118 });
    default:
      return err(404, `Mock has no route for ${method} ${path}`);
  }
}

// ---- fake chat stream: tool label, text deltas, movies, done ----

const sleep = (ms, signal) => new Promise((resolve, reject) => {
  const t = setTimeout(resolve, ms);
  signal?.addEventListener('abort', () => { clearTimeout(t); reject(new DOMException('Aborted', 'AbortError')); }, { once: true });
});

export async function* chatStream(messages, signal) {
  const ask = (messages[messages.length - 1]?.content || '').toLowerCase();
  await sleep(500, signal);
  if (ask.includes('error')) {
    yield { event: 'text', data: { delta: 'Let me look' } };
    yield { event: 'error', data: { detail: 'The assistant stopped unexpectedly. Try again in a moment.' } };
    yield { event: 'done', data: {} };
    return;
  }
  if (!NOCOMPLETE && /saga|series|franchise|finish|continue|complet/.test(ask)) {
    yield* sagaChat(signal);
    return;
  }
  const mood = ask.includes('tense') ? 'tense' : ask.includes('comfort') ? 'light' : ask.includes('watchlist') ? 'wl' : null;
  yield { event: 'tool', data: { name: 'search_library', label: 'Searching your library…' } };
  await sleep(1100, signal);
  const pool = movies.filter((m) => !isDismissed(m)
    && (mood === 'wl' ? m.on_watchlist : mood ? MOODS.find((x) => x.id === mood).test(m) : true)
    && (!ask.includes('under 2 hours') || (m.runtime && m.runtime < 120)));
  const picks = (pool.length >= 3 ? pool : movies).slice(0, 3);
  yield { event: 'tool', data: { name: 'check_taste', label: 'Checking it against your taste…' } };
  await sleep(900, signal);
  const intro = `Here is what I would put on tonight.\n\n**${picks[0].title}** is the safest bet: ${picks[0].genres[0].toLowerCase()} done with real confidence, and it fits the time you have.\n\nTwo more worth a look:\n- **${picks[1].title}** if you want something with a different pace\n- **${picks[2].title}** if you are in the mood to be surprised\n\nTell me if you would like something shorter, older or lighter.`;
  for (const word of intro.match(/\S+\s*|\n+/g)) {
    yield { event: 'text', data: { delta: word } };
    await sleep(28, signal);
    if (word.includes('Two')) {
      yield { event: 'movies', data: { items: picks.map((m, i) => ({ ...view(m), pitch: ['A clean, confident watch that rewards your love of tight plots.', 'Slower and stranger, with an ending you will want to talk about.', 'A wildcard that sits just outside your usual taste.'][i] })) } };
    }
  }
  yield { event: 'done', data: {} };
}

// "Which saga should I continue?": the list_completions tool, then the sagas' next films in the library as cards.
async function* sagaChat(signal) {
  yield { event: 'tool', data: { name: 'list_completions', label: 'Checking the sagas you started…' } };
  await sleep(900, signal);
  const nexts = sagaNext();
  const picks = [...nexts.keys()].slice(0, 3).map((id) => completionMovies.find((m) => m.id === id)).filter(Boolean);
  if (!picks.length) {
    yield { event: 'text', data: { delta: 'You have no saga in progress with its next film in your library right now.' } };
    yield { event: 'done', data: {} };
    return;
  }
  const line = (m) => { const g = nexts.get(m.id); return `**${m.title}** continues ${g.name} (${g.seen} of ${g.total} seen)`; };
  const intro = `You have a few sagas waiting.\n\n${line(picks[0])}, and it is already in your library.${picks.length > 1 ? `\n\nAlso ready to go:\n${picks.slice(1).map((m) => `- ${line(m)}`).join('\n')}` : ''}`;
  for (const word of intro.match(/\S+\s*|\n+/g)) {
    yield { event: 'text', data: { delta: word } };
    await sleep(24, signal);
  }
  yield { event: 'movies', data: { items: picks.map((m) => ({ ...sagaView(m, nexts), pitch: `The next one in ${nexts.get(m.id).name}, so the story picks up where you left it.` })) } };
  yield { event: 'done', data: {} };
}
