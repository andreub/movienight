"""Small shared helpers."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm_title(title: str | None) -> str:
    """Lowercase, accent-free, punctuation-free title used for matching."""
    if not title:
        return ""
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    t = t.lower().replace("&", " and ")
    t = _NON_ALNUM.sub(" ", t).strip()
    return t


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        v = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def stars(rating: float | None) -> str:
    if rating is None:
        return ""
    full = int(rating)
    return "★" * full + ("½" if rating - full >= 0.5 else "")


def to_int(value, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_float(value, default: float | None = None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def safe_error(exc: BaseException) -> str:
    """Error text that never contains URLs (which could embed credentials)."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}"
    if isinstance(exc, httpx.TimeoutException):
        return "request timed out"
    if isinstance(exc, httpx.HTTPError):
        return f"network error ({type(exc).__name__})"
    return f"{type(exc).__name__}: {str(exc)[:120]}" if not isinstance(exc, KeyError) else "KeyError"
