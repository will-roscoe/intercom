"""The Intercom integration.

Provides a single ``intercom.broadcast`` service that speaks a message over a set
of media players (via ``tts.speak`` with ``cache: true``, which is required for
Sonos) while temporarily setting and then restoring their volume, and fans the
same message out to a set of ``notify`` targets.

The integration also serves a self-contained Lovelace card (``intercom-card.js``)
and registers it with the frontend automatically.
"""

from __future__ import annotations

import asyncio
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import (
    ATTR_ENGINE,
    ATTR_MESSAGE,
    ATTR_NOTIFY,
    ATTR_PLAYERS,
    ATTR_RESTORE_VOLUME,
    ATTR_TITLE,
    ATTR_VOLUME,
    CARD_FILENAME,
    CONF_DEFAULT_TITLE,
    CONF_TTS_ENGINE,
    CONF_WAIT_TIMEOUT,
    DATA_CARD_REGISTERED,
    DEFAULT_TITLE,
    DEFAULT_TTS_ENGINE,
    DEFAULT_WAIT_TIMEOUT,
    DOMAIN,
    EVENT_RESULT,
    SERVICE_BROADCAST,
    UNAVAILABLE_STATES,
    URL_BASE,
)

_LOGGER = logging.getLogger(__name__)

# How long to wait for a player to *enter* the playing state before assuming it
# never will (e.g. Sonos "announcement" playback that doesn't flip transport state).
_START_GRACE_SECONDS = 3.0
# Delay after setting volume so the level is applied before audio starts.
_VOLUME_SETTLE_SECONDS = 0.4

BROADCAST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MESSAGE): cv.string,
        vol.Optional(ATTR_PLAYERS, default=list): vol.All(
            cv.ensure_list, [cv.entity_id]
        ),
        vol.Optional(ATTR_NOTIFY, default=list): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_VOLUME): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=100)
        ),
        vol.Optional(ATTR_TITLE): cv.string,
        vol.Optional(ATTR_ENGINE): cv.string,
        vol.Optional(ATTR_RESTORE_VOLUME, default=True): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intercom from a config entry."""
    await _async_register_card(hass)

    async def _handle_broadcast(call: ServiceCall) -> ServiceResponse:
        options = {**entry.data, **entry.options}
        return await _async_broadcast(hass, options, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_BROADCAST,
        _handle_broadcast,
        schema=BROADCAST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_BROADCAST)
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_broadcast(
    hass: HomeAssistant, options: dict, call: ServiceCall
) -> ServiceResponse:
    """Execute a broadcast: speak on players + notify targets."""
    message: str = call.data[ATTR_MESSAGE].strip()
    if not message:
        raise HomeAssistantError("Cannot broadcast an empty message")

    players: list[str] = call.data[ATTR_PLAYERS]
    notify_targets: list[str] = call.data[ATTR_NOTIFY]
    volume: float | None = call.data.get(ATTR_VOLUME)
    restore: bool = call.data[ATTR_RESTORE_VOLUME]
    title: str = call.data.get(ATTR_TITLE) or options.get(
        CONF_DEFAULT_TITLE, DEFAULT_TITLE
    )
    engine: str = call.data.get(ATTR_ENGINE) or options.get(
        CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE
    )
    timeout: float = float(options.get(CONF_WAIT_TIMEOUT, DEFAULT_WAIT_TIMEOUT))

    # --- work out which players can actually be spoken to right now ---
    available: list[str] = []
    offline: list[str] = []
    for player in players:
        state = hass.states.get(player)
        if state is None or state.state in UNAVAILABLE_STATES:
            offline.append(player)
        else:
            available.append(player)

    engine_state = hass.states.get(engine)
    engine_ok = (
        engine_state is not None and engine_state.state not in UNAVAILABLE_STATES
    )

    spoke: list[str] = []
    if available and engine_ok:
        spoke = await _async_speak(
            hass, engine, available, message, volume, restore, timeout
        )
    elif available and not engine_ok:
        _LOGGER.warning("intercom: TTS engine %s unavailable; skipping speech", engine)

    # --- fan the message out to notify targets ---
    notified, notify_failed = await _async_notify(hass, notify_targets, message, title)

    result: dict = {
        "message": message,
        "spoke": spoke,
        "offline": offline,
        "notified": notified,
        "notify_failed": notify_failed,
        "engine_available": engine_ok,
    }
    hass.bus.async_fire(EVENT_RESULT, result)
    await _async_report_problems(
        hass, players, engine, engine_ok, offline, notify_failed
    )
    return result


async def _async_speak(
    hass: HomeAssistant,
    engine: str,
    players: list[str],
    message: str,
    volume: float | None,
    restore: bool,
    timeout: float,
) -> list[str]:
    """Set volume, speak, wait, then restore volume. Return players spoken to."""
    saved: dict[str, float] = {}
    if volume is not None and restore:
        for player in players:
            state = hass.states.get(player)
            level = state.attributes.get("volume_level") if state else None
            if level is not None:
                saved[player] = level

    if volume is not None:
        await hass.services.async_call(
            "media_player",
            "volume_set",
            {"entity_id": players, "volume_level": round(volume / 100, 3)},
            blocking=True,
        )
        await asyncio.sleep(_VOLUME_SETTLE_SECONDS)

    try:
        await hass.services.async_call(
            "tts",
            "speak",
            {
                # cache:true is REQUIRED for Sonos. Without it HA serves a
                # synthesis-on-demand stream with no Content-Length, which Sonos
                # accepts but silently refuses to play.
                "entity_id": engine,
                "media_player_entity_id": players,
                "message": message,
                "cache": True,
            },
            blocking=True,
        )
    except Exception as err:  # noqa: BLE001 - surface any TTS failure, keep going
        _LOGGER.error("intercom: tts.speak failed: %s", err)
        # still restore volume if we changed it
        await _async_restore(hass, saved)
        return []

    await _async_wait_for_playback(hass, players, timeout)
    await _async_restore(hass, saved)
    return players


async def _async_wait_for_playback(
    hass: HomeAssistant, players: list[str], timeout: float
) -> None:
    """Wait until players finish playing.

    Two-phase so it is robust whether or not a player reports ``playing``:
    first wait briefly for playback to *start*, then wait for it to *finish*.
    """

    def any_playing() -> bool:
        return any(
            (state := hass.states.get(p)) is not None and state.state == "playing"
            for p in players
        )

    start = hass.loop.time()
    while hass.loop.time() - start < _START_GRACE_SECONDS:
        if any_playing():
            break
        await asyncio.sleep(0.3)

    start = hass.loop.time()
    while hass.loop.time() - start < timeout:
        if not any_playing():
            return
        await asyncio.sleep(0.5)


async def _async_restore(hass: HomeAssistant, saved: dict[str, float]) -> None:
    """Restore each player's saved volume level."""
    for player, level in saved.items():
        await hass.services.async_call(
            "media_player",
            "volume_set",
            {"entity_id": player, "volume_level": level},
            blocking=False,
        )


async def _async_notify(
    hass: HomeAssistant, targets: list[str], message: str, title: str
) -> tuple[list[str], list[str]]:
    """Send the message to each notify target. Returns (notified, failed)."""
    notified: list[str] = []
    failed: list[str] = []
    for target in targets:
        service = target.split(".", 1)[1] if target.startswith("notify.") else target
        try:
            await hass.services.async_call(
                "notify",
                service,
                {"message": message, "title": title},
                blocking=True,
            )
            notified.append(target)
        except Exception as err:  # noqa: BLE001 - one bad target must not stop the rest
            _LOGGER.warning("intercom: notify %s failed: %s", target, err)
            failed.append(target)
    return notified, failed


async def _async_report_problems(
    hass: HomeAssistant,
    players: list[str],
    engine: str,
    engine_ok: bool,
    offline: list[str],
    notify_failed: list[str],
) -> None:
    """Raise a persistent notification if anything went wrong."""
    lines: list[str] = []
    if offline:
        lines.append(f"Speakers offline (skipped): {', '.join(offline)}")
    if players and not engine_ok:
        lines.append(f"TTS engine unavailable: {engine}")
    if notify_failed:
        lines.append(f"Notify targets failed: {', '.join(notify_failed)}")
    if not lines:
        return
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "notification_id": "intercom_broadcast_result",
            "title": "Intercom broadcast",
            "message": "\n".join(lines),
        },
        blocking=False,
    )


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the bundled Lovelace card and register it with the frontend once."""
    if hass.data.get(DATA_CARD_REGISTERED):
        return

    # Imported lazily so the integration still loads if the frontend/http
    # internals move between HA versions.
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    card_path = os.path.join(os.path.dirname(__file__), CARD_FILENAME)
    card_url = f"{URL_BASE}/{CARD_FILENAME}"

    await hass.http.async_register_static_paths(
        [StaticPathConfig(card_url, card_path, cache_headers=False)]
    )
    add_extra_js_url(hass, card_url)
    hass.data[DATA_CARD_REGISTERED] = True
    _LOGGER.debug("intercom: registered Lovelace card at %s", card_url)
