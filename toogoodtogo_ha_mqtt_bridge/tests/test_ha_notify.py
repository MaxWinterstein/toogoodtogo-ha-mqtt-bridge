import json
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from toogoodtogo_ha_mqtt_bridge.ha_notify import NOTIFICATION_ID, HomeAssistantNotifier


@pytest.fixture
def supervisor() -> Generator[dict, None, None]:
    """Fake Supervisor: records calls, answers /addons/self/info."""
    state: dict[str, Any] = {"calls": [], "ingress": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _reply(self, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            state["calls"].append(("GET", self.path, None, self.headers["Authorization"]))
            self._reply({"result": "ok", "data": {"slug": "1a2b3c4d_tgtg-ha-mqtt-bridge", "ingress": state["ingress"]}})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append(("POST", self.path, body, self.headers["Authorization"]))
            self._reply({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


def test_login_required_creates_a_notification_linking_to_ingress(supervisor: dict) -> None:
    notifier = HomeAssistantNotifier("secret", base_url=supervisor["url"], email="me@example.com")
    notifier.login_required(True)

    method, path, body, auth = supervisor["calls"][-1]
    assert (method, path, auth) == ("POST", "/core/api/services/persistent_notification/create", "Bearer secret")
    assert body["notification_id"] == NOTIFICATION_ID
    assert "me@example.com" in body["message"]
    assert "(/app/1a2b3c4d_tgtg-ha-mqtt-bridge/ingress)" in body["message"]


def test_login_done_dismisses_it(supervisor: dict) -> None:
    HomeAssistantNotifier("secret", base_url=supervisor["url"]).login_required(False)
    assert supervisor["calls"] == [
        (
            "POST",
            "/core/api/services/persistent_notification/dismiss",
            {"notification_id": NOTIFICATION_ID},
            "Bearer secret",
        )
    ]


def test_without_ingress_it_points_at_the_web_ui(supervisor: dict) -> None:
    supervisor["ingress"] = False
    HomeAssistantNotifier("secret", base_url=supervisor["url"]).login_required(True)
    assert "Open the add-on's Web UI" in supervisor["calls"][-1][2]["message"]


def test_only_active_inside_an_add_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert HomeAssistantNotifier.from_environment() is None
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    assert HomeAssistantNotifier.from_environment() is not None
