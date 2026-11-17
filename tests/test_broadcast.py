"""The core promise: a player is only ever reported as played if it played.

Every test here is a regression guard on a way the integration could go back to
claiming success for audio nobody heard.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from helpers import (
    ENGINE,
    SONOS,
    STUDY,
    FakeClipSocket,
    always_fails,
    by_entity,
    make_request,
    new_hass,
    register_sonos,
    register_speak,
    speaks_on,
    speaks_on_sonos,
    use_fake_speaker,
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

        async def boom(hass_, request, entity_id, **_):
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


# --- Sonos announcements (no state change while a clip plays) -------------------


def test_sonos_announcement_heard_is_played_not_unverified(monkeypatch):
    """Regression: a native Sonos announcement was reported FAILED when heard."""

    async def scenario():
        hass = new_hass()
        register_sonos(hass)
        socket = FakeClipSocket()
        use_fake_speaker(monkeypatch, socket)
        register_speak(hass, speaks_on_sonos(socket, "ACTIVE", "DONE"))
        outcomes = await async_run_players(hass, make_request(), True)
        return outcomes[0], socket.closed

    outcome, closed = run(scenario())
    assert outcome["status"] == "played"
    assert outcome["verified"] is True
    assert outcome["verified_by"] == "sonos_clip"
    assert outcome["detail"] is None
    assert closed


def test_sonos_clip_the_speaker_cannot_fetch_is_failed_and_retried(monkeypatch):
    async def scenario():
        hass = new_hass()
        register_sonos(hass)
        sockets: list[FakeClipSocket] = []

        from intercom.verifiers import sonos

        async def open_socket(hass_, host):
            sockets.append(FakeClipSocket())
            return sockets[-1]

        async def speak(hass_, data):
            socket = sockets[-1]
            asyncio.get_running_loop().call_later(
                0.02, socket.push_status, ("clip", "ERROR")
            )

        monkeypatch.setattr(sonos, "open_clip_socket", open_socket)
        register_speak(hass, speak)
        return await async_run_players(hass, make_request(), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "failed"
    assert outcome["attempts"] == 2
    assert outcome["verified_by"] == "sonos_clip"
    assert "could not play the clip" in outcome["error"]


def test_sonos_without_a_reachable_speaker_uses_the_state_check(monkeypatch):
    async def scenario():
        hass = new_hass()
        register_sonos(hass)
        use_fake_speaker(monkeypatch, OSError("no route to host"))
        register_speak(hass, speaks_on(SONOS))
        return await async_run_players(hass, make_request(), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "played"
    assert outcome["verified_by"] == "sonos_clip"


def test_non_sonos_player_is_judged_by_state():
    async def scenario():
        hass = new_hass()
        register_speak(hass, speaks_on(SONOS))
        return await async_run_players(hass, make_request(), True)

    assert run(scenario())[0]["verified_by"] == "state"


def test_verify_off_never_contacts_the_speaker(monkeypatch):
    async def scenario():
        hass = new_hass()
        register_sonos(hass)
        use_fake_speaker(monkeypatch, AssertionError("must not connect"))
        register_speak(hass, speaks_on())
        return await async_run_players(hass, make_request(verify=False), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "sent"
    assert outcome["verified_by"] is None
    assert "verification disabled" in outcome["detail"]


# --- alternates ----------------------------------------------------------------

LOUNGE = "media_player.lounge"


def add_lounge(hass, state: str = "idle") -> None:
    hass.states.set(
        LOUNGE, state, {"friendly_name": "Lounge", "supported_features": 512 | 4 | 8}
    )


def spoken_to(hass) -> list[str]:
    return [
        call_[2]["media_player_entity_id"]
        for call_ in hass.services.calls
        if call_[:2] == ("tts", "speak")
    ]


def fails_on(failing: str, *playing: str):
    """A tts.speak that raises for one player and really plays on others."""
    plays = speaks_on(*playing)

    async def speak(hass_, data):
        if data["media_player_entity_id"] == failing:
            raise RuntimeError("The command to the player failed.")
        return await plays(hass_, data)

    return speak


def test_offline_speaker_plays_on_its_alternate():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        register_speak(hass, speaks_on(STUDY))
        request = make_request(alternates={SONOS: (STUDY,)})
        return await async_run_players(hass, request, True)

    outcomes = run(scenario())
    rows = by_entity(outcomes)
    assert len(outcomes) == 2
    assert rows[SONOS]["status"] == "rerouted"
    assert rows[SONOS]["rerouted_to"] == STUDY
    assert rows[STUDY]["status"] == "played"
    assert rows[STUDY]["alternate_for"] == SONOS


def test_a_speaker_retries_before_switching_to_its_alternates():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        register_speak(hass, speaks_on(STUDY))
        request = make_request(alternates={SONOS: (STUDY,)}, max_attempts=2)
        return await async_run_players(hass, request, True)

    sonos = by_entity(run(scenario()))[SONOS]
    assert sonos["attempts"] == 2
    assert sonos["rerouted_to"] == STUDY


def test_a_speaker_back_within_its_retry_plays_itself():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        register_speak(hass, speaks_on(SONOS, STUDY))

        async def comes_back():
            await asyncio.sleep(0.5)
            hass.states.set(SONOS, "idle", {"supported_features": 512 | 4 | 8})

        asyncio.ensure_future(comes_back())
        request = make_request(alternates={SONOS: (STUDY,)}, max_attempts=2)
        return hass, await async_run_players(hass, request, True)

    hass, outcomes = run(scenario())
    assert by_entity(outcomes)[SONOS]["status"] == "played"
    assert STUDY not in spoken_to(hass)


def test_offline_without_alternates_still_retries():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        register_speak(hass, speaks_on())
        return await async_run_players(hass, make_request(max_attempts=2), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "offline"
    assert outcome["attempts"] == 2


def test_unsupported_speaker_falls_back():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "on", {"supported_features": 4})
        register_speak(hass, speaks_on(STUDY))
        request = make_request(alternates={SONOS: (STUDY,)})
        return await async_run_players(hass, request, True)

    sonos = by_entity(run(scenario()))[SONOS]
    assert sonos["status"] == "rerouted"
    assert sonos["rerouted_to"] == STUDY


def test_the_chain_skips_offline_and_missing_alternates():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        add_lounge(hass, "unavailable")
        register_speak(hass, speaks_on(STUDY))
        request = make_request(
            alternates={SONOS: ("media_player.ghost", LOUNGE, STUDY)}
        )
        return await async_run_players(hass, request, True)

    outcomes = run(scenario())
    rows = by_entity(outcomes)
    assert rows[SONOS]["rerouted_to"] == STUDY
    assert set(rows) == {SONOS, STUDY}
    assert "media_player.ghost" in rows[SONOS]["detail"]
    assert "Lounge" in rows[SONOS]["detail"]


def test_every_alternate_offline_leaves_the_speaker_offline():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        hass.states.set(STUDY, "unavailable", {"friendly_name": "Study"})
        add_lounge(hass, "unavailable")
        register_speak(hass, speaks_on())
        request = make_request(alternates={SONOS: (STUDY, LOUNGE)})
        return await async_run_players(hass, request, True)

    outcomes = run(scenario())
    sonos = outcomes[0]
    assert len(outcomes) == 1
    assert sonos["status"] == "offline"
    assert sonos["rerouted_to"] is None
    assert "Study" in sonos["error"] and "Lounge" in sonos["error"]


def test_a_failed_alternate_ends_the_chain():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        add_lounge(hass)
        register_speak(hass, fails_on(STUDY, LOUNGE))
        request = make_request(alternates={SONOS: (STUDY, LOUNGE)})
        return hass, await async_run_players(hass, request, True)

    hass, outcomes = run(scenario())
    rows = by_entity(outcomes)
    assert rows[SONOS]["status"] == "offline"
    assert rows[STUDY]["status"] == "failed"
    assert rows[STUDY]["alternate_for"] == SONOS
    assert LOUNGE not in spoken_to(hass)


def test_a_played_speaker_never_tries_its_alternates():
    async def scenario():
        hass = new_hass()
        register_speak(hass, speaks_on(SONOS, STUDY))
        request = make_request(alternates={SONOS: (STUDY,)})
        return hass, await async_run_players(hass, request, True)

    hass, outcomes = run(scenario())
    assert len(outcomes) == 1
    assert STUDY not in spoken_to(hass)


def test_a_failed_speaker_never_tries_its_alternates():
    async def scenario():
        hass = new_hass()
        register_speak(hass, fails_on(SONOS, STUDY))
        request = make_request(alternates={SONOS: (STUDY,)}, max_attempts=1)
        return hass, await async_run_players(hass, request, True)

    hass, outcomes = run(scenario())
    assert outcomes[0]["status"] == "failed"
    assert STUDY not in spoken_to(hass)


def test_an_alternate_that_is_also_requested_plays_once():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        register_speak(hass, speaks_on(STUDY))
        request = make_request(players=(SONOS, STUDY), alternates={SONOS: (STUDY,)})
        return hass, await async_run_players(hass, request, True)

    hass, outcomes = run(scenario())
    rows = by_entity(outcomes)
    assert spoken_to(hass).count(STUDY) == 1
    assert len(outcomes) == 2
    assert rows[SONOS]["rerouted_to"] == STUDY
    assert rows[STUDY]["alternate_for"] is None


def test_two_speakers_sharing_an_alternate_use_it_once():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        hass.states.set(STUDY, "unavailable", {"supported_features": 512})
        add_lounge(hass)
        register_speak(hass, speaks_on(LOUNGE))
        request = make_request(
            players=(SONOS, STUDY), alternates={SONOS: (LOUNGE,), STUDY: (LOUNGE,)}
        )
        return hass, await async_run_players(hass, request, True)

    hass, outcomes = run(scenario())
    rows = by_entity(outcomes)
    assert spoken_to(hass).count(LOUNGE) == 1
    assert len(outcomes) == 3
    assert rows[SONOS]["rerouted_to"] == LOUNGE
    assert rows[STUDY]["rerouted_to"] == LOUNGE


def test_mutual_alternates_both_offline_finish():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        hass.states.set(STUDY, "unavailable", {"supported_features": 512})
        register_speak(hass, speaks_on())
        request = make_request(
            players=(SONOS, STUDY), alternates={SONOS: (STUDY,), STUDY: (SONOS,)}
        )
        async with asyncio.timeout(3):
            return await async_run_players(hass, request, True)

    outcomes = run(scenario())
    assert [o["status"] for o in outcomes] == ["offline", "offline"]


def test_dead_engine_never_tries_alternates():
    async def scenario():
        hass = new_hass()
        hass.states.set(SONOS, "unavailable", {"supported_features": 512})
        register_speak(hass, speaks_on(STUDY))
        request = make_request(alternates={SONOS: (STUDY,)})
        return hass, await async_run_players(hass, request, False)

    hass, outcomes = run(scenario())
    assert [o["status"] for o in outcomes] == ["failed"]
    assert spoken_to(hass) == []


def test_cancelling_a_broadcast_stops_its_speakers():
    """An automation restarted mid-broadcast must not leave speakers talking."""

    async def scenario():
        hass = new_hass()
        register_speak(hass, speaks_on(SONOS))
        task = asyncio.ensure_future(async_run_players(hass, make_request(), True))
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.3)
        return hass

    assert spoken_to(run(scenario())) == []


# --- chime ---------------------------------------------------------------------

CHIME = "/intercom/chime.mp3"


def register_play_media(hass, fail: bool = False) -> None:
    async def play_media(hass_, data):
        if fail:
            raise RuntimeError("could not fetch the chime")

    hass.services.register("media_player", "play_media", play_media)


def sequence(hass, entity_id: str) -> list[str]:
    """The chime and speech calls made for one player, in order."""
    wanted = {("media_player", "play_media"): "chime", ("tts", "speak"): "speak"}
    return [
        wanted[call_[:2]]
        for call_ in hass.services.calls
        if call_[:2] in wanted
        and entity_id
        in (call_[2].get("entity_id"), call_[2].get("media_player_entity_id"))
    ]


def chime_request(**overrides):
    return make_request(chime=frozenset({SONOS}), chime_media=CHIME, **overrides)


def test_the_chime_plays_before_the_message():
    async def scenario():
        hass = new_hass()
        register_play_media(hass)
        register_speak(hass, speaks_on(SONOS))
        return hass, await async_run_players(hass, chime_request(), True)

    hass, outcomes = run(scenario())
    assert outcomes[0]["status"] == "played"
    assert sequence(hass, SONOS) == ["chime", "speak"]
    chime = next(
        c[2] for c in hass.services.calls if c[:2] == ("media_player", "play_media")
    )
    assert chime == {
        "entity_id": SONOS,
        "media_content_id": CHIME,
        "media_content_type": "music",
        "announce": True,
    }


def test_no_chime_on_a_speaker_not_chosen_for_it():
    async def scenario():
        hass = new_hass()
        register_play_media(hass)
        register_speak(hass, speaks_on(SONOS, STUDY))
        request = make_request(
            players=(SONOS, STUDY), chime=frozenset({SONOS}), chime_media=CHIME
        )
        return hass, await async_run_players(hass, request, True)

    hass, _ = run(scenario())
    assert sequence(hass, STUDY) == ["speak"]


def test_a_failed_chime_never_blocks_the_message():
    async def scenario():
        hass = new_hass()
        register_play_media(hass, fail=True)
        register_speak(hass, speaks_on(SONOS))
        return await async_run_players(hass, chime_request(), True)

    outcome = run(scenario())[0]
    assert outcome["status"] == "played"
    assert any("chime" in warning for warning in outcome["warnings"])


def test_the_chime_plays_once_across_retries():
    async def scenario():
        hass = new_hass()
        register_play_media(hass)
        plays = speaks_on(SONOS)
        tries = []

        async def flaky(hass_, data):
            tries.append(1)
            if len(tries) == 1:
                raise RuntimeError("The command to the player failed.")
            return await plays(hass_, data)

        register_speak(hass, flaky)
        return hass, await async_run_players(hass, chime_request(), True)

    hass, outcomes = run(scenario())
    assert outcomes[0]["status"] == "played"
    assert sequence(hass, SONOS) == ["chime", "speak", "speak"]


# --- speech made before any speaker plays ---------------------------------------
#
# A TTS engine like Piper on a small host takes 10-20 s per sentence. Made on
# demand, a speaker waits mid-announcement: Music Assistant stutters, the chime
# is followed by a long silence, and the speech can start after the playback
# check has given up.


def test_the_message_is_made_before_any_speaker_is_asked():
    async def scenario():
        hass = new_hass()
        register_play_media(hass)
        register_speak(hass, speaks_on(SONOS))
        return hass, await async_run_players(hass, chime_request(), True)

    hass, outcomes = run(scenario())
    assert outcomes[0]["status"] == "played"
    assert hass.data["tts_made"] == [
        {
            "engine": ENGINE,
            "message": "Dinner is ready",
            "cache": True,
            "calls_before": 0,
        }
    ]


def test_the_message_is_made_once_for_every_speaker():
    async def scenario():
        hass = new_hass()
        register_speak(hass, speaks_on(SONOS, STUDY))
        request = make_request(players=(SONOS, STUDY))
        return hass, await async_run_players(hass, request, True)

    hass, outcomes = run(scenario())
    assert [o["status"] for o in outcomes] == ["played", "played"]
    assert len(hass.data["tts_made"]) == 1


def test_a_message_that_cannot_be_made_early_is_still_spoken():
    async def scenario():
        hass = new_hass()

        async def broken():
            raise RuntimeError("engine hiccup")

        hass.data["tts_make"] = broken
        register_speak(hass, speaks_on(SONOS))
        return hass, await async_run_players(hass, make_request(), True)

    hass, outcomes = run(scenario())
    assert outcomes[0]["status"] == "played"
    assert spoken_to(hass) == [SONOS]


def test_making_the_message_early_is_bounded():
    async def scenario():
        hass = new_hass()

        async def slow():
            await asyncio.sleep(10)

        hass.data["tts_make"] = slow
        register_speak(hass, speaks_on(SONOS))
        return await async_run_players(hass, make_request(wait_timeout=0.3), True)

    t0 = time.monotonic()
    outcomes = run(scenario())
    assert outcomes[0]["status"] == "played"
    assert time.monotonic() - t0 < 3


def test_nothing_is_made_when_the_engine_is_unavailable():
    async def scenario():
        hass = new_hass()
        return hass, await async_run_players(hass, make_request(), False)

    hass, _ = run(scenario())
    assert "tts_made" not in hass.data


# --- one message at a time per speaker ------------------------------------------


def timed_speak(log: list):
    """A tts.speak that takes 0.1 s and logs when each message starts and ends."""

    async def speak(hass_, data):
        log.append(("start", data["media_player_entity_id"], data["message"]))
        await asyncio.sleep(0.1)
        log.append(("end", data["media_player_entity_id"], data["message"]))

    return speak


def test_two_broadcasts_to_one_speaker_take_turns():
    async def scenario():
        hass = new_hass()
        log: list = []
        register_speak(hass, timed_speak(log))
        first = make_request(message="one", verify=False, max_attempts=1)
        second = make_request(message="two", verify=False, max_attempts=1)
        await asyncio.gather(
            async_run_players(hass, first, True), async_run_players(hass, second, True)
        )
        return log

    log = run(scenario())
    assert [entry[0] for entry in log] == ["start", "end", "start", "end"]


def test_broadcasts_to_different_speakers_still_overlap():
    async def scenario():
        hass = new_hass()
        log: list = []
        register_speak(hass, timed_speak(log))
        to_sonos = make_request(message="one", verify=False, max_attempts=1)
        to_study = make_request(
            message="two", players=(STUDY,), verify=False, max_attempts=1
        )
        await asyncio.gather(
            async_run_players(hass, to_sonos, True),
            async_run_players(hass, to_study, True),
        )
        return log

    log = run(scenario())
    assert [entry[0] for entry in log] == ["start", "start", "end", "end"]
