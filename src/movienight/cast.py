"""Google Cast (Chromecast, Android TV with built-in Cast): device discovery and starting a Plex movie.

Plex Companion ("Advertise as player") is gone from the new Plex apps, but their Android TV / Chromecast
receivers still answer Cast, which is what the Plex phone app uses to cast. All blocking pychromecast /
plexapi work runs in a thread (`asyncio.to_thread`) with timeouts. Tokens are never logged."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from urllib.parse import urlparse
from uuid import UUID

# Imported at module level on purpose: importing these lazily from worker threads (discovery runs probes
# and mDNS concurrently) deadlocks on the Python import lock (_DeadlockError).
import zeroconf
from plexapi.server import PlexServer
from pychromecast import Chromecast
from pychromecast.controllers.plex import PlexController
from pychromecast.dial import get_device_info
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo, HostServiceInfo

log = logging.getLogger("movienight.cast")

PROBE_TIMEOUT = 5.0
MDNS_SECONDS = 2.0
CONNECT_TIMEOUT = 8.0
PLAY_TIMEOUT = 20.0
QUIT_WAIT = 8.0
POLL = 0.5
PLEX_APP_ID = "9AC194DC"
CAST_PORT = 8009


# --------------------------------------------------------------------------- pure helpers

def parse_hosts(raw: str) -> list[str]:
    return [h.strip() for h in (raw or "").split(",") if h.strip()]


def plex_hint_hosts(players: list[dict]) -> list[str]:
    """LAN hosts of the Plex players on the account (a TV listed by plex.tv is a good Cast candidate)."""
    out: list[str] = []
    for p in players:
        for c in p.get("connections") or []:
            host = urlparse(c.get("uri") or "").hostname
            if host and c.get("local", True) and not c.get("relay") and host not in out:
                out.append(host)
    return out


def candidate_hosts(env_hosts: str, players: list[dict]) -> list[str]:
    seen: list[str] = []
    for h in parse_hosts(env_hosts) + plex_hint_hosts(players):
        if h not in seen:
            seen.append(h)
    return seen


# --------------------------------------------------------------------------- discovery (blocking)

def _device_info(host: str):
    return get_device_info(host, timeout=PROBE_TIMEOUT)


def _probe(host: str) -> dict | None:
    """Read a device's own description over HTTP (:8008 / :8443 eureka_info); works without mDNS."""
    try:
        info = _device_info(host)
    except Exception as e:  # noqa: BLE001 - any failure just means "not a cast device"
        log.debug("cast probe %s failed: %s", host, type(e).__name__)
        return None
    if info is None or info.uuid is None or getattr(info, "cast_type", "cast") == "group":
        return None
    return {"id": str(info.uuid), "name": info.friendly_name, "product": info.model_name or "Chromecast",
            "host": host, "port": CAST_PORT}


def _mdns() -> list[dict]:
    """Brief mDNS browse (works with host networking on Linux; empty behind Docker Desktop)."""
    try:
        zc = zeroconf.Zeroconf(ip_version=zeroconf.IPVersion.V4Only)
        browser = CastBrowser(SimpleCastListener(lambda *_: None), zc)
        try:
            browser.start_discovery()
            time.sleep(MDNS_SECONDS)
            found = list(browser.devices.values())
        finally:
            browser.stop_discovery()
            zc.close()
    except Exception as e:  # noqa: BLE001
        log.debug("cast mDNS unavailable: %s", type(e).__name__)
        return []
    return [{"id": str(d.uuid), "name": d.friendly_name or d.host, "product": d.model_name or "Chromecast",
             "host": d.host, "port": d.port or CAST_PORT}
            for d in found if d.cast_type != "group" and d.host]


def _discover_blocking(hosts: list[str]) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(hosts) + 1) as pool:
        mdns = pool.submit(_mdns)
        probed = list(pool.map(_probe, hosts))
        merged = {d["id"]: d for d in probed if d}
        for d in mdns.result():
            merged.setdefault(d["id"], d)       # a directly probed device wins
    return sorted(merged.values(), key=lambda d: d["name"].lower())


async def discover(hosts: list[str]) -> list[dict]:
    """Cast devices [{id (uuid), name, product, host, port}] from direct probes of `hosts` plus mDNS."""
    return await asyncio.to_thread(_discover_blocking, hosts)


# --------------------------------------------------------------------------- playing (blocking)

class _Stage(Exception):
    """A play failure tagged with the stage it happened in (only the stage and exception type are logged)."""
    def __init__(self, stage: str, cause: Exception):
        super().__init__(stage)
        self.stage, self.cause_type = stage, type(cause).__name__


# One long-lived connection per device. Closing it (Chromecast.disconnect) makes pychromecast send CLOSE on the
# virtual connection to the receiver app, and the Plex receiver stops playing when its only sender goes away
# (observed on an Android TV: playback ran fine while connected and the TV dropped back to its home app the moment
# we disconnected). So the connection is kept open and reused; it is only closed on app shutdown.
_registry_lock = threading.Lock()
_conns: dict[str, dict] = {}          # device uuid -> {"cc", "pc", "lock"}


def _alive(cc) -> bool:
    sc = getattr(cc, "socket_client", None)
    if sc is None:
        return True
    return bool(sc.is_connected) and not sc.stop.is_set()


def _close(cc) -> None:
    try:
        cc.disconnect(timeout=3)
    except Exception:  # noqa: BLE001
        pass


def _entry(device: dict) -> dict:
    with _registry_lock:
        return _conns.setdefault(device["id"], {"cc": None, "pc": None, "lock": threading.Lock()})


def _connection(device: dict, entry: dict):
    """(cc, plex controller) for the device, connecting if there is none or the old one died. Caller holds
    entry["lock"]."""
    cc = entry["cc"]
    if cc is not None and not _alive(cc):
        log.info("cast connection to %s lost, reconnecting", device.get("name"))
        _close(cc)
        entry["cc"] = entry["pc"] = cc = None
    if cc is None:
        host, port = device["host"], device.get("port") or CAST_PORT
        info = CastInfo({HostServiceInfo(host, port)}, UUID(device["id"]), device.get("product"),
                        device.get("name"), host, port, "cast", None)
        cc = Chromecast(info, tries=1, timeout=CONNECT_TIMEOUT)
        try:
            cc.wait(timeout=CONNECT_TIMEOUT)
        except Exception:
            _close(cc)
            raise
        pc = PlexController()
        cc.register_handler(pc)
        entry["cc"], entry["pc"] = cc, pc
    return entry["cc"], entry["pc"]


def close_all() -> None:
    """Close every cached cast connection (app shutdown)."""
    with _registry_lock:
        entries = list(_conns.values())
        _conns.clear()
    for e in entries:
        if e["cc"] is not None:
            _close(e["cc"])


def _play_blocking(device: dict, server_url: str, server_token: str, rating_key: str) -> bool:
    # PlexController copies address/port/protocol/token from the plexapi server's base URL, so that
    # URL is what the TV will use: pass a connection that is reachable (local first, then remote).
    stage = "plex server lookup"
    try:
        server = PlexServer(server_url, server_token, timeout=PROBE_TIMEOUT)
        item = server.fetchItem(int(rating_key))
    except Exception as e:  # noqa: BLE001
        raise _Stage(stage, e) from None
    entry = _entry(device)
    with entry["lock"]:                      # two plays on one TV never interleave
        try:
            stage = "cast connect"
            cc, pc = _connection(device, entry)
            stage = "cast relaunch"
            # A Plex receiver left running by an earlier cast ignores/drops a new LOAD (pychromecast skips the
            # launch when the app is already up), so close it and start from a clean receiver.
            if cc.app_id == PLEX_APP_ID:
                cc.quit_app()
                deadline = time.monotonic() + QUIT_WAIT
                while cc.app_id == PLEX_APP_ID and time.monotonic() < deadline:
                    time.sleep(POLL)
            stage = "cast play"
            pc.play_media(item)
            # play_media_event only means "LOAD sent": confirm via the media status the receiver publishes.
            deadline = time.monotonic() + PLAY_TIMEOUT
            state = None
            while time.monotonic() < deadline:
                state = getattr(cc.media_controller.status, "player_state", None)
                if state in ("PLAYING", "BUFFERING"):
                    return True
                time.sleep(POLL)
            log.warning("cast play: receiver did not start playback (state=%s)", state)
            return False
        except Exception as e:  # noqa: BLE001
            if entry["cc"] is not None:      # unknown state: next click starts from a fresh connection
                _close(entry["cc"])
                entry["cc"] = entry["pc"] = None
            raise _Stage(stage, e) from None


async def play(device: dict, server_url: str, server_token: str, rating_key: str) -> bool:
    """Launch the Plex receiver on the device and load the movie from `server_url` (a connection the TV can
    reach). False on any failure."""
    try:
        return bool(await asyncio.to_thread(_play_blocking, device, server_url, server_token, rating_key))
    except _Stage as e:  # never include URLs/tokens: only the stage and the exception type
        log.warning("cast play failed on %s at %s: %s", device.get("name"), e.stage, e.cause_type)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("cast play failed on %s: %s", device.get("name"), type(e).__name__)
        return False
