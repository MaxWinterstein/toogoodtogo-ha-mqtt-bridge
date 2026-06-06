import json
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


@pytest.mark.parametrize("stock", [3, 0])
def test_publish_stores_data_attrs(stock: int) -> None:
    settings["timezone"] = "Europe/Berlin"
    settings["locale"] = "en_us"

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
