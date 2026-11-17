import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "next_version", Path(__file__).parent.parent / ".github" / "scripts" / "next_version.py"
)
nv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nv)


@pytest.mark.parametrize(
    "title,body,expected",
    [
        ("fix: whatever", "", "1.2.4"),
        ("feat: add thing", "", "1.3.0"),
        ("feat!: drop thing", "", "2.0.0"),
        ("fix(api)!: change shape", "", "2.0.0"),
        ("fix: x", "Details\n\nBREAKING CHANGE: gone", "2.0.0"),
        ("fix: x", "BREAKING-CHANGE: gone", "2.0.0"),
        ("chore: deps", "", "1.2.4"),
        ("docs: readme", "", "1.2.4"),
        ("feat(ui): new button", "", "1.3.0"),
        ("Update stuff", "", "1.2.4"),
        ("", "", "1.2.4"),
    ],
)
def test_bump(title, body, expected):
    assert nv.next_version("v1.2.3", title, body) == expected


@pytest.mark.parametrize("title", ["fix: a", "feat: a", "feat!: a", "random"])
def test_no_previous_tag_is_1_0_0(title):
    assert nv.next_version("", title) == "1.0.0"


def test_bad_tag_raises():
    with pytest.raises(ValueError):
        nv.next_version("release-1", "fix: a")
