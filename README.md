# MovieNight

"What do I watch tonight?" MovieNight ranks the movies available on your Plex servers (or your
Jellyfin server, see [Media server](#media-server-plex-or-jellyfin)) using your Letterboxd history (watched films, ratings, likes, watchlist) and TMDB metadata.
It is a single-user app with no login, meant to run on a private network.

## How it gets data

- **Letterboxd**: a data export (folder or ZIP upload) is the baseline for watched, rated,
  liked and watchlist films, and for diary tags (see below). New diary entries are picked up
  automatically from the public RSS feed.
- **Plex**: connect with a PIN login (plex.tv). Movie libraries from every server you own or
  have been shared are indexed; the first reachable connection is used.
- **Jellyfin** (instead of Plex): user name and password from the environment; the movies that
  account can see are indexed.
- **TMDB**: metadata (genres, cast, crew, keywords, posters, ratings) and recommendations.
  Responses are cached forever in the local database.

## Votes and "seen it" marks

- **Votes** on a film (detail sheet, tonight's pick, browse cards): thumbs down (-1), thumbs up (1)
  and ❤ "a favourite" (2). A vote teaches the recommender about films you never rated on
  Letterboxd: the film counts as a synthetic history entry (signal ±0.6, 0.95 for a favourite,
  about a liked 5-star film) and gets a direct boost or penalty. A Letterboxd rating always wins:
  once a film is rated there, its vote is ignored.
- **Seen it** marks a film as already watched here (not on Letterboxd): it stops showing up in your
  picks. On its own the mark is neutral; it teaches nothing about taste.
- **Seen these?** (`#/seen`) lists popular older films you probably know. Each card has four
  one-tap verdicts: 👎 seen, didn't like it · ✓ seen, no opinion · 👍 seen, liked it · ❤ a
  favourite. One tap saves the mark and the vote together (`POST /api/movies/{id}/seen` with
  `{"seen": true, "vote": 2}`); tapping the chosen verdict again removes both. The "Already
  marked" tab (`GET /api/seen-marked`, newest mark first) lets you rate films marked earlier.
- With `MOVIENIGHT_PUSH_SEEN` on, marks are also set as played on Plex or Jellyfin, whichever way they are
  made. Only items the app marked itself are ever set back to unplayed.

## Letterboxd tags

Tags on diary entries (`diary.csv` and `reviews.csv` of the export, comma-separated, compared in lower
case) are stored per entry. A tag ending in a 4-digit year is an edition of a family: `sitges2024`,
`sitges2025` and `sitges 2026` all belong to `sitges`; a tag without a year is its own family.

Tagged films are already watched, so a tag is a reference group, not a filter on them: "films like
the ones I tagged X".

- **Tonight / Browse**: a "Like my …" chip per family next to the moods; once picked, a second row
  offers its editions. The filter keeps the quarter of the library most similar to the group (at
  least 40 films) and ranks it by your taste plus 1.5x that similarity, computed with the same
  affinity tables, idf weights and TMDB recommendations as the taste score. It combines with moods,
  length, watchlist and the seen/hidden exclusions. API: `tag=sitges2025` or `tag_family=sitges` on
  `/api/recommendations` and `/api/pick`; `GET /api/tags` lists the groups that can be used (at least
  3 films with TMDB details).
- **Taste**: per family and edition, number of films, mean rating of the entries and top genres
  (`tags` in `/api/profile`).
- **Ask**: the chat has a `list_tags` tool and a `tag` parameter on `search_library` ("something like
  what I saw at Sitges 2025").

The RSS feed carries no tags, so new tags arrive with the next export.

## Quick start (Docker)

```sh
cp .env.example .env          # optional: fill in TMDB token, Letterboxd username
mkdir -p data letterboxd      # put your unpacked Letterboxd export in ./letterboxd if you have one
docker compose up -d --build
```

Open http://localhost:8080 and connect Plex in Settings (with Jellyfin, set its variables instead).

## Local development (uv)

```sh
uv sync
uv run uvicorn movienight.app:app --reload --port 8080
uv run pytest
```

The UI can run without a backend in mock mode at http://localhost:8080/?mock=1.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MOVIENIGHT_DATA_DIR` | `./data` (`/data` in image) | SQLite database and image cache |
| `MOVIENIGHT_LETTERBOXD_DIR` | `./letterboxd` (`/letterboxd` in image) | Unpacked Letterboxd export, imported at start if present or changed |
| `MOVIENIGHT_LETTERBOXD_USER` | empty | Username for RSS sync; empty = read from `profile.csv` |
| `MOVIENIGHT_TMDB_TOKEN` | empty | TMDB v4 bearer token (`eyJ...`) or v3 API key; empty = degraded mode |
| `MOVIENIGHT_MEDIA_SERVER` | `plex` | `plex` or `jellyfin`: where the library comes from (one at a time; any other value stops the app at start) |
| `MOVIENIGHT_PLEX_TOKEN` | empty | Plex only: optional pre-set Plex token (otherwise PIN login in the UI) |
| `MOVIENIGHT_JELLYFIN_URL` | empty | Jellyfin only: server URL as opened in the browser, e.g. `https://jellyfin.example.com` |
| `MOVIENIGHT_JELLYFIN_USER` / `_PASSWORD` | empty | Jellyfin only: the account MovieNight signs in with |
| `MOVIENIGHT_JELLYFIN_WATCHED` | on | `0`: ignore what was played in Jellyfin (use it when the account is shared with other people) |
| `MOVIENIGHT_SYNC_HOURS` | `6` | Background sync interval |
| `MOVIENIGHT_PUSH_SEEN` | off | `1`/`true`/`yes`: marks films you flag "seen it" as played on the media server (writes to that account). `MOVIENIGHT_PLEX_PUSH_SEEN` is the older name and still works |
| `MOVIENIGHT_LOGIN` | `plex` (Plex), empty (Jellyfin) | Login methods, comma-separated: `plex`, `oidc`, or `none`. See [Login](#login-optional) |
| `MOVIENIGHT_PLEX_OWNER` | empty | Plex login only: the account id, username or email allowed in; empty = the first sign-in (or the account of the stored token) |
| `MOVIENIGHT_PORT` | `8080` | Compose only: host port |
| `TZ` | `Europe/Madrid` | Compose only: container timezone |

## Media server: Plex or Jellyfin

`MOVIENIGHT_MEDIA_SERVER` picks one: `plex` (default) or `jellyfin`. The app never reads both.

**Jellyfin** needs `MOVIENIGHT_JELLYFIN_URL`, `MOVIENIGHT_JELLYFIN_USER` and
`MOVIENIGHT_JELLYFIN_PASSWORD`. Settings then shows a read-only Jellyfin card (account, film count,
any problem, "Sync now"); there is no connection form. MovieNight signs in once
(`POST /Users/AuthenticateByName`), keeps the access token in its database and signs in again only if
the server rejects it. The password is read from the environment and never stored, logged or returned
by the API. What the account played in Jellyfin counts as watched (`MOVIENIGHT_JELLYFIN_WATCHED=0`
turns that off), "Play on Jellyfin" opens the film in the Jellyfin web app, and with
`MOVIENIGHT_PUSH_SEEN` on, "seen it" marks are set as played there. If the account is shared with
other people, set `MOVIENIGHT_JELLYFIN_WATCHED=0` and leave `MOVIENIGHT_PUSH_SEEN` off: its played
state is not only yours. Written against the Jellyfin 12.4 API documentation (the endpoints also exist
in 10.8-10.11); played/unplayed falls back to the older path of Jellyfin 10.8.

**Switching** an existing database from one type to the other deletes, once at start, the stored
library, its servers, the films you hid ("not tonight") and the old type's settings (e.g. the stored
Plex token), plus the image cache. Seen marks, votes, Letterboxd data, tags and TMDB data are keyed by
TMDB id and stay. Marks already pushed to the old server stay on that server.

The database tables are `media_servers` and `media_items` since schema version 7 (they were
`plex_servers` and `plex_items`). Older versions of MovieNight cannot open a database after that
upgrade: back up `data/movienight.db` before deploying, and roll back by restoring that copy together
with the previous code.

## Refreshing Letterboxd data

1. On letterboxd.com go to Settings, then Data, then Export, and download the ZIP.
2. In MovieNight open Settings and upload the ZIP (or unpack it into the Letterboxd folder: it is
   imported at start when its files change).

Between exports, new diary entries arrive through RSS automatically (without their tags).

## Deploy

```sh
scripts/deploy.sh             # HOST defaults to the ssh alias "homeserver"
SERVICE=movienight-jf REMOTE_DIR='~/movienight-jf' scripts/deploy.sh   # a second instance
```

The script syncs the committed project to `REMOTE_DIR` (default `~/movienight`) and your local
`letterboxd/` folder to the shared `~/etc/movienight/letterboxd`, builds `SERVICE` (default
`movienight`) through the server's main compose file (which needs direnv variables), and waits up to
two minutes for the container (`CONTAINER`, default `SERVICE`) to report healthy.

## Login (optional)

With Plex the default is "Sign in with Plex": only the Plex account the app belongs to gets in
(`MOVIENIGHT_PLEX_OWNER`, or the first sign-in). With Jellyfin there is no Jellyfin login: use OIDC
(below), or `MOVIENIGHT_LOGIN=none` on a private network. With Jellyfin and neither, the app answers
503 instead of running open; `MOVIENIGHT_LOGIN=plex` with Jellyfin stops the app at start.

Set `MOVIENIGHT_OIDC_ISSUER`, `MOVIENIGHT_OIDC_CLIENT_ID` and `MOVIENIGHT_PUBLIC_URL` to require a
login through an OpenID Connect provider (Pocket ID, Authelia, Keycloak…). Create a **public client
with PKCE** there, with the redirect `<MOVIENIGHT_PUBLIC_URL>/login/callback`.
`MOVIENIGHT_OIDC_ALLOWED_SUBS` (comma-separated `sub` claims) limits who gets in. With an issuer but
no client id the app answers 503: it never runs open by accident. Sessions last 30 days.

## AI chat (optional)

An "Ask" tab lets you describe what you feel like watching; an AI model answers with films from your
library, using tools that search the library, read film details and your taste profile. Two kinds of
provider work:

| Variable | Meaning |
|---|---|
| `MOVIENIGHT_AI_PROVIDER` | `anthropic` (default) or `openai` = any OpenAI-compatible endpoint (Groq, OpenAI, a local server…). Unset with `MOVIENIGHT_AI_BASE_URL` set means `openai`. |
| `ANTHROPIC_API_KEY` | key for `anthropic` (`MOVIENIGHT_AI_MODEL` defaults to `claude-opus-5-5`) |
| `MOVIENIGHT_AI_BASE_URL` | `openai` only, e.g. `https://api.groq.com/openai/v1` |
| `MOVIENIGHT_AI_API_KEY` | `openai` only: the provider's key |
| `MOVIENIGHT_AI_MODEL` | model id; required for `openai` (no default). The model must support streaming tool calls. |
| `MOVIENIGHT_AI_PRICE_INPUT` / `_OUTPUT` | `openai` only: USD per 1M tokens for the spend estimate; default `0` (free tier) |
| `MOVIENIGHT_AI_MAX_TOKENS` | `openai` only: reply cap per request, default `2048` (free tiers meter tokens per minute) |

The chat stays hidden until the chosen provider is fully configured; `GET /api/chat/status` names the
missing variable (never a value). Example for Groq's free tier:

```sh
MOVIENIGHT_AI_PROVIDER=openai
MOVIENIGHT_AI_BASE_URL=https://api.groq.com/openai/v1
MOVIENIGHT_AI_API_KEY=gsk_...
MOVIENIGHT_AI_MODEL=openai/gpt-oss-120b
```

A free tier's own limits are the real cap there (Groq's free plan for that model: 30 requests/minute,
1,000/day, 8K tokens/minute, 200K/day as of 2026-11; one question takes 2-5 requests). When they are hit,
the chat says so and you try again later.

A monthly spending quota protects your tokens: `MOVIENIGHT_AI_MONTHLY_BUDGET_USD` (default `2.00`, `0` = no
limit). Usage is counted per UTC month and priced from list prices (Claude models) or the
`MOVIENIGHT_AI_PRICE_*` values (other models), so it is an estimate, not the invoice. With price `0` it
never trips; the usage line then shows questions and tokens only. Once the budget is reached, new
questions get a short "budget used" reply (no API call) until the 1st. A hard cap can also be set on the
provider's console.

## Privacy

Your Letterboxd export and the Plex token or Jellyfin access token are stored only in the local data
volume (`./data`); the Jellyfin password stays in the environment. Nothing is sent anywhere except the
Letterboxd RSS feed, plex.tv and your Plex servers (with Plex), your Jellyfin server (with Jellyfin),
TMDB and, if you enable the chat, the configured AI provider. Each chat question sends it your taste profile (top
genres, directors, cast, your highest-rated and recently watched titles), titles and details from your
library, and the conversation itself. Free tiers may keep or use that data on their own terms: read the
provider's data policy before you turn the chat on. Without the optional login, keep the service on a private
network (VPN or a reverse proxy with access control).

## Health

`GET /healthz` returns `{"ok": true}`. The container health check uses the same endpoint.

## Project layout

```
src/movienight/          FastAPI app, sync jobs, SQLite storage
src/movienight/static/   Vanilla JS/CSS frontend (no build step)
tests/                   pytest suite
scripts/deploy.sh        Deploy to the home server
Dockerfile               Non-root image, read-only root filesystem at runtime
docker-compose.yml       Local run with a private data volume
```
