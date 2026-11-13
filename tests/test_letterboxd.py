import io
import zipfile

import pytest

from conftest import FIX
from movienight import db, letterboxd as lb


@pytest.fixture
def conn(tmp_path):
    p = tmp_path / "t.db"
    db.migrate(p)
    c = db.connect(p)
    yield c
    c.close()


def files():
    root = FIX / "export"
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.csv")}


def test_import_dir_counts(conn):
    counts = lb.import_dir(conn, FIX / "export")
    # Beta Romance is on both lists but watched, so only Epsilon stays on the watchlist
    assert counts == {"watched": 4, "rated": 4, "liked": 2, "watchlist": 1}
    row = conn.execute("SELECT * FROM lb_films WHERE name='Alpha Heist'").fetchone()
    assert row["rating"] == 4.5 and row["liked"] == 1 and row["uri"] == "https://boxd.it/aaa1"
    assert row["watched_date"] == "2024-01-02"
    # diary-only rating fallback
    assert conn.execute("SELECT rating FROM lb_films WHERE name LIKE 'D%'").fetchone()[0] == 3
    # watched film is not on the watchlist; accents/commas handled
    assert conn.execute("SELECT watchlist FROM lb_films WHERE name='Beta Romance'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM lb_films WHERE name='Gamma, The Return'").fetchone()[0] == 1
    assert lb.username(conn) == "testuser"


def test_import_dir_skips_unchanged_and_reimports_on_change(conn, tmp_path):
    import shutil
    d = tmp_path / "exp"
    shutil.copytree(FIX / "export", d)
    assert lb.import_dir(conn, d) is not None
    assert lb.import_dir(conn, d) is None
    (d / "watchlist.csv").write_text("Date,Name,Year,Letterboxd URI\n2024-01-01,New One,2020,https://boxd.it/n1\n")
    c2 = lb.import_dir(conn, d)
    assert c2["watchlist"] == 1
    assert conn.execute("SELECT COUNT(*) FROM lb_films WHERE name='New One' AND watchlist=1").fetchone()[0] == 1
    assert lb.import_dir(conn, tmp_path / "missing") is None


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return buf.getvalue()


def test_zip_import_with_wrapper_folder(conn):
    data = make_zip({f"export-2024/{k}": v for k, v in files().items()})
    counts = lb.import_zip(conn, data)
    assert counts["watched"] == 4


def test_zip_rejects_path_traversal_and_junk(conn):
    with pytest.raises(lb.ExportError):
        lb.import_zip(conn, make_zip({"../evil.csv": "x", "watched.csv": "Date,Name,Year,Letterboxd URI\n"}))
    with pytest.raises(lb.ExportError):
        lb.import_zip(conn, b"not a zip")
    with pytest.raises(lb.ExportError):
        lb.import_zip(conn, make_zip({"readme.txt": "hi"}))


def test_zip_size_limits(conn, monkeypatch):
    monkeypatch.setattr(lb, "MAX_MEMBER_BYTES", 100)
    with pytest.raises(lb.ExportError):
        lb.import_zip(conn, make_zip({"watched.csv": "Date,Name,Year,Letterboxd URI\n" + "x" * 500}))


def test_rss_parse_and_apply(conn):
    lb.import_dir(conn, FIX / "export")
    entries = lb.parse_rss((FIX / "rss.xml").read_bytes())
    assert [e["title"] for e in entries] == ["Epsilon Future", "Beta Romance", "Brand New"]
    assert entries[0]["tmdb_id"] == 505 and entries[0]["liked"] and entries[0]["rating"] == 4.0
    lb.apply_rss(conn, entries)
    eps = conn.execute("SELECT * FROM lb_films WHERE name='Epsilon Future'").fetchone()
    assert eps["watched"] == 1 and eps["watchlist"] == 0 and eps["rating"] == 4.0 and eps["liked"] == 1
    assert eps["tmdb_id"] == 505
    beta = conn.execute("SELECT * FROM lb_films WHERE name='Beta Romance'").fetchone()
    assert beta["rating"] == 2.5 and beta["tmdb_id"] == 202
    new = conn.execute("SELECT * FROM lb_films WHERE name='Brand New'").fetchone()
    assert new["tmdb_id"] == 909 and new["watched"] == 1
    assert conn.execute("SELECT COUNT(*) FROM lb_films WHERE name='Beta Romance'").fetchone()[0] == 1


def test_rss_rejects_garbage():
    with pytest.raises(lb.ExportError):
        lb.parse_rss(b"<rss><item>")
