"""Optional AI chat assistant: Claude + client-side tools over the local Plex/Letterboxd data.

Mount with `app.include_router(chat.router)`. Everything here is lazy: the Anthropic client is
only created when a chat request arrives, and the app works fully without credentials.

SSE contract (POST /api/chat, text/event-stream, JSON `data:` payloads):
  text    {"delta": str}                      assistant text as it streams
  tool    {"name": str, "label": str}         progress while a tool runs
  movies  {"items": [Movie + "pitch", ...]}   the final recommendation (Movie = /api/recommendations item)
  error   {"detail": str}                     friendly message, never a stack trace or secret
  done    {}                                  always the last event
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import library, queries

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
CHAT_TIMEOUT_S = 150.0
API_TIMEOUT_S = 90.0
MAX_MESSAGES = 40
MAX_CONTENT = 4000
RESULT_LIMIT = 30
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def model_name() -> str:
    return (os.environ.get("MOVIENIGHT_AI_MODEL") or "").strip() or DEFAULT_MODEL


def chat_enabled() -> bool:
    env = os.environ
    return bool((env.get("ANTHROPIC_API_KEY") or "").strip() or (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip())


def make_client():
    """Factory for the async Anthropic client (monkeypatched in tests). Credentials come from env."""
    import anthropic

    return anthropic.AsyncAnthropic(timeout=API_TIMEOUT_S, max_retries=2)


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

def _supports_modern(model: str) -> bool:
    return model.startswith(("claude-opus-5", "claude-fable-5", "claude-mythos-5", "claude-sonnet-5-5"))


def _request_kwargs(model: str, messages: list) -> dict:
    kw: dict = dict(model=model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
                    cache_control={"type": "ephemeral"})
    if _supports_modern(model):
        kw.update(thinking={"type": "adaptive"}, output_config={"effort": "medium"},
                  betas=[FALLBACK_BETA], fallbacks="default")
    return kw


def _echo_content(content: list) -> list:
    """Assistant content to send back. After a mid-output fallback, drop thinking/tool_use before the boundary."""
    last = max((i for i, b in enumerate(content) if getattr(b, "type", None) == "fallback"), default=None)
    if last is None:
        return list(content)
    return [b for i, b in enumerate(content)
            if i >= last or getattr(b, "type", None) not in ("thinking", "redacted_thinking", "tool_use")]


def _friendly(exc: Exception) -> str:
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return "The AI assistant's credentials were rejected. Check the API key."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "The AI assistant's credentials are not allowed to use this model."
    if isinstance(exc, anthropic.NotFoundError):
        return "The configured AI model was not found. Check MOVIENIGHT_AI_MODEL."
    if isinstance(exc, anthropic.RateLimitError):
        return "The AI service is busy right now. Try again in a minute."
    if isinstance(exc, (anthropic.APITimeoutError, TimeoutError)):
        return "The AI service took too long to answer. Try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Could not reach the AI service."
    if isinstance(exc, anthropic.BadRequestError):
        return "The AI service rejected the request."
    if isinstance(exc, anthropic.APIStatusError):
        return "The AI service had a problem. Try again shortly."
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


async def _loop(request: Request, history: list[dict]) -> AsyncIterator[tuple[str, dict]]:
    model = model_name()
    client = make_client()
    messages: list = list(history)
    st, dismissed = await asyncio.to_thread(_load_context, request)
    json_failures = 0
    try:
        turns = 0
        while turns < MAX_TURNS:
            turns += 1
            try:
                async with client.beta.messages.stream(**_request_kwargs(model, messages)) as stream:
                    async for event in stream:
                        if event.type == "text" and event.text:
                            yield "text", {"delta": event.text}
                    msg = await stream.get_final_message()
                json_failures = 0
            except ValueError:
                # tool input JSON the SDK could not parse at all: re-issue the turn (bounded)
                json_failures += 1
                if json_failures > 2:
                    yield "error", {"detail": "The assistant produced a malformed answer. Try again."}
                    return
                turns -= 1
                continue

            stop = msg.stop_reason
            content = _echo_content(msg.content)
            tool_uses = [b for b in content if b.type == "tool_use"]

            if stop == "refusal":
                yield "error", {"detail": "The assistant can't help with that request. Try rephrasing."}
                return
            if stop == "max_tokens":
                yield "error", {"detail": "The answer got cut off. Try asking again, a bit more simply."}
                return
            if stop == "pause_turn":
                messages.append({"role": "assistant", "content": content})
                continue
            if stop != "tool_use" or not tool_uses:
                return                                  # end_turn / stop_sequence / anything final

            for b in tool_uses:
                yield "tool", {"name": b.name, "label": TOOL_LABELS.get(b.name, "Working…")}
            outcomes = await asyncio.gather(
                *(asyncio.to_thread(run_tool, b.name, b.input, st, dismissed) for b in tool_uses))
            results = []
            for b, o in zip(tool_uses, outcomes):
                for ev in o.events:
                    yield ev
                r = {"type": "tool_result", "tool_use_id": b.id, "content": o.content}
                if o.is_error:
                    r["is_error"] = True
                results.append(r)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": results})
        yield "error", {"detail": "I couldn't settle on a pick in time. Try a more specific request."}
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


# =========================================================================== HTTP

router = APIRouter()


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@router.get("/api/chat/status")
def chat_status():
    return {"enabled": chat_enabled(), "model": model_name()}


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
        return JSONResponse({"detail": "AI chat is not configured (set ANTHROPIC_API_KEY)."}, status_code=503)
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
