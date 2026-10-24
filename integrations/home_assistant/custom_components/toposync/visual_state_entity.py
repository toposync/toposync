"""Shared discovery and availability for explicitly published visual sensors."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_VISUAL_STATE_COORDINATOR, DOMAIN


def setup_visual_entities(hass, entry, async_add_entities, entity_type, kind):
    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_VISUAL_STATE_COORDINATOR]
    known = set()

    @callback
    def discover():
        if not coordinator.last_update_success or coordinator.receiver.supported is not True:
            return
        added = []
        for identifier, row in coordinator.data.items():
            publication = row.get("home_assistant") or {}
            if identifier not in known and publication.get("enabled") is True and publication.get("kind") == kind:
                known.add(identifier)
                added.append(entity_type(coordinator, entry.entry_id, identifier))
        if added:
            async_add_entities(added)

    # Keep discovering even when there are no entities or all are disabled in the registry.
    entry.async_on_unload(coordinator.async_add_listener(discover))
    discover()


class ToposyncVisualEntity(CoordinatorEntity):
    kind: str

    def __init__(self, coordinator, entry_id, sensor_id):
        super().__init__(coordinator, context=sensor_id)
        self.sensor_id = sensor_id
        self._attr_unique_id = f"{entry_id}_visual_{sensor_id}_{self.kind}"
        self._attr_name = self.publication.get("name") or f"Toposync {sensor_id}"

    @property
    def row(self):
        return self.coordinator.data.get(self.sensor_id, {})

    @property
    def publication(self):
        return self.row.get("home_assistant") or {}

    @property
    def available(self):
        return (super().available and self.publication.get("enabled") is True
                and self.publication.get("kind") == self.kind
                and self.row.get("state", {}).get("status") in {"confirmed", "pending", "unknown"})

    @property
    def current_class_id(self):
        deadline = self.coordinator.receiver.deadlines.get(self.sensor_id)
        if not self.available or deadline is None or deadline - self.hass.loop.time() < .001:
            return None
        return self.row["state"]["class_id"]

    @property
    def extra_state_attributes(self):
        state = self.row.get("state", {})
        # Observation counters/timestamps belong in inspection, not recorder on every reconfirmation.
        return {"sensor_id": self.sensor_id, "status": state.get("status", "unavailable"),
                "reason": state.get("reason", "not_published"),
                "class_id": self.current_class_id,
                "model_revision": state.get("model_revision"),
                "reference_revision": state.get("reference_revision"),
                "preparation_revision": state.get("preparation_revision"),
                "class_labels": {item["key"]: item["label"] for item in self.row.get("classes", []) if item["enabled"]}}

    @callback
    def _handle_coordinator_update(self):
        if self.publication:
            self._attr_name = self.publication.get("name") or f"Toposync {self.sensor_id}"
        super()._handle_coordinator_update()
