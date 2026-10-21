from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .visual_state_entity import ToposyncVisualEntity, setup_visual_entities


async def async_setup_entry(hass, entry, async_add_entities):
    setup_visual_entities(hass, entry, async_add_entities, ToposyncVisualSensor, "enum")


class ToposyncVisualSensor(ToposyncVisualEntity, SensorEntity):
    kind = "enum"
    _attr_device_class = SensorDeviceClass.ENUM

    @property
    def options(self):
        return [item["key"] for item in self.row.get("classes", []) if item["enabled"]]

    @property
    def native_value(self):
        identifier = self.current_class_id
        return next((item["key"] for item in self.row.get("classes", [])
                     if item["class_id"] == identifier and item["enabled"]), None)
