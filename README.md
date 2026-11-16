# MovieNight

"What do I watch tonight?" MovieNight ranks the movies available on your Plex servers
using your Letterboxd history (watched films, ratings, likes, watchlist) and TMDB metadata.
It is a single-user app with no login, meant to run on a private network.

## How it gets data

- **Letterboxd**: a data export (folder or ZIP upload) is the baseline for watched, rated,
  liked and watchlist films. New diary entries are picked up automatically from the public RSS feed.
- **Plex**: connect with a PIN login (plex.tv). Movie libraries from every server you own or
  have been shared are indexed; the first reachable connection is used.
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
- With `MOVIENIGHT_PLEX_PUSH_SEEN` on, marks are also set as played on Plex, whichever way they are made.

## Quick start (Docker)

```sh
cp .env.example .env          # optional: fill in TMDB token, Letterboxd username
mkdir -p data letterboxd      # put your unpacked Letterboxd export in ./letterboxd if you have one
docker compose up -d --build
```

Open http://localhost:8080 and connect Plex in Settings.

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
| `MOVIENIGHT_PLEX_TOKEN` | empty | Optional pre-set Plex token (otherwise PIN login in the UI) |
| `MOVIENIGHT_SYNC_HOURS` | `6` | Background sync interval |
| `MOVIENIGHT_PLEX_PUSH_SEEN` | off | `1`/`true`/`yes`: marks films you flag "seen it" as played on your Plex servers (writes to your Plex account) |
| `MOVIENIGHT_PORT` | `8080` | Compose only: host port |
| `TZ` | `Europe/Madrid` | Compose only: container timezone |

## Refreshing Letterboxd data

1. On letterboxd.com go to Settings, then Data, then Export, and download the ZIP.
2. In MovieNight open Settings and upload the ZIP.

Between exports, new diary entries arrive through RSS automatically.

## Deploy

```sh
scripts/deploy.sh             # HOST defaults to the ssh alias "homeserver"
```

The script syncs the project and your local `letterboxd/` folder to the server, builds the
image through the server's main compose file (which needs direnv variables), and waits up to
two minutes for the container to report healthy.

## Login (optional)

Set `MOVIENIGHT_OIDC_ISSUER`, `MOVIENIGHT_OIDC_CLIENT_ID` and `MOVIENIGHT_PUBLIC_URL` to require a
login through an OpenID Connect provider (Pocket ID, Authelia, Keycloak…). Create a **public client
with PKCE** there, with the redirect `<MOVIENIGHT_PUBLIC_URL>/login/callback`.
`MOVIENIGHT_OIDC_ALLOWED_SUBS` (comma-separated `sub` claims) limits who gets in. With an issuer but
no client id the app answers 503: it never runs open by accident. Sessions last 30 days.

## AI chat (optional)

With `ANTHROPIC_API_KEY` set, an "Ask" tab lets you describe what you feel like watching; Claude
answers with films from your Plex library. Only film metadata, your ratings and the chat text are
sent to the Anthropic API. `MOVIENIGHT_AI_MODEL` changes the model.

## Privacy

Your Letterboxd export and the Plex token are stored only in the local data volume
(`./data`). Nothing is sent anywhere except the Letterboxd RSS feed, plex.tv, TMDB and, if you
enable the chat, the Anthropic API. Without the optional login, keep the service on a private
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
