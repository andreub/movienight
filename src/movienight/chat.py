"""Optional AI chat assistant: an LLM + client-side tools over the local Plex/Letterboxd data.

Mount with `app.include_router(chat.router)`. Everything here is lazy: the model client is only
created when a chat request arrives, and the app works fully without credentials.

Providers (MOVIENIGHT_AI_PROVIDER): "anthropic" (default, ANTHROPIC_API_KEY) or "openai" = any
OpenAI-compatible endpoint (MOVIENIGHT_AI_BASE_URL + MOVIENIGHT_AI_API_KEY + MOVIENIGHT_AI_MODEL).
The wire formats live in `chat_backends.py`; the loop, tools and HTTP routes here are provider-neutral.

SSE contract (POST /api/chat, text/event-stream, JSON `data:` payloads):
  text    {"delta": str}                      assistant text as it streams
  tool    {"name": str, "label": str}         progress while a tool runs
  movies  {"items": [Movie + "pitch", ...]}   the final recommendation (Movie = /api/recommendations item)
  error   {"detail": str}                     friendly message, never a stack trace or secret
  done    {}                                  always the last event
A question asked once the monthly budget is spent is answered with a plain `text` event (no API call).
GET /api/chat/status: {enabled, provider, model, problem (null or a config hint, never a secret), usage}
with usage = {month, spent_usd, budget_usd (null = no limit), questions, tokens, model, exhausted}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import math
import unicodedata
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import library, queries
from .chat_backends import AnthropicBackend, OpenAIBackend, Usage

log = logging.getLogger("movienight.chat")

# =========================================================================== backend adapter
# All coupling to the rest of the app lives in this block. If the backend renames something,
# fix it here only.

def _load_context(request: Request) -> tuple[Any, dict]:
    """(State, dismissed) for this request. State has .cands, .by_id, .history, .profile."""
    engine = request.app.state.engine
    st = engine.get()
    dismissed: dict = {}
    try:
        conn = engine._connect()
        try:
            dismissed = library.active_dismissals(conn)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - dismissals are a nicety
        dismissed = {}
    return st, dismissed


def _filter_cands(st, dismissed: dict, *, genres, max_runtime, min_year, max_year, watchlist_only,
                  include_watched) -> list:
    f = queries.Filters(genres=genres, max_runtime=max_runtime, min_year=min_year, max_year=max_year,
                        watchlist_only=watchlist_only, include_watched=include_watched)
    return queries.apply_filters(st.cands, f, dismissed)


def _sort_cands(cands: list, sort: str) -> list:
    return queries.sort_cands(cands, sort)


def _movie_json(c, st, dismissed: dict) -> dict:
    return library.movie_json(c, st, dismissed)


def _movie_detail_json(c, st, dismissed: dict) -> dict:
    return library.movie_detail_json(c, st, dismissed)


def _profile_json(st) -> dict:
    return library.profile_json(st)


def _history(st) -> list:
    """Items with .title .year .rating .liked .watched .date (rec.Hist)."""
    return st.history


# =========================================================================== config

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TURNS = 8
MAX_TOKENS = 8000
# OpenAI-compatible providers: smaller default cap. Free tiers meter tokens per minute (Groq free:
# 8K TPM), and replies here are short. MOVIENIGHT_AI_MAX_TOKENS overrides it.
OPENAI_MAX_TOKENS = 2048
CHAT_TIMEOUT_S = 150.0
API_TIMEOUT_S = 90.0
MAX_MESSAGES = 40
MAX_CONTENT = 4000
RESULT_LIMIT = 30
USER_AGENT = "MovieNight/0.1 (self-hosted film picker)"
PROVIDERS = ("anthropic", "openai")


# =========================================================================== usage + budget

# List prices in USD per 1M tokens (input, output) as of 2026-11. The figure derived from them is an
# ESTIMATE of spend, not the invoice. Matched by longest prefix; an unknown model is priced as the most
# expensive row so spend is never under-counted. Cache writes cost 1.25x input, cache reads 0.1x.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.1
DEFAULT_BUDGET_USD = 2.0


def price_for(model: str) -> tuple[float, float]:
    best = max((k for k in PRICES if (model or "").startswith(k)), key=len, default=None)
    return PRICES[best] if best else max(PRICES.values())


def estimate_cost(model: str, input: int = 0, output: int = 0, cache_write: int = 0, cache_read: int = 0) -> float:
    pin, pout = price_for(model)
    return (input * pin + output * pout + cache_write * pin * CACHE_WRITE_MULT
            + cache_read * pin * CACHE_READ_MULT) / 1_000_000


def _env_price(name: str) -> float:
    """USD per 1M tokens from env; unset, blank or invalid = 0 (free tier)."""
    try:
        v = float((os.environ.get(name) or "").strip() or 0)
    except ValueError:
        return 0.0
    return v if math.isfinite(v) and v >= 0 else 0.0


def openai_prices() -> tuple[float, float]:
    return _env_price("MOVIENIGHT_AI_PRICE_INPUT"), _env_price("MOVIENIGHT_AI_PRICE_OUTPUT")


def row_cost(model: str, input: int = 0, output: int = 0, cache_write: int = 0, cache_read: int = 0) -> float:
    """Estimated cost of one ai_usage row. With the openai provider, rows of non-Claude models are priced
    with MOVIENIGHT_AI_PRICE_INPUT/OUTPUT (cache reads at the full input price: the discount varies by
    provider, so never under-count); claude-* rows and everything under the anthropic provider use PRICES."""
    if provider() == "openai" and not (model or "").startswith("claude-"):
        pin, pout = openai_prices()
        return ((input + cache_write + cache_read) * pin + output * pout) / 1_000_000
    return estimate_cost(model, input, output, cache_write, cache_read)


def monthly_budget() -> float:
    """USD per month; 0 = no limit. Unset, blank or invalid (non-number, negative, inf) = default."""
    raw = (os.environ.get("MOVIENIGHT_AI_MONTHLY_BUDGET_USD") or "").strip()
    if not raw:
        return DEFAULT_BUDGET_USD
    try:
        v = float(raw)
    except ValueError:
        return DEFAULT_BUDGET_USD
    return v if math.isfinite(v) and v >= 0 else DEFAULT_BUDGET_USD


def _now() -> datetime:
    return datetime.now(timezone.utc)       # patched in tests


def current_month() -> str:
    return _now().strftime("%Y-%m")


def record_usage(conn, model: str, usage: Usage | None, *, new_question: bool) -> None:
    """usage: the neutral Usage of one API request (None = the provider sent none: counts the request only)."""
    u = usage or Usage()

    def n(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0

    conn.execute(
        "INSERT INTO ai_usage(month,model,input,output,cache_write,cache_read,requests,questions) "
        "VALUES(?,?,?,?,?,?,1,?) ON CONFLICT(month,model) DO UPDATE SET "
        "input=input+excluded.input, output=output+excluded.output, cache_write=cache_write+excluded.cache_write, "
        "cache_read=cache_read+excluded.cache_read, requests=requests+1, questions=questions+excluded.questions",
        (current_month(), model or "unknown", n(u.input), n(u.output), n(u.cache_write), n(u.cache_read),
         1 if new_question else 0))
    conn.commit()


def month_usage(conn) -> dict:
    month = current_month()
    spent, questions, tokens = 0.0, 0, 0
    for r in conn.execute("SELECT * FROM ai_usage WHERE month=?", (month,)):
        spent += row_cost(r["model"], r["input"], r["output"], r["cache_write"], r["cache_read"])
        questions += r["questions"]
        tokens += r["input"] + r["output"] + r["cache_write"] + r["cache_read"]
    budget = monthly_budget()
    return {"month": month, "spent_usd": round(spent, 4), "budget_usd": round(budget, 2) if budget else None,
            "questions": questions, "tokens": tokens, "model": model_name(),
            "exhausted": bool(budget) and spent >= budget}


def budget_message(budget: float) -> str:
    return (f"The chat has used its budget for this month (${budget:.2f}). It comes back on the 1st, "
            "or raise MOVIENIGHT_AI_MONTHLY_BUDGET_USD.")


def _usage_conn(request: Request):
    return request.app.state.engine._connect()


def _db_call(request: Request, fn):
    conn = _usage_conn(request)
    try:
        return fn(conn)
    finally:
        conn.close()


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def provider() -> str:
    """'anthropic', 'openai', or the unknown value as given (lowercased): then the chat is off."""
    p = _env("MOVIENIGHT_AI_PROVIDER").lower()
    if p:
        return p[:32]
    return "openai" if _env("MOVIENIGHT_AI_BASE_URL") else "anthropic"


def model_name() -> str:
    """Configured model id. The openai provider has no default ('' = not configured)."""
    m = _env("MOVIENIGHT_AI_MODEL")
    return m or (DEFAULT_MODEL if provider() == "anthropic" else "")


def openai_max_tokens() -> int:
    raw = _env("MOVIENIGHT_AI_MAX_TOKENS")
    try:
        v = int(raw)
    except ValueError:
        return OPENAI_MAX_TOKENS
    return v if 256 <= v <= 65536 else OPENAI_MAX_TOKENS


def config_problem() -> str | None:
    """Why the chat is off, as a hint for the owner (names of variables only, never values). None = ready."""
    p = provider()
    if p == "anthropic":
        if _env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN"):
            return None
        return "No API key is configured (ANTHROPIC_API_KEY)."
    if p == "openai":
        missing = [n for n in ("MOVIENIGHT_AI_BASE_URL", "MOVIENIGHT_AI_API_KEY", "MOVIENIGHT_AI_MODEL") if not _env(n)]
        return f"Missing {', '.join(missing)}." if missing else None
    return "MOVIENIGHT_AI_PROVIDER must be anthropic or openai."


def chat_enabled() -> bool:
    return config_problem() is None


def make_client():
    """Factory for the async Anthropic client (monkeypatched in tests). Credentials come from env."""
    import anthropic

    return anthropic.AsyncAnthropic(timeout=API_TIMEOUT_S, max_retries=2)


def make_openai_client():
    """Factory for the async OpenAI-compatible client (monkeypatched in tests). The SDK retries 429/5xx
    twice, honouring the provider's retry-after."""
    import openai

    return openai.AsyncOpenAI(base_url=_env("MOVIENIGHT_AI_BASE_URL"), api_key=_env("MOVIENIGHT_AI_API_KEY"),
                              timeout=API_TIMEOUT_S, max_retries=2, default_headers={"User-Agent": USER_AGENT})


def make_backend(model: str):
    if provider() == "openai":
        return OpenAIBackend(make_openai_client(), model, openai_max_tokens())
    return AnthropicBackend(make_client(), model, MAX_TOKENS)


# =========================================================================== prompt + tools
# Both are module constants: byte-stable across requests so prompt caching works.

SYSTEM_PROMPT = (
    "You are MovieNight's assistant. The user is choosing a film to watch tonight from their own Plex "
    "library, and you help them decide by chatting. They read on a phone from the couch, so keep every "
    "reply short and plain: no headings, no long lists.\n\n"
    "Rules:\n"
    "- Only recommend films that your tools returned. Never recommend from memory; if the library has "
    "nothing suitable, say so and offer the closest options.\n"
    "- Use search_library to find candidates (try a couple of different searches if the first is thin), "
    "get_movie for details, and get_taste_profile when taste matters.\n"
    "- Prefer unwatched films unless the user asks for a rewatch.\n"
    "- Ask at most one short clarifying question, and only when the request is truly ambiguous. "
    "Otherwise just pick.\n"
    "- Reply in the language the user writes in.\n"
    "- Name the films you recommend by title in your answer.\n"
    "- Earlier assistant messages may end with a bracketed \"Films shown to the user\" list added by the "
    "app: those are the cards the user saw, in order, with ids for get_movie. Use it to resolve "
    "\"the second one\". Never write such a list yourself.\n"
    "- Always finish a recommendation by calling recommend with 1-5 films and a one-sentence pitch each "
    "(why this film, for this conversation). The app shows the film cards, so after calling recommend add "
    "at most one short sentence; do not repeat the pitches."
)

TOOLS: list[dict] = [
    {
        "name": "search_library",
        "description": (
            "Search the user's Plex library. Returns compact rows (id, title, year, runtime, genres, "
            "directors, cast, match score 0-100 for this user's taste, on_watchlist, watched, TMDB rating, "
            "short overview). Unwatched films only unless include_watched is true."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional short free text: a title, person or keyword. All words must match."},
                "genres": {"type": "array", "items": {"type": "string"}, "description": "Match any of these genres, e.g. Thriller."},
                "max_runtime": {"type": "integer", "description": "Maximum runtime in minutes."},
                "min_year": {"type": "integer"},
                "max_year": {"type": "integer"},
                "watchlist_only": {"type": "boolean"},
                "include_watched": {"type": "boolean", "description": "Include already watched films (rewatch)."},
                "sort": {"type": "string", "enum": ["match", "rating", "recent", "year", "runtime"],
                         "description": "Default match (best fit for this user's taste)."},
                "limit": {"type": "integer", "description": "1-30, default 10."},
            },
        },
    },
    {
        "name": "get_movie",
        "description": "Fuller details for one film by id: overview, tagline, keywords, cast, and why it matches this user's taste.",
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Film id from search_library."}},
            "required": ["id"],
        },
    },
    {
        "name": "get_taste_profile",
        "description": (
            "The user's taste: top genres, directors, cast and decades by affinity, plus their "
            "highest-rated and most recently watched films."
        ),
        "eager_input_streaming": True,
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "recommend",
        "description": (
            "Submit the final recommendation: 1-5 films from the library with a one-sentence pitch each. "
            "The app displays them as cards. Call this to finish every recommendation."
        ),
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "picks": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "movie_id": {"type": "integer"},
                            "pitch": {"type": "string", "description": "One sentence: why this film fits this conversation."},
                        },
                        "required": ["movie_id", "pitch"],
                    },
                }
            },
            "required": ["picks"],
        },
    },
]

TOOL_LABELS = {
    "search_library": "Searching your library…",
    "get_movie": "Checking film details…",
    "get_taste_profile": "Looking at your taste…",
    "recommend": "Picking your films…",
}


# =========================================================================== tool implementations

def _fold(s: str) -> str:
    t = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in t).split())


def _int(v, lo=None, hi=None):
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _short(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _row(c) -> dict:
    m = c.meta
    return {
        "id": c.id, "title": c.title, "year": c.year, "runtime": m.get("runtime"),
        "genres": (m.get("genres") or [])[:4], "directors": (m.get("directors") or [])[:2],
        "cast": (m.get("cast") or [])[:3], "match": c.match, "on_watchlist": c.on_watchlist,
        "watched": c.watched, "tmdb_rating": m.get("vote_average"),
        "overview": _short(m.get("overview"), 140),
    }


def _haystack(c) -> str:
    m = c.meta
    parts = [c.title, m.get("original_title"), m.get("tagline")] + list(m.get("directors") or []) \
        + list(m.get("cast") or []) + list(m.get("keywords") or []) + list(m.get("genres") or [])
    return _fold(" ".join(p for p in parts if p))


def tool_search_library(args: dict, st, dismissed: dict) -> dict:
    known = {g.lower(): g for c in st.cands for g in (c.meta.get("genres") or [])}
    genres, unknown = [], []
    raw_genres = args.get("genres")
    for g in raw_genres if isinstance(raw_genres, list) else []:
        canon = known.get(str(g).strip().lower())
        (genres if canon else unknown).append(canon or str(g))
    sort = args.get("sort") if args.get("sort") in ("match", "rating", "recent", "year", "runtime") else "match"
    limit = _int(args.get("limit"), 1, RESULT_LIMIT) or 10
    cands = _filter_cands(
        st, dismissed, genres=genres, max_runtime=_int(args.get("max_runtime"), 1),
        min_year=_int(args.get("min_year")), max_year=_int(args.get("max_year")),
        watchlist_only=args.get("watchlist_only") is True, include_watched=args.get("include_watched") is True,
    )
    if unknown and not genres:          # only unknown genres requested: nothing can match
        cands = []
    query = args.get("query")
    if isinstance(query, str) and _fold(query):
        words = _fold(query).split()
        cands = [c for c in cands if all(w in _haystack(c) for w in words)]
    cands = _sort_cands(cands, sort)
    out: dict = {"total": len(cands), "results": [_row(c) for c in cands[:limit]]}
    if unknown:
        out["note"] = f"Unknown genres ignored: {', '.join(unknown)}. Known: {', '.join(sorted(known.values()))}"
    elif not cands:
        out["note"] = "No films match. Loosen a filter or try different words."
    return out


def tool_get_movie(args: dict, st, dismissed: dict) -> dict:
    mid = _int(args.get("id"))
    c = st.by_id.get(mid) if mid is not None else None
    if c is None:
        raise ToolError(f"Unknown movie id {args.get('id')!r}. Use ids returned by search_library.")
    d = _movie_detail_json(c, st, dismissed)
    return {
        "id": d["id"], "title": d["title"], "year": d["year"], "runtime": d["runtime"], "tagline": d["tagline"],
        "overview": _short(d["overview"], 600), "genres": d["genres"], "directors": d["directors"],
        "cast": d["cast"][:8], "keywords": (d.get("keywords") or [])[:15], "language": d["language"],
        "tmdb_rating": d["tmdb_rating"], "match": d["match"], "on_watchlist": d["on_watchlist"],
        "watched": d["watched"], "my_rating": d["my_rating"],
        "reasons": [r["text"] for r in d["reasons"]],
        "because": [f'{b["title"]} ({b["year"]}): {b["why"]}' for b in d.get("because") or []][:4],
    }


def tool_get_taste_profile(args: dict, st, dismissed: dict) -> dict:
    p = _profile_json(st)

    def top(rows, key, n=8):
        return [{key: r[key], "films": r["count"], "affinity": r["affinity"]} for r in rows[:n]]

    watched = [h for h in _history(st) if h.watched and h.date]
    watched.sort(key=lambda h: h.date, reverse=True)
    return {
        "stats": {k: p["stats"][k] for k in ("watched", "rated", "mean_rating", "liked")},
        "genres": top(p["genres"], "name"), "directors": top(p["directors"], "name"),
        "cast": top(p["cast"], "name"), "decades": top(sorted(p["decades"], key=lambda r: -r["affinity"]), "decade", 5),
        "highest_rated": [{"title": r["title"], "year": r["year"], "rating": r["my_rating"]} for r in p["top_rated"][:10]],
        "recently_watched": [{"title": h.title, "year": h.year, "rating": h.rating} for h in watched[:10]],
    }


class ToolError(Exception):
    """Raised by a tool to return an is_error tool_result to the model."""


@dataclass
class Outcome:
    content: str
    is_error: bool = False
    events: list = field(default_factory=list)       # extra SSE events [(name, data)]


def tool_recommend(args: dict, st, dismissed: dict) -> Outcome:
    picks = args.get("picks")
    if not isinstance(picks, list) or not 1 <= len(picks) <= 5:
        raise ToolError("picks must be a list of 1-5 {movie_id, pitch} objects.")
    items, seen, bad = [], set(), []
    for p in picks:
        mid = _int(p.get("movie_id")) if isinstance(p, dict) else None
        c = st.by_id.get(mid) if mid is not None else None
        if c is None:
            bad.append(p.get("movie_id") if isinstance(p, dict) else p)
            continue
        if mid in seen:
            continue
        seen.add(mid)
        pitch = _short(str(p.get("pitch") or ""), 300)
        items.append({**_movie_json(c, st, dismissed), "pitch": pitch})
    if bad:
        raise ToolError(f"Unknown movie ids: {bad}. Nothing was shown. Only use ids returned by search_library, then call recommend again.")
    return Outcome("Recommendation shown to the user.", events=[("movies", {"items": items})])


_TOOL_FUNCS = {"search_library": tool_search_library, "get_movie": tool_get_movie,
               "get_taste_profile": tool_get_taste_profile, "recommend": tool_recommend}


def run_tool(name: str, args: Any, st, dismissed: dict) -> Outcome:
    """Synchronous; run in a thread. Never raises."""
    fn = _TOOL_FUNCS.get(name)
    if fn is None:
        return Outcome(f"Unknown tool {name!r}.", True)
    if not isinstance(args, dict):
        return Outcome("Tool input must be a JSON object.", True)
    try:
        res = fn(args, st, dismissed)
    except ToolError as e:
        return Outcome(str(e), True)
    except Exception as e:  # noqa: BLE001
        log.warning("tool %s failed: %s", name, type(e).__name__)
        return Outcome("The tool failed unexpectedly. Try something simpler.", True)
    if isinstance(res, Outcome):
        return res
    return Outcome(json.dumps(res, ensure_ascii=False, separators=(",", ":"), default=str))


# =========================================================================== agent loop

def _friendly(exc: Exception) -> str:
    import anthropic
    import openai

    if isinstance(exc, anthropic.AuthenticationError):
        return "The AI assistant's credentials were rejected. Check the API key."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "The AI assistant's credentials are not allowed to use this model."
    if isinstance(exc, anthropic.NotFoundError):
        return "The configured AI model was not found. Check MOVIENIGHT_AI_MODEL."
    if isinstance(exc, anthropic.RateLimitError):
        return "The AI service is busy right now. Try again in a minute."
    if isinstance(exc, (anthropic.APITimeoutError, openai.APITimeoutError, TimeoutError)):
        return "The AI service took too long to answer. Try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the AI service."
    if isinstance(exc, anthropic.BadRequestError):
        return "The AI service rejected the request."
    if isinstance(exc, anthropic.APIStatusError):
        return "The AI service had a problem. Try again shortly."
    # OpenAI-compatible providers (MOVIENIGHT_AI_PROVIDER=openai)
    if isinstance(exc, openai.AuthenticationError):
        return "The AI provider rejected the credentials. Check MOVIENIGHT_AI_API_KEY."
    if isinstance(exc, openai.PermissionDeniedError):
        return "The AI provider does not allow this key to use the model. Check MOVIENIGHT_AI_MODEL."
    if isinstance(exc, openai.NotFoundError):
        return "The AI provider did not find the model or endpoint. Check MOVIENIGHT_AI_MODEL and MOVIENIGHT_AI_BASE_URL."
    if isinstance(exc, openai.RateLimitError):
        return "The AI provider's usage limit was reached (free tiers allow only a few requests a minute and a day). Try again later."
    if isinstance(exc, openai.APIConnectionError):
        return "Could not reach the AI provider. Check MOVIENIGHT_AI_BASE_URL."
    if isinstance(exc, openai.BadRequestError):
        return "The AI provider rejected the request. The model may not support tool calling."
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 413:
            return "The conversation is too long for the AI provider's limits. Start a new chat."
        return "The AI provider had a problem. Try again shortly."
    return "Something went wrong with the assistant."


async def agent_events(request: Request, history: list[dict]) -> AsyncIterator[tuple[str, dict]]:
    """Yields (event, data) pairs; always ends with ('done', {})."""
    try:
        async with asyncio.timeout(CHAT_TIMEOUT_S):
            async for ev in _loop(request, history):
                yield ev
    except TimeoutError:
        yield "error", {"detail": "The assistant took too long. Try again."}
    except Exception as e:  # noqa: BLE001
        log.warning("chat failed: %s", type(e).__name__)
        yield "error", {"detail": _friendly(e)}
    yield "done", {}


BAD_ARGS = "The tool arguments were not valid JSON. Call the tool again with a single JSON object."


async def _loop(request: Request, history: list[dict]) -> AsyncIterator[tuple[str, dict]]:
    model = model_name()
    budget = monthly_budget()
    if budget:
        try:
            state = await asyncio.to_thread(_db_call, request, month_usage)
        except Exception as e:  # noqa: BLE001 - a broken meter must not take the chat down
            log.warning("usage lookup failed: %s", type(e).__name__)
            state = {"exhausted": False}
        if state["exhausted"]:
            yield "text", {"delta": budget_message(budget)}
            return
    backend = make_backend(model)
    backend.start(SYSTEM_PROMPT, TOOLS, list(history))
    counted = False
    st, dismissed = await asyncio.to_thread(_load_context, request)
    json_failures = 0
    try:
        turns = 0
        while turns < MAX_TURNS:
            turns += 1
            turn = None
            try:
                async for item in backend.turn():
                    if isinstance(item, str):
                        yield "text", {"delta": item}
                    else:
                        turn = item
                json_failures = 0
                try:
                    await asyncio.to_thread(_db_call, request, lambda c: record_usage(
                        c, turn.model or model, turn.usage, new_question=not counted))
                    counted = True
                except Exception as e:  # noqa: BLE001
                    log.warning("usage record failed: %s", type(e).__name__)
            except ValueError:
                # tool input JSON the SDK could not parse at all: re-issue the turn (bounded)
                json_failures += 1
                if json_failures > 2:
                    yield "error", {"detail": "The assistant produced a malformed answer. Try again."}
                    return
                turns -= 1
                continue

            if turn.stop == "refusal":
                yield "error", {"detail": "The assistant can't help with that request. Try rephrasing."}
                return
            if turn.stop == "length":
                yield "error", {"detail": "The answer got cut off. Try asking again, a bit more simply."}
                return
            if turn.stop == "pause":
                continue
            if turn.stop != "tools" or not turn.tool_calls:
                return                                  # end of turn / anything final

            calls = turn.tool_calls
            for c in calls:
                yield "tool", {"name": c.name, "label": TOOL_LABELS.get(c.name, "Working…")}
            outcomes = await asyncio.gather(*(
                asyncio.to_thread(run_tool, c.name, c.args, st, dismissed) if c.args is not None
                else asyncio.sleep(0, Outcome(BAD_ARGS, True)) for c in calls))
            results = []
            for c, o in zip(calls, outcomes):
                for ev in o.events:
                    yield ev
                results.append((c.id, o.content, o.is_error))
            backend.add_tool_results(results)
        yield "error", {"detail": "I couldn't settle on a pick in time. Try a more specific request."}
    finally:
        try:
            await backend.close()
        except Exception:  # noqa: BLE001
            pass


# =========================================================================== HTTP

router = APIRouter()


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@router.get("/api/chat/status")
def chat_status(request: Request):
    try:
        usage = _db_call(request, month_usage)
    except Exception:  # noqa: BLE001
        usage = None
    return {"enabled": chat_enabled(), "provider": provider(), "model": model_name(), "problem": config_problem(),
            "usage": usage}


def _validate(body: Any) -> list[dict]:
    def bad(msg):
        raise HTTPException(400, msg)

    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        bad("Body must be {\"messages\": [...]}")
    msgs = body["messages"]
    if not 1 <= len(msgs) <= MAX_MESSAGES:
        bad(f"Send between 1 and {MAX_MESSAGES} messages")
    out = []
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant") or not isinstance(m.get("content"), str):
            bad("Each message needs a role (user or assistant) and text content")
        text = m["content"].strip()
        if not text:
            bad("Messages cannot be empty")
        if len(text) > MAX_CONTENT:
            bad(f"Messages are limited to {MAX_CONTENT} characters")
        out.append({"role": m["role"], "content": text})
    if out[-1]["role"] != "user":
        bad("The last message must be from the user")
    while out and out[0]["role"] != "user":         # the API needs a leading user turn
        out.pop(0)
    return out


@router.post("/api/chat")
async def chat(request: Request):
    if not chat_enabled():
        return JSONResponse({"detail": f"AI chat is not configured. {config_problem()}"}, status_code=503)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Body must be valid JSON") from None
    history = _validate(body)

    async def gen():
        async for event, data in agent_events(request, history):
            yield _sse(event, data)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
