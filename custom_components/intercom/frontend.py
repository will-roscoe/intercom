"""Serve and register the Intercom Lovelace card.

Serving the JS is not enough for a card to appear — the browser only knows about
``custom:intercom-card`` if the file is registered as a Lovelace *resource*. In
storage mode we add it to the resource collection (like adding it by hand under
Settings -> Dashboards -> Resources); in YAML mode that collection is read-only,
so we fall back to injecting it as an extra frontend module.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import CARD_FILENAME, DATA_CARD_REGISTERED, URL_BASE

_LOGGER = logging.getLogger(__name__)

CARD_URL = f"{URL_BASE}/{CARD_FILENAME}"


async def async_register_card(hass: HomeAssistant, version: str) -> None:
    """Serve the card file and register it with the frontend (once per HA run)."""
    if hass.data.get(DATA_CARD_REGISTERED):
        return
    await _async_serve_static(hass)
    await _async_register_resource(hass, version)
    hass.data[DATA_CARD_REGISTERED] = True


async def _async_serve_static(hass: HomeAssistant) -> None:
    """Serve the bundled card from the integration package (no /config/www copy)."""
    from homeassistant.components.http import StaticPathConfig

    card_path = str(Path(__file__).parent / "www" / CARD_FILENAME)
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, card_path, cache_headers=False)]
        )
    except (RuntimeError, ValueError) as err:
        # Already registered (e.g. after a reload) — not fatal.
        _LOGGER.debug("intercom: static path not (re)registered: %s", err)


async def _async_register_resource(hass: HomeAssistant, version: str) -> None:
    """Add the card as a Lovelace resource, or fall back to an extra JS module."""
    from homeassistant.components.frontend import add_extra_js_url

    url = f"{CARD_URL}?v={version}"

    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None and isinstance(lovelace, dict):
        resources = lovelace.get("resources")

    try:
        from homeassistant.components.lovelace.resources import (
            ResourceStorageCollection,
        )
    except ImportError:
        ResourceStorageCollection = None  # type: ignore[assignment]

    # YAML mode / no storage collection -> inject as an extra frontend module.
    if (
        resources is None
        or ResourceStorageCollection is None
        or not isinstance(resources, ResourceStorageCollection)
    ):
        add_extra_js_url(hass, url)
        _LOGGER.debug("intercom: registered card via extra_js_url (%s)", url)
        return

    await resources.async_get_info()
    for item in resources.async_items():
        if item.get("url", "").split("?", 1)[0] == CARD_URL:
            # Already present; bump the version query param if it changed.
            if item.get("url") != url:
                await resources.async_update_item(
                    item["id"], {"res_type": "module", "url": url}
                )
                _LOGGER.debug("intercom: updated Lovelace resource -> %s", url)
            return

    await resources.async_create_item({"res_type": "module", "url": url})
    _LOGGER.info("intercom: registered Lovelace resource %s", url)
