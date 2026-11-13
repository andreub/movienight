"""Plex: PIN auth, resource discovery, library fetch and image fetch. Tokens are never logged."""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

PLEX_TV = "https://plex.tv"
PROBE_TIMEOUT = httpx.Timeout(4.0, connect=3.0)
API_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
PAGE_SIZE = 500


def base_headers(client_id: str, token: str | None = None) -> dict:
    h = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": client_id,
        "X-Plex-Product": "MovieNight",
        "X-Plex-Version": "1.0",
        "X-Plex-Device-Name": "MovieNight",
    }
    if token:
        h["X-Plex-Token"] = token
    return h


# --------------------------------------------------------------------------- PIN login

def auth_url(client_id: str, code: str) -> str:
    return (
        "https://app.plex.tv/auth#?"
        f"clientID={quote(client_id, safe='')}&code={quote(code, safe='')}"
        "&context%5Bdevice%5D%5Bproduct%5D=MovieNight"
    )


async def create_pin(client: httpx.AsyncClient, client_id: str) -> dict:
    r = await client.post(f"{PLEX_TV}/api/v2/pins", params={"strong": "true"},
                          headers=base_headers(client_id), timeout=API_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return {"id": d["id"], "code": d["code"], "auth_url": auth_url(client_id, d["code"])}


async def check_pin(client: httpx.AsyncClient, client_id: str, pin_id: int) -> str | None:
    r = await client.get(f"{PLEX_TV}/api/v2/pins/{int(pin_id)}", headers=base_headers(client_id),
                         timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json().get("authToken") or None


async def account_name(client: httpx.AsyncClient, client_id: str, token: str) -> str | None:
    try:
        r = await client.get(f"{PLEX_TV}/api/v2/user", headers=base_headers(client_id, token),
                             timeout=API_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        return d.get("username") or d.get("title") or d.get("email", "").split("@")[0] or None
    except (httpx.HTTPError, ValueError):
        return None


# --------------------------------------------------------------------------- resources

async def get_servers(client: httpx.AsyncClient, client_id: str, token: str) -> list[dict]:
    r = await client.get(
        f"{PLEX_TV}/api/v2/resources",
        params={"includeHttps": 1, "includeRelay": 1},
        headers=base_headers(client_id, token), timeout=API_TIMEOUT,
    )
    r.raise_for_status()
    return parse_resources(r.json())


def parse_resources(payload) -> list[dict]:
    servers = []
    for res in payload or []:
        if "server" not in (res.get("provides") or ""):
            continue
        conns = [
            {"uri": c["uri"], "local": bool(c.get("local")), "relay": bool(c.get("relay"))}
            for c in res.get("connections") or [] if c.get("uri")
        ]
        servers.append({
            "id": res.get("clientIdentifier"),
            "name": res.get("name") or "Plex",
            "owned": bool(res.get("owned")),
            "access_token": res.get("accessToken") or "",
            "connections": conns,
        })
    return [s for s in servers if s["id"]]


def connection_order(conns: list[dict]) -> list[dict]:
    """Preference: local direct, remote direct, relay."""
    def key(c):
        return (1 if c["relay"] else 0, 0 if c["local"] else 1)
    return sorted(conns, key=key)


async def _probe(client: httpx.AsyncClient, conn: dict, client_id: str, token: str) -> dict | None:
    try:
        r = await client.get(conn["uri"].rstrip("/") + "/identity",
                             headers=base_headers(client_id, token), timeout=PROBE_TIMEOUT)
        return conn if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def pick_connection(client: httpx.AsyncClient, server: dict, client_id: str) -> str | None:
    """Probe all connections in parallel; return the best one that answered."""
    ordered = connection_order(server["connections"])
    if not ordered:
        return None
    token = server.get("access_token") or ""
    results = await asyncio.gather(*(_probe(client, c, client_id, token) for c in ordered))
    for c in results:  # `ordered` preserves preference
        if c:
            return c["uri"].rstrip("/")
    return None


# --------------------------------------------------------------------------- library

_GUID_RE = re.compile(r"^(tmdb|imdb|tvdb)://(.+)$")


def parse_guids(md: dict) -> tuple[int | None, str | None]:
    tmdb = imdb = None
    for g in md.get("Guid") or []:
        m = _GUID_RE.match(g.get("id") or "")
        if not m:
            continue
        if m.group(1) == "tmdb" and m.group(2).isdigit():
            tmdb = int(m.group(2))
        elif m.group(1) == "imdb":
            imdb = m.group(2)
    return tmdb, imdb


def _resolution(md: dict) -> str | None:
    order = {"4k": 4, "1080": 3, "720": 2, "sd": 1}
    best = None
    for m in md.get("Media") or []:
        raw = str(m.get("videoResolution") or "").lower().replace("p", "")
        if not raw:
            continue
        norm = "4k" if raw in ("4k", "2160") else raw if raw in ("1080", "720") else "sd"
        if best is None or order[norm] > order[best]:
            best = norm
    return best


def _tags(md: dict, key: str, limit: int | None = None) -> list[str]:
    tags = [t["tag"] for t in md.get(key) or [] if t.get("tag")]
    return tags[:limit] if limit else tags


def parse_item(md: dict) -> dict:
    tmdb, imdb = parse_guids(md)
    added = md.get("addedAt")
    added_iso = (
        datetime.fromtimestamp(int(added), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if added else None
    )
    duration = md.get("duration")
    rating = md.get("audienceRating") if md.get("audienceRating") is not None else md.get("rating")
    return {
        "rating_key": str(md.get("ratingKey")),
        "title": md.get("title") or "",
        "original_title": md.get("originalTitle") or md.get("title") or "",
        "year": md.get("year"),
        "tmdb_id": tmdb,
        "imdb_id": imdb,
        "summary": md.get("summary") or "",
        "tagline": md.get("tagline") or "",
        "duration_min": round(duration / 60000) if duration else None,
        "rating": float(rating) if isinstance(rating, (int, float)) else None,
        "view_count": int(md.get("viewCount") or 0),
        "added_at": added_iso,
        "thumb": md.get("thumb") or None,
        "art": md.get("art") or None,
        "genres": _tags(md, "Genre"),
        "directors": _tags(md, "Director"),
        "roles": _tags(md, "Role", 12),
        "countries": _tags(md, "Country"),
        "resolution": _resolution(md),
    }


async def _get_json(client: httpx.AsyncClient, base: str, path: str, client_id: str, token: str,
                    params: dict | None = None, headers: dict | None = None) -> dict:
    h = base_headers(client_id, token)
    h.update(headers or {})
    r = await client.get(base + path, params=params, headers=h, timeout=API_TIMEOUT)
    r.raise_for_status()
    return r.json().get("MediaContainer", {})


async def fetch_movies(client: httpx.AsyncClient, base: str, client_id: str, token: str,
                       progress=None) -> list[dict]:
    sections = (await _get_json(client, base, "/library/sections", client_id, token)).get("Directory") or []
    items: list[dict] = []
    for sec in sections:
        if sec.get("type") != "movie":
            continue
        start = 0
        while True:
            mc = await _get_json(
                client, base, f"/library/sections/{quote(str(sec['key']), safe='')}/all", client_id, token,
                params={"type": 1, "includeGuids": 1},
                headers={"X-Plex-Container-Start": str(start), "X-Plex-Container-Size": str(PAGE_SIZE)},
            )
            batch = mc.get("Metadata") or []
            items.extend(parse_item(m) for m in batch)
            total = mc.get("totalSize") or mc.get("size") or 0
            start += len(batch)
            if progress:
                progress(len(items), max(total, len(items)))
            if not batch or start >= total:
                break
    return items


def web_url(machine_id: str, rating_key: str) -> str:
    key = quote(f"/library/metadata/{rating_key}", safe="")
    return f"https://app.plex.tv/desktop/#!/server/{machine_id}/details?key={key}"


async def fetch_image(client: httpx.AsyncClient, base: str, token: str, client_id: str,
                      path: str, width: int, height: int) -> tuple[bytes, str] | None:
    """Fetch a (transcoded when possible) image. The token travels in a header only."""
    if not path.startswith("/"):
        return None
    attempts = [
        ("/photo/:/transcode", {"width": width, "height": height, "minSize": 1, "upscale": 0, "url": path}),
        (path, None),
    ]
    for p, params in attempts:
        try:
            r = await client.get(base + p, params=params, headers=base_headers(client_id, token),
                                 timeout=API_TIMEOUT)
        except httpx.HTTPError:
            continue
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and ctype.startswith("image/") and r.content:
            return r.content, ctype.split(";")[0]
    return None


# --------------------------------------------------------------------------- dedupe

def movie_key(item: dict, imdb_to_tmdb: dict, title_to_tmdb: dict) -> str:
    from .util import norm_title

    if item.get("tmdb_id"):
        return f"tmdb:{item['tmdb_id']}"
    if item.get("imdb_id"):
        if item["imdb_id"] in imdb_to_tmdb:
            return f"tmdb:{imdb_to_tmdb[item['imdb_id']]}"
        return f"imdb:{item['imdb_id']}"
    tk = f"{norm_title(item.get('title'))}|{item.get('year') or ''}"
    if tk in title_to_tmdb:
        return f"tmdb:{title_to_tmdb[tk]}"
    return f"t:{tk}"


def key_maps(items: list[dict]) -> tuple[dict, dict]:
    from .util import norm_title

    imdb_to_tmdb, title_to_tmdb = {}, {}
    for it in items:
        if it.get("tmdb_id"):
            if it.get("imdb_id"):
                imdb_to_tmdb.setdefault(it["imdb_id"], it["tmdb_id"])
            title_to_tmdb.setdefault(f"{norm_title(it.get('title'))}|{it.get('year') or ''}", it["tmdb_id"])
    return imdb_to_tmdb, title_to_tmdb


