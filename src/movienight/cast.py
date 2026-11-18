"""Google Cast (Chromecast, Android TV with built-in Cast): device discovery and starting a Plex movie.

Plex Companion ("Advertise as player") is gone from the new Plex apps, but their Android TV / Chromecast
receivers still answer Cast, which is what the Plex phone app uses to cast. All blocking pychromecast /
plexapi work runs in a thread (`asyncio.to_thread`) with timeouts. Tokens are never logged."""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlparse
from uuid import UUID

log = logging.getLogger("movienight.cast")

PROBE_TIMEOUT = 5.0
MDNS_SECONDS = 2.0
CONNECT_TIMEOUT = 8.0
PLAY_TIMEOUT = 15.0
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


def lan_server_url(server: dict) -> str | None:
    """The address a TV on the LAN can reach: a local, non-relay connection as plain http://ip:port
    (not the plex.direct name that resolves to the public IP). Falls back to the connection's uri."""
    conns = [c for c in server.get("connections") or [] if c.get("local") and not c.get("relay")]
    for c in conns:
        if c.get("address") and c.get("port"):
            return f"http://{c['address']}:{c['port']}"
    return conns[0]["uri"].rstrip("/") if conns else None


# --------------------------------------------------------------------------- discovery (blocking)

def _device_info(host: str):
    from pychromecast.dial import get_device_info
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
        import zeroconf
        from pychromecast.discovery import CastBrowser, SimpleCastListener

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

def _play_blocking(device: dict, server_url: str, server_token: str, rating_key: str) -> bool:
    from pychromecast import Chromecast
    from pychromecast.controllers.plex import PlexController
    from pychromecast.models import CastInfo, HostServiceInfo
    from plexapi.server import PlexServer

    # PlexController copies address/port/protocol/token from the plexapi server's base URL, so that
    # URL is what the TV will use: build it with the LAN address, not the stored plex.direct name.
    server = PlexServer(server_url, server_token, timeout=PROBE_TIMEOUT)
    item = server.fetchItem(int(rating_key))
    host, port = device["host"], device.get("port") or CAST_PORT
    info = CastInfo({HostServiceInfo(host, port)}, UUID(device["id"]), device.get("product"),
                    device.get("name"), host, port, "cast", None)
    cc = Chromecast(info, tries=1, timeout=CONNECT_TIMEOUT)
    try:
        cc.wait(timeout=CONNECT_TIMEOUT)
        pc = PlexController()
        cc.register_handler(pc)
        pc.play_media(item)
        return pc.play_media_event.wait(PLAY_TIMEOUT)
    finally:
        try:
            cc.disconnect(timeout=3)
        except Exception:  # noqa: BLE001
            pass


async def play(device: dict, server_url: str, server_token: str, rating_key: str) -> bool:
    """Launch the Plex receiver on the device and load the movie. False on any failure."""
    try:
        return bool(await asyncio.to_thread(_play_blocking, device, server_url, server_token, rating_key))
    except Exception as e:  # noqa: BLE001 - network, timeouts, plexapi errors; never include URLs/tokens
        log.warning("cast play failed on %s: %s", device.get("name"), type(e).__name__)
        return False
