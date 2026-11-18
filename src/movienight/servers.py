"""The selected media server (MOVIENIGHT_MEDIA_SERVER) behind one small interface, so the call sites
(seen-mark push, play links, images) do not care whether it is Plex or Jellyfin."""
from __future__ import annotations

import sqlite3

import httpx

from . import jellyfin, plex
from .db import get_meta


class PlexBackend:
    name = "plex"
    label = "Plex"
    count_watched = True       # Plex's played state is always the user's own

    def user_id(self, conn: sqlite3.Connection) -> str:
        return ""

    def web_url(self, server_id: str, base_url: str | None, rating_key: str) -> str:
        return plex.web_url(server_id, rating_key)

    def mobile_url(self, slug: str) -> str | None:
        return plex.mobile_url(slug)

    async def mark_played(self, http: httpx.AsyncClient, base_url: str, client_id: str, token: str,
                          user_id: str, rating_key: str) -> None:
        await plex.scrobble(http, base_url, client_id, token, rating_key)

    async def mark_unplayed(self, http: httpx.AsyncClient, base_url: str, client_id: str, token: str,
                            user_id: str, rating_key: str) -> None:
        await plex.unscrobble(http, base_url, client_id, token, rating_key)

    async def fetch_image(self, http: httpx.AsyncClient, base_url: str, token: str, client_id: str,
                          path: str, width: int, height: int):
        return await plex.fetch_image(http, base_url, token, client_id, path, width, height)


class JellyfinBackend:
    name = "jellyfin"
    label = "Jellyfin"

    def __init__(self, count_watched: bool = True):
        self.count_watched = count_watched

    def user_id(self, conn: sqlite3.Connection) -> str:
        return get_meta(conn, "jellyfin_user_id") or ""

    def web_url(self, server_id: str, base_url: str | None, rating_key: str) -> str:
        return jellyfin.web_url(base_url or "", server_id, rating_key)

    def mobile_url(self, slug: str) -> str | None:
        return None

    async def mark_played(self, http, base_url, client_id, token, user_id, rating_key) -> None:
        await jellyfin.mark_played(http, base_url, client_id, token, user_id, rating_key)

    async def mark_unplayed(self, http, base_url, client_id, token, user_id, rating_key) -> None:
        await jellyfin.mark_unplayed(http, base_url, client_id, token, user_id, rating_key)

    async def fetch_image(self, http, base_url, token, client_id, path, width, height):
        return await jellyfin.fetch_image(http, base_url, token, client_id, path, width, height)


PLEX = PlexBackend()


def backend(cfg) -> PlexBackend | JellyfinBackend:
    if cfg is not None and cfg.media_server == "jellyfin":
        return JellyfinBackend(cfg.jellyfin_watched)
    return PLEX
