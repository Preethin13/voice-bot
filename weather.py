"""IP-based location plus current conditions from Open-Meteo."""

from __future__ import annotations

import ipaddress
import json
from typing import Any

import httpx

WEATHER_TOOL = {
    "type": "function",
    "name": "get_current_weather",
    "description": (
        "Get live weather for the user. Call this whenever they ask about the weather, "
        "temperature, or conditions. Do not ask where they are. Omit place to use "
        "their detected location. Only pass place if they named a city or region."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "place": {
                "type": "string",
                "description": "City or region they named. Omit to use detected location.",
            }
        },
        "additionalProperties": False,
    },
}

_WMO = {
    0: "clear",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


def client_ip(headers: Any, host: str | None) -> str | None:
    for key in ("cf-connecting-ip", "x-real-ip"):
        value = headers.get(key)
        if value:
            return value.split(",")[0].strip()
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return host


def _is_public_ip(value: str | None) -> bool:
    if not value:
        return False
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local)


async def _http_json(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(url, headers={"User-Agent": "lumen-weather/1.0"})
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}


async def _locate_ip(ip: str | None) -> dict[str, Any] | None:
    query = ip if _is_public_ip(ip) else ""
    data = await _http_json(f"https://ipwho.is/{query}")
    if not data.get("success", True):
        data = await _http_json("https://ipapi.co/json/")
    lat, lon = data.get("latitude"), data.get("longitude")
    if lat is None or lon is None:
        return None
    city = data.get("city") or data.get("region") or "your area"
    region = data.get("region") or data.get("regionName") or ""
    country = data.get("country_code") or data.get("country") or ""
    label = ", ".join(part for part in (city, region, country) if part)
    return {
        "latitude": float(lat),
        "longitude": float(lon),
        "label": label or city,
        "country_code": str(country).upper()[:2],
    }


async def _geocode(place: str) -> dict[str, Any] | None:
    q = httpx.QueryParams({"name": place, "count": 1, "language": "en"})
    data = await _http_json(f"https://geocoding-api.open-meteo.com/v1/search?{q}")
    results = data.get("results") or []
    if not results:
        return None
    hit = results[0]
    parts = [hit.get("name"), hit.get("admin1"), hit.get("country")]
    return {
        "latitude": float(hit["latitude"]),
        "longitude": float(hit["longitude"]),
        "label": ", ".join(part for part in parts if part),
        "country_code": str(hit.get("country_code") or "").upper()[:2],
    }


async def get_current_weather(*, place: str | None = None, ip: str | None = None) -> str:
    try:
        if place and place.strip():
            loc = await _geocode(place.strip())
            if not loc:
                return json.dumps({"error": f"Could not find {place.strip()}."})
        else:
            loc = await _locate_ip(ip)
            if not loc:
                return json.dumps(
                    {"error": "Could not detect location. Ask for a city name."}
                )

        use_f = loc.get("country_code") in {"US", "BS", "BZ", "KY", "PW"}
        params = httpx.QueryParams(
            {
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
                "temperature_unit": "fahrenheit" if use_f else "celsius",
                "wind_speed_unit": "mph" if use_f else "kmh",
                "precipitation_unit": "inch" if use_f else "mm",
                "timezone": "auto",
            }
        )
        data = await _http_json(f"https://api.open-meteo.com/v1/forecast?{params}")
        current = data.get("current") or {}
        unit = "F" if use_f else "C"
        wind_unit = "mph" if use_f else "km/h"
        code = int(current.get("weather_code") or 0)
        payload = {
            "place": loc["label"],
            "condition": _WMO.get(code, "unknown conditions"),
            "temperature": current.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "unit": unit,
            "humidity_percent": current.get("relative_humidity_2m"),
            "wind": current.get("wind_speed_10m"),
            "wind_unit": wind_unit,
            "precipitation": current.get("precipitation"),
        }
        return json.dumps(payload)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Weather lookup failed: {exc}"})
