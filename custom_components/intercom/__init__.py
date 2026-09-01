"""The Intercom integration.

Provides a single ``intercom.broadcast`` service that speaks a message over a set
of media players (via ``tts.speak`` with ``cache: true``, which is required for
Sonos) while temporarily setting and then restoring their volume, and fans the
same message out to a set of ``notify`` targets.

The service is built to be trusted with a message that matters: every target is
attempted independently, playback is *verified* rather than assumed, and the
call returns a structured response saying exactly what was heard where. The
integration also serves a self-contained Lovelace card (``intercom-card.js``)
and registers it with the frontend automatically.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .broadcast import (
    BroadcastRequest,
    async_run_notify,
    async_run_players,
    build_result,
    new_broadcast_id,
)
from .const import (
    ATTR_CRITICAL,
    ATTR_ENGINE,
    ATTR_MAX_ATTEMPTS,
    ATTR_MESSAGE,
    ATTR_NOTIFY,
    ATTR_PLAYERS,
    ATTR_RESTORE_VOLUME,
    ATTR_TITLE,
    ATTR_VERIFY,
    ATTR_VOLUME,
    CONF_DEFAULT_TITLE,
    CONF_MAX_ATTEMPTS,
    CONF_START_TIMEOUT,
    CONF_TTS_ENGINE,
    CONF_UNMUTE,
    CONF_WAIT_TIMEOUT,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_START_TIMEOUT,
    DEFAULT_TITLE,
    DEFAULT_TTS_ENGINE,
    DEFAULT_UNMUTE,
    DEFAULT_WAIT_TIMEOUT,
    DOMAIN,
    EVENT_RESULT,
    SERVICE_BROADCAST,
    UNAVAILABLE_STATES,
)
from .frontend import async_register_card

_LOGGER = logging.getLogger(__name__)

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
        vol.Optional(ATTR_CRITICAL, default=False): cv.boolean,
        vol.Optional(ATTR_VERIFY, default=True): cv.boolean,
        vol.Optional(ATTR_MAX_ATTEMPTS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=5)
        ),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intercom from a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    await async_register_card(hass, str(integration.version))

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


def _build_request(options: dict, call: ServiceCall) -> BroadcastRequest:
    """Resolve a service call plus the entry options into one request."""
    message: str = call.data[ATTR_MESSAGE].strip()
    if not message:
        raise HomeAssistantError("Cannot broadcast an empty message")

    players: list[str] = call.data[ATTR_PLAYERS]
    notify_targets: list[str] = call.data[ATTR_NOTIFY]
    if not players and not notify_targets:
        # Silently doing nothing is the one outcome this service must never have.
        raise HomeAssistantError(
            "Nothing to broadcast to: give at least one player or notify target"
        )

    critical: bool = call.data[ATTR_CRITICAL]
    attempts = call.data.get(ATTR_MAX_ATTEMPTS)
    if attempts is None:
        attempts = int(options.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS))

    return BroadcastRequest(
        message=message,
        title=call.data.get(ATTR_TITLE)
        or options.get(CONF_DEFAULT_TITLE, DEFAULT_TITLE),
        engine=call.data.get(ATTR_ENGINE)
        or options.get(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE),
        players=tuple(dict.fromkeys(players)),
        notify_targets=tuple(dict.fromkeys(notify_targets)),
        volume=call.data.get(ATTR_VOLUME),
        restore_volume=call.data[ATTR_RESTORE_VOLUME],
        unmute=bool(options.get(CONF_UNMUTE, DEFAULT_UNMUTE)),
        verify=call.data[ATTR_VERIFY],
        critical=critical,
        max_attempts=max(1, int(attempts)),
        wait_timeout=float(options.get(CONF_WAIT_TIMEOUT, DEFAULT_WAIT_TIMEOUT)),
        start_timeout=float(options.get(CONF_START_TIMEOUT, DEFAULT_START_TIMEOUT)),
    )


async def _async_broadcast(
    hass: HomeAssistant, options: dict, call: ServiceCall
) -> ServiceResponse:
    """Execute a broadcast: speak on players + notify targets."""
    request = _build_request(options, call)
    broadcast_id = new_broadcast_id()
    started_at = dt_util.utcnow()

    engine_state = hass.states.get(request.engine)
    engine_ok = (
        engine_state is not None and engine_state.state not in UNAVAILABLE_STATES
    )
    if request.players and not engine_ok:
        _LOGGER.error(
            "intercom[%s]: TTS engine %s is unavailable; no speech is possible",
            broadcast_id,
            request.engine,
        )

    # Speech can take tens of seconds; phones should not wait for it.
    player_outcomes, notify_outcomes = await asyncio.gather(
        async_run_players(hass, request, engine_ok),
        async_run_notify(hass, request),
    )

    result = build_result(
        request, broadcast_id, engine_ok, player_outcomes, notify_outcomes, started_at
    )

    if not result["delivered"]:
        _LOGGER.error("intercom[%s]: %s", broadcast_id, result["summary"])
    elif not result["complete"]:
        _LOGGER.warning("intercom[%s]: %s", broadcast_id, result["summary"])
    else:
        _LOGGER.info("intercom[%s]: %s", broadcast_id, result["summary"])

    hass.bus.async_fire(EVENT_RESULT, result)
    await _async_report_problems(hass, result)

    if request.critical and not result["complete"]:
        # An opt-in loud failure, so a critical automation cannot mistake a
        # half-delivered broadcast for a delivered one. The event and the
        # persistent notification above still carry the full detail.
        raise HomeAssistantError(
            f"Critical intercom broadcast {broadcast_id} was not fully delivered: "
            f"{result['summary']}"
        )

    return result


async def _async_report_problems(hass: HomeAssistant, result: dict) -> None:
    """Raise a persistent notification if anything went wrong.

    A broadcast that could not be delivered at all gets its own notification so
    a later, healthier broadcast cannot quietly overwrite the evidence.
    """
    if result["complete"]:
        return

    lines: list[str] = [result["summary"], ""]
    for outcome in result["players"]:
        bits = [f"- {outcome['name']} ({outcome['entity_id']}): {outcome['status']}"]
        if outcome["error"]:
            bits.append(f"  {outcome['error']}")
        if outcome["detail"]:
            bits.append(f"  {outcome['detail']}")
        for warning in outcome["warnings"]:
            bits.append(f"  {warning}")
        lines.extend(bits)
    for outcome in result["notify"]:
        line = f"- {outcome['target']}: {outcome['status']}"
        if outcome["error"]:
            line += f" ({outcome['error']})"
        lines.append(line)
    lines.extend(["", f'Message: "{result["message"]}"'])

    notification_id = (
        f"intercom_broadcast_{result['id']}"
        if not result["delivered"]
        else "intercom_broadcast_result"
    )
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "notification_id": notification_id,
            "title": "Intercom broadcast problem",
            "message": "\n".join(lines),
        },
        blocking=False,
    )
