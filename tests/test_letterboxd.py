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
    assert row["watched_date"] == "2024-01-01"            # diary Watched Date, not the rating date
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


def test_watched_date_prefers_diary_over_rating_date(conn):
    # #51: the rating was set years after the watch; watched.csv has its own (logged) date
    exp = lb.parse_export({
        "watched.csv": b"Date,Name,Year,Letterboxd URI\n2019-06-01,Old One,2010,https://boxd.it/o1\n"
                       b"2020-03-01,No Diary,2011,https://boxd.it/n1\n",
        "ratings.csv": b"Date,Name,Year,Letterboxd URI,Rating\n2023-02-02,Old One,2010,https://boxd.it/o1,4\n"
                       b"2023-02-03,No Diary,2011,https://boxd.it/n1,3\n2023-02-04,Rated Only,2012,https://boxd.it/r1,3\n",
        "diary.csv": b"Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
                     b"2019-05-31,Old One,2010,https://boxd.it/d1,4,,,2019-05-30\n"
                     b"2021-01-02,Old One,2010,https://boxd.it/d2,4,Yes,,2021-01-01\n",
    })
    lb.apply_export(conn, exp)
    dates = dict(conn.execute("SELECT name, watched_date FROM lb_films").fetchall())
    assert dates == {"Old One": "2021-01-01", "No Diary": "2020-03-01", "Rated Only": "2023-02-04"}
    # a wrong (rating) date stored by an older import is corrected; a rewatch newer than the export is kept
    conn.execute("UPDATE lb_films SET watched_date='2023-02-02' WHERE name='Old One'")
    conn.execute("UPDATE lb_films SET watched_date='2024-05-05' WHERE name='No Diary'")
    lb.apply_export(conn, exp)
    dates = dict(conn.execute("SELECT name, watched_date FROM lb_films").fetchall())
    assert dates["Old One"] == "2021-01-01" and dates["No Diary"] == "2024-05-05"
