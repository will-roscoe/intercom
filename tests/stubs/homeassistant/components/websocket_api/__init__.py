"""Minimal stand-in for homeassistant.components.websocket_api.

Mirrors the registration and decorator contract read from HA core `dev`:
`websocket_command` stashes `_ws_command`/`_ws_schema` on the handler,
`async_register_command` reads them back, and a connection exposes the
`subscriptions` dict that core's generic unsubscribe_events handler pops from.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

HANDLERS: dict[str, tuple[Any, Any]] = {}


class ActiveConnection:
    """A connected websocket client."""

    def __init__(self, user=None):
        self.user = user
        self.subscriptions: dict[Any, Any] = {}
        self.messages: list[dict] = []
        self.results: list[tuple] = []

    def send_message(self, message: dict) -> None:
        self.messages.append(message)

    def send_result(self, msg_id, result=None) -> None:
        self.results.append((msg_id, result))


def event_message(iden, event) -> dict:
    return {"id": iden, "type": "event", "event": event}


def result_message(iden, result=None) -> dict:
    return {"id": iden, "type": "result", "success": True, "result": result}


def websocket_command(schema):
    """Tag a handler as a websocket command."""
    command = schema["type"]

    def decorate(func):
        func._ws_schema = False if len(schema) == 1 else vol.Schema(schema)
        func._ws_command = command
        return func

    return decorate


def require_admin(func):
    """Mark a handler admin-only."""
    func._ws_require_admin = True
    return func


def async_response(func):
    """Mark a handler as async."""
    func._ws_async = True
    return func


def async_register_command(hass, command_or_handler, handler=None, schema=None) -> None:
    """Register a websocket command, replacing any existing handler."""
    if handler is None:
        handler = command_or_handler
        command = handler._ws_command
        schema = handler._ws_schema
    else:
        command = command_or_handler
    HANDLERS[command] = (handler, schema)
