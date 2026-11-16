"""Environment-driven configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    plex_push_seen: bool = False     # mark "seen it" films as played on Plex (writes to the account)

    @property
    def login_methods(self) -> set[str]:
        """An OIDC issuer turns OIDC on by itself; "none" turns every login off."""
        asked = {m.strip() for m in self.login.split(",") if m.strip()}
        if "none" in asked:
            return set()
        if self.oidc_issuer:
            asked.add("oidc")
        return {m for m in asked if m == "plex" or (m == "oidc" and self.oidc_issuer)}

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
        return cls(
            data_dir=Path(env.get("MOVIENIGHT_DATA_DIR") or "./data"),
            letterboxd_dir=Path(env.get("MOVIENIGHT_LETTERBOXD_DIR") or "./letterboxd"),
            letterboxd_user=(env.get("MOVIENIGHT_LETTERBOXD_USER") or "").strip(),
            tmdb_token=(env.get("MOVIENIGHT_TMDB_TOKEN") or "").strip(),
            plex_token=(env.get("MOVIENIGHT_PLEX_TOKEN") or "").strip(),
            sync_hours=hours,
            login=(env.get("MOVIENIGHT_LOGIN") or "plex").strip().lower(),
            plex_owner=(env.get("MOVIENIGHT_PLEX_OWNER") or "").strip(),
            oidc_name=(env.get("MOVIENIGHT_OIDC_NAME") or "Single sign-on").strip()[:40],
            oidc_issuer=(env.get("MOVIENIGHT_OIDC_ISSUER") or "").strip(),
            oidc_client_id=(env.get("MOVIENIGHT_OIDC_CLIENT_ID") or "").strip(),
            oidc_allowed_subs=(env.get("MOVIENIGHT_OIDC_ALLOWED_SUBS") or "").strip(),
            public_url=(env.get("MOVIENIGHT_PUBLIC_URL") or "http://localhost:8080").strip(),
            plex_push_seen=(env.get("MOVIENIGHT_PLEX_PUSH_SEEN") or "").strip().lower() in ("1", "true", "yes"),
        )
