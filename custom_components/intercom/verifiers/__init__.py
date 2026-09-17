"""Pluggable checks that a media player really played an announcement.

See ``base.py`` for how to add a check for a new kind of player. Importing this
package registers the built-in verifiers.
"""

from .base import PlaybackVerifier, Verdict, select_verifier, wait_event
from .sonos import SonosClipVerifier
from .state import StateVerifier

__all__ = [
    "PlaybackVerifier",
    "SonosClipVerifier",
    "StateVerifier",
    "Verdict",
    "select_verifier",
    "wait_event",
]
