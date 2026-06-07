"""Integration test for full_cleanup against a real MQTT broker.

amqtt cannot be a locked dev dependency (its CLI caps click<8.2, conflicting with the
project's click pin), so the broker is run in an isolated environment via `uvx`. The test
skips gracefully if uv/uvx or the network is unavailable, so it never hard-breaks CI.
"""

import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import pytest

from toogoodtogo_ha_mqtt_bridge import main
from toogoodtogo_ha_mqtt_bridge.config import settings

BROKER_CONFIG = """\
listeners:
  default:
    type: tcp
    bind: 127.0.0.1:{port}
sys_interval: 0
auth:
  allow-anonymous: true
  plugins:
    - auth_anonymous
topic-check:
  enabled: false
"""

STORE_STATE = re.compile(r"^homeassistant/sensor/toogoodtogo_(\d+)/state$")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _broker_command(config_path: Path) -> list[str] | None:
    base = ["--from", "amqtt==0.11.3", "amqtt", "-c", str(config_path)]
    if shutil.which("uvx"):
        return ["uvx", *base]
    if shutil.which("uv"):
        return ["uv", "tool", "run", *base]
    return None


def _connected_client(port: int, client_id: str) -> Any:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.connect("127.0.0.1", port)
    client.loop_start()
    return client


def _scan_store_ids(port: int, seconds: float = 1.0) -> set[str]:
    seen: set[str] = set()

    def on_message(client: Any, userdata: Any, message: Any) -> None:
        match = STORE_STATE.match(message.topic)
        if match and message.payload:
            seen.add(match.group(1))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="verify")
    client.on_message = on_message
    client.connect("127.0.0.1", port)
    client.loop_start()
    client.subscribe("homeassistant/sensor/+/state")
    time.sleep(seconds)
    client.loop_stop()
    client.disconnect()
    return seen


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[int]:
    config = tmp_path / "amqtt.yaml"
    port = _free_port()
    config.write_text(BROKER_CONFIG.format(port=port))
    command = _broker_command(config)
    if command is None:
        pytest.skip("uv/uvx not available to run the amqtt broker")
    # `command` is a fixed list (uvx/uv + pinned amqtt + a temp config path); no untrusted input.
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    try:
        if not _wait_for_port(port):
            pytest.skip("amqtt broker did not start (offline or uvx fetch failed)")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_full_cleanup_removes_orphans_against_real_broker(broker: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "CLEANUP_SCAN_SECONDS", 1)
    original_mqtt = settings.get("mqtt")
    settings["mqtt"] = {"host": "127.0.0.1", "port": broker, "username": "", "password": ""}

    seeder = _connected_client(broker, "seeder")
    main.mqtt_client = _connected_client(broker, "bridge")
    try:
        # one active favourite, two orphans, and two non-store diagnostic sensors - all retained
        seeder.publish("homeassistant/sensor/toogoodtogo_111/state", '{"stock": 3}', retain=True)
        seeder.publish("homeassistant/sensor/toogoodtogo_222/state", '{"stock": 0}', retain=True)
        seeder.publish("homeassistant/sensor/toogoodtogo_333/state", '{"stock": 1}', retain=True)
        seeder.publish("homeassistant/sensor/toogoodtogo_next_collection/state", "x", retain=True)
        seeder.publish("homeassistant/sensor/toogoodtogo_last_updated/state", "x", retain=True)
        time.sleep(0.5)

        main.full_cleanup({"111"})  # only 111 is still a favourite
        time.sleep(0.5)

        # 222/333 cleared; 111 kept; the diagnostic sensors never match the numeric-id filter
        assert _scan_store_ids(broker) == {"111"}
    finally:
        seeder.loop_stop()
        seeder.disconnect()
        main.mqtt_client.loop_stop()
        main.mqtt_client.disconnect()
        settings["mqtt"] = original_mqtt
