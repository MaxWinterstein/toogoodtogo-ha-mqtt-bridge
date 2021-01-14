from threading import Timer


class Watchdog(Exception):
    """Thx https://stackoverflow.com/a/16148744"""

    def __init__(self, timeout, user_handler=None):  # timeout in seconds
        self.timeout = timeout
        self.handler = user_handler if user_handler is not None else self.default_handler
        self.timer = Timer(self.timeout, self.handler)
        self.timer.start()

    def reset(self):
        self.timer.cancel()
        self.timer = Timer(self.timeout, self.handler)
        self.timer.start()

    def stop(self):
        self.timer.cancel()

    def default_handler(self):
        raise self
