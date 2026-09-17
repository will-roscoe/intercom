"""The generic check: watch the media player entity's own state."""

from __future__ import annotations

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event

from ..const import PLAYING_STATES, UNAVAILABLE_STATES
from .base import PlaybackVerifier, Verdict, wait_event


def looks_like_playback_start(
    old: State | None,
    new: State,
    baseline_content: str | None,
    was_playing: bool,
) -> bool:
    """Decide whether a state change is our announcement starting.

    Players differ wildly in what they report, so we accept any of three signals:
    a transition into a playing state, a switch to different media, or (for a
    player that was already playing music) a new media duration.
    """
    if new.state in PLAYING_STATES and (old is None or old.state not in PLAYING_STATES):
        return True

    if new.state in UNAVAILABLE_STATES:
        return False

    content = new.attributes.get("media_content_id")
    if content and content != baseline_content:
        return True

    if was_playing and new.state in PLAYING_STATES:
        duration = new.attributes.get("media_duration")
        previous = old.attributes.get("media_duration") if old else None
        if duration is not None and duration != previous:
            return True

    return False


class StateVerifier(PlaybackVerifier):
    """Watch one media player's state for evidence that our announcement played.

    Works for any player that changes state while it plays, and is the fallback
    for every player a more specific verifier does not claim. Armed *before*
    ``tts.speak`` is called so nothing is missed, including clips short enough to
    begin and end while the service call is still returning.
    """

    name = "state"
    priority = 0

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        super().__init__(hass, entity_id)
        self._unsub: CALLBACK_TYPE | None = None
        self._baseline_content: str | None = None
        self._was_playing = False
        self._playing_content: str | None = None
        self._state_started = False

    @classmethod
    def matches(cls, hass: HomeAssistant, entity_id: str) -> bool:
        return True

    async def async_arm(self) -> None:
        state = self.hass.states.get(self.entity_id)
        if state is not None:
            self._baseline_content = state.attributes.get("media_content_id")
            self._was_playing = state.state in PLAYING_STATES
        self._unsub = async_track_state_change_event(
            self.hass, [self.entity_id], self._handle
        )

    async def async_disarm(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def async_wait_started(self, timeout: float) -> Verdict:
        return await self._async_wait_evidence(timeout)

    async def async_wait_finished(self, timeout: float) -> bool:
        return await wait_event(self.finished, timeout)

    # --- hooks a subclass can override to weigh state evidence differently ---

    def _on_state_started(self) -> None:
        self._mark_started()

    def _on_state_finished(self) -> None:
        self._mark_finished()

    @callback
    def _handle(self, event: Event) -> None:
        new: State | None = event.data.get("new_state")
        if new is None:
            return

        if not self._state_started:
            if looks_like_playback_start(
                event.data.get("old_state"),
                new,
                self._baseline_content,
                self._was_playing,
            ):
                self._state_started = True
                self._playing_content = new.attributes.get("media_content_id")
                self._on_state_started()
            return

        # Finished = no longer playing, or moved on to different media (a player
        # that resumes the music it interrupted never leaves the playing state).
        if new.state not in PLAYING_STATES:
            self._on_state_finished()
            return
        content = new.attributes.get("media_content_id")
        if self._playing_content is not None and content not in (
            None,
            self._playing_content,
        ):
            self._on_state_finished()
