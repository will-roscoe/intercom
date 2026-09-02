"""The non-admin subscription path.

Home Assistant refuses `subscribe_events` for custom event types unless the user
is an admin, so results reach the card through a command of our own. These tests
guard the two properties that matters: it is not admin-gated, and it is torn down
by core's generic unsubscribe handler.
"""

from __future__ import annotations

from helpers import new_hass
from homeassistant.components import websocket_api
from intercom.const import EVENT_RESULT, WS_TYPE_SUBSCRIBE_RESULT
from intercom.websocket import async_register_websocket_api, async_store_result


class User:
    def __init__(self, name: str, is_admin: bool):
        self.name = name
        self.is_admin = is_admin
        self.id = name.lower()


def registered_handler(hass):
    async_register_websocket_api(hass)
    handler, _schema = websocket_api.HANDLERS[WS_TYPE_SUBSCRIBE_RESULT]
    return handler


def subscribe(handler, hass, msg_id, is_admin=False):
    connection = websocket_api.ActiveConnection(User("Nikki", is_admin))
    handler(hass, connection, {"id": msg_id, "type": WS_TYPE_SUBSCRIBE_RESULT})
    return connection


def test_command_name_matches_the_card():
    assert WS_TYPE_SUBSCRIBE_RESULT == "intercom/subscribe_result"


def test_the_command_is_not_admin_gated():
    hass = new_hass()
    handler = registered_handler(hass)
    assert getattr(handler, "_ws_require_admin", False) is False


def test_registering_twice_is_safe():
    hass = new_hass()
    registered_handler(hass)
    registered_handler(hass)
    assert list(websocket_api.HANDLERS) == [WS_TYPE_SUBSCRIBE_RESULT]


def test_a_non_admin_receives_results():
    hass = new_hass()
    handler = registered_handler(hass)
    connection = subscribe(handler, hass, 7)

    assert connection.results == [(7, None)]
    assert 7 in connection.subscriptions  # where unsubscribe_events looks
    assert connection.messages == []  # nothing replayed yet

    result = {"id": "abc123", "summary": "played on 1/1 speakers"}
    hass.bus.async_fire(EVENT_RESULT, result)

    assert len(connection.messages) == 1
    message = connection.messages[0]
    assert message["id"] == 7
    assert message["type"] == "event"
    assert message["event"] == {"result": result, "replay": False}


def test_the_last_broadcast_is_replayed_on_subscribe():
    hass = new_hass()
    handler = registered_handler(hass)
    result = {"id": "abc123", "summary": "played on 1/1 speakers"}
    async_store_result(hass, result)

    connection = subscribe(handler, hass, 9)

    assert len(connection.messages) == 1
    assert connection.messages[0]["event"] == {"result": result, "replay": True}


def test_every_subscriber_gets_its_own_copy():
    hass = new_hass()
    handler = registered_handler(hass)
    connections = [subscribe(handler, hass, msg_id) for msg_id in (1, 2, 3)]

    hass.bus.async_fire(EVENT_RESULT, {"id": "def456"})

    assert [len(c.messages) for c in connections] == [1, 1, 1]
    assert [c.messages[0]["id"] for c in connections] == [1, 2, 3]


def test_unsubscribing_detaches_the_listener():
    hass = new_hass()
    handler = registered_handler(hass)
    going = subscribe(handler, hass, 1)
    staying = subscribe(handler, hass, 2)

    going.subscriptions.pop(1)()  # what core's unsubscribe_events does
    hass.bus.async_fire(EVENT_RESULT, {"id": "ghi789"})

    assert going.messages == []
    assert len(staying.messages) == 1
