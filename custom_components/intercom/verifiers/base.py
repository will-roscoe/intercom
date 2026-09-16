"""The playback verifier contract and the registry that picks one per player.

A verifier answers one question for one media player: did the announcement we
just asked for actually start? Players differ wildly in what evidence they
expose, so each kind gets its own subclass and the most specific one that
matches a player is used.

Adding a check for a new kind of player::

    class MyVerifier(StateVerifier):
        name = "my_player"
        priority = 50

        @classmethod
        def matches(cls, hass, entity_id) -> bool:
            ...  # cheap, synchronous, no I/O

        async def async_arm(self) -> None:
            await super().async_arm()
            ...  # start collecting evidence; call self._mark_started() etc.

Defining the subclass registers it. ``name`` and ``matches`` are required;
``priority`` decides between several matches (highest wins). Subclass
``StateVerifier`` rather than this base to keep entity-state evidence as a
fallback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from enum import StrEnum
import logging
from typing import ClassVar

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__package__)


class Verdict(StrEnum):
    """What a verifier concluded about the start of playback."""

    # Positive evidence that the clip began playing.
    STARTED = "started"
    # The player itself reported that it could not play the clip.
    FAILED = "failed"
    # No evidence either way within the allotted time.
    TIMEOUT = "timeout"
    # No check was made; the clip is assumed to have played.
    ASSUMED = "assumed"


_REGISTRY: list[type[PlaybackVerifier]] = []


async def wait_event(event: asyncio.Event, timeout: float) -> bool:
    """Wait for an asyncio event, returning False instead of raising on timeout."""
    try:
        async with asyncio.timeout(timeout):
            await event.wait()
    except TimeoutError:
        return False
    return True


class PlaybackVerifier:
    """The base verifier: it checks nothing and assumes the clip played.

    Used as-is when the caller turns verification off, and as the last resort
    when no registered verifier matches.
    """

    name: ClassVar[str] = "none"
    priority: ClassVar[int] = 0

    def __init_subclass__(cls, register: bool = True, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        for required in ("name", "matches"):
            if required not in cls.__dict__:
                raise TypeError(
                    f"{cls.__name__} must define {required!r} to be a playback verifier"
                )
        if register:
            _REGISTRY.append(cls)
            _REGISTRY.sort(key=lambda verifier: verifier.priority, reverse=True)

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.failed = asyncio.Event()
        # Why the player could not play the clip, when it told us.
        self.failure: str | None = None

    @classmethod
    def matches(cls, hass: HomeAssistant, entity_id: str) -> bool:
        """Whether this verifier knows how to check ``entity_id``."""
        return True

    async def async_arm(self) -> None:
        """Start collecting evidence. Called before the player is asked to speak."""

    async def async_disarm(self) -> None:
        """Stop collecting evidence and release anything that was opened."""

    async def async_wait_started(self, timeout: float) -> Verdict:
        """Wait for evidence that playback started, or for a reported failure."""
        return Verdict.ASSUMED

    async def async_wait_finished(self, timeout: float) -> bool:
        """Return True once playback has ended, False on timeout."""
        return True

    # --- helpers for subclasses ---------------------------------------------

    def _mark_started(self) -> None:
        self.started.set()

    def _mark_finished(self) -> None:
        self.started.set()
        self.finished.set()

    def _mark_failed(self, reason: str) -> None:
        # Once audio has started, a later error cannot un-play it.
        if self.started.is_set() or self.failed.is_set():
            return
        self.failure = reason
        self.failed.set()

    async def _async_wait_evidence(self, timeout: float) -> Verdict:
        if not (self.started.is_set() or self.failed.is_set()):
            waiters = [
                asyncio.ensure_future(self.started.wait()),
                asyncio.ensure_future(self.failed.wait()),
            ]
            try:
                await asyncio.wait(
                    waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for waiter in waiters:
                    waiter.cancel()
        if self.started.is_set():
            return Verdict.STARTED
        if self.failed.is_set():
            return Verdict.FAILED
        return Verdict.TIMEOUT


def select_verifier(
    hass: HomeAssistant,
    entity_id: str,
    registry: Iterable[type[PlaybackVerifier]] | None = None,
) -> PlaybackVerifier:
    """Build the most specific verifier that matches ``entity_id``."""
    candidates = _REGISTRY if registry is None else registry
    for verifier in sorted(candidates, key=lambda v: v.priority, reverse=True):
        try:
            if verifier.matches(hass, entity_id):
                return verifier(hass, entity_id)
        except Exception:  # noqa: BLE001 - a broken matcher must not stop a broadcast
            _LOGGER.debug(
                "intercom: %s could not check %s",
                verifier.name,
                entity_id,
                exc_info=True,
            )
    return PlaybackVerifier(hass, entity_id)
