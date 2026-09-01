"""Minimal stand-in for homeassistant.config_entries."""

from __future__ import annotations

from typing import Any


class ConfigEntry:
    """A configured instance of an integration."""

    def __init__(self, data: dict | None = None, options: dict | None = None):
        self.entry_id = "test"
        self.data: dict[str, Any] = dict(data or {})
        self.options: dict[str, Any] = dict(options or {})
        self._unload_callbacks: list = []

    def add_update_listener(self, listener):
        def _remove() -> None:
            return None

        return _remove

    def async_on_unload(self, func) -> None:
        self._unload_callbacks.append(func)


class ConfigFlowResult(dict):
    """Result of a config flow step."""


class ConfigFlow:
    """Base config flow."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()


class OptionsFlow:
    """Base options flow."""
