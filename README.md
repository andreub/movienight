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

An "Ask" tab lets you describe what you feel like watching; an AI model answers with films from your
Plex library, using tools that search the library, read film details and your taste profile. Two kinds of
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

Your Letterboxd export and the Plex token are stored only in the local data volume
(`./data`). Nothing is sent anywhere except the Letterboxd RSS feed, plex.tv, TMDB and, if you
enable the chat, the configured AI provider. Each chat question sends it your taste profile (top
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
