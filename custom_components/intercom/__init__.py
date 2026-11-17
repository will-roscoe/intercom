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
    ATTR_ALTERNATES,
    ATTR_CHIME,
    ATTR_CRITICAL,
    ATTR_ENGINE,
    ATTR_HOLD,
    ATTR_HOLD_LABEL,
    ATTR_MAX_ATTEMPTS,
    ATTR_MESSAGE,
    ATTR_NOTIFY,
    ATTR_PLAYERS,
    ATTR_RESTORE_VOLUME,
    ATTR_TITLE,
    ATTR_VERIFY,
    ATTR_VOLUME,
    CHIME_URL,
    CONF_CHIME,
    CONF_CHIME_MEDIA,
    CONF_CHIME_SECONDS,
    CONF_DEFAULT_TITLE,
    CONF_MAX_ATTEMPTS,
    CONF_START_TIMEOUT,
    CONF_TTS_ENGINE,
    CONF_UNMUTE,
    CONF_WAIT_TIMEOUT,
    DATA_HELD,
    DEFAULT_CHIME,
    DEFAULT_CHIME_SECONDS,
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
from .held import HeldMessages
from .websocket import async_register_websocket_api, async_store_result

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
        vol.Optional(ATTR_ALTERNATES, default=dict): {
            cv.entity_id: vol.All(cv.ensure_list, [cv.entity_id])
        },
        # Conditions as in an automation's `conditions:`; validated by held.py
        # (kept as given, since the validated form cannot be saved).
        vol.Optional(ATTR_HOLD): vol.All(cv.ensure_list, [dict]),
        vol.Optional(ATTR_HOLD_LABEL): cv.string,
        # true/false for every speaker, or the speakers that get the chime.
        vol.Optional(ATTR_CHIME): vol.Any(
            cv.boolean, vol.All(cv.ensure_list, [cv.entity_id])
        ),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intercom from a config entry."""
    integration = await async_get_integration(hass, DOMAIN)
    await async_register_card(hass, str(integration.version))
    async_register_websocket_api(hass)

    def _options() -> dict:
        return {**entry.data, **entry.options}

    held = HeldMessages(
        hass, _options, lambda data: _async_broadcast(hass, _options(), data)
    )
    await held.async_load()
    hass.data[DATA_HELD] = held

    async def _handle_broadcast(call: ServiceCall) -> ServiceResponse:
        options = {**entry.data, **entry.options}
        return await _async_service(hass, options, dict(call.data))

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
    if (held := hass.data.pop(DATA_HELD, None)) is not None:
        await held.async_unload()
    return True


async def _async_service(hass: HomeAssistant, options: dict, data: dict) -> dict:
    """Run an ``intercom.broadcast`` call: hold it, or broadcast it now.

    A call with ``hold`` is checked as a broadcast first, so a mistake is
    reported now rather than when its conditions come true, then handed to
    :class:`~.held.HeldMessages`.

    :param hass: Home Assistant.
    :param options: the entry's data and options.
    :param data: the validated service data.
    :returns: the broadcast result, or the held message's summary.
    :raises HomeAssistantError: the call is invalid, or a ``critical``
        broadcast was not fully delivered.
    """
    hold = data.pop(ATTR_HOLD, None)
    label = data.pop(ATTR_HOLD_LABEL, None)
    if not hold:
        return await _async_broadcast(hass, options, data)
    _build_request(options, data)
    return await hass.data[DATA_HELD].async_submit(data, hold, label)


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _build_request(options: dict, data: dict) -> BroadcastRequest:
    """Resolve a service call plus the entry options into one request."""
    message: str = data[ATTR_MESSAGE].strip()
    if not message:
        raise HomeAssistantError("Cannot broadcast an empty message")

    players: list[str] = data[ATTR_PLAYERS]
    notify_targets: list[str] = data[ATTR_NOTIFY]
    if not players and not notify_targets:
        # Silently doing nothing is the one outcome this service must never have.
        raise HomeAssistantError(
            "Nothing to broadcast to: give at least one player or notify target"
        )

    alternates = _alternates(data[ATTR_ALTERNATES], players)

    critical: bool = data[ATTR_CRITICAL]
    attempts = data.get(ATTR_MAX_ATTEMPTS)
    if attempts is None:
        attempts = int(options.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS))

    return BroadcastRequest(
        message=message,
        title=data.get(ATTR_TITLE) or options.get(CONF_DEFAULT_TITLE, DEFAULT_TITLE),
        engine=data.get(ATTR_ENGINE)
        or options.get(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE),
        players=tuple(dict.fromkeys(players)),
        notify_targets=tuple(dict.fromkeys(notify_targets)),
        volume=data.get(ATTR_VOLUME),
        restore_volume=data[ATTR_RESTORE_VOLUME],
        unmute=bool(options.get(CONF_UNMUTE, DEFAULT_UNMUTE)),
        verify=data[ATTR_VERIFY],
        critical=critical,
        max_attempts=max(1, int(attempts)),
        wait_timeout=float(options.get(CONF_WAIT_TIMEOUT, DEFAULT_WAIT_TIMEOUT)),
        start_timeout=float(options.get(CONF_START_TIMEOUT, DEFAULT_START_TIMEOUT)),
        alternates=alternates,
        chime=_chimed(data.get(ATTR_CHIME), options, players, alternates),
        chime_media=options.get(CONF_CHIME_MEDIA) or CHIME_URL,
        chime_seconds=float(options.get(CONF_CHIME_SECONDS, DEFAULT_CHIME_SECONDS)),
    )


def _chimed(
    given: bool | list[str] | None,
    options: dict,
    players: list[str],
    alternates: dict[str, tuple[str, ...]],
) -> frozenset[str]:
    """Return the players that get a chime, with the alternates of each.

    :param given: the call's ``chime``: true/false for every player, a list of
        players, or None to use the integration's ``chime`` option.
    :param options: the entry's options.
    :param players: the requested players.
    :param alternates: requested player -> its alternates (already cleaned).
    :returns: every chosen player plus its alternates, since an alternate
        stands in for the speaker it covers.
    :raises HomeAssistantError: a listed player is not one of ``players``.
    """
    if given is None:
        given = bool(options.get(CONF_CHIME, DEFAULT_CHIME))
    if given is True:
        chosen = list(players)
    elif given is False:
        chosen = []
    else:
        unknown = [entity for entity in given if entity not in players]
        if unknown:
            raise HomeAssistantError(
                f"chime given for {', '.join(unknown)}, which is not in players"
            )
        chosen = list(given)
    return frozenset(entity for p in chosen for entity in (p, *alternates.get(p, ())))


def _alternates(
    given: dict[str, list[str]], players: list[str]
) -> dict[str, tuple[str, ...]]:
    """Return each requested player's alternates, deduplicated, in order.

    A player listed among its own alternates is dropped, and so is a player
    left with none.

    :param given: the validated ``alternates`` field of the service call.
    :param players: the requested players.
    :returns: requested player -> its alternates.
    :raises HomeAssistantError: an alternates key is not one of ``players``.
    """
    unknown = [entity for entity in given if entity not in players]
    if unknown:
        raise HomeAssistantError(
            f"alternates given for {', '.join(unknown)}, which is not in players"
        )
    cleaned: dict[str, tuple[str, ...]] = {}
    for entity, alternates in given.items():
        ordered = tuple(alt for alt in dict.fromkeys(alternates) if alt != entity)
        if ordered:
            cleaned[entity] = ordered
    return cleaned


async def _async_broadcast(
    hass: HomeAssistant, options: dict, data: dict
) -> ServiceResponse:
    """Execute a broadcast: speak on players + notify targets."""
    request = _build_request(options, data)
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

    async_store_result(hass, result)
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
    names = {o["entity_id"]: o["name"] for o in result["players"]}
    for outcome in result["players"]:
        head = f"- {outcome['name']} ({outcome['entity_id']}): {outcome['status']}"
        stood_in_for = outcome.get("alternate_for")
        if stood_in_for:
            head += f", alternate for {names.get(stood_in_for, stood_in_for)}"
        bits = [head]
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
