"""Letterboxd diary tags: import, groups, tag-steered picks, profile stats and the chat tool. Synthetic data only."""
import json

import pytest

from conftest import FIX
from movienight import chat, db, letterboxd as lb, recommender as rec, tags as lbtags

DIARY = (
    "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
    "2023-10-05,Alpha Heist,2015,https://boxd.it/t1,4.5,,\"Fest2023,  favs \",2023-10-05\n"
    "2023-10-06,Delta Noir,2012,https://boxd.it/t2,4,,fest2023,2023-10-06\n"
    "2024-10-04,Beta Romance,2019,https://boxd.it/t3,2,,fest-2024,2024-10-04\n"
    "2024-11-01,Beta Romance,2019,https://boxd.it/t4,,Yes,,2024-11-01\n"
)
REVIEWS = (
    "Date,Name,Year,Letterboxd URI,Rating,Rewatch,Review,Tags,Watched Date\n"
    "2023-10-06,Delta Noir,2012,https://boxd.it/t2,4,,\"Great, really\",\"fest2023, noir night\",2023-10-06\n"
)


@pytest.fixture
def conn(tmp_path):
    p = tmp_path / "t.db"
    db.migrate(p)
    c = db.connect(p)
    yield c
    c.close()


def test_parse_tags_and_family():
    assert lb.parse_tags(" Sitges2025, rec 2021 ,,sitges2025, Night  Out ") == ["sitges2025", "rec 2021", "night out"]
    assert lb.parse_tags(None) == [] and lb.parse_tags("") == []
    assert lb.tag_family("sitges2025") == ("sitges", 2025)
    assert lb.tag_family("rec 2021") == ("rec", 2021)
    assert lb.tag_family("fest-1999") == ("fest", 1999)
    assert lb.tag_family("favs") == ("favs", None)
    assert lb.tag_family("2025") == ("2025", None)          # only a year: its own family
    assert lb.tag_family("top12345") == ("top12345", None)  # not a 4-digit year at the end
    assert lb.tag_family("x1850") == ("x1850", None)        # outside 1900-2099


def test_migration_6_creates_tags_table(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) >= 6
    cols = {r[1] for r in conn.execute("PRAGMA table_info(lb_tags)")}
    assert {"lb_id", "tag", "family", "edition", "watched_date", "rating", "source"} <= cols


def _files():
    root = FIX / "export"
    files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*.csv")}
    files["diary.csv"] = DIARY.encode()
    files["reviews.csv"] = REVIEWS.encode()
    return files


def _tags(conn):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT f.name, t.tag, t.family, t.edition, t.watched_date, t.rating FROM lb_tags t JOIN lb_films f ON f.id=t.lb_id"))


def test_import_stores_diary_and_review_tags_idempotently(conn):
    lb.apply_export(conn, lb.parse_export(_files()))
    expected = [
        ("Alpha Heist", "favs", "favs", None, "2023-10-05", 4.5),
        ("Alpha Heist", "fest2023", "fest", 2023, "2023-10-05", 4.5),
        ("Beta Romance", "fest-2024", "fest", 2024, "2024-10-04", 2.0),
        ("Delta Noir", "fest2023", "fest", 2023, "2023-10-06", 4.0),     # diary + review: one row
        ("Delta Noir", "noir night", "noir night", None, "2023-10-06", 4.0),
    ]
    assert _tags(conn) == expected
    lb.apply_export(conn, lb.parse_export(_files()))                     # re-import: same rows
    assert _tags(conn) == expected
    # an export without a diary (partial upload) keeps the stored tags
    no_diary = {k: v for k, v in _files().items() if k not in ("diary.csv", "reviews.csv")}
    lb.apply_export(conn, lb.parse_export(no_diary))
    assert _tags(conn) == expected
    # a newer export where a tag was removed replaces the export's rows
    lb.apply_export(conn, lb.parse_export({**_files(), "reviews.csv": b"", "diary.csv": DIARY.replace("fest-2024", "").encode()}))
    assert ("Beta Romance", "fest-2024", "fest", 2024, "2024-10-04", 2.0) not in _tags(conn)


def test_zip_and_folder_import_read_reviews(conn, tmp_path):
    import shutil
    d = tmp_path / "exp"
    shutil.copytree(FIX / "export", d)
    (d / "diary.csv").write_text(DIARY)
    (d / "reviews.csv").write_text(REVIEWS)
    assert lb.import_dir(conn, d) is not None
    assert len(_tags(conn)) == 5
    assert lb.import_dir(conn, d) is None                                 # unchanged: skipped


def test_import_version_forces_one_reimport(conn, tmp_path, monkeypatch):
    import shutil
    d = tmp_path / "exp"
    shutil.copytree(FIX / "export", d)
    assert lb.import_dir(conn, d) is not None
    assert lb.import_dir(conn, d) is None
    monkeypatch.setattr(lb, "IMPORT_VERSION", "test-next")
    assert lb.import_dir(conn, d) is not None                             # same files, newer importer


def test_rss_has_no_tags_and_keeps_them(conn):
    lb.apply_export(conn, lb.parse_export(_files()))
    lb.apply_rss(conn, lb.parse_rss((FIX / "rss.xml").read_bytes()))
    assert len(_tags(conn)) == 5


# --------------------------------------------------------------------------- API


def test_tags_endpoint_lists_usable_groups(tag_client):
    r = tag_client.get("/api/tags").json()
    assert r["min_films"] == lbtags.MIN_GROUP_FILMS == 3
    # favs (1 film) is not offered; fest is, but only its usable edition
    assert [f["family"] for f in r["families"]] == ["fest"]
    fest = r["families"][0]
    assert fest["films"] == 4 and fest["label"] == "Fest"
    assert [e["tag"] for e in fest["editions"]] == ["fest2023"]


def test_tag_steers_recommendations(tag_client):
    base = tag_client.get("/api/recommendations").json()
    r = tag_client.get("/api/recommendations", params={"tag": "fest2023"}).json()
    titles = [m["title"] for m in r["items"]]
    assert titles[0] == "Crime Two"                       # same director, cast and themes as the tagged films
    assert all(not m["watched"] for m in r["items"])      # tagged films themselves are watched: never offered
    assert [m["match"] for m in r["items"]] != [m["match"] for m in base["items"]]
    top = r["items"][0]
    assert top["reasons"][0]["kind"] == "tag" and "fest2023" in top["reasons"][0]["text"]
    fam = tag_client.get("/api/recommendations", params={"tag_family": "fest"}).json()
    assert fam["total"] >= 1 and fam["items"][0]["reasons"][0]["kind"] == "tag"
    # case and spaces do not matter for an exact tag
    assert tag_client.get("/api/recommendations", params={"tag": " FEST2023 "}).status_code == 200


def test_tag_errors(tag_client):
    assert tag_client.get("/api/recommendations", params={"tag": "nope"}).status_code == 400
    assert tag_client.get("/api/recommendations", params={"tag": "fest2024"}).status_code == 400   # too small
    assert tag_client.get("/api/pick", params={"tag": "fest2023", "tag_family": "fest"}).status_code == 400
    assert tag_client.get("/api/recommendations", params={"tag": ""}).status_code == 200            # empty = off


def test_tag_composes_with_filters_and_exclusions(tag_client):
    first = tag_client.get("/api/pick", params={"tag": "fest2023"}).json()
    assert first["pick"]["watched"] is False
    crime = next(m for m in tag_client.get("/api/recommendations", params={"tag": "fest2023"}).json()["items"]
                 if m["title"] == "Crime Two")
    tag_client.post(f"/api/movies/{crime['id']}/dismiss", json={"days": 30})
    after = tag_client.get("/api/recommendations", params={"tag": "fest2023"}).json()
    assert "Crime Two" not in [m["title"] for m in after["items"]]
    tag_client.post(f"/api/movies/{crime['id']}/seen", json={"seen": True})
    tag_client.delete(f"/api/movies/{crime['id']}/dismiss")
    seen = tag_client.get("/api/recommendations", params={"tag": "fest2023"}).json()
    assert "Crime Two" not in [m["title"] for m in seen["items"]]
    # mood + tag: only horror, still steered by the tag
    scary = tag_client.get("/api/recommendations", params={"tag": "fest2023", "mood": "scary"}).json()
    assert all("Horror" in m["genres"] for m in scary["items"])
    for m in tag_client.get("/api/recommendations", params={"tag": "fest2023", "max_runtime": 90}).json()["items"]:
        assert m["runtime"] <= 90


def test_profile_has_tag_stats(tag_client):
    p = tag_client.get("/api/profile").json()
    fams = {f["family"]: f for f in p["tags"]}
    assert set(fams) == {"fest", "favs"}
    fest = fams["fest"]
    assert fest["films"] == 4 and fest["matched"] == 4 and fest["mean_rating"] == pytest.approx((4.5 + 4 + 3 + 2) / 4, abs=0.01)
    assert fest["top_genres"][0] == "Crime" and len(fest["top_genres"]) <= 3
    eds = {e["tag"]: e for e in fest["editions"]}
    assert [e["tag"] for e in fest["editions"]] == ["fest2024", "fest2023"]       # newest edition first
    assert eds["fest2023"]["films"] == 3 and eds["fest2023"]["edition"] == 2023
    assert eds["fest2023"]["mean_rating"] == pytest.approx(3.83, abs=0.01)
    assert eds["fest2024"]["top_genres"] == ["Comedy", "Romance"]


def test_profile_without_tags(client):
    assert client.get("/api/profile").json()["tags"] == []
    assert client.get("/api/tags").json()["families"] == []


# --------------------------------------------------------------------------- recommender + chat


def test_group_similarity_prefers_shared_traits():
    def h(key, genres, director, rating=None):
        return rec.Hist(key=key, title=f"H{key}", rating=rating, meta={"genres": genres, "directors": [director]})

    group = [h(1, ["Horror"], "Dir A", 4.0), h(2, ["Horror", "Thriller"], "Dir A", 2.0), h(3, ["Horror"], "Dir B")]
    cands = [rec.Cand(id=10, title="Scary", meta={"genres": ["Horror"], "directors": ["Dir A"]}),
             rec.Cand(id=11, title="Sweet", meta={"genres": ["Romance"], "directors": ["Dir Z"]})]
    for c in cands:
        c.feats = rec.features(c.meta)
    idf = rec.build_idf(group, cands)
    sim = rec.group_similarity(group, cands, idf, user_mean=3.0)
    assert sim[10] > 0.1 and sim[11] == 0
    assert rec.closest_in_group(cands[0], group).key in (1, 2)
    assert rec.closest_in_group(cands[1], group) is None


def run(tool, args, client):
    st = client.app.state.engine.get()
    return chat.run_tool(tool, args, st, {})


def test_chat_list_tags_and_search_by_tag(tag_client):
    o = run("list_tags", {}, tag_client)
    data = json.loads(o.content)
    fest = next(f for f in data["families"] if f["family"] == "fest")
    assert fest["usable"] is True and {e["tag"]: e["usable"] for e in fest["editions"]} == {"fest2024": False, "fest2023": True}
    # loose resolution: "Fest 2023" is the tag, "fest" the family
    for tag in ("Fest 2023", "fest"):
        res = json.loads(run("search_library", {"tag": tag, "limit": 3}, tag_client).content)
        assert res["results"][0]["title"] == "Crime Two", tag
    bad = run("search_library", {"tag": "fest2024"}, tag_client)
    assert bad.is_error and "fest2023" in bad.content
    assert "list_tags" in chat.SYSTEM_PROMPT and any(t["name"] == "list_tags" for t in chat.TOOLS)


def test_chat_list_tags_empty(client):
    assert json.loads(run("list_tags", {}, client).content)["families"] == []
