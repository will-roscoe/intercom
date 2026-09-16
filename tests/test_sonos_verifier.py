"""Sonos announcements, which never change the media player's state.

HA's Sonos integration plays announcements through the speaker's audio-clip
API, so the only evidence that one played is the speaker's own clip status.
These tests cover finding the speaker behind an entity and reading that status.
"""

from __future__ import annotations

import asyncio

from helpers import (
    SONOS,
    SONOS_HOST,
    SONOS_UID,
    FakeClipSocket,
    new_hass,
    register_music_assistant,
    register_sonos,
    use_fake_speaker,
)
from homeassistant.helpers import entity_registry as er
from intercom.verifiers import Verdict
from intercom.verifiers.sonos import SonosClipVerifier, resolve_sonos_host

MA_SONOS = "media_player.sonos_speaker"
MA_PROXY = "media_player.sonos_speaker_2"


# --- finding the speaker --------------------------------------------------------


def test_native_sonos_entity_resolves_to_its_speaker():
    hass = new_hass()
    register_sonos(hass)
    assert resolve_sonos_host(hass, SONOS) == SONOS_HOST
    assert SonosClipVerifier.matches(hass, SONOS)


def test_music_assistant_sonos_player_resolves_through_the_player_id():
    hass = new_hass()
    register_sonos(hass)
    register_music_assistant(hass, MA_SONOS, SONOS_UID)
    assert resolve_sonos_host(hass, MA_SONOS) == SONOS_HOST


def test_music_assistant_proxy_resolves_through_the_wrapped_entity():
    hass = new_hass()
    register_sonos(hass)
    register_music_assistant(hass, MA_PROXY, SONOS)
    assert resolve_sonos_host(hass, MA_PROXY) == SONOS_HOST


def test_proxy_loops_do_not_recurse_forever():
    hass = new_hass()
    register_music_assistant(hass, "media_player.a", "media_player.b")
    register_music_assistant(hass, "media_player.b", "media_player.a")
    register_music_assistant(hass, "media_player.self", "media_player.self")
    assert resolve_sonos_host(hass, "media_player.a") is None
    assert resolve_sonos_host(hass, "media_player.self") is None


def test_music_assistant_player_without_a_native_sonos_device_does_not_match():
    hass = new_hass()
    register_music_assistant(hass, MA_SONOS, SONOS_UID)
    assert resolve_sonos_host(hass, MA_SONOS) is None
    assert not SonosClipVerifier.matches(hass, MA_SONOS)


def test_other_platforms_and_unknown_entities_do_not_match():
    hass = new_hass()
    er.async_get(hass).add("media_player.kitchen", "cast", "abc")
    assert resolve_sonos_host(hass, "media_player.kitchen") is None
    assert resolve_sonos_host(hass, "media_player.nope") is None


def test_sonos_device_without_an_address_does_not_match():
    hass = new_hass()
    register_sonos(hass, host=None)
    assert resolve_sonos_host(hass, SONOS) is None


# --- following the clip -----------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


async def armed(monkeypatch, socket, hass=None):
    hass = hass or new_hass()
    register_sonos(hass)
    use_fake_speaker(monkeypatch, socket)
    verifier = SonosClipVerifier(hass, SONOS)
    await verifier.async_arm()
    return hass, verifier


def test_a_clip_that_plays_is_started_then_finished(monkeypatch):
    async def scenario():
        socket = FakeClipSocket()
        _, verifier = await armed(monkeypatch, socket)
        assert verifier.channel_live
        socket.push_status(("ours", "ACTIVE"))
        started = await verifier.async_wait_started(1)
        socket.push_status(("ours", "DONE"))
        finished = await verifier.async_wait_finished(1)
        await verifier.async_disarm()
        return started, finished, socket.closed

    assert run(scenario()) == (Verdict.STARTED, True, True)


def test_subscribes_to_the_resolved_player(monkeypatch):
    async def scenario():
        socket = FakeClipSocket()
        _, verifier = await armed(monkeypatch, socket)
        await verifier.async_disarm()
        return socket.sent[-1][0]

    assert run(scenario()) == {
        "namespace": "audioClip:1",
        "command": "subscribe",
        "playerId": SONOS_UID,
    }


def test_clips_the_speaker_already_had_are_ignored(monkeypatch):
    async def scenario():
        socket = FakeClipSocket(existing=("old-1", "old-2"))
        _, verifier = await armed(monkeypatch, socket)
        socket.push_status(("old-1", "DONE"), ("old-2", "ACTIVE"))
        verdict = await verifier.async_wait_started(0.2)
        await verifier.async_disarm()
        return verdict

    assert run(scenario()) is Verdict.TIMEOUT


def test_a_clip_the_speaker_cannot_fetch_is_a_failure(monkeypatch):
    """The outage this was written for: the speaker could not reach HA."""

    async def scenario():
        socket = FakeClipSocket()
        _, verifier = await armed(monkeypatch, socket)
        socket.push_status(("ours", "ERROR"))
        verdict = await verifier.async_wait_started(1)
        await verifier.async_disarm()
        return verdict, verifier.failure

    verdict, failure = run(scenario())
    assert verdict is Verdict.FAILED
    assert "could not play the clip" in failure
    assert "address" in failure


def test_a_later_clip_from_someone_else_is_ignored(monkeypatch):
    async def scenario():
        socket = FakeClipSocket()
        _, verifier = await armed(monkeypatch, socket)
        socket.push_status(("ours", "INACTIVE"))
        socket.push_status(("theirs", "ERROR"), ("ours", "ACTIVE"))
        verdict = await verifier.async_wait_started(1)
        await verifier.async_disarm()
        return verdict

    assert run(scenario()) is Verdict.STARTED


def test_a_dismissed_clip_that_never_played_is_a_failure(monkeypatch):
    async def scenario():
        socket = FakeClipSocket()
        _, verifier = await armed(monkeypatch, socket)
        socket.push_status(("ours", "DISMISSED"))
        verdict = await verifier.async_wait_started(1)
        await verifier.async_disarm()
        return verdict

    assert run(scenario()) is Verdict.FAILED


def test_cannot_connect_falls_back_to_state(monkeypatch):
    async def scenario():
        hass, verifier = await armed(monkeypatch, OSError("connect refused"))
        live = verifier.channel_live
        hass.states.set(SONOS, "playing", {"media_content_id": "clip"})
        verdict = await verifier.async_wait_started(1)
        await verifier.async_disarm()
        return live, verdict

    assert run(scenario()) == (False, Verdict.STARTED)


def test_speaker_missing_from_its_household_falls_back_to_state(monkeypatch):
    async def scenario():
        socket = FakeClipSocket(advertised_host="10.0.0.1")
        _, verifier = await armed(monkeypatch, socket)
        return verifier.channel_live, socket.closed

    assert run(scenario()) == (False, True)


def test_speaker_without_audio_clips_falls_back_to_state(monkeypatch):
    async def scenario():
        socket = FakeClipSocket(capabilities=("PLAYBACK",))
        _, verifier = await armed(monkeypatch, socket)
        return verifier.channel_live

    assert run(scenario()) is False


def test_state_alone_is_not_trusted_while_the_speaker_is_talking(monkeypatch):
    """A proxy entity can look busy while the speaker is refusing the clip."""

    async def scenario():
        socket = FakeClipSocket()
        hass, verifier = await armed(monkeypatch, socket)
        hass.states.set(SONOS, "playing", {"media_content_id": "clip"})
        # Live, an unreachable clip is reported ~3s in, well inside the timeout.
        asyncio.get_running_loop().call_later(
            0.2, socket.push_status, ("ours", "ERROR")
        )
        verdict = await verifier.async_wait_started(1)
        await verifier.async_disarm()
        return verdict, verifier.started.is_set()

    assert run(scenario()) == (Verdict.FAILED, False)


def test_state_evidence_counts_once_the_speaker_hangs_up(monkeypatch):
    async def scenario():
        socket = FakeClipSocket()
        hass, verifier = await armed(monkeypatch, socket)
        hass.states.set(SONOS, "playing", {"media_content_id": "clip"})
        socket.hang_up()
        verdict = await verifier.async_wait_started(1)
        await verifier.async_disarm()
        return verdict

    assert run(scenario()) is Verdict.STARTED


def test_audio_that_bypasses_clips_is_judged_by_state(monkeypatch):
    """E.g. Music Assistant streaming to the Sonos over AirPlay: no clip at all."""

    async def scenario():
        socket = FakeClipSocket()
        hass, verifier = await armed(monkeypatch, socket)
        hass.states.set(SONOS, "playing", {"media_content_id": "stream"})
        started = await verifier.async_wait_started(0.3)
        hass.states.set(SONOS, "idle", {})
        finished = await verifier.async_wait_finished(1)
        await verifier.async_disarm()
        return started, finished

    assert run(scenario()) == (Verdict.STARTED, True)


def test_state_is_not_used_once_the_speaker_has_seen_our_clip(monkeypatch):
    async def scenario():
        socket = FakeClipSocket()
        hass, verifier = await armed(monkeypatch, socket)
        socket.push_status(("ours", "INACTIVE"))
        await asyncio.sleep(0.05)
        hass.states.set(SONOS, "playing", {"media_content_id": "clip"})
        verdict = await verifier.async_wait_started(0.3)
        await verifier.async_disarm()
        return verdict

    assert run(scenario()) is Verdict.TIMEOUT
