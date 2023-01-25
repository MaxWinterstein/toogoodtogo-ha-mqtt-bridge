import datetime
import time
from http import HTTPStatus

from tgtg import (
    AUTH_POLLING_ENDPOINT,
    MAX_POLLING_TRIES,
    POLLING_WAIT_TIME,
    REFRESH_ENDPOINT,
    TgtgAPIError,
    TgtgClient,
    TgtgLoginError,
    TgtgPollingError,
    logger,
)


class MyTgtgClient(TgtgClient):
    cookie_datadome = None

    def __init__(self, cookie_datadome=None, *args, **kwargs):
        TgtgClient.__init__(self, *args, **kwargs)
        self.cookie_datadome = cookie_datadome

    @property
    def _headers(self):
        headers = {
            "accept": "application/json",
            "Accept-Encoding": "gzip",
            "accept-language": self.language,
            "content-type": "application/json; charset=utf-8",
            "user-agent": self.user_agent,
        }
        if self.cookie_datadome:
            headers["Cookie"] = self.cookie_datadome
        if self.access_token:
            headers["authorization"] = f"Bearer {self.access_token}"
        return headers

    def _refresh_token(self):
        if (
            self.last_time_token_refreshed
            and (datetime.datetime.now() - self.last_time_token_refreshed).seconds
            <= self.access_token_lifetime
        ):
            return

        response = self.session.post(
            self._get_url(REFRESH_ENDPOINT),
            json={"refresh_token": self.refresh_token},
            headers=self._headers,
            proxies=self.proxies,
            timeout=self.timeout,
        )
        if response.status_code == HTTPStatus.OK:
            self.access_token = response.json()["access_token"]
            self.refresh_token = response.json()["refresh_token"]
            self.last_time_token_refreshed = datetime.datetime.now()
            self.cookie_datadome = response.headers["Set-Cookie"]
        else:
            raise TgtgAPIError(response.status_code, response.content)

    def start_polling(self, polling_id):
        for _ in range(MAX_POLLING_TRIES):
            response = self.session.post(
                self._get_url(AUTH_POLLING_ENDPOINT),
                headers=self._headers,
                json={
                    "device_type": self.device_type,
                    "email": self.email,
                    "request_polling_id": polling_id,
                },
                proxies=self.proxies,
                timeout=self.timeout,
            )
            if response.status_code == HTTPStatus.ACCEPTED:
                logger.warning(
                    f"Check your mailbox ({self.email}) on desktop to continue... "
                    "(Mailbox on mobile won't work, if you have tgtg app installed)"
                )
                time.sleep(POLLING_WAIT_TIME)
                continue
            elif response.status_code == HTTPStatus.OK:
                logger.info("Logged in")
                login_response = response.json()
                self.access_token = login_response["access_token"]
                self.refresh_token = login_response["refresh_token"]
                self.last_time_token_refreshed = datetime.datetime.now()
                self.user_id = login_response["startup_data"]["user"]["user_id"]
                self.cookie_datadome = response.headers["Set-Cookie"]
                return
            else:
                if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                    raise TgtgAPIError(response.status_code, "Too many requests. Try again later.")
                else:
                    raise TgtgLoginError(response.status_code, response.content)

        raise TgtgPollingError(
            f"Max retries ({MAX_POLLING_TRIES * POLLING_WAIT_TIME} seconds) reached. Try again."
        )
