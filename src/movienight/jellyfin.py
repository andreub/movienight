"""Jellyfin: sign in with a user name and password, read the movie library, mark items played, fetch
images. Mirrors plex.py and returns the same normalised item dict. Tokens and passwords are never
logged and never put in a URL (they travel in the Authorization header only).

Endpoints (Jellyfin 12.4 API, also present in 10.8-10.11 unless noted):
  GET    /System/Info/Public                      server id, name, version (no auth)
  POST   /Users/AuthenticateByName                {"Username", "Pw"} -> AccessToken, User.Id, ServerId
  GET    /Items?userId=...&includeItemTypes=Movie&recursive=true&fields=...   paged with startIndex/limit
  POST   /UserPlayedItems/{itemId}?userId=...     mark played (10.9+); DELETE = unplayed
  POST   /Users/{userId}/PlayedItems/{itemId}     same on 10.8 (kept, obsolete, on newer servers)
  GET    /Items/{itemId}/Images/{Primary|Backdrop}?maxWidth=&maxHeight=&quality=
  web:   <base>/web/#/details?id=<itemId>&serverId=<serverId>
"""
from __future__ import annotations

import re
from urllib.parse import quote

import httpx

API_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
PLAYED_TIMEOUT = httpx.Timeout(10.0, connect=4.0)
PAGE_SIZE = 200
SERVER_PREFIX = "jf:"          # media_servers.id = "jf:<ServerId>"
FIELDS = ",".join(["ProviderIds", "OriginalTitle", "Overview", "Taglines", "Genres", "People",
                   "ProductionLocations", "DateCreated", "MediaStreams"])


class AuthError(Exception):
    """The server refused the configured user name or password (the message is safe to show)."""


def _quote_value(v: str) -> str:
    return str(v).replace("\\", "").replace('"', "")


def auth_header(device_id: str, token: str | None = None) -> dict:
    """The `Authorization: MediaBrowser ...` header every Jellyfin request carries."""
    parts = {"Client": "MovieNight", "Device": "MovieNight", "DeviceId": device_id, "Version": "1.0"}
    if token:
        parts["Token"] = token
    value = "MediaBrowser " + ", ".join(f'{k}="{_quote_value(v)}"' for k, v in parts.items())
    return {"Authorization": value, "Accept": "application/json"}


def server_key(server_id: str) -> str:
    return SERVER_PREFIX + server_id


def raw_server_id(server_key_: str) -> str:
    return server_key_[len(SERVER_PREFIX):] if server_key_.startswith(SERVER_PREFIX) else server_key_


async def server_info(client: httpx.AsyncClient, base: str) -> dict:
    """{"id", "name", "version"} from the public system info (no credentials sent)."""
    r = await client.get(base + "/System/Info/Public", headers={"Accept": "application/json"}, timeout=API_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return {"id": str(d.get("Id") or ""), "name": d.get("ServerName") or "Jellyfin", "version": d.get("Version") or ""}


async def authenticate(client: httpx.AsyncClient, base: str, device_id: str, user: str, password: str) -> dict:
    """Sign in; returns {"token", "user_id", "name", "server_id"}. Raises AuthError on bad credentials."""
    r = await client.post(base + "/Users/AuthenticateByName", json={"Username": user, "Pw": password},
                          headers=auth_header(device_id), timeout=API_TIMEOUT)
    if r.status_code in (400, 401, 403):
        raise AuthError("Jellyfin rejected the user name or password (MOVIENIGHT_JELLYFIN_USER/PASSWORD)")
    r.raise_for_status()
    d = r.json()
    u = d.get("User") or {}
    token = d.get("AccessToken") or ""
    if not token or not u.get("Id"):
        raise AuthError("Jellyfin answered the sign-in without a token")
    return {"token": token, "user_id": str(u["Id"]), "name": u.get("Name") or user,
            "server_id": str(d.get("ServerId") or u.get("ServerId") or "")}


# --------------------------------------------------------------------------- library

def _resolution(it: dict) -> str | None:
    w = h = 0
    for s in it.get("MediaStreams") or []:
        if s.get("Type") == "Video":
            w, h = max(w, int(s.get("Width") or 0)), max(h, int(s.get("Height") or 0))
    if not (w or h):
        w, h = int(it.get("Width") or 0), int(it.get("Height") or 0)
    if not (w or h):
        return None
    if w >= 3200 or h >= 2000:
        return "4k"
    if w >= 1800 or h >= 1000:
        return "1080"
    if w >= 1200 or h >= 700:
        return "720"
    return "sd"


_ISO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def _iso(value) -> str | None:
    m = _ISO_RE.match(str(value or ""))
    return m.group(1) + "Z" if m else None


def parse_item(it: dict) -> dict:
    """A Jellyfin BaseItemDto (movie) -> the same dict as plex.parse_item."""
    item_id = str(it.get("Id") or "")
    ids = {str(k).lower(): str(v) for k, v in (it.get("ProviderIds") or {}).items() if v}
    tmdb = ids.get("tmdb")
    imdb = ids.get("imdb") or None
    ud = it.get("UserData") or {}
    views = int(ud.get("PlayCount") or 0)
    if ud.get("Played") and views == 0:
        views = 1
    ticks = it.get("RunTimeTicks")
    rating = it.get("CommunityRating")
    people = it.get("People") or []
    image_tags = it.get("ImageTags") or {}
    return {
        "rating_key": item_id,
        "title": it.get("Name") or "",
        "original_title": it.get("OriginalTitle") or it.get("Name") or "",
        "year": it.get("ProductionYear"),
        "tmdb_id": int(tmdb) if tmdb and tmdb.isdigit() else None,
        "imdb_id": imdb,
        "summary": it.get("Overview") or "",
        "tagline": (it.get("Taglines") or [""])[0] or "",
        "duration_min": round(ticks / 600_000_000) if isinstance(ticks, (int, float)) and ticks else None,
        "rating": float(rating) if isinstance(rating, (int, float)) else None,
        "view_count": views,
        "added_at": _iso(it.get("DateCreated")),
        "thumb": f"/Items/{quote(item_id, safe='')}/Images/Primary" if image_tags.get("Primary") else None,
        "art": f"/Items/{quote(item_id, safe='')}/Images/Backdrop" if it.get("BackdropImageTags") else None,
        "genres": [g for g in it.get("Genres") or [] if g],
        "directors": [p["Name"] for p in people if p.get("Type") == "Director" and p.get("Name")],
        "roles": [p["Name"] for p in people if p.get("Type") == "Actor" and p.get("Name")][:12],
        "countries": [c for c in it.get("ProductionLocations") or [] if c],
        "resolution": _resolution(it),
    }


async def fetch_movies(client: httpx.AsyncClient, base: str, device_id: str, token: str, user_id: str,
                       progress=None) -> list[dict]:
    """Every movie the user can see, with their played state. Raises httpx errors (401 = token gone)."""
    items: list[dict] = []
    start = 0
    while True:
        r = await client.get(base + "/Items", params={
            "userId": user_id, "includeItemTypes": "Movie", "recursive": "true", "fields": FIELDS,
            "enableUserData": "true", "enableImageTypes": "Primary,Backdrop", "imageTypeLimit": 1,
            "sortBy": "SortName", "sortOrder": "Ascending", "startIndex": start, "limit": PAGE_SIZE,
            "enableTotalRecordCount": "true",
        }, headers=auth_header(device_id, token), timeout=API_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        batch = d.get("Items") or []
        items.extend(parse_item(m) for m in batch
                     if m.get("Id") and m.get("Type", "Movie") == "Movie" and m.get("LocationType") != "Virtual")
        total = int(d.get("TotalRecordCount") or 0)
        start += len(batch)
        if progress:
            progress(len(items), max(total, len(items)))
        if not batch or start >= total:
            break
    return items


async def _played(client: httpx.AsyncClient, method: str, base: str, device_id: str, token: str,
                  user_id: str, item_id: str) -> None:
    h = auth_header(device_id, token)
    item = quote(str(item_id), safe="")
    r = await client.request(method, f"{base}/UserPlayedItems/{item}", params={"userId": user_id} if user_id else None,
                             headers=h, timeout=PLAYED_TIMEOUT)
    if r.status_code == 404 and user_id:      # Jellyfin 10.8 and older: the user id is in the path
        r = await client.request(method, f"{base}/Users/{quote(user_id, safe='')}/PlayedItems/{item}",
                                 headers=h, timeout=PLAYED_TIMEOUT)
    r.raise_for_status()


async def mark_played(client: httpx.AsyncClient, base: str, device_id: str, token: str, user_id: str,
                      item_id: str) -> None:
    await _played(client, "POST", base, device_id, token, user_id, item_id)


async def mark_unplayed(client: httpx.AsyncClient, base: str, device_id: str, token: str, user_id: str,
                        item_id: str) -> None:
    await _played(client, "DELETE", base, device_id, token, user_id, item_id)


def web_url(base: str, server_id: str, item_id: str) -> str:
    return (f"{base.rstrip('/')}/web/#/details?id={quote(str(item_id), safe='')}"
            f"&serverId={quote(raw_server_id(server_id), safe='')}")


async def fetch_image(client: httpx.AsyncClient, base: str, token: str, device_id: str,
                      path: str, width: int, height: int) -> tuple[bytes, str] | None:
    if not path.startswith("/Items/"):
        return None
    try:
        r = await client.get(base + path, params={"maxWidth": width, "maxHeight": height, "quality": 90},
                             headers=auth_header(device_id, token), timeout=API_TIMEOUT)
    except httpx.HTTPError:
        return None
    ctype = r.headers.get("content-type", "")
    if r.status_code == 200 and ctype.startswith("image/") and r.content:
        return r.content, ctype.split(";")[0]
    return None
