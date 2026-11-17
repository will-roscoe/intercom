"""Websocket API for the Intercom integration.

Home Assistant refuses to let a non-admin subscribe to a custom bus event:
``subscribe_events`` checks the event type against a hardcoded allowlist in
``homeassistant.auth.permissions.events`` and raises ``Unauthorized`` otherwise,
logging "Refusing to allow <user> to subscribe to event ...". There is no hook to
extend that allowlist, so a card built on ``subscribe_events`` silently never
updates for anyone who is not an admin.

Rather than widen the bus for everyone, this module exposes one narrow
subscription of its own — the same pattern core uses for
``persistent_notification/subscribe``. A subscriber sees intercom broadcast
results and nothing else.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import Event, HomeAssistant, callback
import voluptuous as vol

from .const import (
    DATA_HELD,
    DATA_LAST_RESULT,
    EVENT_RESULT,
    WS_TYPE_CANCEL_HELD,
    WS_TYPE_SUBSCRIBE_HELD,
    WS_TYPE_SUBSCRIBE_RESULT,
)


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register the intercom websocket commands.

    Idempotent: registering simply replaces the handler, so a reload is safe.
    """
    websocket_api.async_register_command(hass, ws_subscribe_result)
    websocket_api.async_register_command(hass, ws_subscribe_held)
    websocket_api.async_register_command(hass, ws_cancel_held)


@callback
def async_store_result(hass: HomeAssistant, result: dict[str, Any]) -> None:
    """Remember the last broadcast so a card can show it as soon as it loads."""
    hass.data[DATA_LAST_RESULT] = result


@callback
@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_SUBSCRIBE_RESULT})
def ws_subscribe_result(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stream broadcast results to any signed-in user.

    Deliberately *not* decorated with ``@websocket_api.require_admin``: everyone
    in the household who can send a broadcast should be able to see how it went.
    The payload carries only what the broadcast itself contains.
    """
    msg_id = msg["id"]

    @callback
    def _forward(event: Event) -> None:
        connection.send_message(
            websocket_api.event_message(msg_id, {"result": event.data, "replay": False})
        )

    # Core's generic unsubscribe_events handler tears this down for us.
    connection.subscriptions[msg_id] = hass.bus.async_listen(EVENT_RESULT, _forward)
    connection.send_result(msg_id)

    # Replay the last broadcast so a freshly loaded card is not blank until the
    # next one happens. Flagged so the card can show it as history, not as news.
    if (last := hass.data.get(DATA_LAST_RESULT)) is not None:
        connection.send_message(
            websocket_api.event_message(msg_id, {"result": last, "replay": True})
        )


@callback
@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_SUBSCRIBE_HELD})
def ws_subscribe_held(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Stream the held messages to any signed-in user: now, then on every change.

    Each event is ``{"held": [...]}`` with the items of
    :meth:`~.held.HeldMessages.items`, oldest first.
    """
    msg_id = msg["id"]
    held = hass.data.get(DATA_HELD)

    @callback
    def _forward(items: list[dict[str, Any]]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, {"held": items}))

    connection.subscriptions[msg_id] = (
        held.async_subscribe(_forward) if held is not None else (lambda: None)
    )
    connection.send_result(msg_id)
    _forward(held.items() if held is not None else [])


@websocket_api.websocket_command(
    {vol.Required("type"): WS_TYPE_CANCEL_HELD, vol.Required("held_id"): str}
)
@websocket_api.async_response
async def ws_cancel_held(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Cancel a held message for any signed-in user; replies ``{"cancelled": bool}``."""
    held = hass.data.get(DATA_HELD)
    cancelled = held is not None and await held.async_cancel(msg["held_id"])
    connection.send_result(msg["id"], {"cancelled": cancelled})
