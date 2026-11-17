"""Minimal stand-in for homeassistant.components.tts: making speech ahead of time.

Records each request in ``hass.data["tts_made"]`` with how many service calls
had been made when it ran, so tests can see it came before any speaker was
asked to play. A test can make it slow or failing by putting an async callable
in ``hass.data["tts_make"]``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit


def generate_media_source_id(
    hass: Any,
    message: str,
    engine: str | None = None,
    language: str | None = None,
    options: dict | None = None,
    cache: bool | None = None,
) -> str:
    query = {"message": message, "cache": str(cache).lower()}
    return f"media-source://tts/{engine}?{urlencode(query)}"


async def async_get_media_source_audio(
    hass: Any, media_source_id: str
) -> tuple[str, bytes]:
    parts = urlsplit(media_source_id)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    hass.data.setdefault("tts_made", []).append(
        {
            "engine": parts.path.lstrip("/"),
            "message": query["message"],
            "cache": query["cache"] == "true",
            "calls_before": len(hass.services.calls),
        }
    )
    make = hass.data.get("tts_make")
    if make is not None:
        await make()
    return "mp3", b""
