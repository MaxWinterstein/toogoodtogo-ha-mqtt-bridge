from freezegun import freeze_time

from toogoodtogo_ha_mqtt_bridge.config import settings
from toogoodtogo_ha_mqtt_bridge.main import calc_next_run


# freeze time to specific moment for stable test, tz_offset might not reflect DST
@freeze_time("2022-01-01 17:00:00", tz_offset=1)
def test_calc_next_run():

    # When we set the  polling interval to every 10 minutes
    settings["tgtg"] = {"polling_schedule": "*/10 * * * *"}

    # and calculate the seconds to sleep until next fetch
    sleep_seconds = calc_next_run()

    # we would expect to sleep less than 10 minutes
    print(f"Would sleep {sleep_seconds / 60:.3} minutes")
    assert sleep_seconds <= 60 * 10
