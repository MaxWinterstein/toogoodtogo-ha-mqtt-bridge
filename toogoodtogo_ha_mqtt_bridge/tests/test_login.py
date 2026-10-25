"""The bridge's login against a fake TGTG API: saved tokens, the 401 fallback, the PIN page."""

import json
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
import tgtg

from toogoodtogo_ha_mqtt_bridge import main
from toogoodtogo_ha_mqtt_bridge.config import settings
from toogoodtogo_ha_mqtt_bridge.login_portal import FAILED, LOGGED_IN, WAITING_FOR_PIN, LoginPortal
from toogoodtogo_ha_mqtt_bridge.tests.fake_tgtg import FakeTgtg

EMAIL = "me@example.com"


@pytest.fixture
def fake() -> Generator[FakeTgtg, None, None]:
    fake = FakeTgtg()
    yield fake
    fake.close()


@pytest.fixture
def bridge(fake: FakeTgtg, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Generator[dict, None, None]:
    """Point the bridge at the fake API with a fresh data dir, portal and MQTT mock."""
    keys = ("tgtg", "data_dir")
    original = {key: settings.get(key) for key in keys}
    settings["tgtg"] = {"email": EMAIL, "language": "en-GB", "base_url": fake.url}
    settings["data_dir"] = str(tmp_path)
    monkeypatch.setattr(tgtg, "DATADOME_SDK_URL", fake.sdk_url)
    monkeypatch.setattr(main, "build_ua", lambda: "TGTG/24.11.0 Dalvik/2.1.0 (Linux; U; Android 14)")
    watchdog = MagicMock()
    monkeypatch.setattr(main, "watchdog", watchdog)

    published: dict[str, Any] = {}

    def fake_publish(topic: str, payload: str | None = None, retain: bool = False) -> MagicMock:
        published[topic] = payload
        return MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)

    mqtt_client = MagicMock()
    mqtt_client.publish.side_effect = fake_publish
    monkeypatch.setattr(main, "mqtt_client", mqtt_client)

    portal = LoginPortal(port=0)
    portal.add_listener(main.publish_login_required)
    monkeypatch.setattr(main, "login_portal", portal)
    monkeypatch.setattr(main, "tokens", {})

    yield {"portal": portal, "published": published, "tokens": tmp_path / "tokens.json", "watchdog": watchdog}

    for key, value in original.items():
        settings[key] = value


def _client_with_saved_tokens() -> Any:
    main.tokens = {"cookie": "c=0", "access_token": "at-old", "refresh_token": "rt-old", "ua": "TGTG/24.11.0 x"}
    main.rebuild_tgtg_client()
    return main.tgtg_client


def _login_in_thread() -> tuple[threading.Thread, dict]:
    result: dict = {}

    def run() -> None:
        try:
            main.ensure_login()
            result["ok"] = True
        except Exception as err:
            result["error"] = err

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def _wait_for(portal: LoginPortal, state: str) -> None:
    event = threading.Event()
    for _ in range(250):
        if portal.state == state:
            return
        event.wait(0.02)
    raise AssertionError(f"portal never reached {state!r}, is {portal.state!r}")


LOGIN_TOPIC = "homeassistant/binary_sensor/toogoodtogo_login_required/state"


def test_valid_saved_tokens_just_refresh(fake: FakeTgtg, bridge: dict) -> None:
    _client_with_saved_tokens()
    main.ensure_login()
    assert fake.paths() == ["token/v1/refresh"]
    assert main.tgtg_client.access_token == "at-refreshed"
    assert bridge["portal"].state == LOGGED_IN
    assert bridge["published"][LOGIN_TOPIC] == "OFF"


def test_rejected_tokens_fall_back_to_a_pin_login(fake: FakeTgtg, bridge: dict) -> None:
    """The bug report: refresh answers 401, and the bridge used to crash-loop on it forever."""
    fake.refresh_status = 401
    bridge["tokens"].write_text("{}")
    _client_with_saved_tokens()
    portal = bridge["portal"]

    thread, result = _login_in_thread()
    _wait_for(portal, WAITING_FOR_PIN)
    assert not bridge["tokens"].exists()  # the rejected session is gone
    assert bridge["published"][LOGIN_TOPIC] == "ON"
    assert bridge["watchdog"].stop.called  # a pending PIN must not trip the watchdog

    assert portal.submit_pin("123456")
    thread.join(5)
    assert result == {"ok": True}
    assert fake.paths() == ["token/v1/refresh", "auth/v5/authByEmail", "auth/v5/authByRequestPin"]
    assert main.tgtg_client.access_token == "at-pin"
    assert json.loads(bridge["tokens"].read_text())["refresh_token"] == "rt-pin"
    assert portal.state == LOGGED_IN
    assert bridge["published"][LOGIN_TOPIC] == "OFF"
    assert bridge["watchdog"].reset.called


def test_wrong_pin_asks_again_with_the_same_email(fake: FakeTgtg, bridge: dict) -> None:
    main.tgtg_client = main.new_email_client()
    portal = bridge["portal"]

    thread, result = _login_in_thread()
    _wait_for(portal, WAITING_FOR_PIN)
    portal.submit_pin("000000")
    for _ in range(250):  # back to the form, now with an explanation
        if portal.state == WAITING_FOR_PIN and portal.message:
            break
        threading.Event().wait(0.02)
    assert "did not accept that PIN" in portal.message

    portal.submit_pin("123456")
    thread.join(5)
    assert result == {"ok": True}
    assert fake.paths().count("auth/v5/authByEmail") == 1


def test_resend_starts_a_new_email_login(fake: FakeTgtg, bridge: dict) -> None:
    main.tgtg_client = main.new_email_client()
    portal = bridge["portal"]

    thread, result = _login_in_thread()
    _wait_for(portal, WAITING_FOR_PIN)
    portal.request_resend()
    for _ in range(250):
        if fake.paths().count("auth/v5/authByEmail") == 2 and portal.state == WAITING_FOR_PIN:
            break
        threading.Event().wait(0.02)
    portal.submit_pin("123456")
    thread.join(5)
    assert result == {"ok": True}
    assert fake.paths().count("auth/v5/authByEmail") == 2


def test_failed_email_request_waits_for_the_user_instead_of_retrying(fake: FakeTgtg, bridge: dict) -> None:
    fake.email_status = 429
    main.tgtg_client = main.new_email_client()
    portal = bridge["portal"]

    thread, result = _login_in_thread()
    _wait_for(portal, FAILED)
    assert "rate limiting" in portal.message
    threading.Event().wait(0.3)
    assert fake.paths().count("auth/v5/authByEmail") == 1  # no hammering while the user is away

    fake.email_status = 200
    portal.request_resend()
    _wait_for(portal, WAITING_FOR_PIN)
    portal.submit_pin("123456")
    thread.join(5)
    assert result == {"ok": True}


@pytest.mark.parametrize("status", [403, 500])
def test_other_refresh_errors_keep_the_saved_tokens(fake: FakeTgtg, bridge: dict, status: int) -> None:
    """403 is bot protection and 500 is TGTG having a bad day; neither means the session is dead."""
    fake.refresh_status = status
    bridge["tokens"].write_text("{}")
    _client_with_saved_tokens()

    with pytest.raises(tgtg.exceptions.TgtgAPIError):
        main.ensure_login()
    assert bridge["tokens"].exists()
    assert "auth/v5/authByEmail" not in fake.paths()


def test_check_logs_in_again_instead_of_killing_the_fetch_thread(fake: FakeTgtg, bridge: dict) -> None:
    """A session that expires while running used to raise straight out of check()."""
    fake.refresh_status = 500
    _client_with_saved_tokens()
    main.first_run = False
    try:
        assert main.check() is False
    finally:
        main.first_run = True


def test_publish_login_required_announces_a_problem_sensor(bridge: dict) -> None:
    main.publish_login_required(True)
    config = json.loads(bridge["published"]["homeassistant/binary_sensor/toogoodtogo_bridge/login_required/config"])
    assert config["device_class"] == "problem"
    assert config["state_topic"] == LOGIN_TOPIC
    assert config["default_entity_id"] == "binary_sensor.toogoodtogo_login_required"
    assert bridge["published"][LOGIN_TOPIC] == "ON"
