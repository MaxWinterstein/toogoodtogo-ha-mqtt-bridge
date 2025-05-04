# TooGoodToGo Home Assistant Mqtt Bridge

[![ko-fi](https://www.ko-fi.com/img/donate_sm.png)](https://ko-fi.com/MaxWinterstein)

Small python tool to forward stock of you favourite stores to Home Assistant via MQTT.

Stores are automatically created via MQTT Discovery and contain some addition attributes like price and more.

![Screen Shot 2022-06-26 at 19 24 32](https://user-images.githubusercontent.com/5927148/175826396-0a9c5c16-ae7f-4809-a8a7-5eda18b96370.png)

## Installation:

### Home Assistant Add-On

Easiest way is to use the Home Assistant Add-on. I created one in [my Home Assistant Add-on repository](https://github.com/MaxWinterstein/homeassistant-addons/).

### Docker

Docker image is created automatically and available at dockerhub: [maxwinterstein/toogoodtogo-ha-mqtt-bridge](https://hub.docker.com/r/maxwinterstein/toogoodtogo-ha-mqtt-bridge)

### Native

This project uses some awesome tools for better developer expirence:

- [`direnv`](https://direnv.net/)
- [`pkgx`](https://pkgx.dev/)
- [`Taskfile`](https://taskfile.dev/)

None of this is required to run it natively, but makes the job a lot easier.

There are multiples ways to get going, some of the fastest might be using `pkgx` and `direnv`.
Having both pre-installed on your machine (see related docs) will get you going this quick:

- Adjust `settings.local.json` - see below
- Start a _pkgx_ dev environment
  ```bash
  dev # provided by pkgx - will read pkgx.yaml and install deps
  ```
- Run it though Taskfile (the better Makefile INHO)
  ```bash
  task run
  ```

### Configuration

_Not needed when using HA Add-On_

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
    "polling_schedule": "*/10 * * * *",
    "intense_fetch": { "interval": 30, "period_of_time": 5 }
  },
  "timezone": "Europe/Berlin",
  "locale": "en_us"
}
```

#### `tgtg.polling_schedule`

sets the polling interval in cron notation. For more Infomation have a look here: https://crontab.guru/

#### `tgtg.intense_fetch` (optional)

Is meant query your favourites for a short amount of time with a higher frequency.
Ideal for those boxes you always miss!
With the `interval`, the time between the queries can be controlled.
With the setting `period_of_time` the duration of the intense fetch can be defined.
The smallest interval is 10 seconds, and the maximum duration of the intense_fetch is 60 minutes.
**Attention:** This is meant for expierenced users as you might get blocked for a certain amount of time by toogoodtogo.

#### `enable_auto_intense_fetch` (optional)

When enabled, above mentioned `intense_fetch` will be started automatically when a shops sales window (automatically created portions) starts.

#### `randomize_calls` (optional)

We add some [jitter](https://en.wikipedia.org/wiki/Jitter) on the fetch interval, so not everyone hits the poor API at the same second.

#### `timezone` (optional)

as TooGoodToGo provides its times as UTC we format it to local time. See [Wikipedia](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) for valid values.

#### `locale` (optional)

to format pickup times like _in 2 hours_. E.g. `de` for german, `en_us` for american english.

#### `cleanup` (optional)

remove items from Home Assistant if they are no longer in the fetched result.

#### `data_dir` (optional)

folder to store persistent data. Needed e.g. for `cleanup` feature.

And start with the mounted settings file, e.g. for macOS:

```bash
docker run --rm -ti --pull always -v $PWD/settings.local.json:/app/settings.local.json -v $PWD/data/:/data -v /etc/localtime:/etc/localtime:ro maxwinterstein/toogoodtogo-ha-mqtt-bridge
```

Or using docker-compose:

```yaml
version: "3"
services:
  toogoodtogo-bridge:
    image: maxwinterstein/toogoodtogo-ha-mqtt-bridge
    container_name: toogoodtogo-bridge
    volumes:
      - ./settings.local.json:/app/settings.local.json
      - ./data:/data
      - /etc/localtime:/etc/localtime:ro
    restart: unless-stopped
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

## Get a list of all toogoodtogo sensors:

Add the following piece of code into /developer-tools/template in HomeAssistant. (Remove the last comma)

```yaml
{%- for state in states -%}
    {%- if (state.entity_id.startswith('sensor.toogoodtogo_'))-%}
      {{state.entity_id}},
    {%- endif -%}
{%- endfor -%}
```

## Example HomeAssistant Automation

### Notification

```yaml
alias: TooGoodToGo Notification
description: "Sends a notification when a toogoodtogo offer becomes available"
trigger:
  - platform: state
    entity_id:
      >- # This is your list of toogoodoto sensors which are generated (Copy paste the list from above)
      sensor.toogoodtogo_1,sensor.toogoodtogo_2,sensor.toogoodtogo_3
    attribute: stock_available
    from: false
    to: true
condition: []
action:
  - service: notify.mobile_app_android
    data:
      message: >-
        Available: {{trigger.to_state.state}}, For:
        {{trigger.to_state.attributes.price}} in
        {{trigger.to_state.attributes.pickup_start_human}}
      title: "{{trigger.to_state.attributes.friendly_name}}"
      data:
        clickAction: "{{trigger.to_state.attributes.url}}"
        image: "{{trigger.to_state.attributes.picture}}"
        group: tgtg
        tag: "{{trigger.entity_id}}"
  - service: notify.mobile_app_iphone
    data:
      message: >-
        Available: {{trigger.to_state.state}}, For:
        {{trigger.to_state.attributes.price}} in
        {{trigger.to_state.attributes.pickup_start_human}},
        {{trigger.to_state.attributes.friendly_name}}
      title: "{{trigger.to_state.attributes.friendly_name}}"
      data:
        url: "{{trigger.to_state.attributes.url}}"
        image: "{{trigger.to_state.attributes.picture}}"
        group: tgtg
        tag: "{{trigger.entity_id}}"
mode: parallel
max: 10
```

### Remove Notification

```yaml
alias: TooGoodToGo UnNotification
description: ""
trigger:
  - platform: state
    entity_id:
      >- # This is your list of toogoodoto sensors which are generated (Copy paste the list from above)
      sensor.toogoodtogo_1,sensor.toogoodtogo_2,sensor.toogoodtogo_3
    attribute: stock_available
    from: true
    to: false
condition: []
action:
  - service: notify.mobile_app_android
    data:
      message: clear_notification
      data:
        tag: "{{trigger.entity_id}}"
  - service: notify.mobile_app_iphone
    data:
      message: clear_notification
      data:
        tag: "{{trigger.entity_id}}"
mode: parallel
max: 10
```

### Pickup Reminder

```yaml
alias: TGTG Pickup Reminder
description: ""

triggers:
  - trigger: time
    at:
      entity_id: sensor.too_good_to_go_toogoodtogo_next_collection
      offset: "-00:30:00"

actions:
  - variables:
      next_collection_entity: sensor.too_good_to_go_toogoodtogo_next_collection
      notification_message: |-
        🛍️ <b>Too Good To Go Pickup Reminder</b>

        <b>Item:</b> {{ state_attr(next_collection_entity, 'item_name') }}
        <b>Store:</b> {{ state_attr(next_collection_entity, 'store_name') }}
        <b>Time:</b> {{ as_timestamp(state_attr(next_collection_entity, 'pickup_start')) | timestamp_custom('%H:%M') }} - {{ as_timestamp(state_attr(next_collection_entity, 'pickup_end')) | timestamp_custom('%H:%M') }} <i>({{ state_attr(next_collection_entity, 'pickup_start_human') }})</i>
        <b>Address:</b> {{ state_attr(next_collection_entity, 'address') }}
        <b>Maps:</b> <a href="https://www.google.com/maps/search/?api=1&query={{ state_attr(next_collection_entity, 'address') | urlencode }}">Open in Google Maps</a>

  - action: telegram_bot.send_message
    metadata: {}
    data:
      disable_notification: true
      target: <TELEGRAM USERID>
      parse_mode: html
      message: "{{ notification_message }}"
```

## Development

This project uses [pre-commit](https://pre-commit.com/) to make sure the code keeps clean and similar. Usage is highly advised.

## Contributors

Big thanks to everyone contributing <3

<a href="https://github.com/maxwinterstein/toogoodtogo-ha-mqtt-bridge/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=maxwinterstein/toogoodtogo-ha-mqtt-bridge" />
</a>

Made with [contrib.rocks](https://contrib.rocks).
