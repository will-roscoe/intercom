"""Constants for the Intercom integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "intercom"

# Config / options keys
CONF_TTS_ENGINE: Final = "tts_engine"
CONF_DEFAULT_TITLE: Final = "default_title"
CONF_WAIT_TIMEOUT: Final = "wait_timeout"

# Defaults
DEFAULT_TTS_ENGINE: Final = "tts.piper"
DEFAULT_TITLE: Final = "Broadcast"
DEFAULT_WAIT_TIMEOUT: Final = 30

# Service
SERVICE_BROADCAST: Final = "broadcast"

ATTR_MESSAGE: Final = "message"
ATTR_PLAYERS: Final = "players"
ATTR_VOLUME: Final = "volume"
ATTR_NOTIFY: Final = "notify"
ATTR_TITLE: Final = "title"
ATTR_ENGINE: Final = "engine"
ATTR_RESTORE_VOLUME: Final = "restore_volume"

# Fired on the HA event bus after every broadcast so cards/automations can react.
EVENT_RESULT: Final = "intercom_broadcast_result"

# States that mean "don't try to play here".
UNAVAILABLE_STATES: Final = frozenset({"unavailable", "unknown", "none", ""})

# Frontend card
URL_BASE: Final = "/intercom"
CARD_FILENAME: Final = "intercom-card.js"

# Marker in hass.data so we only register static paths / JS once per HA run.
DATA_CARD_REGISTERED: Final = f"{DOMAIN}_card_registered"
