"""Play on a Plex player (Plex Companion): parsing, request shape, fallback, endpoints."""
import asyncio
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from conftest import seed
from movienight import db, plex
from movienight.app import create_app
from movienight.config import Config

RESOURCES = [
    {"name": "Home", "clientIdentifier": "mach1", "provides": "server", "connections": []},
    {"name": "Living Room TV", "clientIdentifier": "tv1", "provides": "client,player", "product": "Plex for Android (TV)",
     "platform": "Android", "connections": [
         {"uri": "https://relay.example/tv", "local": False, "relay": True},
         {"uri": "http://192.168.1.50:32500", "local": True, "relay": False}]},
    {"name": "Phone", "clientIdentifier": "ph1", "provides": "client", "connections": []},
]
TV = {"id": "tv1", "name": "Living Room TV", "connections": [{"uri": "http://192.168.1.50:32500", "local": True, "relay": False}]}


def test_parse_players_keeps_players_and_drops_relay():
    players = plex.parse_players(RESOURCES)
    assert [p["id"] for p in players] == ["tv1"]
    p = players[0]
    assert p["name"] == "Living Room TV" and p["product"] == "Plex for Android (TV)"
    assert [c["uri"] for c in p["connections"]] == ["http://192.168.1.50:32500"]


def run_play(handler, player=TV):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await plex.play_media(c, player, "m1", "http://plex.local:32400", "SRV-TOKEN", "11",
                                         "cid", "USER-TOKEN")
    return asyncio.run(go())


def test_play_media_request_shape():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200)

    assert run_play(handler)
    assert len(seen) == 1
    r = seen[0]
    assert r.url.host == "192.168.1.50" and r.url.port == 32500 and r.url.path == "/player/playback/playMedia"
    q = {k: v[0] for k, v in parse_qs(r.url.query.decode()).items()}
    assert q["key"] == "/library/metadata/11" and q["machineIdentifier"] == "m1"
    assert (q["address"], q["port"], q["protocol"]) == ("plex.local", "32400", "http")
    assert q["token"] == "SRV-TOKEN" and q["type"] == "video" and q["offset"] == "0"
    assert q["providerIdentifier"] == "com.plexapp.plugins.library" and q["commandID"].isdigit()
    assert r.headers["X-Plex-Target-Client-Identifier"] == "tv1"
    assert r.headers["X-Plex-Token"] == "USER-TOKEN"


def test_play_media_falls_back_to_server_proxy():
    hosts = []

    def handler(req):
        hosts.append(req.url.host)
        if req.url.host == "192.168.1.50":
            raise httpx.ConnectError("unreachable")
        return httpx.Response(200)

    assert run_play(handler)
    assert hosts == ["192.168.1.50", "plex.local"]


def test_play_media_fails_when_nothing_answers():
    assert run_play(lambda req: httpx.Response(500)) is False


def make_client(tmp_path, player_status=200, **over):
    calls = []

    def handler(req):
        calls.append(req)
        if req.url.host == "plex.tv":
            return httpx.Response(200, json=RESOURCES)
        return httpx.Response(player_status)

    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", sync_on_start=False, login="none", **over)
    return TestClient(create_app(cfg, transport=httpx.MockTransport(handler))), cfg, calls


def test_players_and_play_endpoints(tmp_path):
    client, cfg, calls = make_client(tmp_path)
    with client:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as c:
            db.set_meta(c, "plex_token", "USER-TOKEN")
        assert client.get("/api/players").json() == [{"id": "tv1", "name": "Living Room TV", "product": "Plex for Android (TV)"}]
        client.get("/api/players")
        assert sum(r.url.host == "plex.tv" for r in calls) == 1            # cached

        ok = client.post("/api/play", json={"player_id": "tv1", "server_id": "m1", "rating_key": "11"})
        assert ok.status_code == 204
        assert calls[-1].url.path == "/player/playback/playMedia"
        assert "token=SECRET" in calls[-1].url.query.decode()

        n = len(calls)
        bad = client.post("/api/play", json={"player_id": "tv1", "server_id": "m1", "rating_key": "999"})
        assert bad.status_code == 404 and len(calls) == n                   # not an open proxy
        nope = client.post("/api/play", json={"player_id": "nope", "server_id": "m1", "rating_key": "11"})
        assert nope.status_code == 404


def test_play_reports_502_when_player_unreachable(tmp_path):
    client, cfg, _ = make_client(tmp_path, player_status=500)
    with client:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as c:
            db.set_meta(c, "plex_token", "USER-TOKEN")
        r = client.post("/api/play", json={"player_id": "tv1", "server_id": "m1", "rating_key": "11"})
        assert r.status_code == 502 and "Living Room TV" in r.json()["detail"]


def test_players_empty_for_jellyfin(tmp_path):
    client, cfg, calls = make_client(tmp_path, media_server="jellyfin")
    with client:
        assert client.get("/api/players").json() == []
        assert not calls
