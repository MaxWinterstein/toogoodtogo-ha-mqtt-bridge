import datetime as dt

import arrow
import pytest
from freezegun import freeze_time

from toogoodtogo_ha_mqtt_bridge.config import settings
from toogoodtogo_ha_mqtt_bridge.main import calc_next_run


# freeze time to specific moment for stable test, tz_offset might not reflect DST
@pytest.mark.parametrize(
    "_time,_cron,expected",
    [
        ("2022-01-01 17:00:00", "*/10 * * * *", "2022-01-01 17:10:00"),
        ("2022-11-12 20:54:44", "*/10 7-20 * * *", "2022-11-13 07:00:00"),
    ],
)
def test_calc_next_run(_time, _cron, expected):

    expected_date = dt.datetime.strptime(expected, "%Y-%m-%d %H:%M:%S")
    time_date = dt.datetime.strptime(_time, "%Y-%m-%d %H:%M:%S")

    # When we set the  polling interval to _cron
    settings["tgtg"] = {"polling_schedule": _cron}

    # and calculate the seconds to sleep until next fetch
    with freeze_time(_time):
        sleep_seconds = calc_next_run()

    # we would expect to sleep less than expected_sleep plus maybe a little randomize
    present = arrow.utcnow()
    future = present.shift(seconds=sleep_seconds)
    print(f'Would sleep {present.humanize(future, granularity=["hour", "minute"])}')
    assert (
        expected_date
        <= time_date + dt.timedelta(seconds=sleep_seconds)
        <= expected_date + dt.timedelta(seconds=20)
    )
