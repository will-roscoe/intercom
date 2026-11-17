"""Config flow for the Intercom integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_CHIME,
    CONF_CHIME_MEDIA,
    CONF_CHIME_SECONDS,
    CONF_DEFAULT_TITLE,
    CONF_HOLD_EXPIRY,
    CONF_MAX_ATTEMPTS,
    CONF_START_TIMEOUT,
    CONF_TTS_ENGINE,
    CONF_UNMUTE,
    CONF_WAIT_TIMEOUT,
    DEFAULT_CHIME,
    DEFAULT_CHIME_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_START_TIMEOUT,
    DEFAULT_TITLE,
    DEFAULT_TTS_ENGINE,
    DEFAULT_UNMUTE,
    DEFAULT_WAIT_TIMEOUT,
    DOMAIN,
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the shared config/options schema, pre-filled with defaults."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_TTS_ENGINE,
                default=defaults.get(CONF_TTS_ENGINE, DEFAULT_TTS_ENGINE),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="tts")),
            vol.Optional(
                CONF_DEFAULT_TITLE,
                default=defaults.get(CONF_DEFAULT_TITLE, DEFAULT_TITLE),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_WAIT_TIMEOUT,
                default=defaults.get(CONF_WAIT_TIMEOUT, DEFAULT_WAIT_TIMEOUT),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=5, max=120, step=1, unit_of_measurement="s", mode="box"
                )
            ),
            vol.Optional(
                CONF_START_TIMEOUT,
                default=defaults.get(CONF_START_TIMEOUT, DEFAULT_START_TIMEOUT),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=60, step=1, unit_of_measurement="s", mode="box"
                )
            ),
            vol.Optional(
                CONF_MAX_ATTEMPTS,
                default=defaults.get(CONF_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=5, step=1, mode="box")
            ),
            vol.Optional(
                CONF_UNMUTE,
                default=defaults.get(CONF_UNMUTE, DEFAULT_UNMUTE),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_CHIME,
                default=defaults.get(CONF_CHIME, DEFAULT_CHIME),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_CHIME_MEDIA,
                default=defaults.get(CONF_CHIME_MEDIA, ""),
            ): selector.TextSelector(),
            vol.Optional(
                CONF_CHIME_SECONDS,
                default=defaults.get(CONF_CHIME_SECONDS, DEFAULT_CHIME_SECONDS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=10, step=0.1, unit_of_measurement="s", mode="box"
                )
            ),
            # No default: left empty, held messages never expire.
            vol.Optional(
                CONF_HOLD_EXPIRY,
                description={"suggested_value": defaults.get(CONF_HOLD_EXPIRY)},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0.5, max=720, step=0.5, unit_of_measurement="h", mode="box"
                )
            ),
        }
    )


class IntercomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step. Single instance only."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="Intercom", data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow."""
        return IntercomOptionsFlow()


class IntercomOptionsFlow(OptionsFlow):
    """Handle options (defaults for the broadcast service)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
