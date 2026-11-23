"""Address lookup through an operator-configured geocoding service."""
from __future__ import annotations

import hashlib
import os
import time
import urllib.parse

import httpx

_cache: dict[str, tuple[float, tuple[float, float]]] = {}


async def geocode_address(address: str) -> tuple[float, float] | None:
    address = " ".join(address.split())
    endpoint = os.getenv("GEOCODER_URL", "").strip()
    if not endpoint or not address:
        return None
    cache_key = hashlib.sha256(address.casefold().encode()).hexdigest()
    cached = _cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    if len(_cache) > 2000:
        _cache.clear()
    if "{address}" in endpoint:
        url = endpoint.replace("{address}", urllib.parse.quote(address, safe=""))
        params = None
    else:
        url = endpoint
        params = {"q": address, "format": "json", "limit": 1}
    headers = {"User-Agent": os.getenv("GEOCODER_USER_AGENT", "FreshProduceDevelopment/1.0")}
    async with httpx.AsyncClient(timeout=8, headers=headers) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    if isinstance(payload, dict):
        results = payload.get("results", payload.get("features", [payload]))
    else:
        results = payload
    if not results:
        return None
    first = results[0]
    if "geometry" in first:
        coordinates = first["geometry"].get("coordinates", [])
        lon, lat = coordinates[:2]
    else:
        lat, lon = first.get("lat", first.get("latitude")), first.get("lon", first.get("longitude"))
    if lat is None or lon is None:
        return None
    point = (float(lat), float(lon))
    if not (-90 <= point[0] <= 90 and -180 <= point[1] <= 180):
        return None
    _cache[cache_key] = (time.monotonic() + 86_400, point)
    return point
