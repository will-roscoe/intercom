"""Minimal stand-in for homeassistant.helpers.storage.

Saved data lives in a class-level dict keyed by store key, so a new Store with
the same key (a "restart" in a test) sees what an earlier one saved.
"""

import copy
import json


class Store:
    DISK: dict = {}

    def __init__(self, hass, version, key, private=False, **kwargs):
        self.hass = hass
        self.version = version
        self.key = key

    async def async_load(self):
        data = Store.DISK.get(self.key)
        return copy.deepcopy(data)

    async def async_save(self, data) -> None:
        json.dumps(data)  # real storage writes JSON; fail the same way
        Store.DISK[self.key] = copy.deepcopy(data)
