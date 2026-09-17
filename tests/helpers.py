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


SONOS_UID = "RINCON_38420B35243E01400"
SONOS_HOST = "192.168.1.8"


def register_sonos(
    hass,
    entity_id: str = SONOS,
    uid: str = SONOS_UID,
    host: str | None = SONOS_HOST,
) -> None:
    """Register ``entity_id`` the way HA's native Sonos integration does."""
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    url = f"http://{host}:1400/support/review" if host else None
    dr.async_get(hass).add(f"device-{uid}", {("sonos", uid)}, url)
    er.async_get(hass).add(entity_id, "sonos", uid, f"device-{uid}")


def register_music_assistant(hass, entity_id: str, unique_id: str) -> None:
    """Register a Music Assistant player entity.

    Its unique_id is the Sonos player id for MA's own Sonos provider, or the
    wrapped entity_id for MA's Home Assistant player provider.
    """
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    device_id = f"ma-{unique_id}"
    dr.async_get(hass).add(device_id, {("music_assistant", unique_id)})
    er.async_get(hass).add(entity_id, "music_assistant", unique_id, device_id)


class FakeClipSocket:
    """A Sonos speaker's websocket, as far as the clip channel can tell.

    Answers the handshake the way a real Roam does (captured live), then
    lists the clips it already has. ``push`` delivers an event; ``hang_up``
    closes the connection from the speaker's side.
    """

    def __init__(
        self,
        host: str = SONOS_HOST,
        player_id: str = SONOS_UID,
        existing: tuple[str, ...] = ("stale-clip",),
        capabilities: tuple[str, ...] = ("PLAYBACK", "AUDIO_CLIP"),
        advertised_host: str | None = None,
    ) -> None:
        self.host = host
        # Where the household says this player lives; normally the same host.
        self.advertised_host = advertised_host or host
        self.player_id = player_id
        self.existing = existing
        self.capabilities = list(capabilities)
        self.sent: list[list[dict]] = []
        self.closed = False
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, payload):
        self.sent.append(payload)
        command = payload[0]
        if not command:
            await self._inbox.put([{"householdId": "Sonos_H", "success": False}, {}])
        elif command.get("command") == "getGroups":
            player = {
                "id": self.player_id,
                "websocketUrl": f"wss://{self.advertised_host}:1443/websocket/api",
                "capabilities": self.capabilities,
            }
            await self._inbox.put([{"success": True}, {"players": [player]}])
        elif command.get("command") == "subscribe":
            await self._inbox.put([{"success": True, "response": "none"}, {}])
            self.push_status(*((clip, "DONE") for clip in self.existing))

    async def receive(self):
        return await self._inbox.get()

    async def close(self):
        self.closed = True
        self._inbox.put_nowait(None)

    def push_status(self, *clips: tuple[str, str]) -> None:
        body = {
            "_objectType": "audioClipStatus",
            "audioClips": [
                {"_objectType": "audioClip", "id": clip_id, "status": status}
                for clip_id, status in clips
            ],
        }
        self._inbox.put_nowait([{"namespace": "audioClip:1", "type": "x"}, body])

    def hang_up(self) -> None:
        self._inbox.put_nowait(None)


def use_fake_speaker(monkeypatch, socket: FakeClipSocket | Exception):
    """Route the Sonos verifier's connection to ``socket`` (or raise it)."""
    from intercom.verifiers import sonos

    async def open_socket(hass, host):
        if isinstance(socket, Exception):
            raise socket
        assert host == socket.host
        return socket

    monkeypatch.setattr(sonos, "open_clip_socket", open_socket)


def speaks_on_sonos(socket: FakeClipSocket, *statuses: str, clip: str = "our-clip"):
    """A tts.speak that plays a Sonos audio clip: no entity state change at all."""

    async def speak(hass_, data):
        async def play():
            for status in statuses:
                await asyncio.sleep(0.02)
                socket.push_status((clip, status))

        asyncio.ensure_future(play())

    return speak
