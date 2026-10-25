"""A small web page that takes the TGTG login PIN.

Since tgtg 0.19.0 the email login ends with a PIN that the library reads from stdin. The bridge
runs as a service without a terminal, so this page is where the user types it instead. It is
served through Home Assistant ingress for the add-on, or on a plain port for Docker users.

All links and form actions are relative, because ingress serves the page below a per-session
path (``/api/hassio_ingress/<token>/``) that the bridge never sees.
"""

from __future__ import annotations

import html
import logging
import threading
from collections.abc import Callable, Collection
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

STARTING = "starting"
WAITING_FOR_PIN = "waiting_for_pin"
CHECKING = "checking"
FAILED = "failed"
LOGGED_IN = "logged_in"

MAX_PIN_LENGTH = 32
MAX_FORM_BYTES = 1024


class ResendRequested(Exception):
    """The user asked for a new login email instead of entering a PIN."""


class LoginPortal:
    """Login state shared between the login thread and the web page.

    The login thread blocks in :meth:`wait_for_pin` or :meth:`wait_for_retry`; the web page
    unblocks it through :meth:`submit_pin` and :meth:`request_resend`. Listeners are told
    whenever the "a login is needed" flag flips, so Home Assistant can raise or clear a notice.
    """

    def __init__(self, port: int = 8099, allowed_ips: Collection[str] = ()) -> None:
        self.port = port
        self.allowed_ips = frozenset(allowed_ips)
        self.state = STARTING
        self.email = ""
        self.message = ""
        self._pin: str | None = None
        self._resend = False
        self._cond = threading.Condition()
        self._listeners: list[Callable[[bool], Any]] = []
        self._login_required: bool | None = None
        self._server: ThreadingHTTPServer | None = None

    # --- listeners -------------------------------------------------------------------------

    def add_listener(self, listener: Callable[[bool], Any]) -> None:
        """Call ``listener(login_required)`` every time that flag changes."""
        self._listeners.append(listener)

    def _set_login_required(self, required: bool) -> None:
        # Called without holding the lock: listeners do network I/O.
        if self._login_required == required:
            return
        self._login_required = required
        for listener in self._listeners:
            try:
                listener(required)
            except Exception:
                logger.exception("Login state listener failed")

    # --- called by the login thread --------------------------------------------------------

    def wait_for_pin(self, email: str, message: str = "") -> str:
        """Show the PIN form and block until a PIN arrives.

        Raises :class:`ResendRequested` if the user asks for a new email instead.
        """
        with self._cond:
            self.state, self.email, self.message = WAITING_FOR_PIN, email, message
            self._pin, self._resend = None, False
        self._set_login_required(True)
        if not message:  # a message means we are asking again for the same email's PIN
            logger.warning("TGTG sent a login PIN to %s. Enter it on the login page.", email)

        with self._cond:
            self._cond.wait_for(lambda: self._pin is not None or self._resend)
            if self._resend:
                self._resend = False
                raise ResendRequested
            pin, self._pin = self._pin, None
            self.state, self.message = CHECKING, ""
            return pin  # type: ignore[return-value]  # not None: checked by wait_for

    def wait_for_retry(self, email: str, message: str) -> None:
        """Show an error and block until the user asks for a new login email."""
        with self._cond:
            self.state, self.email, self.message = FAILED, email, message
            self._resend = False
        self._set_login_required(True)
        with self._cond:
            self._cond.wait_for(lambda: self._resend)
            self._resend = False

    def logged_in(self, email: str) -> None:
        with self._cond:
            self.state, self.email, self.message = LOGGED_IN, email, ""
        self._set_login_required(False)

    # --- called by the web page ------------------------------------------------------------

    def submit_pin(self, pin: str) -> bool:
        pin = "".join(pin.split())  # people paste "123 456"
        with self._cond:
            if self.state != WAITING_FOR_PIN:
                return False
            if not pin or len(pin) > MAX_PIN_LENGTH or not pin.isalnum():
                self.message = "That does not look like a PIN. Copy it from the email and try again."
                return False
            self._pin = pin
            self._cond.notify_all()
        return True

    def request_resend(self) -> bool:
        with self._cond:
            if self.state not in (WAITING_FOR_PIN, FAILED):
                return False
            self.state, self.message = CHECKING, ""
            self._resend = True
            self._cond.notify_all()
        return True

    # --- web server ------------------------------------------------------------------------

    def start(self) -> None:
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), _make_handler(self))  # noqa: S104
        self.port = self._server.server_address[1]  # resolves port 0 in tests
        threading.Thread(target=self._server.serve_forever, name="login-portal", daemon=True).start()
        logger.info("Login page listening on port %s", self.port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def render(self) -> str:
        with self._cond:
            state, email, message = self.state, self.email, self.message
        return _render_page(state, email, message)


class _Handler(BaseHTTPRequestHandler):
    portal: LoginPortal  # set per server by _make_handler

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug("login page: " + format, *args)

    def _allowed(self) -> bool:
        if not self.portal.allowed_ips:
            return True
        client = self.client_address[0].removeprefix("::ffff:")
        if client in self.portal.allowed_ips:
            return True
        self.send_error(HTTPStatus.FORBIDDEN)
        return False

    def _send_page(self, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = self.portal.render().encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect_home(self) -> None:
        # Post/Redirect/Get, so a reload never resubmits the PIN. Relative for ingress.
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "./")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._allowed():
            return
        if self.path.split("?")[0] in ("/", ""):
            self._send_page()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._allowed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_FORM_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        form = parse_qs(self.rfile.read(length).decode(errors="replace"))
        path = self.path.split("?")[0]
        if path == "/pin":
            self.portal.submit_pin(form.get("pin", [""])[0])
        elif path == "/resend":
            self.portal.request_resend()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._redirect_home()


def _make_handler(portal: LoginPortal) -> type[BaseHTTPRequestHandler]:
    return type("LoginPortalHandler", (_Handler,), {"portal": portal})


_STYLE = """
body{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;margin:0;padding:2rem 1rem;
background:#f5f5f5;color:#212121}
main{max-width:28rem;margin:0 auto;background:#fff;border-radius:12px;padding:1.5rem 1.75rem;
box-shadow:0 1px 3px rgba(0,0,0,.12)}
h1{font-size:1.25rem;margin:0 0 1rem}
p{line-height:1.5}
.msg{background:#fdecea;color:#8a1c12;border-radius:8px;padding:.6rem .8rem}
.ok{color:#1b5e20}
input[name=pin]{font-size:1.5rem;letter-spacing:.3em;width:100%;box-sizing:border-box;padding:.5rem .75rem;
border:1px solid #bdbdbd;border-radius:8px;margin:.5rem 0 1rem}
button{font-size:1rem;padding:.6rem 1.2rem;border-radius:8px;border:0;background:#00615f;color:#fff;cursor:pointer}
button.link{background:none;color:#00615f;padding:.6rem 0;text-decoration:underline}
@media (prefers-color-scheme:dark){body{background:#111;color:#e1e1e1}main{background:#1c1c1c}
input[name=pin]{background:#111;color:#e1e1e1;border-color:#444}.msg{background:#4a1a15;color:#ffd7d2}
.ok{color:#a5d6a7}button.link{color:#80cbc4}}
"""


def _render_page(state: str, email: str, message: str) -> str:
    who = html.escape(email) if email else "your TGTG account"
    msg = f'<p class="msg">{html.escape(message)}</p>' if message else ""
    refresh = ""
    resend = (
        '<form method="post" action="resend"><button class="link" type="submit">'
        "Send me a new login email</button></form>"
    )

    if state == WAITING_FOR_PIN:
        body = f"""<h1>Enter your login PIN</h1>
<p>Too Good To Go has emailed a PIN to <strong>{who}</strong>. Enter it here to log the bridge in.</p>
{msg}
<form method="post" action="pin">
<label for="pin">PIN from the email</label>
<input id="pin" name="pin" inputmode="numeric" autocomplete="one-time-code" maxlength="{MAX_PIN_LENGTH}"
 required autofocus>
<button type="submit">Log in</button>
</form>
{resend}"""
    elif state == FAILED:
        body = f"""<h1>Login failed</h1>
{msg}
<p>The bridge will not try again by itself, so it does not trigger Too Good To Go's rate limit.</p>
{resend}"""
    elif state == LOGGED_IN:
        body = f'<h1>Logged in</h1><p class="ok">The bridge is logged in as <strong>{who}</strong>.</p>'
    else:  # STARTING or CHECKING
        refresh = '<meta http-equiv="refresh" content="3">'
        body = "<h1>Working on it</h1><p>Talking to Too Good To Go. This page refreshes by itself.</p>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>TooGoodToGo bridge login</title><style>{_STYLE}</style></head>
<body><main>{body}</main></body></html>
"""
