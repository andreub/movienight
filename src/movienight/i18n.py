"""The UI language on the server side: which language a request wants, and TMDB text in that language.

Labels the server builds (reasons, moods, eras, errors...) go out as stable keys plus params and the web UI words
them with its catalogues (static/i18n/). This module covers the rest: TMDB data (film titles, overviews, taglines,
collection names) in the UI language, and the server-rendered pages (login).

The language is the UI's own choice (localStorage `movienight.lang` on the device), never the browser's or the
system's locale where the app has said otherwise: the web UI sends it on every API call as the `X-MovieNight-Lang`
header and keeps it in the `movienight_lang` cookie for the server-rendered pages (login). Only a page asked for
before the UI ever ran (first sign-in) falls back to Accept-Language. `es*` gives es-ES, anything else English.

TMDB translations are stored inside the cached TMDB rows themselves, under `i18n.<lang>` (details: title, overview,
tagline; collections: name and part titles; person credits: credit titles), so one row holds every language and a
refetch never overwrites another language's text. `Texts` merges them into per-language lookups, cached on the
engine state (rebuilt after every sync, like the state).
"""
from __future__ import annotations

import json
import re
import sqlite3

DEFAULT = "en"
EXTRA = ("es-ES",)          # languages fetched from TMDB besides the default (English) text
LANGS = (DEFAULT, *EXTRA)
COOKIE = "movienight_lang"
HEADER = "x-movienight-lang"


def normalize(value: str | None) -> str | None:
    v = (value or "").strip()
    if not v:
        return None
    if re.match(r"^es\b", v, re.I):
        return "es-ES"
    if re.match(r"^en\b", v, re.I):
        return "en"
    return None


def from_header(header: str | None) -> str:
    """The first Accept-Language entry that is Spanish or English decides; none, English."""
    for part in (header or "").split(","):
        lang = normalize(part.split(";")[0])
        if lang:
            return lang
    return DEFAULT


def request_lang(request) -> str:
    if request is None:
        return DEFAULT
    return (normalize(request.headers.get(HEADER)) or normalize(request.cookies.get(COOKIE))
            or from_header(request.headers.get("accept-language")))


# --------------------------------------------------------------------------- TMDB translations

def pick_translation(raw: dict | None, lang: str) -> dict:
    """{title, overview, tagline} of `lang` from TMDB's `translations` (append_to_response); the exact region
    first (es-ES), else any entry of the language (es-MX...). Empty strings are left out (they mean "none")."""
    items = ((raw or {}).get("translations") or {}).get("translations") or []
    lc, _, region = lang.partition("-")
    same = [t for t in items if t.get("iso_639_1") == lc]
    best = next((t for t in same if t.get("iso_3166_1") == region), None) or (same[0] if same else None)
    data = (best or {}).get("data") or {}
    return {k: data[k].strip() for k in ("title", "overview", "tagline") if isinstance(data.get(k), str) and data[k].strip()}


def details_i18n(raw: dict | None) -> dict:
    return {lang: pick_translation(raw, lang) for lang in EXTRA}


class Texts:
    """TMDB text in one language: titles by TMDB id, details (overview, tagline) and collection names.
    English, or anything TMDB has no translation for, falls back to the text given."""

    def __init__(self, lang: str, titles: dict | None = None, details: dict | None = None,
                 collections: dict | None = None):
        self.lang = lang
        self.titles = titles or {}
        self.details = details or {}
        self.collections = collections or {}

    @property
    def localized(self) -> bool:
        return self.lang != DEFAULT

    def title(self, tmdb_id, fallback):
        if not self.localized or not tmdb_id:
            return fallback
        return self.titles.get(tmdb_id) or fallback

    def detail(self, tmdb_id, key: str, fallback):
        if not self.localized or not tmdb_id:
            return fallback
        return (self.details.get(tmdb_id) or {}).get(key) or fallback

    def collection(self, cid, fallback):
        if not self.localized or not cid:
            return fallback
        return self.collections.get(cid) or fallback


ENGLISH = Texts(DEFAULT)


def load_texts(conn: sqlite3.Connection, lang: str) -> Texts:
    if lang == DEFAULT or lang not in EXTRA:
        return ENGLISH
    titles: dict[int, str] = {}
    details: dict[int, dict] = {}
    collections: dict[int, str] = {}
    path = f"$.i18n.\"{lang}\""
    # least specific first, so a film's own details win over the titles listed in a collection or a filmography
    for (js,) in conn.execute(f"SELECT json_extract(json, '{path}') FROM tmdb_person_credits"
                              f" WHERE json_extract(json, '{path}') IS NOT NULL"):
        for k, v in (json.loads(js).get("titles") or {}).items():
            if v:
                titles[int(k)] = v
    for cid, js in conn.execute(f"SELECT collection_id, json_extract(json, '{path}') FROM tmdb_collections"
                                f" WHERE json_extract(json, '{path}') IS NOT NULL"):
        d = json.loads(js)
        for k, v in (d.get("titles") or {}).items():
            if v:
                titles[int(k)] = v
        if d.get("name"):
            collections[cid] = d["name"]
    for tid, js in conn.execute(f"SELECT tmdb_id, json_extract(json, '{path}') FROM tmdb_details"
                                f" WHERE json_extract(json, '{path}') IS NOT NULL"):
        d = json.loads(js)
        if d:
            details[tid] = d
            if d.get("title"):
                titles[tid] = d["title"]
    return Texts(lang, titles, details, collections)


def texts(st, conn_factory, lang: str) -> Texts:
    """`load_texts`, cached on the engine state `st` (dropped with it after a sync)."""
    if lang == DEFAULT or lang not in EXTRA:
        return ENGLISH
    key = f"i18n:{lang}"
    if key not in st.cache:
        conn = conn_factory()
        try:
            st.cache[key] = load_texts(conn, lang)
        finally:
            conn.close()
    return st.cache[key]


# --------------------------------------------------------------------------- server-rendered pages

PAGES = {
    "en": {
        "signin": "Sign in",
        "signin.plex": "Sign in with Plex",
        "signin.tv": "Sign in with a code (TV)",
        "signin.with": "Sign in with {name}",
        "signin.text": "Sign in to pick tonight's film.",
        "notConfigured": "Login not configured",
        "notConfigured.oidc": "Set MOVIENIGHT_OIDC_ISSUER and MOVIENIGHT_OIDC_CLIENT_ID (or MOVIENIGHT_LOGIN=none) and restart.",
        "notConfigured.client": "Set MOVIENIGHT_OIDC_CLIENT_ID and restart.",
        "unavailable": "Sign-in unavailable",
        "unavailable.provider": "The login provider could not be reached. Try again in a moment.",
        "unavailable.plex": "plex.tv could not be reached. Try again in a moment.",
        "failed": "Could not sign you in",
        "failed.provider": "Something went wrong talking to the login provider.",
        "failed.plex": "Something went wrong talking to plex.tv.",
        "signedout": "Signed out",
        "signedout.text": "See you at the next movie night.",
        "code.title": "Sign in with a code",
        "code.go": "Go to {link} on your phone and enter this code",
        "code.getting": "Getting a code…",
        "code.cantGet": "Could not get a code.",
        "code.unreachable": "Could not reach MovieNight.",
        "code.waiting": "Waiting for the code to be entered…",
        "code.signedIn": "Signed in.",
        "code.expired": "This code expired.",
        "code.failed": "Could not sign you in.",
        "code.again": "Get a new code",
        "code.other": "Other ways to sign in",
        "plexOff": "Plex sign-in is off",
    },
    "es-ES": {
        "signin": "Iniciar sesión",
        "signin.plex": "Entrar con Plex",
        "signin.tv": "Entrar con un código (TV)",
        "signin.with": "Entrar con {name}",
        "signin.text": "Inicia sesión para elegir la peli de esta noche.",
        "notConfigured": "Inicio de sesión sin configurar",
        "notConfigured.oidc": "Define MOVIENIGHT_OIDC_ISSUER y MOVIENIGHT_OIDC_CLIENT_ID (o MOVIENIGHT_LOGIN=none) y reinicia.",
        "notConfigured.client": "Define MOVIENIGHT_OIDC_CLIENT_ID y reinicia.",
        "unavailable": "No se puede iniciar sesión",
        "unavailable.provider": "No se ha podido contactar con el proveedor de inicio de sesión. Prueba de nuevo en un momento.",
        "unavailable.plex": "No se ha podido contactar con plex.tv. Prueba de nuevo en un momento.",
        "failed": "No se ha podido iniciar sesión",
        "failed.provider": "Algo ha fallado al hablar con el proveedor de inicio de sesión.",
        "failed.plex": "Algo ha fallado al hablar con plex.tv.",
        "signedout": "Sesión cerrada",
        "signedout.text": "Nos vemos en la próxima noche de cine.",
        "code.title": "Entra con un código",
        "code.go": "Ve a {link} en el móvil e introduce este código",
        "code.getting": "Pidiendo un código…",
        "code.cantGet": "No se ha podido obtener un código.",
        "code.unreachable": "No se ha podido contactar con MovieNight.",
        "code.waiting": "Esperando a que introduzcas el código…",
        "code.signedIn": "Sesión iniciada.",
        "code.expired": "Este código ha caducado.",
        "code.failed": "No se ha podido iniciar sesión.",
        "code.again": "Pedir otro código",
        "code.other": "Otras formas de entrar",
        "plexOff": "El inicio de sesión con Plex está desactivado",
    },
}

# auth.LoginError messages (English, written by auth.py) in the other languages: shown on the login pages.
LOGIN_ERRORS = {
    "es-ES": {
        "This login expired. Try again.": "Este inicio de sesión ha caducado. Prueba de nuevo.",
        "The login provider's issuer does not match the configuration":
            "El emisor del proveedor de inicio de sesión no coincide con la configuración",
        "The login provider signed with an unknown key": "El proveedor de inicio de sesión ha firmado con una clave desconocida",
        "The login provider rejected the sign-in": "El proveedor de inicio de sesión ha rechazado el acceso",
        "The login provider sent no identity": "El proveedor de inicio de sesión no ha enviado ninguna identidad",
        "The identity sent by the login provider is not valid": "La identidad enviada por el proveedor no es válida",
        "This login does not match the one that was started": "Este inicio de sesión no coincide con el que se empezó",
        "This account is not allowed to use MovieNight": "Esta cuenta no tiene permiso para usar MovieNight",
        "Plex did not confirm the sign-in. Try again.": "Plex no ha confirmado el inicio de sesión. Prueba de nuevo.",
        "Plex did not say which account signed in. Try again.": "Plex no ha dicho qué cuenta ha entrado. Prueba de nuevo.",
        "Too many sign-in codes right now. Wait a minute and try again.":
            "Hay demasiados códigos de acceso ahora mismo. Espera un minuto y prueba de nuevo.",
        "This code expired. Get a new one.": "Este código ha caducado. Pide otro.",
        "This code belongs to another sign-in": "Este código es de otro inicio de sesión",
        "This MovieNight belongs to another Plex account": "Este MovieNight pertenece a otra cuenta de Plex",
    },
}


def page_text(lang: str, key: str, **params) -> str:
    s = PAGES.get(lang, PAGES[DEFAULT]).get(key) or PAGES[DEFAULT][key]
    return s.format(**params) if params else s


def login_error(lang: str, message: str) -> str:
    return LOGIN_ERRORS.get(lang, {}).get(message, message)
