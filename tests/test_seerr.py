"""Seerr (Overseerr / Jellyseerr) requests: status mapping, requests, errors, caching, the batch cap and the
off switch. Seerr is a mock transport: no real instance is ever contacted."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from movienight import seerr
from movienight.app import create_app
from movienight.config import Config

from conftest import no_network, seed

URL = "http://seerr.local:5055"
KEY = "test-key"


def movie(media_status=None, requests=()):
    if media_status is None and not requests:
        return {"id": 1}
    return {"id": 1, "mediaInfo": {"status": media_status or 1, "requests": list(requests)}}


@pytest.mark.parametrize("payload, want", [
    ({"id": 1}, "none"),                                         # Seerr has no media record yet
    (movie(1), "none"),                                          # unknown
    (movie(2), "pending"),
    (movie(3), "processing"),
    (movie(4), "available"),                                     # partially available
    (movie(5), "available"),
    (movie(6), "blocked"),                                       # Jellyseerr blocklist
    (movie(7), "none"),                                          # deleted: can be requested again
    (movie(1, [{"status": 1}]), "pending"),                      # request pending approval
    (movie(1, [{"status": 2}]), "processing"),                   # approved, downloading
    (movie(1, [{"status": 3}]), "none"),                         # declined
    (movie(1, [{"status": 4}]), "none"),                         # failed
    (movie(1, [{"status": 1, "is4k": True}]), "none"),           # only a 4K request
    (movie(1, [{"status": 1}, {"status": 2}]), "processing"),
])
def test_status_mapping(payload, want):
    assert seerr.status_of(payload) == want


@pytest.mark.parametrize("payload, want", [
    ({"status": 1}, "pending"), ({"status": 2}, "processing"), ({"status": 2, "media": {"status": 3}}, "processing"),
    ({"status": 1, "media": {"status": 5}}, "available"), ({}, "pending"),
])
def test_request_state(payload, want):
    assert seerr.request_state(payload) == want


class FakeSeerr:
    """Seerr over a mock transport: per-film payloads, a log of calls, and switchable failures."""

    def __init__(self):
        self.movies: dict[int, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.request_answer = (201, {"status": 1})
        self.fail: Exception | int | None = None
        self.bodies: list[dict] = []
        self.headers: list[str | None] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "seerr.local":
            return no_network(request)
        self.calls.append((request.method, request.url.path))
        self.headers.append(request.headers.get("x-api-key"))
        if isinstance(self.fail, Exception):
            raise self.fail
        if isinstance(self.fail, int):
            return httpx.Response(self.fail, json={"message": "nope"})
        if request.method == "GET" and request.url.path.startswith("/api/v1/movie/"):
            tid = int(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json=self.movies.get(tid, {"id": tid}))
        if request.method == "POST" and request.url.path == "/api/v1/request":
            self.bodies.append(json.loads(request.content))
            code, body = self.request_answer
            return httpx.Response(code, json=body)
        return httpx.Response(404, json={})


def make(fake, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return seerr.Seerr(client, URL + "/", KEY, **kw)


def run(coro):
    return asyncio.run(coro)


def test_movie_status_sends_key_and_maps():
    fake = FakeSeerr()
    fake.movies[603] = movie(3)
    s = make(fake)
    assert run(s.movie_status(603)) == "processing"
    assert fake.calls == [("GET", "/api/v1/movie/603")]
    assert fake.headers == [KEY]


def test_status_is_cached_then_expires():
    fake = FakeSeerr()
    fake.movies[603] = movie(2)
    now = [1000.0]
    s = make(fake, clock=lambda: now[0], ttl=300)
    assert run(s.movie_status(603)) == "pending"
    fake.movies[603] = movie(5)
    assert run(s.movie_status(603)) == "pending"            # cached
    assert len(fake.calls) == 1
    now[0] += 301
    assert run(s.movie_status(603)) == "available"          # expired: asked again
    assert len(fake.calls) == 2


def test_request_success_and_body():
    fake = FakeSeerr()
    fake.request_answer = (201, {"id": 9, "status": 2})     # auto-approved
    s = make(fake)
    assert run(s.request(550)) == "processing"
    assert fake.bodies == [{"mediaType": "movie", "mediaId": 550}]
    assert run(s.movie_status(550)) == "processing"         # remembered, no GET
    assert [c[0] for c in fake.calls] == ["POST"]


def test_request_409_counts_as_requested():
    fake = FakeSeerr()
    fake.request_answer = (409, {"message": "Request for this media already exists."})
    s = make(fake)
    assert run(s.request(550)) == "pending"                 # Seerr says nothing yet: still requested
    fake.movies[551] = movie(3)
    assert run(s.request(551)) == "processing"              # its real state when Seerr knows it


@pytest.mark.parametrize("fail, message", [
    (401, "rejected the API key"),
    (403, "may not request"),
    (500, "error (500)"),
    (httpx.ReadTimeout("slow"), "did not answer in time"),
    (httpx.ConnectError("down"), "Could not reach Seerr"),
])
def test_errors_are_clean(fail, message):
    fake = FakeSeerr()
    fake.fail = fail
    s = make(fake)
    with pytest.raises(seerr.SeerrError) as e:
        run(s.request(550))
    assert message in str(e.value)
    assert URL not in str(e.value) and KEY not in str(e.value)


def test_batch_is_capped_deduped_and_bounded():
    fake = FakeSeerr()
    in_flight = [0, 0]
    inner = fake.__call__

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            in_flight[0] += 1
            in_flight[1] = max(in_flight[1], in_flight[0])
            await asyncio.sleep(0.005)
            in_flight[0] -= 1
            return inner(request)

    s = seerr.Seerr(httpx.AsyncClient(transport=Slow()), URL, KEY, concurrency=3)
    ids = list(range(1, 101)) + [1, 2, 3]
    got = run(s.statuses(ids))
    assert len(got) == seerr.BATCH_CAP and set(got) == set(range(1, seerr.BATCH_CAP + 1))
    assert len(fake.calls) == seerr.BATCH_CAP
    assert in_flight[1] <= 3
    run(s.statuses(ids))                                     # all cached now
    assert len(fake.calls) == seerr.BATCH_CAP


def test_batch_all_failed_raises_first_error():
    fake = FakeSeerr()
    fake.fail = 401
    s = make(fake)
    with pytest.raises(seerr.SeerrError, match="API key"):
        run(s.statuses([1, 2]))


# ---- the API


@pytest.fixture
def fake():
    return FakeSeerr()


@pytest.fixture
def seerr_client(tmp_path, fake):
    cfg = Config(data_dir=tmp_path / "seerr-data", letterboxd_dir=tmp_path / "nolb", sync_on_start=False,
                 seerr_url=URL, seerr_api_key=KEY)
    app = create_app(cfg, transport=httpx.MockTransport(fake))
    with TestClient(app) as c:
        seed(cfg.db_path)
        app.state.engine.invalidate()
        yield c


def test_api_status_flag(client, seerr_client):
    assert client.get("/api/status").json()["features"]["seerr"] is False
    assert seerr_client.get("/api/status").json()["features"]["seerr"] is True


def test_api_off_when_not_configured(client):
    assert client.get("/api/seerr/status?tmdb=1").status_code == 404
    assert client.post("/api/seerr/request/1").status_code == 404


def test_api_needs_both_settings(tmp_path):
    cfg = Config(data_dir=tmp_path / "data", letterboxd_dir=tmp_path / "nolb", sync_on_start=False, seerr_url=URL)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(no_network))) as c:
        assert c.get("/api/status").json()["features"]["seerr"] is False
        assert c.post("/api/seerr/request/1").status_code == 404


def test_api_batch_status(seerr_client, fake):
    fake.movies[10] = movie(2)
    fake.movies[11] = movie(5)
    r = seerr_client.get("/api/seerr/status?tmdb=10,11,12,10")
    assert r.status_code == 200
    assert r.json() == {"statuses": {"10": "pending", "11": "available", "12": "none"}}
    assert seerr_client.get("/api/seerr/status?tmdb=").json() == {"statuses": {}}
    assert seerr_client.get("/api/seerr/status?tmdb=a,b").status_code == 422


def test_api_request_and_errors(seerr_client, fake):
    r = seerr_client.post("/api/seerr/request/550")
    assert r.status_code == 200 and r.json() == {"status": "pending"}
    assert seerr_client.get("/api/seerr/status?tmdb=550").json()["statuses"] == {"550": "pending"}
    fake.fail = 401
    r = seerr_client.post("/api/seerr/request/551")
    assert r.status_code == 502                               # never a 401: that would end the app's session
    assert "API key" in r.json()["detail"]
    fake.fail = httpx.ReadTimeout("slow")
    r = seerr_client.post("/api/seerr/request/552")
    assert r.status_code == 504 and "in time" in r.json()["detail"]


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("MOVIENIGHT_SEERR_URL", URL + "/")
    monkeypatch.setenv("MOVIENIGHT_SEERR_API_KEY", " k ")
    cfg = Config.from_env()
    assert cfg.seerr_url == URL and cfg.seerr_api_key == "k" and cfg.seerr_configured
    assert "k'" not in repr(cfg) and "seerr_api_key" not in repr(cfg)
