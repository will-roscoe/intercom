"""Broadcasts held until their conditions are met, kept across restarts.

A call to ``intercom.broadcast`` with ``hold`` (a list of conditions in the
format of an automation's ``conditions:``) is sent at once if they already
hold; otherwise :class:`HeldMessages` keeps it and sends it when they do.

.. code-block:: yaml

    action: intercom.broadcast
    data:
      message: "Your parcel came"
      players: [media_player.kitchen]
      hold:
        - condition: state
          entity_id: person.will
          state: home
      hold_label: Will is home

Held messages are saved with :class:`homeassistant.helpers.storage.Store`, so
they survive a restart, and are re-checked whenever an entity their conditions
name changes, and every :data:`~.const.HELD_CHECK_SECONDS` for conditions no
entity announces (templates, time). A card lists them through the
``intercom/subscribe_held`` websocket command and cancels one with
``intercom/cancel_held``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
import logging
from typing import Any
from uuid import uuid4

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import condition, config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import CONF_HOLD_EXPIRY, DOMAIN, HELD_CHECK_SECONDS

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
#: Shown as "held until <label>" when the call gives no ``hold_label``.
DEFAULT_LABEL = "conditions are met"


class HeldMessages:
    """The broadcasts waiting for their conditions.

    Each item is a dict with ``id``, ``data`` (the validated service data,
    without ``hold``), ``hold`` (the conditions as given), ``label``,
    ``held_at`` and ``expires_at`` (ISO times; ``expires_at`` is None when the
    integration sets no expiry).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        get_options: Callable[[], dict[str, Any]],
        send: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> None:
        """Create the store; nothing is loaded or watched until :meth:`async_load`.

        :param hass: Home Assistant.
        :param get_options: returns the entry's current options (read for
            ``hold_expiry``).
        :param send: broadcasts validated service data; called when a held
            message is released, or at once when its conditions already hold.
        """
        self._hass = hass
        self._get_options = get_options
        self._send = send
        self._store: Store = Store(hass, STORE_VERSION, f"{DOMAIN}.held")
        self._items: list[dict[str, Any]] = []
        # id -> (checker, entities its conditions name); built, never stored.
        self._runtime: dict[str, tuple[Callable[..., bool], set[str]]] = {}
        self._listeners: list[Callable[[list[dict[str, Any]]], None]] = []
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_interval: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load saved messages, start watching them, and check them once.

        A saved message whose conditions no longer load is dropped and
        reported in a persistent notification.
        """
        stored = await self._store.async_load() or {}
        dropped: list[dict[str, Any]] = []
        for item in stored.get("items", []):
            try:
                self._runtime[item["id"]] = await self._async_compile(item)
            except HomeAssistantError as err:
                _LOGGER.error("intercom: dropping held message %s: %s", item["id"], err)
                dropped.append(item)
                continue
            self._items.append(item)
        if dropped:
            await self._store.async_save({"items": self._items})
            for item in dropped:
                await self._async_notify(
                    item, "could not be restored: its conditions no longer load"
                )
        self._rearm()
        if self._items:
            # Conditions may have come true while Home Assistant was down.
            self._hass.async_create_task(self.async_check())

    async def async_unload(self) -> None:
        """Stop watching; saved messages stay saved."""
        self._stop_watching(interval=True)
        self._listeners.clear()

    async def async_submit(
        self, data: dict[str, Any], hold: list[dict[str, Any]], label: str | None
    ) -> Any:
        """Send ``data`` now if ``hold`` is met, otherwise keep it until it is.

        :param data: validated ``intercom.broadcast`` data, without ``hold``.
        :param hold: conditions in the format of an automation's ``conditions:``.
        :param label: words for "held until ...", or None for a generic one.
        :returns: ``send``'s result when sent now; otherwise a dict with
            ``id``, ``held`` (True), ``message``, ``label``, ``held_at``,
            ``expires_at`` and ``summary``.
        :raises HomeAssistantError: the conditions are not valid.
        """
        item: dict[str, Any] = {"id": uuid4().hex[:8], "hold": hold}
        runtime = await self._async_compile(item)
        if runtime[0]():
            return await self._send(data)

        now = dt_util.utcnow()
        hours = self._get_options().get(CONF_HOLD_EXPIRY)
        item.update(
            data=data,
            label=label or DEFAULT_LABEL,
            held_at=now.isoformat(),
            expires_at=(now + timedelta(hours=float(hours))).isoformat()
            if hours
            else None,
        )
        async with self._lock:
            self._items.append(item)
            self._runtime[item["id"]] = runtime
            await self._async_changed()
        _LOGGER.info("intercom: holding message %s until %s", item["id"], item["label"])
        return {
            **self._public(item),
            "held": True,
            "summary": f"held until {item['label']}",
        }

    async def async_cancel(self, held_id: str) -> bool:
        """Drop a held message so it is never sent.

        :returns: False when no held message has that id.
        """
        async with self._lock:
            if self._pop(held_id) is None:
                return False
            await self._async_changed()
        return True

    def items(self) -> list[dict[str, Any]]:
        """Return the held messages, oldest first, as the card shows them.

        :returns: dicts with ``id``, ``message``, ``players``, ``notify``,
            ``label``, ``held_at`` and ``expires_at``.
        """
        return [self._public(item) for item in self._items]

    @callback
    def async_subscribe(
        self, listener: Callable[[list[dict[str, Any]]], None]
    ) -> CALLBACK_TYPE:
        """Call ``listener`` with :meth:`items` whenever the list changes."""
        self._listeners.append(listener)

        @callback
        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe

    async def async_check(self, *_: Any) -> None:
        """Send every message whose conditions hold; drop expired ones.

        Ready messages are sent one after another in the order they were held.
        An expired message is reported in a persistent notification, not sent.
        """
        async with self._lock:
            now = dt_util.utcnow()
            ready: list[dict[str, Any]] = []
            expired: list[dict[str, Any]] = []
            for item in self._items:
                expires = item["expires_at"]
                if expires and datetime.fromisoformat(expires) <= now:
                    expired.append(item)
                elif self._runtime[item["id"]][0]():
                    ready.append(item)
            if not ready and not expired:
                return
            for item in ready + expired:
                self._pop(item["id"])
            await self._async_changed()

        for item in expired:
            _LOGGER.warning("intercom: held message %s expired", item["id"])
            await self._async_notify(item, f"expired at {item['expires_at']}")
        for item in ready:
            _LOGGER.info("intercom: releasing held message %s", item["id"])
            try:
                await self._send(item["data"])
            except Exception:  # noqa: BLE001 - one message must not stop the rest
                _LOGGER.exception("intercom: held message %s failed", item["id"])

    # --- internals -------------------------------------------------------------

    async def _async_compile(
        self, item: dict[str, Any]
    ) -> tuple[Callable[..., bool], set[str]]:
        try:
            configs = cv.CONDITIONS_SCHEMA(item["hold"])
            configs = await condition.async_validate_conditions_config(
                self._hass, configs
            )
            checker = await condition.async_conditions_from_config(
                self._hass, configs, _LOGGER, f"intercom held message {item['id']}"
            )
        except (vol.Invalid, HomeAssistantError) as err:
            raise HomeAssistantError(f"invalid hold conditions: {err}") from err
        entities: set[str] = set()
        for config in configs:
            entities |= condition.async_extract_entities(config)
        return checker, entities

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        data = item["data"]
        return {
            "id": item["id"],
            "message": data["message"],
            "players": list(data.get("players", [])),
            "notify": list(data.get("notify", [])),
            "label": item["label"],
            "held_at": item["held_at"],
            "expires_at": item["expires_at"],
        }

    def _pop(self, held_id: str) -> dict[str, Any] | None:
        for index, item in enumerate(self._items):
            if item["id"] == held_id:
                self._runtime.pop(held_id, None)
                return self._items.pop(index)
        return None

    async def _async_changed(self) -> None:
        await self._store.async_save({"items": self._items})
        self._rearm()
        snapshot = self.items()
        for listener in list(self._listeners):
            listener(snapshot)

    @callback
    def _rearm(self) -> None:
        self._stop_watching(interval=not self._items)
        if not self._items:
            return
        entities = set().union(*(self._runtime[i["id"]][1] for i in self._items))
        if entities:
            self._unsub_state = async_track_state_change_event(
                self._hass, sorted(entities), self._on_state_change
            )
        if self._unsub_interval is None:
            self._unsub_interval = async_track_time_interval(
                self._hass, self.async_check, timedelta(seconds=HELD_CHECK_SECONDS)
            )

    @callback
    def _stop_watching(self, interval: bool) -> None:
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        if interval and self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    @callback
    def _on_state_change(self, _event: Any) -> None:
        self._hass.async_create_task(self.async_check())

    async def _async_notify(self, item: dict[str, Any], what: str) -> None:
        message = item["data"]["message"] if "data" in item else "?"
        try:
            await self._hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": f"intercom_held_{item['id']}",
                    "title": "Held intercom message not sent",
                    "message": f'"{message}", held until {item.get("label")} since '
                    f"{item.get('held_at')}, {what}. It was not sent.",
                },
                blocking=False,
            )
        except Exception:  # noqa: BLE001 - the log line above still records it
            _LOGGER.exception("intercom: could not report held message %s", item["id"])
