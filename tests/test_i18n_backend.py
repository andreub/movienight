"""The server side of the two UI languages: labels the server builds go out as stable keys + params (with their
English text) and every key has a catalogue entry with the same placeholders; TMDB text (titles, overviews,
collection names) follows the UI language sent by the web UI, stored in the cached TMDB rows themselves; the
sign-in pages and the chat assistant follow it too. Synthetic data only."""
import json
import re
from pathlib import Path

import httpx
import pytest

from movienight import app as app_module, chat, completion, db, i18n, queries, recommender as rec, tmdb
from movienight.sync import SyncManager
from movienight import library
from movienight.config import Config

STATIC = Path(__file__).resolve().parents[1] / "src" / "movienight" / "static"
SRC = STATIC.parent
EN = json.loads((STATIC / "i18n" / "en.json").read_text(encoding="utf-8"))
ES = {"X-MovieNight-Lang": "es-ES"}


def placeholders(s):
    return set(re.findall(r"\{(\w+)\}", s))


# ---------------------------------------------------------------- every server key has a catalogue entry

@pytest.mark.parametrize("prefix,table", [
    ("reason", rec.REASON_EN), ("because", rec.BECAUSE_EN), ("creason", completion.REASON_EN),
])
def test_server_templates_have_catalogue_entries_with_the_same_placeholders(prefix, table):
    for key, en in table.items():
        assert f"{prefix}.{key}" in EN, f"{prefix}.{key}"
        assert placeholders(EN[f"{prefix}.{key}"]) == placeholders(en), key


def test_saga_reason_moods_eras_genres_and_tools_have_entries():
    r = completion.saga_reason({"name": "Alpha", "seen": 1, "total": 4})
    assert r["key"] == "saga.next" and r["text"] == "Next in Alpha · 1 of 4 seen"
    assert placeholders(EN["reason.saga.next"]) == set(r["params"])
    for m in queries.MOODS:
        assert EN[f"mood.{m['id']}.label"] == m["label"] and EN[f"mood.{m['id']}.description"] == m["description"]
    assert {"era.recent", "era.classic", "era.decade"} <= set(EN)
    for name in tmdb.GENRES.values():
        assert EN[f"genre.{name}"] == name
    for name, label in chat.TOOL_LABELS.items():
        assert EN[f"ask.tool.{name}"] == label


def test_every_error_code_the_server_sends_has_an_entry():
    codes = set()
    for f in ("app.py", "chat.py", "seerr.py", "letterboxd.py", "sync.py"):
        text = (SRC / f).read_text(encoding="utf-8")
        codes |= set(re.findall(r'Problem\(\d+, "([\w.]+)"', text))
        codes |= set(re.findall(r'_error\("([\w.]+)"', text))
        codes |= set(re.findall(r'"code": "([\w.]+)"', text))
        codes |= set(re.findall(r'return "(ai\.[\w.]+)", "', text))
        codes |= set(re.findall(r'(?:ExportError|SeerrError)\([^)]*"([a-z]+\.[\w.]+)"', text))
        codes |= set(re.findall(r'problem_code = \("([\w.]+)"', text))
    codes |= set(chat.CONFIG_PROBLEM_EN)
    assert len(codes) > 40
    missing = sorted(c for c in codes if f"apiError.{c}" not in EN)
    assert not missing, missing


# ---------------------------------------------------------------- keys + params, English text kept

def test_reason_keeps_the_english_text_and_raw_numbers():
    r = rec.reason("acclaimed", "acclaimed", rating=8.2, votes=1800)
    assert r == {"kind": "acclaimed", "key": "acclaimed", "params": {"rating": 8.2, "votes": 1800},
                 "text": "Acclaimed: 8.2 on TMDB (1.8k votes)"}
    assert rec.reason("keyword", "keywords", keywords=["heist", "caper"])["text"] == "Themes you like: heist, caper"


def test_api_reasons_carry_keys_and_params(client):
    items = client.get("/api/recommendations", params={"limit": 50}).json()["items"]
    reasons = [r for m in items for r in m["reasons"]]
    assert reasons and all(r["key"] and isinstance(r["params"], dict) and r["text"] for r in reasons)
    assert all(f"reason.{r['key']}" in EN for r in reasons)
    alpha = next(r for r in reasons if r["key"].startswith("similar"))
    assert alpha["params"]["title"] == "Alpha Heist" and "Fans of Alpha Heist" in alpha["text"]
    m = client.get(f"/api/movies/{items[0]['id']}").json()
    assert all(b["why_key"] in rec.BECAUSE_EN and "why" in b for b in m["because"])


def test_group_reasons_carry_keys(client):
    d = {"films": [{"tmdb_id": 1, "state": "seen", "release_date": "2000-01-01", "title": "A", "year": 2000}],
         "available": 1, "seen": 1, "total": 2}
    total, key, params = completion.score_reason(d, {}, 3.0, __import__("datetime").date(2026, 1, 1))
    assert key == "library" and completion.reason_text(key, params) == "In your library"


def test_errors_carry_a_code(client):
    r = client.get("/api/movies/999999")
    assert r.status_code == 404 and r.json() == {"detail": "Movie not found", "code": "movie.notFound", "params": {}}
    assert client.get("/api/completions/collection-424242").json()["code"] == "group.notFound"


# ---------------------------------------------------------------- the UI language: header, cookie, fallbacks

class Req:
    def __init__(self, headers=None, cookies=None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.cookies = cookies or {}


def test_request_language_prefers_the_ui_choice():
    assert i18n.request_lang(Req({"X-MovieNight-Lang": "es-ES", "Accept-Language": "en-US"})) == "es-ES"
    # the app's own choice wins over the system locale (the TV app's WebView sends Android's)
    assert i18n.request_lang(Req({"X-MovieNight-Lang": "en", "Accept-Language": "es-ES"})) == "en"
    assert i18n.request_lang(Req({"Accept-Language": "es-ES"}, {"movienight_lang": "en"})) == "en"
    assert i18n.request_lang(Req({"Accept-Language": "ca, es;q=0.8"})) == "es-ES"
    assert i18n.request_lang(Req({"Accept-Language": "fr-FR"})) == "en"
    assert i18n.request_lang(None) == "en"


# ---------------------------------------------------------------- TMDB text in the UI language

def raw_details(tid, title, translations):
    return {"id": tid, "title": title, "original_title": title, "release_date": "1957-12-17", "overview": "English.",
            "translations": {"translations": [
                {"iso_639_1": lc, "iso_3166_1": rg, "data": data} for lc, rg, data in translations]}}


def test_compact_details_stores_spanish_from_spain_first():
    raw = raw_details(1, "Witness for the Prosecution", [
        ("es", "MX", {"title": "Testigo de cargo (MX)", "overview": "MX", "tagline": ""}),
        ("es", "ES", {"title": "Testigo de cargo", "overview": "Un abogado...", "tagline": ""}),
        ("fr", "FR", {"title": "Témoin à charge"})])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(i18n, "EXTRA", ("es-ES",))
        d = tmdb.compact_details(raw)
        assert d["i18n"] == {"es-ES": {"title": "Testigo de cargo", "overview": "Un abogado..."}}
        assert tmdb.compact_details(raw_details(2, "X", [("es", "MX", {"title": "Equis"})]))["i18n"] == {"es-ES": {"title": "Equis"}}
        assert tmdb.compact_details(raw_details(3, "Y", []))["i18n"] == {"es-ES": {}}      # none: the original stays


def test_collection_name_cleanup():
    assert tmdb._clean_collection_name("El señor de los anillos - Colección") == "El señor de los anillos"
    assert tmdb._clean_collection_name("Colección Harry Potter") == "Harry Potter"


@pytest.mark.i18n
async def test_collection_and_person_fetch_the_other_languages(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.url.path, request.url.params.get("language")))
        es = request.url.params.get("language") == "es-ES"
        if request.url.path == "/3/collection/7":
            return httpx.Response(200, json={"id": 7, "name": "El caballero oscuro - Colección" if es else "Dark Collection",
                                             "parts": [{"id": 70, "title": "El caballero oscuro" if es else "The Dark Knight",
                                                        "release_date": "2008-07-16"}]})
        if request.url.path == "/3/person/5":
            return httpx.Response(200, json={"id": 5, "name": "D", "movie_credits": {"crew": [
                {"id": 70, "title": "The Dark Knight", "job": "Director", "release_date": "2008-07-16"}]}})
        if request.url.path == "/3/person/5/movie_credits":
            return httpx.Response(200, json={"crew": [{"id": 70, "title": "El caballero oscuro", "job": "Director"}]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        t = tmdb.Tmdb(http, "eyJx")
        c = await t.collection(7)
        p = await t.person_credits(5)
    assert c["parts"][0]["title"] == "The Dark Knight" and c["name"] == "Dark Collection"
    assert c["i18n"] == {"es-ES": {"name": "El caballero oscuro", "titles": {"70": "El caballero oscuro"}}}
    assert p["i18n"] == {"es-ES": {"titles": {"70": "El caballero oscuro"}}}
    assert ("/3/collection/7", "es-ES") in calls and ("/3/person/5/movie_credits", "es-ES") in calls


@pytest.mark.i18n
async def test_sync_backfills_details_stored_before_the_translations(tmp_path):
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", tmdb_token="eyJtest", sync_on_start=False)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.migrate(cfg.db_path)
    base = {"release_date": "2020-01-01", "collection": None, "keyword_ids": []}
    with db.session(cfg.db_path) as c:
        for tid in (1, 2):
            c.execute("INSERT INTO lb_films(name,year,norm_title,tmdb_id,resolve_state,watched) VALUES(?,2020,?,?,1,1)",
                      (f"F{tid}", f"f{tid}", tid))
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(1,?,'x')", (json.dumps(base),))
        c.execute("INSERT INTO tmdb_details(tmdb_id,json,fetched_at) VALUES(2,?,'x')",
                  (json.dumps({**base, "i18n": {"es-ES": {}}}),))
    fetched = []

    def handler(request):
        tid = int(request.url.path.split("/")[3])
        fetched.append((tid, request.url.params.get("append_to_response")))
        return httpx.Response(200, json=raw_details(tid, "T", [("es", "ES", {"title": "Te"})]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mgr = SyncManager(cfg, http, library.Engine(lambda: db.connect(cfg.db_path), None))
        mgr._stage("tmdb")
        with db.session(cfg.db_path) as c:
            await mgr._details(c)
            stored = json.loads(c.execute("SELECT json FROM tmdb_details WHERE tmdb_id=1").fetchone()[0])
    assert fetched == [(1, "credits,keywords,translations")]          # once: 2 already has its translations
    assert stored["i18n"] == {"es-ES": {"title": "Te"}}


@pytest.fixture
def es_client(client, monkeypatch):
    """The seeded library with Spanish TMDB text for some films (Crime Two = 111, history film Alpha Heist = 101)."""
    monkeypatch.setattr(i18n, "EXTRA", ("es-ES",))
    with db.session(client.cfg.db_path) as c:
        for tid, title, overview in ((111, "Crimen Dos", "Un golpe."), (101, "El gran golpe", ""), (112, "", "")):
            row = json.loads(c.execute("SELECT json FROM tmdb_details WHERE tmdb_id=?", (tid,)).fetchone()[0])
            row["i18n"] = {"es-ES": {k: v for k, v in (("title", title), ("overview", overview)) if v}}
            c.execute("UPDATE tmdb_details SET json=? WHERE tmdb_id=?", (json.dumps(row), tid))
        c.execute("INSERT INTO tmdb_collections(collection_id,json,fetched_at) VALUES(900,?,?)", (json.dumps(
            {"id": 900, "name": "Alpha Collection", "parts": [
                {"id": 101, "title": "Alpha Heist", "year": 2015, "release_date": "2015-06-01", "vote_count": 10},
                {"id": 111, "title": "Crime Two", "year": 2016, "release_date": "2016-06-01", "vote_count": 10},
                {"id": 9001, "title": "Alpha Three", "year": 2018, "release_date": "2018-06-01", "vote_count": 10}],
             "i18n": {"es-ES": {"name": "Saga Alfa", "titles": {"9001": "Alfa Tres"}}}}), "2026-01-01T00:00:00Z"))
    client.app.state.engine.invalidate()
    return client


def test_titles_and_overviews_follow_the_ui_language(es_client):
    by_id = lambda r: {m["tmdb_id"]: m for m in r.json()["items"]}  # noqa: E731
    es = by_id(es_client.get("/api/recommendations", params={"limit": 50}, headers=ES))
    en = by_id(es_client.get("/api/recommendations", params={"limit": 50}))
    assert es[111]["title"] == "Crimen Dos" and es[111]["overview"] == "Un golpe."
    assert es[111]["original_title"] == "Crime Two"
    assert es[112]["title"] == "Rom Com"                       # TMDB has no Spanish title: the original
    assert en[111]["title"] == "Crime Two" and en[111]["overview"] == "o"
    # titles inside the reasons too (Alpha Heist, a history film, is "El gran golpe" in Spanish)
    titles = [r["params"].get("title") for m in es.values() for r in m["reasons"] if r["params"].get("title")]
    assert "El gran golpe" in titles and "Alpha Heist" not in titles
    assert any(r["text"].startswith("Fans of El gran golpe") for m in es.values() for r in m["reasons"])  # English wording
    # the cache of one language never answers for the other
    again = by_id(es_client.get("/api/recommendations", params={"limit": 50}))
    assert again[111]["title"] == "Crime Two"


def test_groups_follow_the_ui_language(es_client):
    es = es_client.get("/api/completions", headers=ES).json()
    g = next(g for g in es["groups"] if g["key"] == "collection-900")
    assert g["name"] == "Saga Alfa"
    assert [f["title"] for f in g["films"]] == ["El gran golpe", "Crimen Dos", "Alfa Tres"]
    assert g["next"]["title"] == "Crimen Dos"
    en = es_client.get("/api/completions").json()
    assert next(g for g in en["groups"] if g["key"] == "collection-900")["name"] == "Alpha"
    mid = next(m["id"] for m in es_client.get("/api/recommendations", params={"limit": 50}).json()["items"]
               if m["tmdb_id"] == 111)
    m = es_client.get(f"/api/movies/{mid}", headers=ES).json()     # Crime Two: "Part of Saga Alfa"
    assert m["title"] == "Crimen Dos" and m["groups"][0]["name"] == "Saga Alfa"
    f = es_client.get("/api/films/9001", headers=ES)
    assert f.status_code == 200, f.json()
    assert f.json()["title"] == "Alfa Tres"


def test_film_lists_follow_the_ui_language(es_client):
    items = es_client.get("/api/seen-candidates", params={"limit": 50}, headers=ES).json()["items"]
    assert "Crimen Dos" in [m["title"] for m in items] or all(m["tmdb_id"] != 111 for m in items)
    top = es_client.get("/api/profile", headers=ES).json()["top_rated"]
    assert "El gran golpe" in [t["title"] for t in top]


# ---------------------------------------------------------------- sign-in pages and the assistant

@pytest.fixture
def login_client(tmp_path):
    from fastapi.testclient import TestClient
    cfg = Config(data_dir=tmp_path / "data", letterboxd_dir=tmp_path / "nolb", sync_on_start=False, login="plex")
    app = app_module.create_app(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with TestClient(app) as c:
        yield c


def test_sign_in_pages_follow_the_ui_language(login_client):
    page = login_client.get("/login", headers={"Cookie": "movienight_lang=es-ES"}).text
    assert "<html lang=es-ES>" in page and "Entrar con Plex" in page and "Sign in" not in page
    assert "Entrar con un código (TV)" in page
    tv = login_client.get("/login/plex/tv", headers={"Accept-Language": "es-ES,es;q=0.9"}).text
    assert "Entra con un código" in tv and "plex.tv/link</a> en el móvil" in tv and "Pedir otro código" in tv
    assert "Sign in with Plex" in login_client.get("/login").text
    out = login_client.get("/logged-out", headers={"Cookie": "movienight_lang=es-ES"}).text
    assert "Sesión cerrada" in out and "Nos vemos" in out
    assert i18n.login_error("es-ES", "This code expired. Get a new one.") == "Este código ha caducado. Pide otro."


def test_assistant_answers_in_the_ui_language():
    assert chat.system_prompt("en") == chat.SYSTEM_PROMPT
    es = chat.system_prompt("es-ES")
    assert es.startswith(chat.SYSTEM_PROMPT) and "Spanish" in es and "Spain" in es
