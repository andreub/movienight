import re

from movienight import version
from movienight.app import create_app

from test_auth import make


def test_header_matches_page_and_is_stable(client):
    page = client.get("/")
    m = re.search(r'name="movienight-version" content="([0-9a-f]{12})"', page.text)
    assert m and version.PLACEHOLDER not in page.text
    for path in ("/api/moods", "/api/status", "/api/nope"):
        assert client.get(path).headers[version.HEADER] == m.group(1)
    assert client.get("/healthz").status_code == 200


def test_compute_version_changes_with_content(tmp_path):
    (tmp_path / "a.js").write_text("one")
    (tmp_path / "b.py").write_text("x")
    v1 = version.compute_version(tmp_path)
    assert v1 == version.compute_version(tmp_path)
    (tmp_path / "a.js").write_text("two")
    v2 = version.compute_version(tmp_path)
    assert v2 != v1
    (tmp_path / "c.css").write_text("")
    assert version.compute_version(tmp_path) != v2
    (tmp_path / "notes.txt").write_text("ignored")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "z.py").write_text("ignored")
    assert version.compute_version(tmp_path) == version.compute_version(tmp_path)


def test_version_header_survives_auth(tmp_path):
    client, _ = make(tmp_path)
    with client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/login").status_code == 200
        r = client.get("/api/status")
        assert r.status_code == 401 and r.headers[version.HEADER]
        assert client.get("/").status_code == 303
