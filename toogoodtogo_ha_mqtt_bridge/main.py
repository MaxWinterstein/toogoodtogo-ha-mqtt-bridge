import json
import logging
import os
import threading
from pathlib import Path
from time import sleep

import arrow
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
    user_agent="TooGoodToGo/21.6.2 (813) (iPhone/iPhone 7 (GSM); iOS 13.7; Scale/2.00)",
)
watchdog: Watchdog = None


def check():
    shops = tgtg_client.get_items(page_size=400)
    for shop in shops:
        stock = shop["items_available"]
        item_id = shop["item"]["item_id"]

        logger.debug(f"Pushing message for {shop['display_name']} // {item_id}")

        # Autodiscover
        result_ad = mqtt_client.publish(
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

        result_state = mqtt_client.publish(
            f"homeassistant/sensor/toogoodtogo_{item_id}/state",
            json.dumps({"stock": stock}),
        )

        # prepare attrs
        price = shop["item"]["price"]["minor_units"] / pow(10, shop["item"]["price"]["decimals"])
        pickup_start_date = (
            None if not stock else arrow.get(shop["pickup_interval"]["start"]).to(tz=settings.timezone)
        )
        pickup_end_date = (
            None if not stock else arrow.get(shop["pickup_interval"]["end"]).to(tz=settings.timezone)
        )
        pickup_start_str = ("Unknown" if stock == 0 else pickup_start_date.to(tz=settings.timezone).format(),)
        pickup_end_str = ("Unknown" if stock == 0 else pickup_end_date.to(tz=settings.timezone).format(),)
        pickup_start_human = (
            "Unknown"
            if stock == 0
            else pickup_start_date.humanize(only_distance=False, locale=settings.locale)
        )
        pickup_end_human = (
            "Unknown" if stock == 0 else pickup_end_date.humanize(only_distance=False, locale=settings.locale)
        )

        # get company logo
        try:
            picture = shop["store"]["logo_picture"]["current_url"]  # this location fits for the most
        except KeyError:
            try:
                picture = shop["item"]["logo_picture"]["current_url"]  # fits some longer existing ones
            except KeyError:
                # okay, i give up. Take TGTG brand logo.
                picture = "https://toogoodtogo.com/images/logo/econ-textless.svg"

        result_attrs = mqtt_client.publish(
            f"homeassistant/sensor/toogoodtogo_{item_id}/attr",
            json.dumps(
                {
                    "price": price,
                    "stock_available": True if stock > 0 else False,
                    "url": f"http://share.toogoodtogo.com/item/{item_id}",
                    "pickup_start": pickup_start_str,
                    "pickup_start_human": pickup_start_human,
                    "pickup_end": pickup_end_str,
                    "pickup_end_human": pickup_end_human,
                    "picture": picture,
                }
            ),
        )
        logger.debug(
            f"Message published: Autodiscover: {bool(result_ad.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"State: {bool(result_state.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"Attributes: {bool(result_attrs.rc == mqtt.MQTT_ERR_SUCCESS)}"
        )
        if not result_ad.rc == result_state.rc == result_attrs.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.warning("Seems like some message was not transferred successfully.")
            return False

    if settings.get("cleanup"):
        check_for_removed_stores(shops)

    return True


def check_for_removed_stores(shops: []):
    path = settings.get("data_dir") + "/known_shops.json"

    checked_items = [shop["item"]["item_id"] for shop in shops]

    if os.path.isfile(path):
        logger.debug(f"known_shops.json exists at {path}")
        try:
            known_items = json.load(open(path, "r"))
        except:
            logger.error("Error happened when reading known_shops file")
            return

        deprecated_items = [x for x in known_items if x not in checked_items]
        for deprecated_item in deprecated_items:
            logger.info(f"Shop {deprecated_item} was not checked, will send remove message")
            result = mqtt_client.publish(f"homeassistant/sensor/toogoodtogo_{deprecated_item}/config")
            logger.debug(f"Message published: Removal: {bool(result.rc == mqtt.MQTT_ERR_SUCCESS)}")

    Path(settings.get("data_dir")).mkdir(parents=True, exist_ok=True)
    json.dump(checked_items, open(path, "w"))

    pass


def loop(event):
    logger.info("Starting loop")
    while True:
        logger.debug("Loop run started")
        if not check():
            logger.error("Loop was not successfully.")
        else:
            logger.debug("Loop run finished")
            watchdog.reset()
        event.wait(settings.tgtg.every_n_minutes * 60)


def watchdog_handler():
    logger.error(f"Watchdog handler fired! No pull in the last {settings.tgtg.every_n_minutes} minutes!")

    os._exit(1)  # easy way to die from within a thread


def on_disconnect(client, userdata, rc):
    if rc != 0:
        logger.error("Wow, mqtt client lost connection. Will try to reconnect once in 30s.")
        sleep(30)
        logger.debug("Trying to reconnect")
        client.reconnect()


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
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.loop_start()

    event = threading.Event()

    thread = threading.Thread(target=loop, args=(event,))
    thread.start()


if __name__ == "__main__":
    start()
