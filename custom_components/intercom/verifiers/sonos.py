"""Confirm Sonos announcements from the speaker's own audio-clip status.

Home Assistant's Sonos integration plays announcements (and so ``tts.speak``)
through the speaker's local audio-clip API. The media player entity never
changes state while a clip plays, so the generic state check can only ever say
"no playback detected" -- even for a clip everyone heard.

The speaker does publish what happens to every clip, whoever queued it: a
client subscribed to the ``audioClip:1`` namespace receives ``audioClipStatus``
events, and a clip moves ``ACTIVE`` -> ``DONE`` when it plays or reports
``ERROR`` when the speaker could not fetch it. This verifier opens its own
connection to the speaker, subscribes, and follows the first clip that appears
after it was armed.

The same speaker is reachable from Music Assistant's player entities, which
either carry the Sonos player id or wrap the native entity, so those are
matched too. When the connection cannot be made, or drops, the inherited
entity-state check is used instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
from typing import Any, Protocol
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .state import StateVerifier

_LOGGER = logging.getLogger(__package__)

SONOS_DOMAIN = "sonos"
MUSIC_ASSISTANT_DOMAIN = "music_assistant"
WEBSOCKET_PORT = 1443
NAMESPACE = "audioClip:1"

# Budget for connecting and subscribing before we give up and use state checks.
CONNECT_TIMEOUT = 3.0
# How long to wait after subscribing for the speaker's list of existing clips.
BASELINE_TIMEOUT = 0.5
# Music Assistant can wrap a player that is itself a wrapper; stop somewhere.
MAX_PROXY_DEPTH = 3

CLIP_ERROR_REASON = (
    "the speaker reported it could not play the clip. This usually means it "
    "cannot reach the address Home Assistant gave it for the audio; see the "
    "troubleshooting guide"
)
CLIP_DISMISSED_REASON = "the speaker dismissed the clip before it played"


# --- finding the speaker --------------------------------------------------------


def resolve_sonos_host(
    hass: HomeAssistant, entity_id: str, _depth: int = 0
) -> str | None:
    """Return the address of the Sonos speaker behind ``entity_id``, if any."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    devices = dr.async_get(hass)

    if entry.platform == SONOS_DOMAIN:
        device = devices.async_get(entry.device_id) if entry.device_id else None
        return _device_host(device)

    if entry.platform == MUSIC_ASSISTANT_DOMAIN and entry.unique_id:
        unique_id = entry.unique_id
        if unique_id.startswith("media_player."):
            # MA's Home Assistant provider: the unique_id is the wrapped entity.
            if unique_id == entity_id or _depth >= MAX_PROXY_DEPTH:
                return None
            return resolve_sonos_host(hass, unique_id, _depth + 1)
        # MA's Sonos provider: the unique_id is the Sonos player id, which is
        # also the native Sonos device's identifier.
        device = devices.async_get_device(identifiers={(SONOS_DOMAIN, unique_id)})
        return _device_host(device)

    return None


def _device_host(device: Any) -> str | None:
    # HA's Sonos integration sets this to http://<speaker ip>:1400/support/review.
    url = getattr(device, "configuration_url", None)
    if not url:
        return None
    return urlparse(str(url)).hostname


# --- talking to the speaker -----------------------------------------------------


class ClipSocket(Protocol):
    """The slice of a websocket the clip channel needs."""

    async def send(self, payload: list[dict[str, Any]]) -> None: ...

    async def receive(self) -> list[dict[str, Any]] | None:
        """The next message, or None once the connection has closed."""

    async def close(self) -> None: ...


class _AiohttpClipSocket:
    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, payload: list[dict[str, Any]]) -> None:
        await self._ws.send_json(payload)

    async def receive(self) -> list[dict[str, Any]] | None:
        msg = await self._ws.receive()
        if msg.type.name != "TEXT":
            return None
        return json.loads(msg.data)

    async def close(self) -> None:
        await self._ws.close()


def websocket_url(host: str) -> str:
    return f"wss://{host}:{WEBSOCKET_PORT}/websocket/api"


async def open_clip_socket(hass: HomeAssistant, host: str) -> ClipSocket:
    """Connect to the speaker's local websocket API."""
    # Imported here so the integration loads without these; they are present
    # whenever HA's Sonos integration is, which is the only time we get here.
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from sonos_websocket.const import API_KEY

    session = async_get_clientsession(hass, verify_ssl=False)
    ws = await session.ws_connect(
        websocket_url(host),
        headers={
            "X-Sonos-Api-Key": API_KEY,
            "Sec-WebSocket-Protocol": "v1.api.smartspeaker.audio",
        },
        ssl=False,
    )
    return _AiohttpClipSocket(ws)


def _split(message: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    header = message[0] if message else {}
    body = message[1] if len(message) > 1 else {}
    return header, body


class SonosClipChannel:
    """Follow one clip's status on a subscribed speaker connection."""

    def __init__(self, socket: ClipSocket, host: str) -> None:
        self._socket = socket
        self._host = host
        # Clips the speaker already knew about when we subscribed.
        self._known: set[str] = set()
        self.clip_id: str | None = None

    async def async_subscribe(self) -> None:
        """Subscribe to clip status. Raises if the speaker cannot be followed."""
        # An empty command is rejected, but the reply names the household.
        await self._socket.send([{}, {}])
        reply = await self._socket.receive()
        if reply is None:
            raise ConnectionError("the speaker closed the connection")
        household = _split(reply)[0].get("householdId")
        if not household:
            raise ConnectionError("the speaker did not name its household")

        header, body = await self._request(
            {"namespace": "groups:1", "command": "getGroups", "householdId": household}
        )
        player = next(
            (
                p
                for p in body.get("players", [])
                if p.get("websocketUrl") == websocket_url(self._host)
            ),
            None,
        )
        if player is None:
            raise LookupError(f"no player at {self._host} in the household")
        if "AUDIO_CLIP" not in player.get("capabilities", []):
            raise LookupError("the speaker does not support audio clips")

        await self._request(
            {"namespace": NAMESPACE, "command": "subscribe", "playerId": player["id"]}
        )
        await self._async_read_baseline()

    async def async_listen(self, on_status: Callable[[str], None]) -> None:
        """Report our clip's status changes until the connection closes."""
        while (message := await self._socket.receive()) is not None:
            for status in self._our_statuses(_split(message)[1]):
                on_status(status)

    async def async_close(self) -> None:
        await self._socket.close()

    async def _request(
        self, command: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        await self._socket.send([command, {}])
        while True:
            message = await self._socket.receive()
            if message is None:
                raise ConnectionError("the speaker closed the connection")
            header, body = _split(message)
            # Replies carry "success"; events that arrive meanwhile do not.
            if "success" not in header:
                self._remember(body)
                continue
            if not header["success"]:
                raise ConnectionError(f"{command.get('command')} was rejected: {body}")
            return header, body

    async def _async_read_baseline(self) -> None:
        # Right after subscribing the speaker lists the clips it already has,
        # typically finished ones. None of those can be ours.
        try:
            async with asyncio.timeout(BASELINE_TIMEOUT):
                while True:
                    message = await self._socket.receive()
                    if message is None:
                        raise ConnectionError("the speaker closed the connection")
                    if self._remember(_split(message)[1]):
                        return
        except TimeoutError:
            return

    def _remember(self, body: dict[str, Any]) -> bool:
        if body.get("_objectType") != "audioClipStatus":
            return False
        self._known.update(
            clip["id"] for clip in body.get("audioClips", []) if clip.get("id")
        )
        return True

    def _our_statuses(self, body: dict[str, Any]) -> list[str]:
        if body.get("_objectType") != "audioClipStatus":
            return []
        statuses: list[str] = []
        for clip in body.get("audioClips", []):
            clip_id = clip.get("id")
            if not clip_id or clip_id in self._known:
                continue
            # The first new clip is the one we asked for; anything after it
            # belongs to someone else.
            if self.clip_id is None:
                self.clip_id = clip_id
            if clip_id == self.clip_id and clip.get("status"):
                statuses.append(clip["status"])
        return statuses


# --- the verifier ---------------------------------------------------------------


class SonosClipVerifier(StateVerifier):
    """Judge playback from the Sonos speaker's audio-clip status."""

    name = "sonos_clip"
    priority = 50

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        super().__init__(hass, entity_id)
        self._channel: SonosClipChannel | None = None
        self._reader: asyncio.Task[None] | None = None
        # State evidence seen while the clip channel was deciding.
        self._state_evidence: list[Callable[[], None]] = []

    @classmethod
    def matches(cls, hass: HomeAssistant, entity_id: str) -> bool:
        return resolve_sonos_host(hass, entity_id) is not None

    @property
    def channel_live(self) -> bool:
        return self._reader is not None and not self._reader.done()

    async def async_arm(self) -> None:
        await super().async_arm()
        host = resolve_sonos_host(self.hass, self.entity_id)
        if host is None:
            return
        socket: ClipSocket | None = None
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                socket = await open_clip_socket(self.hass, host)
                channel = SonosClipChannel(socket, host)
                await channel.async_subscribe()
        except Exception as err:  # noqa: BLE001 - fall back to state checks
            _LOGGER.warning(
                "intercom: cannot follow Sonos clips on %s (%s: %s); "
                "falling back to state checks",
                self.entity_id,
                host,
                str(err) or type(err).__name__,
            )
            if socket is not None:
                await _close_quietly(socket.close)
            return
        self._channel = channel
        self._reader = asyncio.get_running_loop().create_task(
            self._async_follow(channel), name=f"intercom sonos clip {self.entity_id}"
        )

    async def async_disarm(self) -> None:
        await super().async_disarm()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        if self._channel is not None:
            await _close_quietly(self._channel.async_close)
            self._channel = None

    async def _async_follow(self, channel: SonosClipChannel) -> None:
        try:
            await channel.async_listen(self._on_clip_status)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - losing the channel is not fatal
            _LOGGER.debug("intercom: Sonos clip channel failed", exc_info=True)
        # The channel is gone before it reached a verdict: let whatever the
        # entity state showed meanwhile decide instead.
        if not (self.finished.is_set() or self.failed.is_set()):
            _LOGGER.debug(
                "intercom: lost the Sonos clip channel for %s; using state checks",
                self.entity_id,
            )
            evidence, self._state_evidence = self._state_evidence, []
            for apply in evidence:
                apply()

    def _on_clip_status(self, status: str) -> None:
        if status == "ACTIVE":
            self._mark_started()
        elif status == "DONE":
            self._mark_finished()
        elif status == "ERROR":
            if self.started.is_set():
                self.finished.set()
            else:
                self._mark_failed(CLIP_ERROR_REASON)
        elif status == "DISMISSED":
            if self.started.is_set():
                self.finished.set()
            else:
                self._mark_failed(CLIP_DISMISSED_REASON)

    # While the clip channel is live it is authoritative: a proxy entity may
    # claim to be playing while the speaker is refusing the clip.

    def _on_state_started(self) -> None:
        if self.channel_live:
            self._state_evidence.append(self._mark_started)
        else:
            super()._on_state_started()

    def _on_state_finished(self) -> None:
        if self.channel_live:
            self._state_evidence.append(self._mark_finished)
        else:
            super()._on_state_finished()


async def _close_quietly(close: Callable[[], Any]) -> None:
    try:
        await close()
    except Exception:  # noqa: BLE001 - already giving up on this connection
        _LOGGER.debug("intercom: error closing Sonos connection", exc_info=True)
