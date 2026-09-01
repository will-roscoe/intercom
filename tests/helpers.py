"""Shared fixtures for driving a broadcast against the stubbed Home Assistant."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
from intercom.broadcast import BroadcastRequest

# supported_features bits: PLAY_MEDIA | VOLUME_SET | VOLUME_MUTE
FULL_FEATURES = 512 | 4 | 8

ENGINE = "tts.piper"
SONOS = "media_player.sonos"
STUDY = "media_player.study"


def make_request(**overrides: Any) -> BroadcastRequest:
    """A broadcast request with fast timeouts, overridable per test."""
    base: dict[str, Any] = {
        "message": "Dinner is ready",
        "title": "Broadcast",
        "engine": ENGINE,
        "players": (SONOS,),
        "notify_targets": (),
        "volume": None,
        "restore_volume": True,
        "unmute": True,
        "verify": True,
        "critical": False,
        "max_attempts": 2,
        "wait_timeout": 5.0,
        "start_timeout": 0.6,
    }
    base.update(overrides)
    return BroadcastRequest(**base)


def new_hass(player_state: str = "idle", attributes: dict | None = None):
    """A hass with a TTS engine, two speakers and working volume services."""
    hass = HomeAssistant()
    hass.states.set(ENGINE, "2026-09-01T00:00:00+00:00")
    for entity_id, name in ((SONOS, "Sonos"), (STUDY, "Study")):
        hass.states.set(
            entity_id,
            player_state,
            {
                "friendly_name": name,
                "supported_features": FULL_FEATURES,
                **(attributes or {}),
            },
        )

    async def volume_set(hass_, data):
        current = hass_.states.get(data["entity_id"])
        hass_.states.set(
            data["entity_id"], current.state, {"volume_level": data["volume_level"]}
        )

    async def volume_mute(hass_, data):
        current = hass_.states.get(data["entity_id"])
        hass_.states.set(
            data["entity_id"],
            current.state,
            {"is_volume_muted": data["is_volume_muted"]},
        )

    hass.services.register("media_player", "volume_set", volume_set)
    hass.services.register("media_player", "volume_mute", volume_mute)
    return hass


def speaks_on(*entity_ids: str, clip: str = "tts-clip"):
    """A tts.speak that really plays, but only on the named players.

    Any other player accepts the command and stays silent — the exact failure
    mode this integration exists to detect.
    """

    async def speak(hass_, data):
        target = data["media_player_entity_id"]
        if target not in entity_ids:
            return None

        async def play():
            await asyncio.sleep(0.02)
            hass_.states.set(
                target, "playing", {"media_content_id": f"{clip}-{target}"}
            )
            await asyncio.sleep(0.05)
            hass_.states.set(target, "idle", {})

        asyncio.ensure_future(play())

    return speak


def always_fails(message: str = "The command to the player failed."):
    """A tts.speak that raises the way Sonos does."""

    async def speak(hass_, data):
        raise RuntimeError(message)

    return speak


def register_speak(hass, behaviour) -> None:
    hass.services.register("tts", "speak", behaviour)


def by_entity(outcomes: list[dict]) -> dict[str, dict]:
    return {outcome["entity_id"]: outcome for outcome in outcomes}
