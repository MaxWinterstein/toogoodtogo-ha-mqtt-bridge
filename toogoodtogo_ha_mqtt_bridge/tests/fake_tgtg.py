"""A tiny stand-in for the TGTG API, just enough for the login endpoints the bridge uses."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeTgtg:
    """Records requests and answers like TGTG would.

    ``refresh_status`` is what ``token/v1/refresh`` returns (401 = session rejected).
    ``pin`` is the PIN that ``authByRequestPin`` accepts.
    """

    def __init__(self) -> None:
        self.refresh_status = 200
        self.email_status = 200
        self.pin = "123456"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _reply(self, status: int, body: dict[str, Any], cookie: str | None = None) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:  # the DataDome SDK call is form-encoded
                    body = {}
                path = self.path.removeprefix("/api/").lstrip("/")
                fake.calls.append((path, body))
                if path.startswith("sdk"):
                    self._reply(200, {"status": 200, "cookie": "datadome=fake-dd; Path=/"})
                elif path == "token/v1/refresh":
                    if fake.refresh_status == 200:
                        self._reply(200, {"access_token": "at-refreshed", "refresh_token": "rt-refreshed"}, "c=1")
                    else:
                        self._reply(fake.refresh_status, {"errors": [{"code": "UNAUTHORIZED"}]})
                elif path == "auth/v5/authByEmail":
                    if fake.email_status == 200:
                        self._reply(200, {"state": "WAIT", "polling_id": "poll-1"})
                    else:
                        self._reply(fake.email_status, {"errors": [{"code": "NOPE"}]})
                elif path == "auth/v5/authByRequestPin":
                    if body.get("request_pin") == fake.pin and body.get("request_polling_id") == "poll-1":
                        self._reply(200, {"access_token": "at-pin", "refresh_token": "rt-pin"}, "c=2")
                    else:
                        self._reply(400, {"errors": [{"code": "WRONG_PIN"}]})
                else:
                    self._reply(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/"
        self.sdk_url = f"http://127.0.0.1:{self.server.server_address[1]}/sdk/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls if not path.startswith("sdk")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
