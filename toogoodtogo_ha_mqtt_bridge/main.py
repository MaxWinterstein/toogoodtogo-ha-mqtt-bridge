import json
import logging
import os
import threading

import coloredlogs
import paho.mqtt.client as mqtt
import watchdog
from config import settings
from tgtg import TgtgClient

# pretty logging is pretty
from watchdog import Watchdog

logger = logging.getLogger(__name__)
coloredlogs.install(level="DEBUG", logger=logger)


mqtt_client = None
tgtg_client = TgtgClient(
    email=settings.tgtg.email,
    password=settings.tgtg.password,
    timeout=30,
)
watchdog: Watchdog = None


def check():
    shops = tgtg_client.get_items(page_size=400)
    for shop in shops:
        stock = shop["items_available"]
        item_id = shop["item"]["item_id"]

        logger.debug(f"Pushing message for {shop['display_name']} // {item_id}")

        # Autodiscover
        mqtt_client.publish(
            f"homeassistant/sensor/toogoodtogo_{item_id}/config",
            json.dumps(
                {
                    "name": f"TooGoodToGo - {shop['display_name']}",
                    "icon": "mdi:food" if stock > 0 else "mdi:food-off",
                    "state_topic": f"homeassistant/sensor/toogoodtogo_{item_id}/state",
                    "json_attributes_topic": f"homeassistant/sensor/toogoodtogo_{item_id}/attr",
                    "unit_of_measurement": "portions",
                    "value_template": "{{ value_json.stock }}",
                    "unique_id": f"toogoodtogo_{item_id}",
                }
            ),
        )

        mqtt_client.publish(
            f"homeassistant/sensor/toogoodtogo_{item_id}/state",
            json.dumps({"stock": stock}),
        )

        mqtt_client.publish(
            f"homeassistant/sensor/toogoodtogo_{item_id}/attr",
            json.dumps(
                {
                    "price": (
                        shop["item"]["price"]["minor_units"] / pow(10, shop["item"]["price"]["decimals"])
                    ),
                    "stock_available": True if stock > 0 else False,
                    "url": f"http://share.toogoodtogo.com/item/{item_id}",
                }
            ),
        )

    return True


def loop(event):
    logger.info("Starting loop")
    while True:
        logger.debug("Loop run started")
        if not check():
            logger.error("Stopping loop as retries exhausted.")
            # shutdown()
        else:
            logger.debug("Loop run finished")
        watchdog.reset()
        event.wait(settings.tgtg.every_n_minutes * 60)


def watchdog_handler():
    logger.error("Watchdog handler fired! No pull in the last 5 minutes!")

    os._exit(1)  # easy way to die from within a thread


def start():

    global watchdog, mqtt_client
    watchdog = Watchdog(
        timeout=settings.tgtg.every_n_minutes * 60 * 3 + 30,  # 3 pull intervals + 1 timeout
        user_handler=watchdog_handler,
    )

    logger.info("Connecting mqtt")
    mqtt_client = mqtt.Client("toogoodtogo-ha-mqtt-bridge")
    if settings.mqtt.username:
        mqtt_client.username_pw_set(username=settings.mqtt.username, password=settings.mqtt.password)
    mqtt_client.connect(host=settings.mqtt.host, port=int(settings.mqtt.port))

    event = threading.Event()

    thread = threading.Thread(target=loop, args=(event,))
    thread.start()


if __name__ == "__main__":
    start()
