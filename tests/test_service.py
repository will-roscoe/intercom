"""End-to-end intercom.broadcast: response, event, notification, escalation."""

from __future__ import annotations

import asyncio

from helpers import SONOS, STUDY, always_fails, new_hass, speaks_on
from homeassistant.core import ServiceCall
from homeassistant.exceptions import HomeAssistantError
from intercom import BROADCAST_SCHEMA, _async_broadcast
import pytest
import voluptuous as vol

OPTIONS = {"wait_timeout": 5, "start_timeout": 0.6, "max_attempts": 1}


def call(**data):
    return ServiceCall(BROADCAST_SCHEMA(data))


def broadcast(hass, **data):
    return asyncio.run(_async_broadcast(hass, OPTIONS, call(**data)))


def with_notify(hass):
    async def phone(hass_, data):
        return None

    async def create(hass_, data):
        return None

    hass.services.register("notify", "mobile_app_phone", phone)
    hass.services.register("persistent_notification", "create", create)
    return hass


def notifications(hass):
    return [
        call_[2]
        for call_ in hass.services.calls
        if call_[:2] == ("persistent_notification", "create")
    ]


def test_partial_broadcast_names_the_speaker_that_stayed_silent():
    hass = with_notify(new_hass())
    hass.services.register("tts", "speak", speaks_on(SONOS))

    result = broadcast(
        hass,
        message="Dinner is ready",
        players=[SONOS, STUDY],
        notify=["notify.mobile_app_phone"],
        volume=40,
    )

    assert result["delivered"] is True
    assert result["complete"] is False
    assert result["played"] == [SONOS]
    assert result["unverified"] == [STUDY]
    assert result["notified"] == ["notify.mobile_app_phone"]
    assert result["summary"].startswith("partial")

    assert hass.bus.fired[0][0] == "intercom_broadcast_result"
    posted = notifications(hass)
    assert len(posted) == 1
    assert "Study" in posted[0]["message"]
    assert "unverified" in posted[0]["message"]


def test_total_failure_gets_its_own_notification_id():
    """A later broadcast must not overwrite the record of one that failed."""
    hass = new_hass()
    hass.services.register("tts", "speak", always_fails())

    async def create(hass_, data):
        return None

    hass.services.register("persistent_notification", "create", create)

    result = broadcast(
        hass,
        message="Smoke detected",
        players=[SONOS],
        notify=["notify.mobile_app_phone"],
    )

    assert result["delivered"] is False
    assert result["summary"].startswith("FAILED")
    assert result["players"][0]["error"] == "The command to the player failed."
    assert (
        notifications(hass)[0]["notification_id"]
        == f"intercom_broadcast_{result['id']}"
    )


def test_speech_failure_never_stops_the_phones():
    hass = with_notify(new_hass())
    hass.services.register("tts", "speak", always_fails())

    result = broadcast(
        hass,
        message="Leave the building",
        players=[SONOS],
        notify=["notify.mobile_app_phone"],
        volume=90,
    )

    assert result["notified"] == ["notify.mobile_app_phone"]
    assert result["delivered"] is True
    assert result["failed"] == [SONOS]


def test_critical_raises_when_anything_was_missed():
    hass = with_notify(new_hass())
    hass.services.register("tts", "speak", speaks_on(SONOS))

    with pytest.raises(HomeAssistantError, match="1/2 speakers"):
        broadcast(
            hass,
            message="Smoke detected in the garage",
            players=[SONOS, STUDY],
            critical=True,
        )

    # The record still exists even though the call raised.
    assert len(hass.bus.fired) == 1
    assert len(notifications(hass)) == 1


def test_critical_returns_normally_when_everything_landed():
    hass = with_notify(new_hass())
    hass.services.register("tts", "speak", speaks_on(SONOS, STUDY))

    result = broadcast(
        hass,
        message="All clear",
        players=[SONOS, STUDY],
        notify=["notify.mobile_app_phone"],
        critical=True,
    )

    assert result["complete"] is True
    assert "2/2 speakers" in result["summary"]
    assert notifications(hass) == []


def test_a_broadcast_with_no_targets_is_an_error():
    hass = with_notify(new_hass())
    hass.services.register("tts", "speak", speaks_on(SONOS))

    with pytest.raises(HomeAssistantError, match="at least one player or notify"):
        broadcast(hass, message="hello")


def test_an_empty_message_is_an_error():
    hass = with_notify(new_hass())
    hass.services.register("tts", "speak", speaks_on(SONOS))

    with pytest.raises(HomeAssistantError, match="empty message"):
        broadcast(hass, message="   ", notify=["notify.mobile_app_phone"])


def test_duplicate_targets_collapse_and_a_dead_engine_is_named():
    hass = with_notify(new_hass())
    hass.services.register("tts", "speak", speaks_on(SONOS))
    hass.states.set("tts.piper", "unavailable")

    result = broadcast(
        hass,
        message="test",
        players=[SONOS, SONOS],
        notify=["notify.mobile_app_phone", "notify.mobile_app_phone"],
    )

    assert len(result["players"]) == 1
    assert len(result["notify"]) == 1
    assert result["engine_available"] is False
    assert "tts.piper is unavailable" in result["players"][0]["error"]


def test_the_result_is_stored_for_replay():
    hass = with_notify(new_hass())
    hass.services.register("tts", "speak", speaks_on(SONOS))

    result = broadcast(hass, message="Dinner is ready", players=[SONOS])

    assert hass.data["intercom_last_result"] == result


def test_schema_rejects_a_bad_entity_id():
    with pytest.raises(vol.Invalid):
        BROADCAST_SCHEMA({"message": "hi", "players": ["not-an-entity"]})
