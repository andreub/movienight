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
        )
