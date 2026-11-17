"""Minimal stand-in for homeassistant.helpers.condition: `state` conditions only."""

import voluptuous as vol


def _entities(config) -> list[str]:
    ids = config.get("entity_id", [])
    return [ids] if isinstance(ids, str) else list(ids)


async def async_validate_conditions_config(hass, conditions):
    for config in conditions:
        if not isinstance(config, dict) or config.get("condition") != "state":
            raise vol.Invalid(f"unsupported condition {config!r}")
        if not _entities(config) or "state" not in config:
            raise vol.Invalid("state condition needs entity_id and state")
    return conditions


async def async_conditions_from_config(hass, condition_configs, logger, name):
    def check_conditions(variables=None) -> bool:
        for config in condition_configs:
            wanted = config["state"]
            wanted = [wanted] if isinstance(wanted, str) else list(wanted)
            for entity_id in _entities(config):
                state = hass.states.get(entity_id)
                if state is None or state.state not in wanted:
                    return False
        return True

    return check_conditions


def async_extract_entities(config) -> set[str]:
    return set(_entities(config))
