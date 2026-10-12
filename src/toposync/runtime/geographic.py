from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field


DEFAULT_BACKGROUND = {
    "id": "openstreetmap", "name": "OpenStreetMap",
    "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "attribution": '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    "nativeZoom": 19,
}
DEFAULT_GEOCODING = "https://nominatim.openstreetmap.org/search"


class GeographicSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=240)
    language: str = Field(default="pt-BR", pattern=r"^(en|pt-BR)$")


def map_configuration(core: dict) -> dict:
    settings = core.get("geographic_map", {})
    if not isinstance(settings, dict):
        settings = {}
    configured_background = settings.get("background", {})
    if not isinstance(configured_background, dict):
        raise ValueError("Invalid background configuration")
    background = {**DEFAULT_BACKGROUND, **configured_background}
    endpoint = settings.get("geocoding_url", DEFAULT_GEOCODING)
    if not isinstance(endpoint, str) or urlsplit(endpoint).scheme not in {"https", "http"}:
        raise ValueError("Invalid geocoding service")
    if any(not isinstance(background[key], str) or not background[key].strip() for key in ("id", "name", "url", "attribution")):
        raise ValueError("Invalid background configuration")
    if type(background["nativeZoom"]) is not int or not 0 <= background["nativeZoom"] <= 22:
        raise ValueError("Invalid native map zoom")
    if urlsplit(background["url"]).scheme not in {"https", "http"}:
        raise ValueError("Invalid background service")
    attribution = settings.get("geocoding_attribution", DEFAULT_BACKGROUND["attribution"])
    if not isinstance(attribution, str) or not attribution.strip():
        raise ValueError("Invalid geocoding attribution")
    return {"background": background, "geocoding": {"name": settings.get("geocoding_name", "Nominatim"),
            "host": urlsplit(endpoint).netloc, "attribution": attribution}, "geocoding_url": endpoint}


def read_places(payload: object) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("Invalid geocoding response")
    places = []
    for item in payload[:5]:
        if not isinstance(item, dict):
            raise ValueError("Invalid geocoding result")
        latitude, longitude = float(item["lat"]), float(item["lon"])
        if not math.isfinite(latitude) or not math.isfinite(longitude) or abs(latitude) > 85 or abs(longitude) > 180:
            raise ValueError("Invalid geocoding coordinates")
        label = item.get("display_name")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Invalid geocoding label")
        places.append({"id": str(item.get("place_id", len(places))), "label": label[:1000],
                       "latitude": latitude, "longitude": longitude})
    return places


class GeographicSearch:
    """A shared persistent cache and request gate for this installation's processes."""

    def __init__(self, path: Path):
        self.path = path

    def search(self, endpoint: str, query: str, language: str) -> list[dict]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = json.dumps([endpoint, query.casefold(), language], ensure_ascii=False)
        with sqlite3.connect(self.path, timeout=2) as database:
            database.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, result TEXT)")
            database.execute("CREATE TABLE IF NOT EXISTS gate (id INTEGER PRIMARY KEY, next_request REAL)")
            database.execute("BEGIN IMMEDIATE")
            now = time.time()
            cached = database.execute("SELECT result FROM cache WHERE key=? AND expires>?", (key, now)).fetchone()
            if cached:
                return json.loads(cached[0])
            next_request = database.execute("SELECT next_request FROM gate WHERE id=1").fetchone()
            if next_request and next_request[0] > now:
                raise GeographicRateLimit()
            database.execute("INSERT OR REPLACE INTO gate VALUES (1, ?)", (now + 1.1,))
            database.commit()
        parameters = urlencode({"q": query, "format": "jsonv2", "limit": 5, "accept-language": language})
        request = Request(f"{endpoint}{'&' if '?' in endpoint else '?'}{parameters}",
                          headers={"User-Agent": "Toposync/0.9 (https://toposync.com)", "Accept": "application/json"})
        with urlopen(request, timeout=8) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Geocoding response too large")
        places = read_places(json.loads(raw))
        with sqlite3.connect(self.path, timeout=2) as database:
            database.execute("DELETE FROM cache WHERE expires<=?", (time.time(),))
            database.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, time.time() + 86400, json.dumps(places)))
            database.execute("DELETE FROM cache WHERE key NOT IN (SELECT key FROM cache ORDER BY expires DESC LIMIT 100)")
        return places


class GeographicRateLimit(Exception):
    pass
