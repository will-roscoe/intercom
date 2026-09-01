"""Minimal stand-in for homeassistant.util.dt."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
