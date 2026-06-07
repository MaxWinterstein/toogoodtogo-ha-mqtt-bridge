import json
from collections.abc import Generator
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest

from toogoodtogo_ha_mqtt_bridge import main
from toogoodtogo_ha_mqtt_bridge.config import settings


def _fake_shop(stock: int) -> dict:
    return {
        "display_name": "Test Store",
        "items_available": stock,
        "item": {"item_id": "123", "price": {"minor_units": 499, "decimals": 2}},
        "pickup_interval": {"start": "2022-01-01T17:00:00Z", "end": "2022-01-01T18:00:00Z"},
        "store": {"logo_picture": {"current_url": "http://logo"}},
    }


@pytest.fixture
def _settings_env() -> Generator[None, None, None]:
    # dynaconf's settings object has no __delitem__, so snapshot/restore the
    # keys we touch instead of using monkeypatch.setitem (whose teardown deletes).
    keys = ("timezone", "locale")
    original = {key: settings.get(key) for key in keys}
    settings["timezone"] = "Europe/Berlin"
    settings["locale"] = "en_us"
    yield
    for key, value in original.items():
        settings[key] = value


@pytest.mark.parametrize("stock", [3, 0])
def test_publish_stores_data_attrs(stock: int, _settings_env: None) -> None:
    published: dict[str, str] = {}
    retained: dict[str, bool] = {}

    def fake_publish(topic: str, payload: str | None = None, retain: bool = False) -> MagicMock:
        published[topic] = payload  # type: ignore[assignment]
        retained[topic] = retain
        return MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)

    main.mqtt_client = MagicMock()
    main.mqtt_client.publish.side_effect = fake_publish

    assert main.publish_stores_data([_fake_shop(stock=stock)]) is True

    attrs = json.loads(published["homeassistant/sensor/toogoodtogo_123/attr"])
    # Regression for the trailing-comma bug: these must be plain strings,
    # not 1-tuples serialized as JSON arrays.
    assert isinstance(attrs["pickup_start"], str)
    assert isinstance(attrs["pickup_end"], str)
    assert attrs["price"] == 4.99
    assert attrs["stock_available"] is (stock > 0)

    # State/attribute messages must be retained so a freshly discovered entity shows its
    # value immediately on subscribe instead of 'unknown' until the next poll (issue #85).
    assert retained["homeassistant/sensor/toogoodtogo_123/state"] is True
    assert retained["homeassistant/sensor/toogoodtogo_123/attr"] is True

    # Stable entity-id naming: default_entity_id (domain-prefixed) pins the entity_id to the
    # immutable item id (sensor.toogoodtogo_<id>) instead of the volatile store name. name is
    # the brand-free sub-name (HA forces has_entity_name=True, prefixing the device brand).
    config = json.loads(published["homeassistant/sensor/toogoodtogo_bridge/123/config"])
    assert config["default_entity_id"] == "sensor.toogoodtogo_123"
    assert config["name"] == "Test Store"


def test_register_fetch_sensor_naming() -> None:
    # The switch is the only non-sensor entity, so its default_entity_id must carry the
    # switch. domain (not sensor.). Covers the distinct domain path of entity_naming.
    published: dict[str, str] = {}

    def fake_publish(topic: str, payload: str | None = None) -> MagicMock:
        published[topic] = payload  # type: ignore[assignment]
        return MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)

    main.mqtt_client = MagicMock()
    main.mqtt_client.publish.side_effect = fake_publish

    main.register_fetch_sensor()

    config = json.loads(published["homeassistant/switch/toogoodtogo_bridge/intense_fetch/config"])
    assert config["default_entity_id"] == "switch.toogoodtogo_intense_fetch_switch"
    assert config["name"] == "Intense fetch"
