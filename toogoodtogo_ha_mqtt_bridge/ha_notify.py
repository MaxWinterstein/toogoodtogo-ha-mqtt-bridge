"""Tell Home Assistant that the bridge needs a new login.

Only available when running as a Home Assistant add-on: the Supervisor hands the add-on a
``SUPERVISOR_TOKEN`` and proxies Home Assistant's REST API under ``http://supervisor/core/api``
(the add-on needs ``homeassistant_api: true``). Add-ons cannot open entries in Home Assistant's
Repairs dashboard, so this uses a persistent notification, which shows up in the notification
drawer and links straight to the login page.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
NOTIFICATION_ID = "toogoodtogo_login_required"


class HomeAssistantNotifier:
    def __init__(self, token: str, base_url: str = SUPERVISOR_URL, email: str = "") -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.email = email
        self._login_page: str | None = None

    @classmethod
    def from_environment(cls, email: str = "") -> HomeAssistantNotifier | None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        return cls(token, email=email) if token else None

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(  # noqa: S310 - fixed http://supervisor URL
            self.base_url + path,
            data=data,
            method="POST" if data is not None else "GET",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            body = response.read()
        return json.loads(body) if body else None

    def login_page(self) -> str | None:
        """Frontend path of this add-on's ingress page, e.g. ``/app/1a2b3c4d_tgtg-ha-mqtt-bridge/ingress``."""
        if self._login_page is None:
            try:
                info = self._request("/addons/self/info")["data"]
                if info.get("ingress"):
                    self._login_page = f"/app/{info['slug']}/ingress"
            except Exception:
                logger.exception("Could not look up the add-on's ingress page")
        return self._login_page

    def login_required(self, required: bool) -> None:
        if required:
            self._request("/core/api/services/persistent_notification/create", self._message())
            logger.info("Raised a Home Assistant notification asking for the login PIN")
        else:
            self._request("/core/api/services/persistent_notification/dismiss", {"notification_id": NOTIFICATION_ID})

    def _message(self) -> dict[str, str]:
        who = self.email or "your account's email address"
        page = self.login_page()
        where = f"[Open the login page]({page})" if page else "Open the add-on's Web UI"
        return {
            "notification_id": NOTIFICATION_ID,
            "title": "Too Good To Go: login needed",
            "message": (
                f"The TooGoodToGo bridge has to log in again. Too Good To Go emails a login PIN to {who}. "
                f"{where} and enter it there. Until then your TGTG sensors are not updated."
            ),
        }
