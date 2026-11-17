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
    assert list(websocket_api.HANDLERS) == [
        WS_TYPE_SUBSCRIBE_RESULT,
        "intercom/subscribe_held",
        "intercom/cancel_held",
    ]


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


# --- held messages -------------------------------------------------------------

import asyncio  # noqa: E402

from homeassistant.helpers.storage import Store  # noqa: E402
from intercom.const import (  # noqa: E402
    DATA_HELD,
    WS_TYPE_CANCEL_HELD,
    WS_TYPE_SUBSCRIBE_HELD,
)
from intercom.held import HeldMessages  # noqa: E402

PERSON = "person.will"
HOLD = [{"condition": "state", "entity_id": PERSON, "state": "home"}]


async def held_hass():
    Store.DISK.clear()
    hass = new_hass()
    hass.states.set(PERSON, "not_home")

    async def send(data):
        return {}

    held = HeldMessages(hass, dict, send)
    await held.async_load()
    hass.data[DATA_HELD] = held
    async_register_websocket_api(hass)
    return hass, held


def test_subscribing_to_held_messages_gets_the_list_and_its_changes():
    async def scenario():
        hass, held = await held_hass()
        await held.async_submit({"message": "one", "players": []}, HOLD, "Will is home")
        handler, _ = websocket_api.HANDLERS[WS_TYPE_SUBSCRIBE_HELD]
        connection = websocket_api.ActiveConnection(User("Nikki", False))
        handler(hass, connection, {"id": 7, "type": WS_TYPE_SUBSCRIBE_HELD})
        await held.async_submit({"message": "two", "players": []}, HOLD, None)
        connection.subscriptions[7]()  # what core's unsubscribe does
        await held.async_submit({"message": "three", "players": []}, HOLD, None)
        return connection

    connection = asyncio.run(scenario())
    assert connection.results == [(7, None)]
    lists = [[i["message"] for i in m["event"]["held"]] for m in connection.messages]
    assert lists == [["one"], ["one", "two"]]


def test_cancelling_a_held_message_over_the_websocket():
    async def scenario():
        hass, held = await held_hass()
        result = await held.async_submit({"message": "one", "players": []}, HOLD, None)
        handler, schema = websocket_api.HANDLERS[WS_TYPE_CANCEL_HELD]
        connection = websocket_api.ActiveConnection(User("Nikki", False))
        # Core adds and checks the message "id" itself.
        fields = schema({"type": WS_TYPE_CANCEL_HELD, "held_id": result["id"]})
        msg = {"id": 8, **fields}
        await handler(hass, connection, msg)
        return connection, held.items()

    connection, items = asyncio.run(scenario())
    assert connection.results == [(8, {"cancelled": True})]
    assert items == []
