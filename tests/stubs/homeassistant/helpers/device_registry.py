"""Minimal stand-in for homeassistant.helpers.device_registry.

Like core, ``async_get_device(identifiers=...)`` returns the device that has
*any* of the given identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DATA_REGISTRY = "device_registry"


@dataclass
class DeviceEntry:
    id: str
    identifiers: set[tuple[str, str]] = field(default_factory=set)
    configuration_url: str | None = None


class DeviceRegistry:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceEntry] = {}

    def async_get(self, device_id: str) -> DeviceEntry | None:
        return self.devices.get(device_id)

    def async_get_device(
        self,
        identifiers: set[tuple[str, str]] | None = None,
        connections: set[tuple[str, str]] | None = None,
    ) -> DeviceEntry | None:
        for device in self.devices.values():
            if identifiers and device.identifiers & identifiers:
                return device
        return None

    def add(
        self,
        device_id: str,
        identifiers: set[tuple[str, str]],
        configuration_url: str | None = None,
    ) -> DeviceEntry:
        device = DeviceEntry(device_id, set(identifiers), configuration_url)
        self.devices[device_id] = device
        return device


def async_get(hass) -> DeviceRegistry:
    return hass.data.setdefault(DATA_REGISTRY, DeviceRegistry())
