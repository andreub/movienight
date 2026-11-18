"""Play on a TV via Google Cast: host list, device merge, LAN reachable server address, endpoints. No network."""
import asyncio
from types import SimpleNamespace
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from conftest import seed
from movienight import cast, db, plex
from movienight.app import create_app
from movienight.config import Config

TV_UUID = "11111111-2222-3333-4444-555555555555"
RESOURCES = [
    {"name": "Home", "clientIdentifier": "m1", "provides": "server", "accessToken": "SRV-TOKEN", "connections": [
        {"uri": "https://192-168-1-10.abc.plex.direct:32400", "local": True, "relay": False,
         "address": "192.168.1.10", "port": 32400, "protocol": "https"},
        {"uri": "https://80-1-2-3.abc.plex.direct:32400", "local": False, "relay": False,
         "address": "80.1.2.3", "port": 32400, "protocol": "https"},
        {"uri": "https://relay.example", "local": False, "relay": True, "address": "r", "port": 443}]},
    {"name": "Old name", "clientIdentifier": "tv1", "provides": "client,player", "product": "Plex for Android (TV)",
     "connections": [{"uri": "https://relay.example/tv", "local": False, "relay": True},
                     {"uri": "http://192.168.1.50:32500", "local": True, "relay": False}]},
]
DEVICE = {"id": TV_UUID, "name": "Living Room TV", "product": "Chromecast", "host": "192.168.1.50", "port": 8009}


# --------------------------------------------------------------------------- pure pieces

def test_candidate_hosts_merge_env_and_plex_hints_deduped():
    hints = plex.parse_players(RESOURCES)
    assert cast.plex_hint_hosts(hints) == ["192.168.1.50"]                       # relay dropped
    assert cast.candidate_hosts("10.0.0.7, 192.168.1.50 ,", hints) == ["10.0.0.7", "192.168.1.50"]
    assert cast.candidate_hosts("", []) == []


def test_discover_takes_name_from_device_and_merges_by_uuid(monkeypatch):
    info = SimpleNamespace(uuid=UUID(TV_UUID), friendly_name="Whatever The Device Says", model_name="Chromecast",
                           cast_type="cast")
    monkeypatch.setattr(cast, "_device_info", lambda host: info if host == "192.168.1.50" else None)
    monkeypatch.setattr(cast, "_mdns", lambda: [
        {"id": TV_UUID, "name": "mDNS dup", "product": "x", "host": "192.168.1.50", "port": 8009},
        {"id": "99999999-0000-0000-0000-000000000000", "name": "Kitchen", "product": "Nest", "host": "192.168.1.60",
         "port": 8009}])
    found = asyncio.run(cast.discover(["192.168.1.50", "192.168.1.99"]))
    assert [(d["name"], d["id"]) for d in found] == [("Kitchen", "99999999-0000-0000-0000-000000000000"),
                                                     ("Whatever The Device Says", TV_UUID)]
    assert found[1]["product"] == "Chromecast" and found[1]["host"] == "192.168.1.50"


def test_discover_ignores_groups_and_unreachable(monkeypatch):
    group = SimpleNamespace(uuid=UUID(TV_UUID), friendly_name="Group", model_name="Group", cast_type="group")
    monkeypatch.setattr(cast, "_device_info", lambda host: group)
    monkeypatch.setattr(cast, "_mdns", lambda: [])
    assert asyncio.run(cast.discover(["1.2.3.4"])) == []


# --------------------------------------------------------------------------- endpoints

LOCAL_HOST = "192-168-1-10.abc.plex.direct"
PUBLIC_HOST = "80-1-2-3.abc.plex.direct"


def make_client(tmp_path, monkeypatch, play_ok=True, reachable=(LOCAL_HOST,), **over):
    calls, plays, discovered = [], [], []

    def handler(req):
        calls.append(req)
        if req.url.host == "plex.tv":
            return httpx.Response(200, json=RESOURCES)
        if req.url.path == "/identity" and req.url.host in reachable:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    async def fake_discover(hosts):
        discovered.append(hosts)
        return [dict(DEVICE)]

    async def fake_play(device, url, token, rating_key):
        plays.append((device["id"], url, token, rating_key))
        return play_ok

    monkeypatch.setattr(cast, "discover", fake_discover)
    monkeypatch.setattr(cast, "play", fake_play)
    cfg = Config(data_dir=tmp_path / "d", letterboxd_dir=tmp_path / "nolb", sync_on_start=False, login="none", **over)
    return TestClient(create_app(cfg, transport=httpx.MockTransport(handler))), cfg, calls, plays, discovered


def test_players_and_play_endpoints(tmp_path, monkeypatch):
    client, cfg, calls, plays, discovered = make_client(tmp_path, monkeypatch, cast_hosts="10.0.0.7")
    with client:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as c:
            db.set_meta(c, "plex_token", "USER-TOKEN")
        assert client.get("/api/players").json() == [{"id": TV_UUID, "name": "Living Room TV", "product": "Chromecast"}]
        client.get("/api/players")
        assert discovered == [["10.0.0.7", "192.168.1.50"]]                  # env + plex hint, cached 60s

        ok = client.post("/api/play", json={"player_id": TV_UUID, "server_id": "m1", "rating_key": "11"})
        assert ok.status_code == 204
        assert plays == [(TV_UUID, f"https://{LOCAL_HOST}:32400", "SRV-TOKEN", "11")]    # reachable local preferred

        bad = client.post("/api/play", json={"player_id": TV_UUID, "server_id": "m1", "rating_key": "999"})
        assert bad.status_code == 404 and len(plays) == 1                    # not an open proxy
        nope = client.post("/api/play", json={"player_id": "nope", "server_id": "m1", "rating_key": "11"})
        assert nope.status_code == 404


def test_play_reports_502_when_cast_fails(tmp_path, monkeypatch):
    client, cfg, *_ = make_client(tmp_path, monkeypatch, play_ok=False)
    with client:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as c:
            db.set_meta(c, "plex_token", "USER-TOKEN")
        r = client.post("/api/play", json={"player_id": TV_UUID, "server_id": "m1", "rating_key": "11"})
        assert r.status_code == 502 and "Living Room TV" in r.json()["detail"]


def test_players_empty_and_play_400_for_jellyfin(tmp_path, monkeypatch):
    client, cfg, calls, plays, discovered = make_client(tmp_path, monkeypatch, media_server="jellyfin")
    with client:
        assert client.get("/api/players").json() == []
        assert client.post("/api/play", json={"player_id": TV_UUID, "server_id": "m1", "rating_key": "11"}).status_code == 400
        assert not calls and not discovered and not plays


def _play_url(tmp_path, monkeypatch, reachable):
    client, cfg, calls, plays, _ = make_client(tmp_path, monkeypatch, reachable=reachable)
    with client:
        seed(cfg.db_path)
        with db.session(cfg.db_path) as c:
            db.set_meta(c, "plex_token", "USER-TOKEN")
        r = client.post("/api/play", json={"player_id": TV_UUID, "server_id": "m1", "rating_key": "11"})
        assert r.status_code == 204
    return plays[0][1]


def test_shared_server_with_unreachable_local_uses_public_plex_direct(tmp_path, monkeypatch):
    assert _play_url(tmp_path, monkeypatch, (PUBLIC_HOST,)) == f"https://{PUBLIC_HOST}:32400"


def test_remote_direct_preferred_over_relay(tmp_path, monkeypatch):
    assert _play_url(tmp_path, monkeypatch, ("relay.example", PUBLIC_HOST)) == f"https://{PUBLIC_HOST}:32400"


def test_relay_used_when_only_it_answers(tmp_path, monkeypatch):
    assert _play_url(tmp_path, monkeypatch, ("relay.example",)) == "https://relay.example"


def test_no_reachable_connection_falls_back_to_stored_base_url(tmp_path, monkeypatch):
    url = _play_url(tmp_path, monkeypatch, ())
    with db.session(tmp_path / "d" / "movienight.db") as c:
        stored = c.execute("SELECT base_url FROM media_servers WHERE id='m1'").fetchone()["base_url"]
    assert url == stored
