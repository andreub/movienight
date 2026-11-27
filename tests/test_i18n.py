"""UI message catalogues (static/i18n): Spanish covers every English key with the same placeholders, and every
key the frontend asks for exists in English (the fallback language)."""
import json
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "movienight" / "static"
PLURAL_FORMS = {"zero", "one", "two", "few", "many", "other"}


def load(lang):
    return json.loads((STATIC / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


EN = load("en")
ES = load("es-ES")


def forms(value):
    """The strings of an entry: one, or each plural form."""
    if isinstance(value, str):
        return [value]
    assert isinstance(value, dict) and value, value
    assert set(value) <= PLURAL_FORMS and "other" in value, value
    return list(value.values())


def placeholders(value):
    return {p for s in forms(value) for p in re.findall(r"\{(\w+)\}", s)}


def test_catalogues_are_flat_and_not_empty():
    assert len(EN) > 100
    for cat in (EN, ES):
        for key, value in cat.items():
            assert all(s.strip() for s in forms(value)), key


@pytest.mark.parametrize("key", sorted(EN))
def test_spanish_has_every_key_with_the_same_placeholders(key):
    assert key in ES, f"es-ES is missing {key}"
    assert placeholders(ES[key]) == placeholders(EN[key]), key
    assert isinstance(ES[key], str) == isinstance(EN[key], str), f"{key}: plural in one language only"


def test_spanish_has_no_extra_keys():
    assert set(ES) - set(EN) == set()


def used_keys():
    keys = {}
    for path in [*sorted((STATIC / "js").rglob("*.js")), STATIC / "index.html"]:
        text = path.read_text(encoding="utf-8")
        found = re.findall(r"\bt\(\s*'([^']+)'", text) + re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', text)
        for k in found:
            keys.setdefault(k, path.name)
    return keys


def test_scanner_finds_the_frontend_keys():
    keys = used_keys()
    assert "tab.tonight" in keys and "common.undo" in keys and len(keys) > 300


def test_every_key_used_by_the_frontend_exists_in_english():
    missing = {k: f for k, f in used_keys().items() if k not in EN}
    assert not missing, missing


def test_language_switch_and_fallback_exist():
    src = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
    assert "'es-ES'" in src and "localStorage" in src and "documentElement.lang" in src
    assert "lookup(EN, key, vars)" in src  # a key missing from Spanish falls back to English
