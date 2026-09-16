"""Minimal stand-in for homeassistant.helpers.entity_registry.

Mirrors the read side core offers: ``async_get(hass)`` returns the registry and
``registry.async_get(entity_id)`` returns an entry or None.
"""

from __future__ import annotations

from dataclasses import dataclass

DATA_REGISTRY = "entity_registry"


@dataclass
class RegistryEntry:
    entity_id: str
    platform: str
    unique_id: str
    device_id: str | None = None


class EntityRegistry:
    def __init__(self) -> None:
        self.entities: dict[str, RegistryEntry] = {}

    def async_get(self, entity_id: str) -> RegistryEntry | None:
        return self.entities.get(entity_id)

    def add(
        self,
        entity_id: str,
        platform: str,
        unique_id: str,
        device_id: str | None = None,
    ) -> RegistryEntry:
        entry = RegistryEntry(entity_id, platform, unique_id, device_id)
        self.entities[entity_id] = entry
        return entry


def async_get(hass) -> EntityRegistry:
    return hass.data.setdefault(DATA_REGISTRY, EntityRegistry())
