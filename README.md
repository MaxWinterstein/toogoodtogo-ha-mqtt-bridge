# TooGoodToGoo Home Assistant Mqtt Bridge

Small python tool to forward stock of you favourite stores to Home Assistant via MQTT.

Stores are automatically created via MQTT Discovery and contain some addition attributes like price and more.

## Installation:

### Add-On

Easiest way is to use the Home Assistant Add-on. I created one in [my Home Assistant Add-on repository](https://github.com/MaxWinterstein/homeassistant-addons/).

### Docker

Docker image is created automatically and available at dockerhub: [maxwinterstein/toogoodtogo-ha-mqtt-bridge](https://hub.docker.com/r/maxwinterstein/toogoodtogo-ha-mqtt-bridge)

Create some settings file called `settings.local.json`:

```json
{
  "mqtt": {
    "host": "homeassistant.local",
    "port": 1883,
    "username": "mqtt",
    "password": "mqtt"
  },
  "toogoodtogo": {
    "email": "me@example.ocm",
    "password": "iliketurtles",
    "every_n_minutes": 5
  }
}
```

`every_n_minutes` sets the polling intervall. A value of e.g. 10 would fetch data every 10 minutes.

And start with the mounted settings file, e.g. for Mac OS:

```bash
docker run --rm -ti -v $PWD/settings.local.json:/app/settings.local.json maxwinterstein/toogoodtogo-ha-mqtt-bridge
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
