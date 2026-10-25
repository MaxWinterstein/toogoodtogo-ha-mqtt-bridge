import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Generator
from urllib.parse import urlencode

import pytest

from toogoodtogo_ha_mqtt_bridge.login_portal import (
    CHECKING,
    FAILED,
    LOGGED_IN,
    REQUEST_TIMEOUT,
    WAITING_FOR_PIN,
    LoginPortal,
    ResendRequested,
    _Handler,
)


@pytest.fixture
def portal() -> Generator[LoginPortal, None, None]:
    portal = LoginPortal(port=0)
    portal.start()
    yield portal
    portal.stop()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


def _get(portal: LoginPortal, path: str = "/") -> tuple[int, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{portal.port}{path}", timeout=5) as response:
        return response.status, response.read().decode()


def _post(portal: LoginPortal, path: str, **form: str) -> tuple[int, str | None]:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        f"http://127.0.0.1:{portal.port}{path}", data=urlencode(form).encode(), method="POST"
    )
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.headers.get("Location")
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Location")


def _wait_until(predicate, timeout: float = 5) -> None:  # type: ignore[no-untyped-def]
    event = threading.Event()
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        event.wait(0.02)
    raise AssertionError("condition not reached")


def _wait_for_pin_in_thread(portal: LoginPortal, email: str = "me@example.com") -> dict:
    result: dict = {}

    def run() -> None:
        try:
            result["pin"] = portal.wait_for_pin(email)
        except ResendRequested:
            result["resend"] = True

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    _wait_until(lambda: portal.state == WAITING_FOR_PIN)
    result["thread"] = thread
    return result


def test_pin_form_hands_the_pin_to_the_waiting_login(portal: LoginPortal) -> None:
    result = _wait_for_pin_in_thread(portal)

    status, page = _get(portal)
    assert status == 200
    assert "me@example.com" in page
    # Relative action: under ingress the page lives below /api/hassio_ingress/<token>/.
    assert 'action="pin"' in page

    status, location = _post(portal, "/pin", pin=" 123 456 ")
    assert (status, location) == (303, "./")  # relative redirect, also for ingress
    result["thread"].join(5)
    assert result["pin"] == "123456"
    assert portal.state == CHECKING


def test_invalid_pin_is_rejected_on_the_page(portal: LoginPortal) -> None:
    result = _wait_for_pin_in_thread(portal)

    _post(portal, "/pin", pin="<script>")
    assert "pin" not in result
    _, page = _get(portal)
    assert "does not look like a PIN" in page
    assert "<script>" not in page

    _post(portal, "/pin", pin="654321")
    result["thread"].join(5)
    assert result["pin"] == "654321"


def test_resend_unblocks_the_login_with_resend_requested(portal: LoginPortal) -> None:
    result = _wait_for_pin_in_thread(portal)
    status, _ = _post(portal, "/resend")
    assert status == 303
    result["thread"].join(5)
    assert result == {"resend": True, "thread": result["thread"]}


def test_failed_login_waits_for_a_resend(portal: LoginPortal) -> None:
    done = threading.Event()

    def retry() -> None:
        portal.wait_for_retry("me@example.com", "rate limited")
        done.set()

    threading.Thread(target=retry, daemon=True).start()
    _wait_until(lambda: portal.state == FAILED)
    _, page = _get(portal)
    assert "rate limited" in page
    assert not done.is_set()

    _post(portal, "/resend")
    assert done.wait(5)


def test_pin_is_ignored_when_nobody_is_waiting(portal: LoginPortal) -> None:
    portal.logged_in("me@example.com")
    assert portal.submit_pin("123456") is False
    assert portal.request_resend() is False
    _, page = _get(portal)
    assert portal.state == LOGGED_IN
    assert "logged in as" in page


def test_listeners_only_hear_about_changes(portal: LoginPortal) -> None:
    seen: list[bool] = []
    portal.add_listener(seen.append)
    portal.logged_in("me@example.com")
    portal.logged_in("me@example.com")
    result = _wait_for_pin_in_thread(portal)
    portal.submit_pin("111111")
    result["thread"].join(5)
    result = _wait_for_pin_in_thread(portal)  # wrong PIN: asked again, no second notification
    portal.submit_pin("222222")
    result["thread"].join(5)
    portal.logged_in("me@example.com")
    assert seen == [False, True, False]


def test_a_failing_listener_does_not_break_the_login(portal: LoginPortal) -> None:
    def broken(required: bool) -> None:
        raise RuntimeError("Home Assistant is down")

    portal.add_listener(broken)
    portal.logged_in("me@example.com")
    assert portal.state == LOGGED_IN


def test_allowlist_blocks_other_clients() -> None:
    portal = LoginPortal(port=0, allowed_ips=["172.30.32.2"])  # Home Assistant's ingress proxy
    portal.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(portal)
        assert err.value.code == 403
        assert _post(portal, "/pin", pin="123456")[0] == 403
    finally:
        portal.stop()


def test_unknown_paths_are_404(portal: LoginPortal) -> None:
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(portal, "/etc/passwd")
    assert err.value.code == 404
    assert _post(portal, "/other")[0] == 404


def _raw_post(portal: LoginPortal, content_length: str) -> str:
    """POST with a hand-written Content-Length header; returns the status line."""
    with socket.create_connection(("127.0.0.1", portal.port), timeout=5) as conn:
        conn.sendall(f"POST /pin HTTP/1.1\r\nHost: x\r\nContent-Length: {content_length}\r\n\r\n".encode())
        return conn.makefile("rb").readline().decode().strip()


@pytest.mark.parametrize(("content_length", "status"), [("abc", 400), ("-1", 400), ("4096", 413)])
def test_bad_content_length_is_rejected_without_reading(portal: LoginPortal, content_length: str, status: int) -> None:
    assert _raw_post(portal, content_length).split()[1] == str(status)


def test_handler_threads_have_a_socket_timeout() -> None:
    assert _Handler.timeout == REQUEST_TIMEOUT > 0


def test_a_failed_listener_is_retried_until_it_succeeds(portal: LoginPortal) -> None:
    """At boot Home Assistant Core may not answer yet; the "login needed" notice must still arrive."""
    portal.listener_retry_seconds = 0.05
    calls: list[bool] = []

    def flaky(required: bool) -> None:
        calls.append(required)
        if len(calls) == 1:
            raise RuntimeError("Home Assistant is still starting")

    portal.add_listener(flaky)
    result = _wait_for_pin_in_thread(portal)
    _wait_until(lambda: len(calls) >= 2)
    assert calls[:2] == [True, True]
    portal.submit_pin("123456")
    result["thread"].join(5)


def test_a_retry_is_dropped_once_the_state_moved_on(portal: LoginPortal) -> None:
    portal.listener_retry_seconds = 0.2
    calls: list[bool] = []

    def fails_on_true(required: bool) -> None:
        calls.append(required)
        if required:
            raise RuntimeError("down")

    portal.add_listener(fails_on_true)
    result = _wait_for_pin_in_thread(portal)
    portal.submit_pin("123456")
    result["thread"].join(5)
    portal.logged_in("me@example.com")
    threading.Event().wait(0.5)
    assert calls == [True, False]  # the stale "login needed" is not re-sent after the login
