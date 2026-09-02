"""Minimal stand-in for homeassistant.exceptions."""


class HomeAssistantError(Exception):
    """Base error."""


class ServiceValidationError(HomeAssistantError):
    """Invalid service call."""


class Unauthorized(HomeAssistantError):
    """Not permitted."""
