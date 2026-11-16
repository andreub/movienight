"""A short id for the code that is running, so an open browser tab can tell a newer deploy happened."""
import hashlib
from pathlib import Path

HEADER = "X-MovieNight-Version"
PLACEHOLDER = "__MOVIENIGHT_VERSION__"
_SUFFIXES = {".py", ".js", ".css", ".html", ".svg"}


def compute_version(*roots: Path) -> str:
    """Hash of the relative path and content of every shipped code file under `roots` (12 hex chars)."""
    h = hashlib.sha256()
    for root in roots:
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in _SUFFIXES and "__pycache__" not in p.parts)
        for p in files:
            h.update(p.relative_to(root).as_posix().encode() + b"\0")
            h.update(p.read_bytes() + b"\0")
    return h.hexdigest()[:12]
