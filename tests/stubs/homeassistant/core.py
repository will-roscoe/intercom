"""Minimal stand-in for homeassistant.core.

Only the surface the intercom integration touches, with the semantics that
matter to it: services that can be registered and can raise, a state machine
that fires state_changed on every write, and a bus that really dispatches.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

CALLBACK_TYPE = Callable[[], None]


def callback(func):
    """No-op stand-in for the real callback marker."""
    return func


class ServiceNotFound(Exception):
    """Raised when a service is called that nobody registered."""


class State:
    """A single entity state."""

    def __init__(self, entity_id: str, state: str, attributes: dict | None = None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = dict(attributes or {})

    def __repr__(self) -> str:
        return f"<State {self.entity_id}={self.state} {self.attributes}>"


class Event:
    """A bus event."""

    def __init__(self, event_type: str, data: dict):
        self.event_type = event_type
        self.data = data


class ServiceCall:
    """A validated service call."""

    def __init__(self, data: dict):
        self.data = data


class ServiceResponse(dict):
    """Service response payload."""


class SupportsResponse:
    """Response support markers."""

    NONE = "none"
    OPTIONAL = "optional"
    ONLY = "only"


class _States:
    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._data: dict[str, State] = {}

    def get(self, entity_id: str) -> State | None:
        return self._data.get(entity_id)

    def set(self, entity_id: str, state: str, attributes: dict | None = None) -> State:
        """Write a state, merging attributes, and fire state_changed."""
        old = self._data.get(entity_id)
        merged = dict(old.attributes) if old else {}
        merged.update(attributes or {})
        new = State(entity_id, state, merged)
        self._data[entity_id] = new
        self._hass.dispatch_state_change(entity_id, old, new)
        return new


class _Services:
    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self.handlers: dict[tuple[str, str], Any] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.handlers

    def register(self, domain: str, service: str, handler) -> None:
        self.handlers[(domain, service)] = handler

    def async_register(self, domain, service, handler, **kwargs) -> None:
        self.handlers[(domain, service)] = handler

    def async_remove(self, domain: str, service: str) -> None:
        self.handlers.pop((domain, service), None)

    async def async_call(self, domain, service, data, blocking=False, **kwargs):
        self.calls.append((domain, service, dict(data)))
        handler = self.handlers.get((domain, service))
        if handler is None:
            raise ServiceNotFound(f"Service {domain}.{service} not found")
        return await handler(self._hass, dict(data))


class _Bus:
    def __init__(self):
        self.fired: list[tuple[str, dict]] = []
        self.listeners: dict[str, list] = {}

    def async_listen(self, event_type: str, listener) -> CALLBACK_TYPE:
        self.listeners.setdefault(event_type, []).append(listener)

        def _unsub() -> None:
            if listener in self.listeners.get(event_type, []):
                self.listeners[event_type].remove(listener)

        return _unsub

    def async_fire(self, event_type: str, data: dict) -> None:
        self.fired.append((event_type, data))
        for listener in list(self.listeners.get(event_type, [])):
            listener(Event(event_type, data))


class HomeAssistant:
    """Just enough hass to run a broadcast."""

    def __init__(self):
        self.states = _States(self)
        self.services = _Services(self)
        self.bus = _Bus()
        self.data: dict = {}
        self.state_listeners: dict[str, list] = {}

    @property
    def loop(self):
        """The running loop, or None when built outside one."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def dispatch_state_change(self, entity_id, old, new) -> None:
        for listener in list(self.state_listeners.get(entity_id, [])):
            listener(
                Event(
                    "state_changed",
                    {"entity_id": entity_id, "old_state": old, "new_state": new},
                )
            )
