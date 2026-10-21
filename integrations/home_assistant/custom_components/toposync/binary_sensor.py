from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity

from .visual_state_entity import ToposyncVisualEntity, setup_visual_entities


async def async_setup_entry(hass, entry, async_add_entities):
    setup_visual_entities(hass, entry, async_add_entities, ToposyncVisualBinarySensor, "binary")


class ToposyncVisualBinarySensor(ToposyncVisualEntity, BinarySensorEntity):
    kind = "binary"

    @property
    def is_on(self):
        identifier = self.current_class_id
        if identifier is None:
            return None
        if identifier in self.publication.get("on_class_ids", []):
            return True
        if identifier in self.publication.get("off_class_ids", []):
            return False
        return None

    @property
    def extra_state_attributes(self):
        attributes = super().extra_state_attributes
        if self.current_class_id is not None and self.is_on is None:
            attributes.update(status="unknown", reason="unmapped_class")
        return attributes
