"""Minimal stand-in for homeassistant.loader."""


class Integration:
    """Loaded integration metadata."""

    def __init__(self, version: str = "0.0.0"):
        self.version = version


async def async_get_integration(hass, domain: str) -> Integration:
    return Integration()
