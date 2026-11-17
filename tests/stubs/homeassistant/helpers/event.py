"""Minimal stand-in for homeassistant.helpers.event."""


def async_track_state_change_event(hass, entity_ids, action):
    """Register `action` for state changes on `entity_ids`; return an unsub."""
    for entity_id in entity_ids:
        hass.state_listeners.setdefault(entity_id, []).append(action)

    def _unsub() -> None:
        for entity_id in entity_ids:
            listeners = hass.state_listeners.get(entity_id, [])
            if action in listeners:
                listeners.remove(action)

    return _unsub


def async_track_time_interval(
    hass, action, interval, *, name=None, cancel_on_shutdown=None
):
    """Remember `action`; tests fire it with `hass.fire_intervals()`."""
    hass.interval_listeners.append(action)

    def _unsub() -> None:
        if action in hass.interval_listeners:
            hass.interval_listeners.remove(action)

    return _unsub
