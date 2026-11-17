"""End-to-end intercom.broadcast: response, event, notification, escalation."""

from __future__ import annotations

import asyncio

from helpers import SONOS, STUDY, always_fails, new_hass, speaks_on
from homeassistant.exceptions import HomeAssistantError
from intercom import BROADCAST_SCHEMA, _async_broadcast, _build_request
import pytest
import voluptuous as vol

OPTIONS = {"wait_timeout": 5, "start_timeout": 0.6, "max_attempts": 1}


def call(**data):
    return dict(BROADCAST_SCHEMA(data))


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


# --- alternates ----------------------------------------------------------------

LOUNGE = "media_player.lounge"


def test_alternates_are_ordered_deduplicated_and_never_self():
    request = _build_request(
        OPTIONS,
        call(
            message="hi",
            players=[SONOS],
            alternates={SONOS: [STUDY, STUDY, SONOS, LOUNGE]},
        ),
    )
    assert request.alternates == {SONOS: (STUDY, LOUNGE)}


def test_alternates_for_a_speaker_not_in_players_is_an_error():
    with pytest.raises(HomeAssistantError, match=LOUNGE):
        _build_request(
            OPTIONS,
            call(message="hi", players=[SONOS], alternates={LOUNGE: [STUDY]}),
        )


def test_no_alternates_means_an_empty_mapping():
    request = _build_request(OPTIONS, call(message="hi", players=[SONOS]))
    assert request.alternates == {}


def test_schema_accepts_alternates_as_a_list_or_a_single_entity():
    data = BROADCAST_SCHEMA(
        {"message": "hi", "players": [SONOS], "alternates": {SONOS: STUDY}}
    )
    assert data["alternates"] == {SONOS: [STUDY]}


def test_schema_rejects_a_bad_alternate_entity_id():
    with pytest.raises(vol.Invalid):
        BROADCAST_SCHEMA(
            {"message": "hi", "players": [SONOS], "alternates": {SONOS: ["nope"]}}
        )


def sonos_offline(hass):
    hass.states.set(
        SONOS, "unavailable", {"friendly_name": "Sonos", "supported_features": 512}
    )
    return hass


def test_a_rerouted_broadcast_is_complete_and_says_where_it_played():
    hass = sonos_offline(with_notify(new_hass()))
    hass.services.register("tts", "speak", speaks_on(STUDY))

    result = broadcast(hass, message="hi", players=[SONOS], alternates={SONOS: STUDY})

    assert result["complete"] is True
    assert result["delivered"] is True
    assert result["rerouted"] == [SONOS]
    assert result["played"] == [STUDY]
    assert result["offline"] == []
    assert result["summary"] == "played on 1/1 speakers (Sonos via Study)"
    assert notifications(hass) == []


def test_critical_does_not_raise_when_reroutes_covered_everything():
    hass = sonos_offline(with_notify(new_hass()))
    hass.services.register("tts", "speak", speaks_on(STUDY))

    result = broadcast(
        hass, message="hi", players=[SONOS], alternates={SONOS: STUDY}, critical=True
    )

    assert result["complete"] is True


def test_a_failed_reroute_is_reported_as_a_problem():
    hass = sonos_offline(with_notify(new_hass()))
    hass.services.register("tts", "speak", always_fails())

    result = broadcast(hass, message="hi", players=[SONOS], alternates={SONOS: STUDY})

    assert result["complete"] is False
    assert result["offline"] == [SONOS]
    assert result["failed"] == [STUDY]
    assert result["rerouted"] == []
    assert "Study" in notifications(hass)[0]["message"]


def test_the_notification_says_which_speaker_an_alternate_stood_in_for():
    hass = sonos_offline(with_notify(new_hass()))
    hass.services.register("tts", "speak", always_fails())

    broadcast(hass, message="hi", players=[SONOS], alternates={SONOS: STUDY})

    assert (
        "- Study (media_player.study): failed, alternate for Sonos"
        in (notifications(hass)[0]["message"])
    )


# --- chime ---------------------------------------------------------------------


def test_chime_true_covers_every_speaker_and_their_alternates():
    request = _build_request(
        OPTIONS,
        call(message="hi", players=[SONOS], alternates={SONOS: [STUDY]}, chime=True),
    )
    assert request.chime == {SONOS, STUDY}
    assert request.chime_media == "/intercom/chime.mp3"
    assert request.chime_seconds == 1.5


def test_chime_for_some_speakers_includes_only_their_alternates():
    request = _build_request(
        OPTIONS,
        call(
            message="hi",
            players=[SONOS, STUDY],
            alternates={SONOS: [LOUNGE]},
            chime=[SONOS],
        ),
    )
    assert request.chime == {SONOS, LOUNGE}


def test_chime_for_a_speaker_not_in_players_is_an_error():
    with pytest.raises(HomeAssistantError, match=LOUNGE):
        _build_request(OPTIONS, call(message="hi", players=[SONOS], chime=[LOUNGE]))


def test_the_integration_default_decides_when_the_call_says_nothing():
    off = _build_request(OPTIONS, call(message="hi", players=[SONOS]))
    on = _build_request(
        {**OPTIONS, "chime": True, "chime_media": "media-source://x/ding.mp3"},
        call(message="hi", players=[SONOS]),
    )
    assert off.chime == frozenset()
    assert on.chime == {SONOS}
    assert on.chime_media == "media-source://x/ding.mp3"


def test_chime_false_overrides_an_integration_default_of_on():
    request = _build_request(
        {**OPTIONS, "chime": True}, call(message="hi", players=[SONOS], chime=False)
    )
    assert request.chime == frozenset()


# --- held messages -------------------------------------------------------------

PERSON = "person.will"
HOLD = [{"condition": "state", "entity_id": PERSON, "state": "home"}]


def held_service(hass, **data):
    async def scenario():
        from homeassistant.helpers.storage import Store
        from intercom import DATA_HELD, _async_service
        from intercom.held import HeldMessages

        Store.DISK.clear()
        held = HeldMessages(
            hass, lambda: OPTIONS, lambda d: _async_broadcast(hass, OPTIONS, d)
        )
        await held.async_load()
        hass.data[DATA_HELD] = held
        result = await _async_service(hass, OPTIONS, call(**data))
        return result, Store.DISK.get("intercom.held")

    return asyncio.run(scenario())


def test_a_held_call_is_kept_without_speaking():
    hass = new_hass()
    hass.states.set(PERSON, "not_home")
    hass.services.register("tts", "speak", speaks_on(SONOS))

    result, saved = held_service(
        hass, message="hi", players=[SONOS], hold=HOLD, hold_label="Will is home"
    )

    assert result["held"] is True and result["summary"] == "held until Will is home"
    assert not [c for c in hass.services.calls if c[:2] == ("tts", "speak")]
    assert "hold" not in saved["items"][0]["data"]
    assert "hold_label" not in saved["items"][0]["data"]


def test_a_held_call_that_could_never_play_is_rejected_at_once():
    hass = new_hass()
    hass.states.set(PERSON, "not_home")
    with pytest.raises(HomeAssistantError, match="Nothing to broadcast"):
        held_service(hass, message="hi", hold=HOLD)


def test_a_call_whose_hold_is_already_met_broadcasts_now():
    hass = new_hass()
    hass.states.set(PERSON, "home")
    hass.services.register("tts", "speak", speaks_on(SONOS))

    result, _ = held_service(hass, message="hi", players=[SONOS], hold=HOLD)

    assert result["played"] == [SONOS]
