from threading import Lock, Timer
from typing import Any, Callable


class Watchdog(Exception):
    """Thx https://stackoverflow.com/a/16148744"""

    def __init__(self, timeout: float, user_handler: Callable[[], Any] | None = None) -> None:  # timeout in seconds
        self.timeout = timeout
        self.handler: Callable[[], Any] = user_handler if user_handler is not None else self.default_handler
        self._lock = Lock()  # reset/stop come from more than one thread (fetch loop, logins)
        self.timer = Timer(self.timeout, self.handler)
        self.timer.start()

    def reset(self) -> None:
        with self._lock:
            self.timer.cancel()
            self.timer = Timer(self.timeout, self.handler)
            self.timer.start()

    def stop(self) -> None:
        with self._lock:
            self.timer.cancel()

    def default_handler(self) -> None:
        raise self
