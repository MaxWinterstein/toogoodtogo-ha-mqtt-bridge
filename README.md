# TooGoodToGoo Home Assistant Mqtt Bridge

<a href="https://www.buymeacoffee.com/MaxWinterstein" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="41" width="174"></a>

Small python tool to forward stock of you favourite stores to Home Assistant via MQTT.

Stores are automatically created via MQTT Discovery and contain some addition attributes like price and more.

## Installation:

### Add-On

Easiest way is to use the Home Assistant Add-on. I created one in [my Home Assistant Add-on repository](https://github.com/MaxWinterstein/homeassistant-addons/).

### Docker

Docker image is created automatically and available at dockerhub: [maxwinterstein/toogoodtogo-ha-mqtt-bridge](https://hub.docker.com/r/maxwinterstein/toogoodtogo-ha-mqtt-bridge)

Create some settings file called `settings.local.json` (see [`settings.example.json`](https://github.com/MaxWinterstein/toogoodtogo-ha-mqtt-bridge/blob/main/toogoodtogo_ha_mqtt_bridge/settings.example.json)):

```json
{
  "mqtt": {
    "host": "homeassistant.local",
    "port": 1883,
    "username": "mqtt",
    "password": "mqtt"
  },
  "tgtg": {
    "email": "me@example.ocm",
    "language": "en-US",
    "polling_schedule": "*/10 * * * *"
  },
  "timezone": "Europe/Berlin",
  "locale": "en_us"
}
```

`polling_schedule` sets the polling intervall in cron notation. For more Infomation have a look here: https://crontab.guru/

`timezone` (optional) as TooGoodToGo provides its times as UTC we format it to local time. See [Wikipedia](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for valid values.

`locale` (optional) to format pickup times like _in 2 hours_. E.g. `de` for german, `en_us` for american english.

`cleanup` (optional) remove items from Home Assistant if they are no longer in the fetched result.

`data_dir` (optional) folder to store persistent data. Needed e.g. for `cleanup` feature.

And start with the mounted settings file, e.g. for macOS:

```bash
docker run --rm -ti --pull always -v $PWD/settings.local.json:/app/settings.local.json -v $PWD/data/:/data maxwinterstein/toogoodtogo-ha-mqtt-bridge
```

## Attributes

Attributes are used to keep the amount of sensors small. If you want some specific sensor you can create it as template sensor.

```yaml
sensor:
  - platform: template
    sensors:
      toogoodtogo_eilles_frankfurt_price:
        unit_of_measurement: "€"
        icon_template: mdi:currency-eur
        friendly_name: "Eilles Frankfurt Price"
        value_template: "{{ state_attr('sensor.toogoodtogo_eilles_frankfurt', 'price') }}"
```
