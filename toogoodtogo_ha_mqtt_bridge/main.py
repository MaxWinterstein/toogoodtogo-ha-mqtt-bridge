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

import arrow
import coloredlogs
import paho.mqtt.client as mqtt
import schedule
from croniter import croniter
from google_play_scraper import app
from packaging import version
from random_user_agent.params import SoftwareName
from random_user_agent.user_agent import UserAgent

from toogoodtogo_ha_mqtt_bridge.config import settings
from toogoodtogo_ha_mqtt_bridge.mytgtgclient import MyTgtgClient
from toogoodtogo_ha_mqtt_bridge.watchdog import Watchdog

logger = logging.getLogger(__name__)
coloredlogs.install(
    level="DEBUG", logger=logger, fmt="%(asctime)s [%(levelname)s] %(message)s"
)  # pretty logging is pretty

mqtt_client: mqtt.Client | None = None
first_run = True
tgtg_client: MyTgtgClient | None = None
tgtg_version: str | None = None
intense_fetch_thread = None
tokens = {}
tokens_rev = 1  # in case of tokens.json changes, bump this
watchdog: Watchdog | None = None
watchdog_timeout = 0
favourite_ids = []
scheduled_jobs = []


def check():
    global first_run
    global favourite_ids
    favourite_ids.clear()

    if not first_run:
        tgtg_client.login()
        write_token_file()

    shops = tgtg_client.get_items(page_size=400)
    for shop in shops:
        stock = shop["items_available"]
        item_id = shop["item"]["item_id"]
        favourite_ids.append(item_id)

        logger.debug(f"Pushing message for {shop['display_name']} // {item_id}")

        # Autodiscover
        result_ad = mqtt_client.publish(
            f"homeassistant/sensor/toogoodtogo_bridge/{item_id}/config",
            json.dumps(
                {
                    "name": f"TooGoodToGo - {shop['display_name']}",
                    "icon": "mdi:food" if stock > 0 else "mdi:food-off",
                    "state_topic": f"homeassistant/sensor/toogoodtogo_{item_id}/state",
                    "json_attributes_topic": f"homeassistant/sensor/toogoodtogo_{item_id}/attr",
                    "unit_of_measurement": "portions",
                    "value_template": "{{ value_json.stock }}",
                    "device": {
                        "identifiers": ["toogoodtogo_bridge"],
                        "manufacturer": "Max Winterstein",
                        "model": "TooGoodToGo favorites",
                        "name": "Too Good To Go",
                    },
                    "unique_id": f"toogoodtogo_{item_id}",
                }
            ),
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
                    "url": f"https://share.toogoodtogo.com/item/{item_id}",
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

    # Start automatic intense fetch watchdog
    if first_run and settings.get("enable_auto_intense_fetch"):
        thread = threading.Thread(target=next_sales_loop)
        thread.start()

    first_run = False

    return True


def build_ua():
    global tgtg_version
    software_names = [SoftwareName.ANDROID.value]
    user_agent_rotator = UserAgent(software_names=software_names, limit=20)
    user_agent = user_agent_rotator.get_random_user_agent()
    user_agent = user_agent.split("(")[1].split(")")[0]

    app_info = app("com.app.tgtg", lang="de", country="de")
    tgtg_version = app_info["version"]
    user_agent = "TGTG/" + app_info["version"] + " Dalvik/2.1.0 (" + user_agent + ")"
    return user_agent


def is_latest_version():
    logger.info("Checking latest tgtg appstore version")
    try:
        app_info = app("com.app.tgtg", lang="de", country="de")
    except Exception as ex:
        logger.error(
            "Error getting version ID from google playstore. Skipping version check this time. "
            "Exception was: " + str(ex)
        )
        return True

    act_version = version.parse(app_info["version"])
    token_version = version.parse(tokens["token_version"])

    # Fix for users having already a tokens.json contain 'Varies with device'
    # see https://github.com/MaxWinterstein/toogoodtogo-ha-mqtt-bridge/issues/87
    if str(token_version) == "Varies with device":
        minor_diff = 999
    else:
        minor_diff = act_version.minor - token_version.minor

    if minor_diff > 2 or act_version.major > token_version.major:
        global tgtg_version
        tgtg_version = app_info["version"]
        return False
    else:
        return True


def is_latest_token_rev():
    return tokens["rev"] >= tokens_rev


def write_token_file():
    global tokens
    tgtg_tokens = {
        "access_token": tgtg_client.access_token,
        "access_token_lifetime": tgtg_client.access_token_lifetime,
        "refresh_token": tgtg_client.refresh_token,
        "user_id": tgtg_client.user_id,
        "last_time_token_refreshed": str(tgtg_client.last_time_token_refreshed),
        "ua": tgtg_client.user_agent,
        "token_version": tgtg_version,
        "cookie_datadome": tgtg_client.cookie_datadome,
        "rev": tokens_rev,
    }
    tokens = tgtg_tokens

    with open(settings.get("data_dir") + "/tokens.json", "w") as json_file:
        json.dump(tgtg_tokens, json_file, indent=4)

    logger.info("Written tokens.json file to filesystem")


def check_existing_token_file():
    if os.path.isfile(settings.get("data_dir") + "/tokens.json"):
        return read_token_file()
    else:
        logger.info("Logging in with credentials")
        return False


def nuke_token_file():
    logger.info("Old tokenfile found. Please login via email again.")
    os.remove(settings.get("data_dir") + "/tokens.json")


def read_token_file():
    global tokens
    with open(settings.get("data_dir") + "/tokens.json") as f:
        tokens = json.load(f)

    if tokens:
        if first_run:
            if "ua" not in tokens or "token_version" not in tokens or "rev" not in tokens:
                nuke_token_file()
                return False
            elif not is_latest_token_rev():
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


def update_ua():
    global tokens
    ua = tokens["ua"]
    updated_ua = ua.split(" ")[1:]
    updated_ua = "TGTG/" + tgtg_version + " " + " ".join(updated_ua)
    tokens["ua"] = updated_ua
    tokens["token_version"] = tgtg_version

    rebuild_tgtg_client()
    write_token_file()


def rebuild_tgtg_client():
    global tgtg_client
    tgtg_client = MyTgtgClient(
        cookie_datadome=tokens["cookie_datadome"],
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        user_id=tokens["user_id"],
        user_agent=tokens["ua"],
        language=settings.tgtg.language,
        timeout=30,
    )


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

    json.dump(checked_items, open(path, "w"))

    pass


def fetch_loop(event):
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


def next_sales_loop():
    while True:
        if favourite_ids:
            for fav_id in favourite_ids:
                item = tgtg_client.get_item(item_id=fav_id)
                if "next_sales_window_purchase_start" in item:
                    next_sales_window = arrow.get(item["next_sales_window_purchase_start"]).to(
                        tz=settings.timezone
                    )
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


def trigger_intense_fetch():
    logger.info("Running automatic intense fetch!")
    mqtt_client.publish(
        f"homeassistant/switch/toogoodtogo_intense_fetch/set",
        "ON",
    )
    return schedule.CancelJob


def ua_check_loop():
    while True:
        now = datetime.now()
        cron = croniter("0 0,12 * * *", now)
        next_run = cron.get_next(datetime)
        sleep_seconds = (next_run - now).seconds
        sleep(sleep_seconds)
        if tokens:
            if not is_latest_version():
                logger.info("Token for old TGTG version found, updating useragent.")
                update_ua()


def calc_next_run():
    cron_schedule = get_cron_schedule()
    now = datetime.now()

    if croniter.is_valid(cron_schedule):
        cron = croniter(cron_schedule, now)
        next_run = cron.get_next(datetime)
        sleep_seconds = (next_run - now).seconds

        if sleep_seconds >= 30:
            if settings.get("randomize_calls"):
                jitter = random.randint(1, 20)
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


def get_fallback_cron(tgtg):
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


def get_cron_schedule():
    tgtg = settings.get("tgtg")
    if "polling_schedule" not in tgtg:
        return get_fallback_cron(tgtg)
    else:
        return tgtg.polling_schedule


def create_data_dir():
    data_dir = settings.get("data_dir")
    if not os.path.isdir(data_dir):
        Path(data_dir).mkdir(parents=True)


def exit_from_thread(message, return_code):
    logger.exception(message)
    os._exit(return_code)


def watchdog_handler():
    exit_from_thread(
        "Watchdog handler fired! No pull in the last " + str(watchdog_timeout / 60) + " minutes!", 1
    )


def on_disconnect(client, userdata, rc):
    if rc != 0:
        logger.error("Wow, mqtt client lost connection. Will try to reconnect once in 30s.")
        sleep(30)
        logger.debug("Trying to reconnect")
        client.reconnect()


def calc_timeout():
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


def intense_fetch():
    if (
        "intense_fetch" not in settings.tgtg
        or "period_of_time" not in settings.tgtg.intense_fetch
        or "interval" not in settings.tgtg.intense_fetch
    ):
        logger.error("Incomplete settings file. Please check the sample!")
        return None

    if settings.tgtg.intense_fetch.period_of_time > 60:
        logger.warning(
            "Stopped intense fetch. Maximal intense fetch period time are 60 minutes. Reduce your setting!"
        )
        return None

    if settings.tgtg.intense_fetch.interval < 10:
        logger.warning(
            "Stopped intense fetch. Minimal intense fetch interval are 10 seconds. Increase your setting!"
        )
        return None

    mqtt_client.publish(
        f"homeassistant/switch/toogoodtogo_intense_fetch/state",
        "ON",
    )

    t = threading.currentThread()
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
        f"homeassistant/switch/toogoodtogo_intense_fetch/state",
        "OFF",
    )

    logger.info("Intense fetch stopped")


def on_message(client, userdata, message):
    global intense_fetch_thread
    if message.topic.endswith("toogoodtogo_intense_fetch/set"):
        if message.payload.decode("utf-8") == "ON":
            if intense_fetch_thread:
                logger.error("Intense fetch thread already running. Doing nothing.")
                return None

            thread = threading.Thread(target=intense_fetch)
            intense_fetch_thread = thread
            thread.start()
        elif message.payload.decode("utf-8") == "OFF":
            if intense_fetch_thread:
                intense_fetch_thread.do_run = False
                logger.info("Intense fetch is stopped in the next cycle.")
                mqtt_client.publish(
                    f"homeassistant/switch/toogoodtogo_intense_fetch/state",
                    "OFF",
                )
            else:
                logger.info("No running thread found. Doing nothing.")


def register_fetch_sensor():
    mqtt_client.publish(
        f"homeassistant/switch/toogoodtogo_bridge/intense_fetch/config",
        json.dumps(
            {
                "name": "Intense fetch",
                "icon": "mdi:fast-forward",
                "state_topic": "homeassistant/switch/toogoodtogo_intense_fetch/state",
                "command_topic": "homeassistant/switch/toogoodtogo_intense_fetch/set",
                "device": {
                    "identifiers": ["toogoodtogo_bridge"],
                    "manufacturer": "Max Winterstein",
                    "model": "TooGoodToGo favorites",
                    "name": "Too Good To Go",
                },
                "unique_id": f"toogoodtogo_intense_fetch_switch",
            }
        ),
    )

    mqtt_client.publish(
        f"homeassistant/switch/toogoodtogo_intense_fetch/state",
        "OFF",
    )


def run_pending_schedules():
    while True:
        schedule.run_pending()
        time.sleep(1)


def start():
    global tgtg_client, watchdog, mqtt_client
    tgtg_client = MyTgtgClient(
        email=settings.tgtg.email, language=settings.tgtg.language, timeout=30, user_agent=build_ua()
    )

    watchdog = Watchdog(
        timeout=calc_timeout(),
        user_handler=watchdog_handler,
    )

    logger.info("Connecting mqtt")
    mqtt_client = mqtt.Client("toogoodtogo-ha-mqtt-bridge")
    if settings.mqtt.username:
        mqtt_client.username_pw_set(username=settings.mqtt.username, password=settings.mqtt.password)
    mqtt_client.connect(host=settings.mqtt.host, port=int(settings.mqtt.port))
    mqtt_client.on_disconnect = on_disconnect

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
