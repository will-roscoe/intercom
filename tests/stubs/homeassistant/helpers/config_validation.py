"""Minimal stand-in for homeassistant.helpers.config_validation."""

import voluptuous as vol


def string(value):
    """Coerce to string, rejecting containers and None like the real cv does."""
    if value is None:
        raise vol.Invalid("string value is None")
    if isinstance(value, (list, dict)):
        raise vol.Invalid("value should be a string")
    return str(value)


def entity_id(value):
    """Validate and normalise an entity id."""
    value = string(value).lower().strip()
    if "." in value and all(value.split(".")):
        return value
    raise vol.Invalid(f"Entity ID {value} is an invalid entity ID")


def boolean(value):
    """Coerce to bool the way the real validator does."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower().strip()
        if lowered in ("1", "true", "yes", "on", "enable"):
            return True
        if lowered in ("0", "false", "no", "off", "disable"):
            return False
        raise vol.Invalid(f"invalid boolean value {value}")
    return bool(value)


def ensure_list(value):
    """Wrap a scalar in a list; None becomes an empty list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
