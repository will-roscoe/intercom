"""The broadcast engine behind ``intercom.broadcast``.

The design goal here is that the caller is never lied to. Every target gets its
own isolated attempt with its own outcome, and a player is only reported as
``played`` when Home Assistant actually saw evidence of audio starting — not
merely because ``tts.speak`` returned without raising.

Three things make that possible:

* one ``tts.speak`` call per player, so a single speaker that rejects the
  command (Sonos: "The command to the player failed.") cannot mask the rest;
* a state-change *listener* armed before the call, so short clips that start and
  finish between two polls are still observed;
* explicit statuses — ``played`` / ``unverified`` / ``failed`` / ``offline`` /
  ``unsupported`` — instead of one boolean.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any
from uuid import uuid4

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    DELIVERED_STATUSES,
    FEATURE_PLAY_MEDIA,
    FEATURE_VOLUME_MUTE,
    FEATURE_VOLUME_SET,
    PLAYING_STATES,
    STATUS_FAILED,
    STATUS_OFFLINE,
    STATUS_PLAYED,
    STATUS_SENT,
    STATUS_UNSUPPORTED,
    STATUS_UNVERIFIED,
    UNAVAILABLE_STATES,
    VOLUME_SETTLE_SECONDS,
)

_LOGGER = logging.getLogger(__package__)

# Pause between attempts, long enough for a flapping speaker to settle.
RETRY_DELAY_SECONDS = 1.5


@dataclass(frozen=True)
class BroadcastRequest:
    """Everything one broadcast needs, resolved from the call + entry options."""

    message: str
    title: str
    engine: str
    players: tuple[str, ...]
    notify_targets: tuple[str, ...]
    volume: float | None
    restore_volume: bool
    unmute: bool
    verify: bool
    critical: bool
    max_attempts: int
    wait_timeout: float
    start_timeout: float


# --- playback verification ----------------------------------------------------


def _looks_like_playback_start(
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


class _PlaybackWatch:
    """Watch one media player for evidence that our announcement played.

    Armed *before* ``tts.speak`` is called so nothing is missed, including clips
    short enough to begin and end while the service call is still returning.
    """

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._unsub: CALLBACK_TYPE | None = None
        self._baseline_content: str | None = None
        self._was_playing = False
        self._playing_content: str | None = None
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    def arm(self) -> None:
        """Start listening. Call this before asking the player to speak."""
        state = self._hass.states.get(self._entity_id)
        if state is not None:
            self._baseline_content = state.attributes.get("media_content_id")
            self._was_playing = state.state in PLAYING_STATES
        self._unsub = async_track_state_change_event(
            self._hass, [self._entity_id], self._handle
        )

    def disarm(self) -> None:
        """Stop listening."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _handle(self, event: Event) -> None:
        new: State | None = event.data.get("new_state")
        if new is None:
            return

        if not self.started.is_set():
            if _looks_like_playback_start(
                event.data.get("old_state"),
                new,
                self._baseline_content,
                self._was_playing,
            ):
                self._playing_content = new.attributes.get("media_content_id")
                self.started.set()
            return

        # Finished = no longer playing, or moved on to different media (a player
        # that resumes the music it interrupted never leaves the playing state).
        if new.state not in PLAYING_STATES:
            self.finished.set()
            return
        content = new.attributes.get("media_content_id")
        if self._playing_content is not None and content not in (
            None,
            self._playing_content,
        ):
            self.finished.set()

    async def wait_started(self, timeout: float) -> bool:
        """Return True once playback evidence is seen, False on timeout."""
        return await _wait_event(self.started, timeout)

    async def wait_finished(self, timeout: float) -> bool:
        """Return True once playback has ended, False on timeout."""
        return await _wait_event(self.finished, timeout)


async def _wait_event(event: asyncio.Event, timeout: float) -> bool:
    """Wait for an asyncio event, returning False instead of raising on timeout."""
    try:
        async with asyncio.timeout(timeout):
            await event.wait()
    except TimeoutError:
        return False
    return True


# --- speaking -----------------------------------------------------------------


def _error_text(err: BaseException) -> str:
    """A short, human-readable description of a failure."""
    return str(err).strip() or type(err).__name__


def _friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is not None:
        name = state.attributes.get("friendly_name")
        if name:
            return str(name)
    return entity_id.split(".", 1)[-1].replace("_", " ")


def _new_player_outcome(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "name": _friendly_name(hass, entity_id),
        "status": STATUS_OFFLINE,
        "verified": False,
        "attempts": 0,
        "error": None,
        "detail": None,
        "warnings": [],
    }


async def _async_player_service(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any],
    outcome: dict[str, Any],
) -> bool:
    """Call a media_player service, recording rather than raising on failure.

    Volume and mute handling must never be able to abort the announcement — the
    message matters more than the level it is played at.
    """
    try:
        await hass.services.async_call("media_player", service, data, blocking=True)
    except Exception as err:  # noqa: BLE001 - best-effort, never fatal
        outcome["warnings"].append(f"media_player.{service} failed: {_error_text(err)}")
        return False
    return True


async def _async_prepare_player(
    hass: HomeAssistant,
    request: BroadcastRequest,
    entity_id: str,
    state: State,
    features: int,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    """Unmute and set volume. Returns the attributes to put back afterwards."""
    restore: dict[str, Any] = {}

    # A muted speaker is the classic "it said it played but I heard nothing".
    if (
        request.unmute
        and features & FEATURE_VOLUME_MUTE
        and state.attributes.get("is_volume_muted")
    ):
        if request.restore_volume:
            restore["is_volume_muted"] = True
        await _async_player_service(
            hass,
            "volume_mute",
            {"entity_id": entity_id, "is_volume_muted": False},
            outcome,
        )

    if request.volume is None:
        return restore

    if not features & FEATURE_VOLUME_SET:
        outcome["warnings"].append("player does not support setting volume")
        return restore

    level = state.attributes.get("volume_level")
    if request.restore_volume and level is not None:
        restore["volume_level"] = float(level)
    if await _async_player_service(
        hass,
        "volume_set",
        {"entity_id": entity_id, "volume_level": round(request.volume / 100, 3)},
        outcome,
    ):
        await asyncio.sleep(VOLUME_SETTLE_SECONDS)
    return restore


async def _async_restore_player(
    hass: HomeAssistant,
    entity_id: str,
    restore: dict[str, Any],
    outcome: dict[str, Any],
) -> None:
    """Put volume and mute back the way we found them."""
    if "volume_level" in restore:
        await _async_player_service(
            hass,
            "volume_set",
            {"entity_id": entity_id, "volume_level": restore["volume_level"]},
            outcome,
        )
    if restore.get("is_volume_muted"):
        await _async_player_service(
            hass,
            "volume_mute",
            {"entity_id": entity_id, "is_volume_muted": True},
            outcome,
        )


async def _async_wait_before_retry(request: BroadcastRequest, attempt: int) -> bool:
    """Sleep between attempts. Returns False when the attempts are used up."""
    if attempt >= request.max_attempts:
        return False
    await asyncio.sleep(RETRY_DELAY_SECONDS)
    return True


def _is_retryable(status: str, request: BroadcastRequest) -> bool:
    """Whether another attempt is worth making for this outcome.

    A call that raised played nothing, so retrying is free. An *unverified* call
    may in fact have been heard, so we only risk saying it twice when the caller
    marked the message critical.
    """
    if status in (STATUS_FAILED, STATUS_OFFLINE):
        return True
    return status == STATUS_UNVERIFIED and request.critical


async def _async_speak_on_player(
    hass: HomeAssistant, request: BroadcastRequest, entity_id: str
) -> dict[str, Any]:
    """Speak the message on a single player and report what actually happened."""
    outcome = _new_player_outcome(hass, entity_id)

    for attempt in range(1, request.max_attempts + 1):
        outcome["attempts"] = attempt

        state = hass.states.get(entity_id)
        if state is None or state.state in UNAVAILABLE_STATES:
            outcome["status"] = STATUS_OFFLINE
            outcome["error"] = (
                "entity does not exist" if state is None else f"state is {state.state}"
            )
            if await _async_wait_before_retry(request, attempt):
                continue
            return outcome

        features = int(state.attributes.get("supported_features") or 0)
        if not features & FEATURE_PLAY_MEDIA:
            # Retrying cannot fix this, so stop here.
            outcome["status"] = STATUS_UNSUPPORTED
            outcome["error"] = "player does not support playing media"
            return outcome

        outcome["error"] = None
        restore = await _async_prepare_player(
            hass, request, entity_id, state, features, outcome
        )
        watch = _PlaybackWatch(hass, entity_id)
        watch.arm()
        try:
            try:
                async with asyncio.timeout(request.wait_timeout):
                    await hass.services.async_call(
                        "tts",
                        "speak",
                        {
                            # cache:true is REQUIRED for Sonos. Without it HA
                            # serves a synthesis-on-demand stream with no
                            # Content-Length, which Sonos accepts but silently
                            # refuses to play.
                            "entity_id": request.engine,
                            "media_player_entity_id": entity_id,
                            "message": request.message,
                            "cache": True,
                        },
                        blocking=True,
                    )
            except TimeoutError:
                outcome["status"] = STATUS_FAILED
                outcome["error"] = (
                    f"tts.speak did not return within {request.wait_timeout:.0f}s"
                )
            except Exception as err:  # noqa: BLE001 - report, then try again
                outcome["status"] = STATUS_FAILED
                outcome["error"] = _error_text(err)
            else:
                await _async_confirm_playback(request, watch, outcome)
        finally:
            watch.disarm()
            await _async_restore_player(hass, entity_id, restore, outcome)

        if outcome["status"] in DELIVERED_STATUSES:
            return outcome
        if not _is_retryable(outcome["status"], request):
            return outcome
        if not await _async_wait_before_retry(request, attempt):
            break

    return outcome


async def _async_confirm_playback(
    request: BroadcastRequest, watch: _PlaybackWatch, outcome: dict[str, Any]
) -> None:
    """Turn "the service call returned" into an honest playback status."""
    if not request.verify:
        outcome["status"] = STATUS_SENT
        outcome["detail"] = "verification disabled; playback not confirmed"
        return

    if not await watch.wait_started(request.start_timeout):
        outcome["status"] = STATUS_UNVERIFIED
        outcome["error"] = (
            f"the player accepted the command but no playback was detected "
            f"within {request.start_timeout:.0f}s"
        )
        return

    outcome["status"] = STATUS_PLAYED
    outcome["verified"] = True
    outcome["error"] = None
    if not await watch.wait_finished(request.wait_timeout):
        outcome["detail"] = "still playing when the wait timed out"


# --- notifying ----------------------------------------------------------------


async def _async_notify_target(
    hass: HomeAssistant, request: BroadcastRequest, target: str
) -> dict[str, Any]:
    """Send the message to one notify target.

    Handles both calling conventions: classic notify services
    (``notify.mobile_app_phone``) and modern notify entities, which have no
    same-named service and are driven through ``notify.send_message``.
    """
    outcome: dict[str, Any] = {
        "target": target,
        "status": STATUS_FAILED,
        "attempts": 0,
        "error": None,
    }
    service = target.split(".", 1)[1] if target.startswith("notify.") else target

    for attempt in range(1, request.max_attempts + 1):
        outcome["attempts"] = attempt
        if hass.services.has_service("notify", service):
            domain_service = service
            data: dict[str, Any] = {
                "message": request.message,
                "title": request.title,
            }
        elif target.startswith("notify.") and hass.states.get(target) is not None:
            domain_service = "send_message"
            data = {
                "entity_id": target,
                "message": request.message,
                "title": request.title,
            }
        else:
            # Nothing to retry against — the target simply is not there.
            outcome["error"] = "no such notify service or entity"
            return outcome

        try:
            async with asyncio.timeout(request.wait_timeout):
                await hass.services.async_call(
                    "notify", domain_service, data, blocking=True
                )
        except TimeoutError:
            outcome["error"] = (
                f"notify did not return within {request.wait_timeout:.0f}s"
            )
        except Exception as err:  # noqa: BLE001 - one bad target must not stop the rest
            outcome["error"] = _error_text(err)
        else:
            outcome["status"] = STATUS_SENT
            outcome["error"] = None
            return outcome

        if not await _async_wait_before_retry(request, attempt):
            break

    return outcome


# --- orchestration ------------------------------------------------------------


async def _async_gather_outcomes(
    coros: list[Any], fallback: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run per-target work concurrently; an unexpected crash becomes an outcome.

    Nothing below should raise, but a broadcast is exactly the wrong place to
    discover otherwise, so a stray exception is recorded against its target
    rather than taking the whole call down.
    """
    results = await asyncio.gather(*coros, return_exceptions=True)
    outcomes: list[dict[str, Any]] = []
    for template, result in zip(fallback, results, strict=True):
        if isinstance(result, BaseException):
            _LOGGER.exception(
                "intercom: unexpected error broadcasting to %s",
                template.get("entity_id") or template.get("target"),
                exc_info=result,
            )
            template["status"] = STATUS_FAILED
            template["error"] = _error_text(result)
            outcomes.append(template)
        else:
            outcomes.append(result)
    return outcomes


async def async_run_players(
    hass: HomeAssistant, request: BroadcastRequest, engine_ok: bool
) -> list[dict[str, Any]]:
    """Speak on every requested player concurrently."""
    if not request.players:
        return []

    if not engine_ok:
        outcomes = []
        for entity_id in request.players:
            outcome = _new_player_outcome(hass, entity_id)
            outcome["status"] = STATUS_FAILED
            outcome["error"] = f"TTS engine {request.engine} is unavailable"
            outcomes.append(outcome)
        return outcomes

    templates = [_new_player_outcome(hass, p) for p in request.players]
    return await _async_gather_outcomes(
        [_async_speak_on_player(hass, request, p) for p in request.players], templates
    )


async def async_run_notify(
    hass: HomeAssistant, request: BroadcastRequest
) -> list[dict[str, Any]]:
    """Send to every notify target concurrently."""
    if not request.notify_targets:
        return []
    templates = [
        {"target": t, "status": STATUS_FAILED, "attempts": 0, "error": None}
        for t in request.notify_targets
    ]
    return await _async_gather_outcomes(
        [_async_notify_target(hass, request, t) for t in request.notify_targets],
        templates,
    )


def build_result(
    request: BroadcastRequest,
    broadcast_id: str,
    engine_ok: bool,
    player_outcomes: list[dict[str, Any]],
    notify_outcomes: list[dict[str, Any]],
    started_at: Any,
) -> dict[str, Any]:
    """Assemble the service response / event payload."""
    by_status: dict[str, list[str]] = {}
    for outcome in player_outcomes:
        by_status.setdefault(outcome["status"], []).append(outcome["entity_id"])

    played = by_status.get(STATUS_PLAYED, [])
    sent_unverified = by_status.get(STATUS_SENT, [])
    unverified = by_status.get(STATUS_UNVERIFIED, [])
    failed = by_status.get(STATUS_FAILED, [])
    offline = by_status.get(STATUS_OFFLINE, [])
    unsupported = by_status.get(STATUS_UNSUPPORTED, [])

    notified = [o["target"] for o in notify_outcomes if o["status"] == STATUS_SENT]
    notify_failed = [o["target"] for o in notify_outcomes if o["status"] != STATUS_SENT]

    errors: list[str] = []
    for outcome in player_outcomes:
        if outcome["status"] not in DELIVERED_STATUSES and outcome["error"]:
            errors.append(f"{outcome['name']}: {outcome['error']}")
    for outcome in notify_outcomes:
        if outcome["status"] != STATUS_SENT:
            errors.append(f"{outcome['target']}: {outcome['error']}")

    delivered = bool(played or sent_unverified or notified)
    complete = bool(
        (player_outcomes or notify_outcomes)
        and all(o["status"] in DELIVERED_STATUSES for o in player_outcomes)
        and all(o["status"] == STATUS_SENT for o in notify_outcomes)
    )
    finished_at = dt_util.utcnow()

    return {
        "id": broadcast_id,
        "message": request.message,
        "title": request.title,
        "engine": request.engine,
        "engine_available": engine_ok,
        "critical": request.critical,
        "verified": request.verify,
        "delivered": delivered,
        "complete": complete,
        "summary": _summarise(
            delivered, complete, player_outcomes, notify_outcomes, errors
        ),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration": round((finished_at - started_at).total_seconds(), 2),
        "players": player_outcomes,
        "notify": notify_outcomes,
        # Flat lists, handy in templates and in the card.
        "played": played,
        "unverified": unverified,
        "failed": failed,
        "offline": offline,
        "unsupported": unsupported,
        "notified": notified,
        "notify_failed": notify_failed,
        "errors": errors,
        # Kept for automations written against 0.1.x. Note the tightened
        # meaning: only players we have evidence for are listed.
        "spoke": played + sent_unverified,
    }


def _summarise(
    delivered: bool,
    complete: bool,
    player_outcomes: list[dict[str, Any]],
    notify_outcomes: list[dict[str, Any]],
    errors: list[str],
) -> str:
    """One sentence describing the broadcast, suitable for a card or a log."""
    good = sum(1 for o in player_outcomes if o["status"] in DELIVERED_STATUSES)
    sent = sum(1 for o in notify_outcomes if o["status"] == STATUS_SENT)
    parts: list[str] = []
    if player_outcomes:
        parts.append(f"played on {good}/{len(player_outcomes)} speakers")
    if notify_outcomes:
        parts.append(f"notified {sent}/{len(notify_outcomes)} targets")
    head = ", ".join(parts) or "nothing to do"
    if complete:
        return head
    if not delivered:
        return f"FAILED — {head}" + (f" ({errors[0]})" if errors else "")
    return f"partial — {head}" + (f" ({errors[0]})" if errors else "")


def new_broadcast_id() -> str:
    """A short id so a broadcast can be followed across logs, events and cards."""
    return uuid4().hex[:8]
