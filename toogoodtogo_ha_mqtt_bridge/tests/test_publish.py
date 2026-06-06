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

    def fake_publish(topic: str, payload: str | None = None) -> MagicMock:
        published[topic] = payload  # type: ignore[assignment]
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

    # Stable entity-id naming: config pins object_id to the immutable item id so the
    # entity_id derives from it (sensor.toogoodtogo_<id>) rather than the store name.
    # has_entity_name is intentionally absent (it makes HA ignore object_id).
    config = json.loads(published["homeassistant/sensor/toogoodtogo_bridge/123/config"])
    assert config["object_id"] == "toogoodtogo_123"
    assert "has_entity_name" not in config
    assert config["name"] == "TooGoodToGo - Test Store"
