"""Compute the next semantic version from a Conventional Commits title.

Usage: next_version.py --last-tag TAG_OR_EMPTY --title TITLE [--body BODY]
Prints the next version (no leading "v").
"""

import argparse
import re

SEMVER_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
HEADER = re.compile(r"^(?P<type>[a-zA-Z]+)(\([^)]*\))?(?P<bang>!)?:\s")
BREAKING_BODY = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)


def bump_kind(title: str, body: str = "") -> str:
    title = title.strip()
    m = HEADER.match(title)
    if (m and m.group("bang")) or BREAKING_BODY.search(body or ""):
        return "major"
    if m and m.group("type").lower() == "feat":
        return "minor"
    return "patch"


def next_version(last_tag: str, title: str, body: str = "") -> str:
    last_tag = (last_tag or "").strip()
    if not last_tag:
        return "1.0.0"
    m = SEMVER_TAG.match(last_tag)
    if not m:
        raise ValueError(f"not a vX.Y.Z tag: {last_tag!r}")
    major, minor, patch = (int(g) for g in m.groups())
    kind = bump_kind(title, body)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--last-tag", default="")
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    a = p.parse_args()
    print(next_version(a.last_tag, a.title, a.body))


if __name__ == "__main__":
    main()
