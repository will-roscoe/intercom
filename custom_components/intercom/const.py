"""Constants for the Intercom integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "intercom"

# Config / options keys
CONF_TTS_ENGINE: Final = "tts_engine"
CONF_DEFAULT_TITLE: Final = "default_title"
CONF_WAIT_TIMEOUT: Final = "wait_timeout"
CONF_START_TIMEOUT: Final = "start_timeout"
CONF_MAX_ATTEMPTS: Final = "max_attempts"
CONF_UNMUTE: Final = "unmute"

# Defaults
DEFAULT_TTS_ENGINE: Final = "tts.piper"
DEFAULT_TITLE: Final = "Broadcast"
DEFAULT_WAIT_TIMEOUT: Final = 30
# How long to wait for a player to show *any* evidence that our announcement
# started. Sonos in particular needs several seconds to fetch and buffer the
# clip before its transport state flips, so this has to be generous.
DEFAULT_START_TIMEOUT: Final = 8
DEFAULT_MAX_ATTEMPTS: Final = 2
DEFAULT_UNMUTE: Final = True

# Delay after setting volume so the level is applied before audio starts.
VOLUME_SETTLE_SECONDS: Final = 0.4
# Volume a critical broadcast raises a silent speaker to when the caller gave no
# volume of its own. An emergency message at volume zero is not a message.
CRITICAL_MIN_VOLUME: Final = 30.0

# Service
SERVICE_BROADCAST: Final = "broadcast"

ATTR_MESSAGE: Final = "message"
ATTR_PLAYERS: Final = "players"
ATTR_VOLUME: Final = "volume"
ATTR_NOTIFY: Final = "notify"
ATTR_TITLE: Final = "title"
ATTR_ENGINE: Final = "engine"
ATTR_RESTORE_VOLUME: Final = "restore_volume"
ATTR_CRITICAL: Final = "critical"
ATTR_VERIFY: Final = "verify"
ATTR_MAX_ATTEMPTS: Final = "max_attempts"

# Fired on the HA event bus after every broadcast so cards/automations can react.
# NOTE: only admins may subscribe to custom events over the websocket API, so the
# card reads the *service response* instead; this event is for automations.
EVENT_RESULT: Final = "intercom_broadcast_result"

# --- per-target outcome codes -------------------------------------------------
# Audio we have positive evidence for: the player entered a playing state, or
# swapped to new media, after we asked it to speak.
STATUS_PLAYED: Final = "played"
# The service call was accepted but the player never showed any sign of playing.
# This is the "Home Assistant said OK but nothing came out of the speaker" case.
STATUS_UNVERIFIED: Final = "unverified"
# The service call itself raised (e.g. Sonos "The command to the player failed").
STATUS_FAILED: Final = "failed"
# Entity missing or unavailable before we even tried.
STATUS_OFFLINE: Final = "offline"
# Entity exists but cannot play media at all.
STATUS_UNSUPPORTED: Final = "unsupported"
# The clip played, but into a muted speaker or one at zero volume, so nobody
# could have heard it. A muted player reports a perfectly successful
# announcement, which is exactly the kind of lie this integration exists to stop.
STATUS_SILENT: Final = "silent"
# Notify target delivered / not delivered.
STATUS_SENT: Final = "sent"

# Statuses that mean the message definitely reached someone.
DELIVERED_STATUSES: Final = frozenset({STATUS_PLAYED, STATUS_SENT})

# States that mean "don't try to play here".
UNAVAILABLE_STATES: Final = frozenset({"unavailable", "unknown", "none", ""})
# States that count as audio actually coming out.
PLAYING_STATES: Final = frozenset({"playing", "buffering"})

# media_player supported_features bits we care about (mirrors
# MediaPlayerEntityFeature so we don't import the component at module scope).
FEATURE_PLAY_MEDIA: Final = 512
FEATURE_VOLUME_SET: Final = 4
FEATURE_VOLUME_MUTE: Final = 8

# Websocket API. Home Assistant only lets admins subscribe to arbitrary bus
# events, so the card subscribes to this narrow command of ours instead.
WS_TYPE_SUBSCRIBE_RESULT: Final = f"{DOMAIN}/subscribe_result"

# Frontend card
URL_BASE: Final = "/intercom"
CARD_FILENAME: Final = "intercom-card.js"

# Marker in hass.data so we only register static paths / JS once per HA run.
DATA_CARD_REGISTERED: Final = f"{DOMAIN}_card_registered"
# Last broadcast result, replayed to a card the moment it subscribes.
DATA_LAST_RESULT: Final = f"{DOMAIN}_last_result"
