from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Any

import arrow
import click
import coloredlogs
import paho.mqtt.client as mqtt
import schedule
from croniter import croniter
from google_play_scraper import app
from packaging import version
from random_user_agent.params import SoftwareName
from random_user_agent.user_agent import UserAgent
from tgtg import TgtgClient

from toogoodtogo_ha_mqtt_bridge.config import settings
from toogoodtogo_ha_mqtt_bridge.watchdog import Watchdog

logger = logging.getLogger(__name__)
coloredlogs.install(
    level="DEBUG", logger=logger, fmt="%(asctime)s [%(levelname)s] %(message)s"
)  # pretty logging is pretty

mqtt_client: mqtt.Client = None  # type: ignore[assignment]
first_run = True
tgtg_client: TgtgClient = None  # type: ignore[no-any-unimported]
tgtg_version: str | None = None
intense_fetch_thread = None
tokens: dict[Any, Any] = {}
tokens_rev = 2  # in case of tokens.json changes, bump this
watchdog: Watchdog = None  # type: ignore[assignment]
watchdog_timeout = 0
favourite_ids: list[int] = []
scheduled_jobs: list[Any] = []

DEVICE_INFO = {
    "identifiers": ["toogoodtogo_bridge"],
    "manufacturer": "Max Winterstein",
    "model": "TooGoodToGo favorites",
    "name": "Too Good To Go",
}


def check() -> bool:
    global first_run

    if not first_run:
        tgtg_client.login()
        write_token_file()

    try:
        shops = tgtg_client.get_items(page_size=400)
        if not publish_stores_data(shops):
            return False
    except Exception as e:
        logger.error(f"Error fetching stores: {e}")
        return False

    if settings.get("cleanup"):
        check_for_removed_stores(shops)

    try:
        active_orders = tgtg_client.get_active()
        if not publish_orders_data(active_orders):
            return False
    except Exception as e:
        logger.error(f"Error fetching active orders: {e}")
        return False

    # Start automatic intense fetch watchdog
    if first_run and settings.get("enable_auto_intense_fetch"):
        thread = threading.Thread(target=next_sales_loop)
        thread.start()

    first_run = False

    return True


def publish_stores_data(shops):
    global favourite_ids
    favourite_ids.clear()

    for shop in shops:
        stock = shop["items_available"]
        item_id = shop["item"]["item_id"]
        favourite_ids.append(item_id)

        logger.debug(f"Pushing message for {shop['display_name']} // {item_id}")

        # Autodiscover
        result_ad = mqtt_client.publish(
            f"homeassistant/sensor/toogoodtogo_bridge/{item_id}/config",
            json.dumps({
                "name": f"TooGoodToGo - {shop['display_name']}",
                "icon": "mdi:food" if stock > 0 else "mdi:food-off",
                "state_topic": f"homeassistant/sensor/toogoodtogo_{item_id}/state",
                "json_attributes_topic": f"homeassistant/sensor/toogoodtogo_{item_id}/attr",
                "unit_of_measurement": "portions",
                "value_template": "{{ value_json.stock }}",
                "device": DEVICE_INFO,
                "unique_id": f"toogoodtogo_{item_id}",
            }),
        )

        result_state = mqtt_client.publish(
            f"homeassistant/sensor/toogoodtogo_{item_id}/state",
            json.dumps({"stock": stock}),
        )

        # prepare attrs
        if shop["item"].get("price"):
            price = shop["item"]["price"]["minor_units"] / pow(10, shop["item"]["price"]["decimals"])
        elif shop["item"].get("price_including_taxes"):
            price = shop["item"]["price_including_taxes"]["minor_units"] / pow(
                10, shop["item"]["price_including_taxes"]["decimals"]
            )
        else:
            logger.error("Can't find price")
            price = 0

        pickup_start_date = None if not stock else arrow.get(shop["pickup_interval"]["start"]).to(tz=settings.timezone)
        pickup_end_date = None if not stock else arrow.get(shop["pickup_interval"]["end"]).to(tz=settings.timezone)
        pickup_start_str = ("Unknown" if stock == 0 else pickup_start_date.to(tz=settings.timezone).isoformat(),)  # type: ignore[union-attr]
        pickup_end_str = ("Unknown" if stock == 0 else pickup_end_date.to(tz=settings.timezone).isoformat(),)  # type: ignore[union-attr]
        pickup_start_human = (
            "Unknown" if stock == 0 else pickup_start_date.humanize(only_distance=False, locale=settings.locale)  # type: ignore[union-attr]
        )
        pickup_end_human = (
            "Unknown" if stock == 0 else pickup_end_date.humanize(only_distance=False, locale=settings.locale)  # type: ignore[union-attr]
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
            json.dumps({
                "price": price,
                "stock_available": True if stock > 0 else False,
                "url": f"https://share.toogoodtogo.com/item/{item_id}",
                "pickup_start": pickup_start_str,
                "pickup_start_human": pickup_start_human,
                "pickup_end": pickup_end_str,
                "pickup_end_human": pickup_end_human,
                "picture": picture,
            }),
        )
        logger.debug(
            f"Message published: Autodiscover: {bool(result_ad.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"State: {bool(result_state.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"Attributes: {bool(result_attrs.rc == mqtt.MQTT_ERR_SUCCESS)}"
        )
        if not result_ad.rc == result_state.rc == result_attrs.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.warning("Seems like some message was not transferred successfully.")
            return False

    return True


def publish_orders_data(active_orders):
    orders = active_orders.get("orders", [])
    has_orders = len(orders) > 0

    result_ad = mqtt_client.publish(
        "homeassistant/sensor/toogoodtogo_next_collection/config",
        json.dumps({
            "name": "TooGoodToGo - Next Collection",
            "icon": "mdi:calendar-clock" if has_orders else "mdi:calendar-remove",
            "device_class": "timestamp",
            "entity_category": "diagnostic",
            "state_topic": "homeassistant/sensor/toogoodtogo_next_collection/state",
            "json_attributes_topic": "homeassistant/sensor/toogoodtogo_next_collection/attr",
            "device": DEVICE_INFO,
            "unique_id": "toogoodtogo_next_collection",
        }),
    )

    result_ad_count = mqtt_client.publish(
        "homeassistant/sensor/toogoodtogo_upcoming_orders/config",
        json.dumps({
            "name": "TooGoodToGo - Upcoming Orders",
            "icon": "mdi:cart" if has_orders else "mdi:cart-off",
            "entity_category": "diagnostic",
            "state_topic": "homeassistant/sensor/toogoodtogo_upcoming_orders/state",
            "json_attributes_topic": "homeassistant/sensor/toogoodtogo_upcoming_orders/attr",
            "unit_of_measurement": "orders",
            "device": DEVICE_INFO,
            "unique_id": "toogoodtogo_upcoming_orders",
        }),
    )

    if orders:
        orders.sort(key=lambda x: x["pickup_interval"]["start"])
        next_order = orders[0]

        pickup_date = next_order["pickup_interval"]["start"]
        pickup_date_arrow = arrow.get(pickup_date).to(tz=settings.timezone)

        result_state = mqtt_client.publish(
            "homeassistant/sensor/toogoodtogo_next_collection/state",
            pickup_date_arrow.isoformat(),
        )

        result_attrs = mqtt_client.publish(
            "homeassistant/sensor/toogoodtogo_next_collection/attr",
            json.dumps({
                "order_id": next_order["order_id"],
                "store_name": next_order["store_name"],
                "store_branch": next_order["store_branch"],
                "address": next_order["pickup_location"]["address"]["address_line"],
                "pickup_start": arrow.get(next_order["pickup_interval"]["start"]).to(tz=settings.timezone).isoformat(),
                "pickup_end": arrow.get(next_order["pickup_interval"]["end"]).to(tz=settings.timezone).isoformat(),
                "pickup_start_human": pickup_date_arrow.humanize(only_distance=False, locale=settings.locale),
                "status": next_order["state"],
                "quantity": next_order["quantity"],
                "price": next_order["total_price"]["minor_units"] / pow(10, next_order["total_price"]["decimals"]),
                "item_name": next_order["item_name"],
                "store_logo": next_order["store_logo"]["current_url"],
                "item_cover_image": next_order["item_cover_image"]["current_url"],
            }),
        )

        result_state_count = mqtt_client.publish(
            "homeassistant/sensor/toogoodtogo_upcoming_orders/state",
            str(len(orders)),
        )

        orders_summary = [
            {
                "store_name": order["store_name"],
                "store_branch": order["store_branch"],
                "pickup_start": arrow.get(order["pickup_interval"]["start"]).to(tz=settings.timezone).isoformat(),
                "pickup_end": arrow.get(order["pickup_interval"]["end"]).to(tz=settings.timezone).isoformat(),
                "quantity": order["quantity"],
                "item_name": order["item_name"],
            }
            for order in orders
        ]

        result_attrs_count = mqtt_client.publish(
            "homeassistant/sensor/toogoodtogo_upcoming_orders/attr",
            json.dumps({"orders": orders_summary}),
        )

        logger.debug(
            f"Next collection sensor published: Autodiscover: {bool(result_ad.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"State: {bool(result_state.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"Attributes: {bool(result_attrs.rc == mqtt.MQTT_ERR_SUCCESS)}"
        )
        logger.debug(
            f"Upcoming orders sensor published: Autodiscover: {bool(result_ad_count.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"State: {bool(result_state_count.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"Attributes: {bool(result_attrs_count.rc == mqtt.MQTT_ERR_SUCCESS)}"
        )

        if not (
            result_ad.rc
            == result_state.rc
            == result_attrs.rc
            == result_ad_count.rc
            == result_state_count.rc
            == result_attrs_count.rc
            == mqtt.MQTT_ERR_SUCCESS
        ):
            logger.warning("Seems like some message was not transferred successfully.")
            return False

    else:
        result_state = mqtt_client.publish(
            "homeassistant/sensor/toogoodtogo_next_collection/state",
            "unknown",
        )
        result_attrs = mqtt_client.publish(
            "homeassistant/sensor/toogoodtogo_next_collection/attr",
            json.dumps({}),
        )
        result_state_count = mqtt_client.publish(
            "homeassistant/sensor/toogoodtogo_upcoming_orders/state",
            "0",
        )
        result_attrs_count = mqtt_client.publish(
            "homeassistant/sensor/toogoodtogo_upcoming_orders/attr",
            json.dumps({"orders": []}),
        )

        logger.debug(
            f"Empty sensors published: Autodiscover: {bool(result_ad.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"State: {bool(result_state.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"Attributes: {bool(result_attrs.rc == mqtt.MQTT_ERR_SUCCESS)}"
        )
        logger.debug(
            f"Empty count sensor published: Autodiscover: {bool(result_ad_count.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"State: {bool(result_state_count.rc == mqtt.MQTT_ERR_SUCCESS)}, "
            f"Attributes: {bool(result_attrs_count.rc == mqtt.MQTT_ERR_SUCCESS)}"
        )

        if not (
            result_ad.rc
            == result_state.rc
            == result_attrs.rc
            == result_ad_count.rc
            == result_state_count.rc
            == result_attrs_count.rc
            == mqtt.MQTT_ERR_SUCCESS
        ):
            logger.warning("Seems like some message was not transferred successfully.")
            return False

    return True


def build_ua() -> Any:
    global tgtg_version
    software_names = [SoftwareName.ANDROID.value]
    user_agent_rotator = UserAgent(software_names=software_names, limit=20)
    user_agent = user_agent_rotator.get_random_user_agent()
    user_agent = user_agent.split("(")[1].split(")")[0]

    app_info = app("com.app.tgtg", lang="de", country="de")
    tgtg_version = app_info["version"]
    user_agent = "TGTG/" + app_info["version"] + " Dalvik/2.1.0 (" + user_agent + ")"
    return user_agent


def is_latest_version() -> bool:
    logger.info("Checking latest tgtg appstore version")
    try:
        app_info = app("com.app.tgtg", lang="de", country="de")
    except Exception:
        logger.exception("Error getting version ID from google playstore. Skipping version check this time.")
        return True

    act_version = version.parse(app_info["version"])
    token_version = version.parse(tokens["token_version"])

    # Fix for users having already a tokens.json contain 'Varies with device'
    # see https://github.com/MaxWinterstein/toogoodtogo-ha-mqtt-bridge/issues/87
    minor_diff = 999 if str(token_version) == "Varies with device" else act_version.minor - token_version.minor

    if minor_diff > 2 or act_version.major > token_version.major:
        global tgtg_version
        tgtg_version = app_info["version"]
        return False
    else:
        return True


def is_latest_token_rev() -> Any:
    return tokens["rev"] >= tokens_rev


def write_token_file() -> None:
    global tokens
    tgtg_tokens = {
        "access_token": tgtg_client.access_token,
        "access_token_lifetime": tgtg_client.access_token_lifetime,
        "refresh_token": tgtg_client.refresh_token,
        "cookie": tgtg_client.cookie,
        "last_time_token_refreshed": str(tgtg_client.last_time_token_refreshed),
        "ua": tgtg_client.user_agent,
        "token_version": tgtg_version,
        "rev": tokens_rev,
    }
    tokens = tgtg_tokens

    with open(settings.get("data_dir") + "/tokens.json", "w") as json_file:
        json.dump(tgtg_tokens, json_file, indent=4)

    logger.info("Written tokens.json file to filesystem")


def check_existing_token_file() -> bool:
    if os.path.isfile(settings.get("data_dir") + "/tokens.json"):
        return read_token_file()
    else:
        logger.info("Logging in with credentials")
        return False


def nuke_token_file() -> None:
    logger.info("Old tokenfile found. Please login via email again.")
    os.remove(settings.get("data_dir") + "/tokens.json")


def read_token_file() -> bool:
    global tokens
    with open(settings.get("data_dir") + "/tokens.json") as f:
        tokens = json.load(f)

    if tokens:
        if first_run and (
            "ua" not in tokens or "token_version" not in tokens or "rev" not in tokens or not is_latest_token_rev()
        ):
            nuke_token_file()
            return False

        if not is_latest_version():
            logger.info("Token for old TGTG version found, updating useragent.")
            update_ua()
        else:
            rebuild_tgtg_client()

        logger.info("Loaded tokens form tokenfile. Logging in with tokens.")
        return True
    else:
        return False


def update_ua() -> None:
    global tokens
    ua = tokens["ua"]
    updated_ua = ua.split(" ")[1:]
    updated_ua = "TGTG/" + tgtg_version + " " + " ".join(updated_ua)  # type: ignore[operator]
    tokens["ua"] = updated_ua
    tokens["token_version"] = tgtg_version

    rebuild_tgtg_client()
    write_token_file()


def rebuild_tgtg_client() -> None:
    global tgtg_client
    tgtg_client = TgtgClient(
        cookie=tokens["cookie"],
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        user_agent=tokens["ua"],
        language=settings.tgtg.language,
        timeout=30,
    )


def check_for_removed_stores(shops: list[Any]) -> None:
    path = settings.get("data_dir") + "/known_shops.json"

    checked_items = [shop["item"]["item_id"] for shop in shops]

    if os.path.isfile(path):
        logger.debug(f"known_shops.json exists at {path}")
        try:
            with open(path) as f:
                known_items = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.exception("Error happened when reading known_shops file")
            return

        deprecated_items = [x for x in known_items if x not in checked_items]
        for deprecated_item in deprecated_items:
            logger.info(f"Shop {deprecated_item} was not checked, will send remove message")
            result = mqtt_client.publish(f"homeassistant/sensor/toogoodtogo_{deprecated_item}/config")
            logger.debug(f"Message published: Removal: {bool(result.rc == mqtt.MQTT_ERR_SUCCESS)}")

    with open(path, "w") as f:
        json.dump(checked_items, f)

    pass


def fetch_loop(event: Any) -> None:
    logger.info("Starting loop")

    create_data_dir()
    token_exits = check_existing_token_file()
    tgtg_client.login()
    if not token_exits and tgtg_client.access_token:
        write_token_file()

    event.wait(calc_next_run())
    while True:
        logger.debug("Loop run started")

        if not intense_fetch_thread:
            if not check():
                logger.error("Loop was not successfully.")
            else:
                logger.debug("Loop run finished")
        else:
            logger.info("Skipping cron scheduled job, as intense fetch is running")

        watchdog.timeout = calc_timeout()
        watchdog.reset()
        event.wait(calc_next_run())


def next_sales_loop() -> None:
    while True:
        if favourite_ids:
            for fav_id in favourite_ids:
                item = tgtg_client.get_item(item_id=fav_id)
                if "next_sales_window_purchase_start" in item:
                    next_sales_window = arrow.get(item["next_sales_window_purchase_start"]).to(tz=settings.timezone)
                    if next_sales_window > arrow.now(tz=settings.timezone):
                        schedule_time = next_sales_window.format("HH:mm")
                        schedule_name = item["display_name"] + " " + schedule_time

                        global scheduled_jobs
                        if not any(d["name"] == schedule_name for d in scheduled_jobs):
                            job = (
                                schedule.every()
                                .day.at(next_sales_window.shift(minutes=-1).format("HH:mm"))
                                .do(trigger_intense_fetch)
                            )
                            scheduled_jobs.append({"name": schedule_name, "job": job})
                            logger.info(
                                "Added new automatic intense fetch run for "
                                + item["display_name"]
                                + " at "
                                + schedule_time
                                + " today"
                            )

        logger.debug("Scheduled automatic intense jobs: " + str(scheduled_jobs))

        now = datetime.now()
        cron = croniter("0 8,11,14,17,20 * * *", now)
        next_run = cron.get_next(datetime)
        sleep_seconds = (next_run - now).seconds
        sleep(sleep_seconds)


def trigger_intense_fetch() -> Any:
    logger.info("Running automatic intense fetch!")
    mqtt_client.publish(
        "homeassistant/switch/toogoodtogo_intense_fetch/set",
        "ON",
    )
    return schedule.CancelJob


def ua_check_loop() -> None:
    while True:
        now = datetime.now()
        cron = croniter("0 0,12 * * *", now)
        next_run = cron.get_next(datetime)
        sleep_seconds = (next_run - now).seconds
        sleep(sleep_seconds)
        if tokens and not is_latest_version():
            logger.info("Token for old TGTG version found, updating useragent.")
            update_ua()


def calc_next_run() -> Any:
    cron_schedule = get_cron_schedule()
    now = datetime.now()

    if croniter.is_valid(cron_schedule):
        cron = croniter(cron_schedule, now)
        next_run = cron.get_next(datetime)
        sleep_seconds = (next_run - now).seconds

        if sleep_seconds >= 30:
            if settings.get("randomize_calls"):
                jitter = random.randint(1, 20)  # noqa: S311 # pseudo is fine here
                sleep_seconds += jitter
                next_run = next_run + timedelta(seconds=jitter)
        elif sleep_seconds < 30:
            # if sleep seconds < 30 skip next runtime
            next_run = cron.get_next(datetime)
            sleep_seconds = (next_run - now).seconds

        logger.info("Next run at " + str(next_run))
        return sleep_seconds + 1
    else:
        exit_from_thread("Invalid cron schedule", 1)
        return  # might never be returned


def get_fallback_cron(tgtg: Any) -> str:
    # Create fallback cron with old every_n_minutes setting
    if "every_n_minutes" not in tgtg:
        exit_from_thread("No interval found in settings, please check your config.", 1)

    if first_run:
        logger.warning(
            "Deprecation waring! - The setting 'every_n_minutes' is not supported anymore. \n"
            "Please use cron schedule setting 'polling_schedule'. \n"
            "If you don't know what to do, have a look at here: "
            "https://github.com/MaxWinterstein/toogoodtogo-ha-mqtt-bridge"
        )

    return "*/" + str(tgtg.every_n_minutes) + " * * * *"


def get_cron_schedule() -> Any:
    tgtg = settings.get("tgtg")
    if "polling_schedule" not in tgtg:
        return get_fallback_cron(tgtg)
    else:
        return tgtg.polling_schedule


def create_data_dir() -> None:
    data_dir = settings.get("data_dir")
    if not os.path.isdir(data_dir):
        Path(data_dir).mkdir(parents=True)


def exit_from_thread(message: str, return_code: int) -> None:
    logger.exception(message)
    os._exit(return_code)


def watchdog_handler() -> None:
    exit_from_thread("Watchdog handler fired! No pull in the last " + str(watchdog_timeout / 60) + " minutes!", 1)


def on_connect(client, userdata, flags, reason_code, properties) -> None:  # type: ignore[no-untyped-def]
    logger.debug(f"MQTT seems connected. (reason_code: {reason_code})")


def on_disconnect(client, userdata, flags, reason_code, properties) -> None:  # type: ignore[no-untyped-def]
    if reason_code != 0:
        logger.error("Wow, mqtt client lost connection. Will try to reconnect once in 30s.")
        logger.debug(f"reason_code: {reason_code}")
        sleep(30)
        logger.debug("Trying to reconnect")
        client.reconnect()


def calc_timeout() -> Any:
    global watchdog_timeout
    now = datetime.now()
    cron_schedule = get_cron_schedule()

    if croniter.is_valid(cron_schedule):
        # Get next run as base
        base = croniter(cron_schedule, now).get_next(datetime)
        # Get next two runs and calculate watchdog timeout
        itr = croniter(cron_schedule, base)
        for _ in range(2):
            next_run = itr.get_next(datetime)
        watchdog_timeout = (next_run - now).seconds + tgtg_client.timeout
        return watchdog_timeout
    else:
        exit_from_thread("Invalid cron schedule", 1)
        return  # might never be returned


def intense_fetch() -> None:
    if (
        "intense_fetch" not in settings.tgtg
        or "period_of_time" not in settings.tgtg.intense_fetch
        or "interval" not in settings.tgtg.intense_fetch
    ):
        logger.error("Incomplete settings file. Please check the sample!")
        return

    if settings.tgtg.intense_fetch.period_of_time > 60:
        logger.warning("Stopped intense fetch. Maximal intense fetch period time are 60 minutes. Reduce your setting!")
        return

    if settings.tgtg.intense_fetch.interval < 10:
        logger.warning("Stopped intense fetch. Minimal intense fetch interval are 10 seconds. Increase your setting!")
        return

    mqtt_client.publish(
        "homeassistant/switch/toogoodtogo_intense_fetch/state",
        "ON",
    )

    t = threading.current_thread()
    t_end = time.time() + 60 * settings.tgtg.intense_fetch.period_of_time

    while time.time() < t_end and getattr(t, "do_run", True):
        logger.info("Intense fetch started")
        if not check():
            logger.error("Intense fetch was not successfully")
        else:
            logger.info("Intense fetch finished")
            sleep(settings.tgtg.intense_fetch.interval)

    global intense_fetch_thread
    intense_fetch_thread = None

    mqtt_client.publish(
        "homeassistant/switch/toogoodtogo_intense_fetch/state",
        "OFF",
    )

    logger.info("Intense fetch stopped")


def on_message(client: Any, userdata: Any, message: Any) -> None:
    global intense_fetch_thread
    if message.topic.endswith("toogoodtogo_intense_fetch/set"):
        if message.payload.decode("utf-8") == "ON":
            if intense_fetch_thread:
                logger.error("Intense fetch thread already running. Doing nothing.")
                return

            thread = threading.Thread(target=intense_fetch)
            intense_fetch_thread = thread
            thread.start()
        elif message.payload.decode("utf-8") == "OFF":
            if intense_fetch_thread:
                intense_fetch_thread.do_run = False  # type: ignore[attr-defined]
                logger.info("Intense fetch is stopped in the next cycle.")
                mqtt_client.publish(
                    "homeassistant/switch/toogoodtogo_intense_fetch/state",
                    "OFF",
                )
            else:
                logger.info("No running thread found. Doing nothing.")


def register_fetch_sensor() -> None:
    mqtt_client.publish(
        "homeassistant/switch/toogoodtogo_bridge/intense_fetch/config",
        json.dumps({
            "name": "Intense fetch",
            "icon": "mdi:fast-forward",
            "state_topic": "homeassistant/switch/toogoodtogo_intense_fetch/state",
            "command_topic": "homeassistant/switch/toogoodtogo_intense_fetch/set",
            "device": DEVICE_INFO,
            "unique_id": "toogoodtogo_intense_fetch_switch",
        }),
    )

    mqtt_client.publish(
        "homeassistant/switch/toogoodtogo_intense_fetch/state",
        "OFF",
    )


def run_pending_schedules() -> None:
    while True:
        schedule.run_pending()
        time.sleep(1)


@click.command()
@click.version_option(package_name="toogoodtogo_ha_mqtt_bridge")
def start() -> None:
    global tgtg_client, watchdog, mqtt_client
    tgtg_client = TgtgClient(
        email=settings.tgtg.email, language=settings.tgtg.language, timeout=30, user_agent=build_ua()
    )

    watchdog = Watchdog(
        timeout=calc_timeout(),
        user_handler=watchdog_handler,
    )

    logger.info("Connecting mqtt")
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="toogoodtogo-ha-mqtt-bridge")
    if settings.mqtt.username:
        mqtt_client.username_pw_set(username=settings.mqtt.username, password=settings.mqtt.password)
    mqtt_client.connect(host=settings.mqtt.host, port=int(settings.mqtt.port))
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_connect = on_connect

    if "intense_fetch" in settings.tgtg:
        mqtt_client.subscribe("homeassistant/switch/toogoodtogo_intense_fetch/set")
        register_fetch_sensor()
        mqtt_client.on_message = on_message

    mqtt_client.loop_start()
    event = threading.Event()
    thread = threading.Thread(target=fetch_loop, args=(event,))
    thread.start()

    thread = threading.Thread(target=run_pending_schedules)
    thread.start()

    thread = threading.Thread(target=ua_check_loop)
    thread.start()


if __name__ == "__main__":
    start()
