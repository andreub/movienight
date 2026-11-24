"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MEDIA_SERVERS = ("plex", "jellyfin")


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def _seconds(value: str | None, default: float) -> float:
    try:
        return max(float(value), 0.0) if (value or "").strip() else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("./data")
    letterboxd_dir: Path = Path("./letterboxd")
    letterboxd_user: str = ""
    tmdb_token: str = ""
    plex_token: str = ""
    sync_hours: float = 6.0
    sync_on_start: bool = True
    login: str = ""              # comma-separated: plex, oidc; "none" = no login at all
    plex_owner: str = ""         # Plex account id, username or email allowed to sign in
    oidc_name: str = "Single sign-on"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_allowed_subs: str = ""
    public_url: str = "http://localhost:8080"
    push_seen: bool = False          # mark "seen it" films as played on the media server (writes to the account)
    plex_history_fix: bool = True    # after a push, date (Letterboxd) or remove the "watched just now" plex.tv activity
    push_lb_backfill: bool = False   # opt in: also push the Letterboxd history that existed before (paced, a batch per sync)
    plex_history_pace: float = 6.0   # seconds between plex.tv history writes (it rate-limits; slower is safer)
    media_server: str = "plex"     # which library the app reads: plex | jellyfin (one at a time)
    jellyfin_url: str = ""
    jellyfin_user: str = ""
    jellyfin_password: str = field(default="", repr=False)
    jellyfin_watched: bool = True    # films played in Jellyfin count as watched
    cast_hosts: str = ""             # comma-separated IPs/hostnames of Cast devices (TVs) to look for
    completionist: bool = True       # the Complete tab: sagas/franchises/series discovered from TMDB (extra lookups)
    seerr_url: str = ""              # Overseerr / Jellyseerr API base (…/api/v1): each user connects their account

    def __post_init__(self):
        if self.media_server not in MEDIA_SERVERS:
            raise ValueError(f"MOVIENIGHT_MEDIA_SERVER must be one of {', '.join(MEDIA_SERVERS)}"
                             f" (got {self.media_server[:40]!r})")
        if self.media_server != "plex" and "plex" in self._asked:
            raise ValueError("MOVIENIGHT_LOGIN=plex needs MOVIENIGHT_MEDIA_SERVER=plex; with Jellyfin use"
                             " OIDC (MOVIENIGHT_OIDC_ISSUER/CLIENT_ID) or MOVIENIGHT_LOGIN=none")

    @property
    def _asked(self) -> set[str]:
        return {m.strip() for m in self.login.split(",") if m.strip()}

    @property
    def login_methods(self) -> set[str]:
        """An OIDC issuer turns OIDC on by itself; "none" turns every login off."""
        asked = self._asked
        if "none" in asked:
            return set()
        if self.oidc_issuer:
            asked.add("oidc")
        return {m for m in asked if (m == "plex" and self.media_server == "plex") or (m == "oidc" and self.oidc_issuer)}

    @property
    def login_required(self) -> bool:
        """Whether the app must refuse to run open. With Jellyfin there is no media-server login, so
        unless MOVIENIGHT_LOGIN=none says otherwise a missing OIDC setup means 503, never open."""
        if self.login_methods:
            return True
        return self.media_server != "plex" and "none" not in self._asked

    @property
    def jellyfin_configured(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_user and self.jellyfin_password)

    @property
    def seerr_configured(self) -> bool:
        return bool(self.seerr_url)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "movienight.db"

    @property
    def image_dir(self) -> Path:
        return self.data_dir / "images"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        try:
            hours = float(env.get("MOVIENIGHT_SYNC_HOURS", "6") or 6)
        except ValueError:
            hours = 6.0
        media = (env.get("MOVIENIGHT_MEDIA_SERVER") or "plex").strip().lower()
        push = env.get("MOVIENIGHT_PUSH_SEEN")
        if push is None or not push.strip():
            push = env.get("MOVIENIGHT_PLEX_PUSH_SEEN")     # older name, still honoured
        jf_watched = (env.get("MOVIENIGHT_JELLYFIN_WATCHED") or "").strip().lower()
        return cls(
            data_dir=Path(env.get("MOVIENIGHT_DATA_DIR") or "./data"),
            letterboxd_dir=Path(env.get("MOVIENIGHT_LETTERBOXD_DIR") or "./letterboxd"),
            letterboxd_user=(env.get("MOVIENIGHT_LETTERBOXD_USER") or "").strip(),
            tmdb_token=(env.get("MOVIENIGHT_TMDB_TOKEN") or "").strip(),
            plex_token=(env.get("MOVIENIGHT_PLEX_TOKEN") or "").strip(),
            sync_hours=hours,
            # Plex: sign in with Plex by default. Jellyfin has no login of its own (OIDC or none).
            login=(env.get("MOVIENIGHT_LOGIN") or ("plex" if media == "plex" else "")).strip().lower(),
            plex_owner=(env.get("MOVIENIGHT_PLEX_OWNER") or "").strip(),
            oidc_name=(env.get("MOVIENIGHT_OIDC_NAME") or "Single sign-on").strip()[:40],
            oidc_issuer=(env.get("MOVIENIGHT_OIDC_ISSUER") or "").strip(),
            oidc_client_id=(env.get("MOVIENIGHT_OIDC_CLIENT_ID") or "").strip(),
            oidc_allowed_subs=(env.get("MOVIENIGHT_OIDC_ALLOWED_SUBS") or "").strip(),
            public_url=(env.get("MOVIENIGHT_PUBLIC_URL") or "http://localhost:8080").strip(),
            push_seen=_flag(push),
            plex_history_fix=(env.get("MOVIENIGHT_PLEX_HISTORY_FIX") or "").strip().lower() not in ("0", "false", "no"),
            push_lb_backfill=_flag(env.get("MOVIENIGHT_PUSH_LETTERBOXD_BACKFILL")),
            plex_history_pace=_seconds(env.get("MOVIENIGHT_PLEX_HISTORY_PACE"), 6.0),
            media_server=media,
            jellyfin_url=(env.get("MOVIENIGHT_JELLYFIN_URL") or "").strip().rstrip("/"),
            jellyfin_user=(env.get("MOVIENIGHT_JELLYFIN_USER") or "").strip(),
            jellyfin_password=env.get("MOVIENIGHT_JELLYFIN_PASSWORD") or "",
            jellyfin_watched=jf_watched not in ("0", "false", "no"),
            cast_hosts=(env.get("MOVIENIGHT_CAST_HOSTS") or "").strip(),
            completionist=(env.get("MOVIENIGHT_COMPLETIONIST") or "").strip().lower() not in ("0", "false", "no"),
            seerr_url=(env.get("MOVIENIGHT_SEERR_URL") or "").strip().rstrip("/"),
        )
