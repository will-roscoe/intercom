"""The core promise: a player is only ever reported as played if it played.

Every test here is a regression guard on a way the integration could go back to
claiming success for audio nobody heard.
"""

from __future__ import annotations

import asyncio

from helpers import (
    ENGINE,
    SONOS,
    STUDY,
    always_fails,
    by_entity,
    make_request,
    new_hass,
    register_speak,
    speaks_on,
)
from intercom.broadcast import async_run_notify, async_run_players


def run(coro):
    return asyncio.run(coro)


def test_player_that_really_plays_is_reported_played():
    async def scenario():
        hass = new_hass()
        register_speak(hass, speaks_on(SONOS))
        return await async_run_players(hass, make_request(), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "played"
    assert outcome["verified"] is True
    assert outcome["attempts"] == 1
    assert outcome["error"] is None


def test_accepted_but_silent_is_unverified_not_played():
    """The original bug: tts.speak returns fine and no audio is ever produced."""

    async def scenario():
        hass = new_hass()
        register_speak(hass, speaks_on())  # accepts everything, plays nothing
        return await async_run_players(hass, make_request(), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "unverified"
    assert outcome["verified"] is False
    assert "no playback was detected" in outcome["error"]


def test_unverified_is_not_retried_unless_critical():
    """Retrying a clip that may have played would say a routine message twice."""

    async def scenario(critical):
        hass = new_hass()
        register_speak(hass, speaks_on())
        return await async_run_players(hass, make_request(critical=critical), True)

    assert run(scenario(False))[0]["attempts"] == 1
    assert run(scenario(True))[0]["attempts"] == 2


def test_sonos_command_failure_is_surfaced_and_retried():
    async def scenario():
        hass = new_hass()
        register_speak(hass, always_fails())
        return await async_run_players(hass, make_request(), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "failed"
    assert outcome["attempts"] == 2
    assert outcome["error"] == "The command to the player failed."


def test_failure_then_success_reports_played_and_clears_the_error():
    async def scenario():
        hass = new_hass()
        attempts = {"n": 0}
        playing = speaks_on(SONOS)

        async def speak(hass_, data):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("The command to the player failed.")
            return await playing(hass_, data)

        register_speak(hass, speak)
        return await async_run_players(hass, make_request(), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "played"
    assert outcome["attempts"] == 2
    assert outcome["error"] is None


def test_one_bad_speaker_does_not_hide_a_good_one():
    """A single rejecting player used to lose the whole broadcast."""

    async def scenario():
        hass = new_hass()

        async def speak(hass_, data):
            if data["media_player_entity_id"] == STUDY:
                raise RuntimeError("The command to the player failed.")
            return await speaks_on(SONOS)(hass_, data)

        register_speak(hass, speak)
        request = make_request(players=(SONOS, STUDY), max_attempts=1)
        return await async_run_players(hass, request, True)

    outcomes = by_entity(run(scenario()))
    assert outcomes[SONOS]["status"] == "played"
    assert outcomes[STUDY]["status"] == "failed"


def test_muted_speaker_is_unmuted_and_remuted():
    async def scenario():
        hass = new_hass(attributes={"is_volume_muted": True, "volume_level": 0.2})
        seen = {}

        playing = speaks_on(SONOS)

        async def speak(hass_, data):
            state = hass_.states.get(SONOS)
            seen["muted_while_speaking"] = state.attributes.get("is_volume_muted")
            return await playing(hass_, data)

        register_speak(hass, speak)
        outcomes = await async_run_players(hass, make_request(volume=60.0), True)
        return outcomes[0], seen, hass

    outcome, seen, hass = run(scenario())
    assert seen["muted_while_speaking"] is False
    assert outcome["status"] == "played"
    state = hass.states.get(SONOS)
    assert state.attributes["is_volume_muted"] is True
    assert state.attributes["volume_level"] == 0.2


def test_left_muted_is_reported_silent_not_played():
    """With unmuting disabled we know it was inaudible, so we must not claim it."""

    async def scenario():
        hass = new_hass(attributes={"is_volume_muted": True, "volume_level": 0.4})
        register_speak(hass, speaks_on(SONOS))
        request = make_request(unmute=False, max_attempts=1)
        return await async_run_players(hass, request, True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "silent"
    assert outcome["verified"] is False
    assert "muted" in outcome["error"]


def test_critical_unmutes_even_when_unmuting_is_disabled():
    """An emergency has to come out of the speaker somebody muted."""

    async def scenario():
        hass = new_hass(attributes={"is_volume_muted": True, "volume_level": 0.4})
        register_speak(hass, speaks_on(SONOS))
        request = make_request(unmute=False, critical=True, max_attempts=1)
        return await async_run_players(hass, request, True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "played"


def test_zero_volume_is_reported_silent():
    async def scenario():
        hass = new_hass(attributes={"volume_level": 0.0})
        register_speak(hass, speaks_on(SONOS))
        return await async_run_players(hass, make_request(max_attempts=1), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "silent"
    assert "volume is 0" in outcome["error"]


def test_critical_raises_a_zero_volume_speaker():
    async def scenario():
        hass = new_hass(attributes={"volume_level": 0.0})
        register_speak(hass, speaks_on(SONOS))
        request = make_request(critical=True, max_attempts=1)
        outcomes = await async_run_players(hass, request, True)
        return outcomes[0], hass

    outcome, hass = run(scenario())
    assert outcome["status"] == "played"
    levels = [
        call[2]["volume_level"]
        for call in hass.services.calls
        if call[:2] == ("media_player", "volume_set")
    ]
    assert levels and levels[0] > 0


def test_volume_failure_never_blocks_the_announcement():
    """A player that cannot set volume used to abort the entire broadcast."""

    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "idle", {"supported_features": 512})
        register_speak(hass, speaks_on(SONOS))
        return await async_run_players(hass, make_request(volume=60.0), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "played"
    assert any("volume" in warning for warning in outcome["warnings"])


def test_offline_and_unsupported_players_are_classified():
    async def scenario():
        hass = new_hass()
        hass.states.set("media_player.dead", "unavailable", {"supported_features": 512})
        hass.states.set("media_player.tv", "on", {"supported_features": 4})
        register_speak(hass, speaks_on())
        request = make_request(
            players=("media_player.dead", "media_player.tv", "media_player.ghost"),
            max_attempts=1,
        )
        return await async_run_players(hass, request, True)

    outcomes = by_entity(run(scenario()))
    assert outcomes["media_player.dead"]["status"] == "offline"
    assert outcomes["media_player.tv"]["status"] == "unsupported"
    assert outcomes["media_player.ghost"]["status"] == "offline"


def test_a_hanging_tts_speak_is_bounded():
    async def scenario():
        hass = new_hass()

        async def speak(hass_, data):
            await asyncio.sleep(30)

        register_speak(hass, speak)
        request = make_request(wait_timeout=0.2, max_attempts=1)
        return await async_run_players(hass, request, True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "failed"
    assert "did not return" in outcome["error"]


def test_unavailable_engine_names_itself_on_every_player():
    async def scenario():
        hass = new_hass()
        return await async_run_players(hass, make_request(), False)

    outcome = run(scenario())[0]
    assert outcome["status"] == "failed"
    assert f"{ENGINE} is unavailable" in outcome["error"]


def test_notify_handles_service_entity_and_missing_targets():
    async def scenario():
        hass = new_hass()
        sent = []

        async def classic(hass_, data):
            sent.append(("classic", data))

        async def send_message(hass_, data):
            sent.append((data["entity_id"], data))

        hass.services.register("notify", "mobile_app_phone", classic)
        hass.services.register("notify", "send_message", send_message)
        hass.states.set("notify.living_room", "2026-09-01T00:00:00+00:00", {})

        request = make_request(
            players=(),
            notify_targets=(
                "notify.mobile_app_phone",
                "notify.living_room",
                "notify.nope",
            ),
            max_attempts=1,
        )
        return await async_run_notify(hass, request), sent

    outcomes, sent = run(scenario())
    by_target = {outcome["target"]: outcome for outcome in outcomes}
    assert by_target["notify.mobile_app_phone"]["status"] == "sent"
    assert by_target["notify.living_room"]["status"] == "sent"
    assert by_target["notify.nope"]["status"] == "failed"
    assert by_target["notify.nope"]["error"] == "no such notify service or entity"
    assert any(target == "notify.living_room" for target, _ in sent)


def test_an_unexpected_crash_becomes_an_outcome():
    """A broadcast is the wrong place to discover an unhandled exception."""

    async def scenario():
        from intercom import broadcast

        async def boom(hass_, request, entity_id):
            raise ValueError("kaboom")

        original = broadcast._async_speak_on_player
        broadcast._async_speak_on_player = boom
        try:
            hass = new_hass()
            return await async_run_players(hass, make_request(), True)
        finally:
            broadcast._async_speak_on_player = original

    outcome = run(scenario())[0]
    assert outcome["status"] == "failed"
    assert outcome["error"] == "kaboom"
