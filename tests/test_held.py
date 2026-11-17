"""Held messages (#12): wait for conditions, survive restarts, cancel, expire."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from helpers import SONOS, new_hass
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from intercom import held as held_module
from intercom.held import HeldMessages
import pytest

PERSON = "person.will"
HOLD = [{"condition": "state", "entity_id": PERSON, "state": "home"}]
DATA = {"message": "Dinner is ready", "players": [SONOS], "notify": []}


@pytest.fixture(autouse=True)
def empty_disk():
    Store.DISK.clear()


def run(coro):
    return asyncio.run(coro)


async def start(hass, options=None):
    sent: list[dict] = []

    async def send(data):
        sent.append(data)
        return {"id": "abc", "delivered": True}

    held = HeldMessages(hass, lambda: options or {}, send)
    await held.async_load()
    return held, sent


def test_a_held_message_waits_for_its_conditions_then_plays():
    async def scenario():
        hass = new_hass()
        hass.states.set(PERSON, "not_home")
        held, sent = await start(hass)
        result = await held.async_submit(DATA, HOLD, "Will is home")
        before = list(sent)
        hass.states.set(PERSON, "home")
        await hass.async_block_till_done()
        return result, before, sent, held.items()

    result, before, sent, items = run(scenario())
    assert result["held"] is True
    assert result["label"] == "Will is home"
    assert result["summary"] == "held until Will is home"
    assert before == []
    assert sent == [DATA]
    assert items == []


def test_a_message_whose_conditions_already_hold_is_sent_now():
    async def scenario():
        hass = new_hass()
        hass.states.set(PERSON, "home")
        held, sent = await start(hass)
        return await held.async_submit(DATA, HOLD, None), sent, held.items()

    result, sent, items = run(scenario())
    assert result == {"id": "abc", "delivered": True}
    assert sent == [DATA]
    assert items == []


def test_a_cancelled_message_never_plays():
    async def scenario():
        hass = new_hass()
        hass.states.set(PERSON, "not_home")
        held, sent = await start(hass)
        result = await held.async_submit(DATA, HOLD, None)
        cancelled = await held.async_cancel(result["id"])
        unknown = await held.async_cancel("nope")
        hass.states.set(PERSON, "home")
        await hass.async_block_till_done()
        return cancelled, unknown, sent

    cancelled, unknown, sent = run(scenario())
    assert cancelled is True and unknown is False
    assert sent == []


def test_held_messages_survive_a_restart():
    async def scenario():
        hass = new_hass()
        hass.states.set(PERSON, "not_home")
        first, _ = await start(hass)
        await first.async_submit(DATA, HOLD, "Will is home")
        await first.async_unload()
        second, sent = await start(hass)
        items = second.items()
        hass.states.set(PERSON, "home")
        await hass.async_block_till_done()
        return items, sent

    items, sent = run(scenario())
    assert [(i["message"], i["label"]) for i in items] == [
        ("Dinner is ready", "Will is home")
    ]
    assert sent == [DATA]


def test_ready_messages_are_sent_in_the_order_they_were_held():
    async def scenario():
        hass = new_hass()
        hass.states.set(PERSON, "not_home")
        held, sent = await start(hass)
        for word in ("one", "two", "three"):
            await held.async_submit({**DATA, "message": word}, HOLD, None)
        hass.states.set(PERSON, "home")
        await hass.async_block_till_done()
        return [d["message"] for d in sent]

    assert run(scenario()) == ["one", "two", "three"]


def test_an_expired_message_is_dropped_and_reported(monkeypatch):
    async def scenario():
        hass = new_hass()
        hass.states.set(PERSON, "not_home")

        async def create(hass_, data):
            return None

        hass.services.register("persistent_notification", "create", create)
        held, sent = await start(hass, {"hold_expiry": 2})
        result = await held.async_submit(DATA, HOLD, "Will is home")
        later = dt_util.utcnow() + timedelta(hours=3)
        monkeypatch.setattr(held_module.dt_util, "utcnow", lambda: later)
        await hass.fire_intervals()
        await hass.async_block_till_done()
        notes = [
            c[2]
            for c in hass.services.calls
            if c[:2] == ("persistent_notification", "create")
        ]
        return result, sent, held.items(), notes

    result, sent, items, notes = run(scenario())
    assert result["expires_at"] is not None
    assert sent == [] and items == []
    assert "Dinner is ready" in notes[0]["message"]


def test_without_an_expiry_setting_a_message_never_expires():
    async def scenario():
        hass = new_hass()
        hass.states.set(PERSON, "not_home")
        held, _ = await start(hass)
        return await held.async_submit(DATA, HOLD, None)

    assert run(scenario())["expires_at"] is None


def test_bad_hold_conditions_are_rejected_at_once():
    async def scenario():
        hass = new_hass()
        held, _ = await start(hass)
        await held.async_submit(DATA, [{"condition": "bogus"}], None)

    with pytest.raises(HomeAssistantError):
        run(scenario())


def test_subscribers_see_the_list_change():
    async def scenario():
        hass = new_hass()
        hass.states.set(PERSON, "not_home")
        held, _ = await start(hass)
        seen: list[list[str]] = []
        held.async_subscribe(lambda items: seen.append([i["message"] for i in items]))
        result = await held.async_submit(DATA, HOLD, None)
        await held.async_cancel(result["id"])
        return seen

    assert run(scenario()) == [["Dinner is ready"], []]
