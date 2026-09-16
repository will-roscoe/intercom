"""The verifier API: how a player's playback check is chosen and what it says.

A new kind of speaker gets its own check by subclassing a verifier. These tests
pin down the contract such a subclass relies on, and that picking a check can
never leave a player without one.
"""

from __future__ import annotations

import asyncio

from helpers import SONOS, new_hass
from intercom.verifiers import (
    PlaybackVerifier,
    StateVerifier,
    Verdict,
    select_verifier,
)
import pytest


def run(coro):
    return asyncio.run(coro)


# --- registration -------------------------------------------------------------


def test_subclass_must_define_matches():
    with pytest.raises(TypeError, match="matches"):

        class NoMatch(PlaybackVerifier, register=False):
            name = "no_match"


def test_subclass_must_define_name():
    with pytest.raises(TypeError, match="name"):

        class NoName(PlaybackVerifier, register=False):
            @classmethod
            def matches(cls, hass, entity_id):
                return True


def test_unregistered_subclass_is_not_selected_by_default():
    class Hidden(PlaybackVerifier, register=False):
        name = "hidden"
        priority = 1000

        @classmethod
        def matches(cls, hass, entity_id):
            return True

    assert type(select_verifier(new_hass(), SONOS)) is not Hidden


# --- selection ----------------------------------------------------------------


def test_highest_priority_match_wins():
    class Low(PlaybackVerifier, register=False):
        name = "low"
        priority = 1

        @classmethod
        def matches(cls, hass, entity_id):
            return True

    class High(PlaybackVerifier, register=False):
        name = "high"
        priority = 9

        @classmethod
        def matches(cls, hass, entity_id):
            return True

    class Picky(PlaybackVerifier, register=False):
        name = "picky"
        priority = 99

        @classmethod
        def matches(cls, hass, entity_id):
            return entity_id == "media_player.other"

    chosen = select_verifier(new_hass(), SONOS, registry=[Low, Picky, High])
    assert type(chosen) is High
    assert chosen.entity_id == SONOS


def test_a_matcher_that_raises_is_skipped():
    class Broken(PlaybackVerifier, register=False):
        name = "broken"
        priority = 99

        @classmethod
        def matches(cls, hass, entity_id):
            raise RuntimeError("registry exploded")

    chosen = select_verifier(new_hass(), SONOS, registry=[Broken, StateVerifier])
    assert type(chosen) is StateVerifier


def test_unknown_player_falls_back_to_the_state_check():
    assert type(select_verifier(new_hass(), SONOS)) is StateVerifier


def test_empty_registry_falls_back_to_the_base():
    assert type(select_verifier(new_hass(), SONOS, registry=[])) is PlaybackVerifier


# --- behaviour ----------------------------------------------------------------


def test_base_assumes_the_clip_played():
    async def scenario():
        verifier = PlaybackVerifier(new_hass(), SONOS)
        await verifier.async_arm()
        started = await verifier.async_wait_started(0.1)
        finished = await verifier.async_wait_finished(0.1)
        await verifier.async_disarm()
        return started, finished

    assert run(scenario()) == (Verdict.ASSUMED, True)


def test_state_check_sees_playback_start_and_finish():
    async def scenario():
        hass = new_hass()
        verifier = StateVerifier(hass, SONOS)
        await verifier.async_arm()
        hass.states.set(SONOS, "playing", {"media_content_id": "clip"})
        started = await verifier.async_wait_started(0.5)
        hass.states.set(SONOS, "idle", {})
        finished = await verifier.async_wait_finished(0.5)
        await verifier.async_disarm()
        return started, finished

    assert run(scenario()) == (Verdict.STARTED, True)


def test_state_check_times_out_without_evidence():
    async def scenario():
        verifier = StateVerifier(new_hass(), SONOS)
        await verifier.async_arm()
        verdict = await verifier.async_wait_started(0.2)
        await verifier.async_disarm()
        return verdict

    assert run(scenario()) is Verdict.TIMEOUT


def test_disarmed_state_check_stops_listening():
    async def scenario():
        hass = new_hass()
        verifier = StateVerifier(hass, SONOS)
        await verifier.async_arm()
        await verifier.async_disarm()
        return hass.state_listeners.get(SONOS)

    assert not run(scenario())


def test_failure_is_reported_only_before_playback_started():
    async def scenario():
        verifier = StateVerifier(new_hass(), SONOS)
        verifier._mark_failed("nope")
        first = await verifier.async_wait_started(0.1)

        later = StateVerifier(new_hass(), SONOS)
        later._mark_started()
        later._mark_failed("too late")
        second = await later.async_wait_started(0.1)
        return first, verifier.failure, second, later.failure

    assert run(scenario()) == (Verdict.FAILED, "nope", Verdict.STARTED, None)
