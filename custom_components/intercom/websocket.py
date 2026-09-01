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

from .const import DATA_LAST_RESULT, EVENT_RESULT, WS_TYPE_SUBSCRIBE_RESULT


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register the intercom websocket commands.

    Idempotent: registering simply replaces the handler, so a reload is safe.
    """
    websocket_api.async_register_command(hass, ws_subscribe_result)


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
