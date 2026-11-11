"""Local-only application registration endpoint for Guest Agent V2.

The listener runs inside the existing SYSTEM service. Any local Windows user
may submit metadata / software-issued auth values WITHOUT access to protected
Agent identity, certificates, or registry files. No remote-facing listener.

Security boundary: an untrusted local user may impersonate a whitelisted app.
The fixed app-id/port map prevents arbitrary localhost endpoints being relayed,
but cannot authenticate the actual software process. Never use on a shared VM
where local users must not influence each other's registered application state.
"""
from __future__ import annotations
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

LISTEN_ADDR = "127.0.0.1"
LISTEN_PORT = 18767
ENDPOINT = f"http://{LISTEN_ADDR}:{LISTEN_PORT}/v1/apps/report"
MAX_BYTES = 8192
# A non-admin reporter can only expose these recognized local application ports.
PORTS = {"snowluma": frozenset((5099,)),
         "dsh-fish-soul": frozenset((56781, 55701, 3080))}
VALID_KINDS = {"random_password", "token", "none"}


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    app = data.get("app_id")
    if app not in PORTS:
        raise ValueError("Unsupported app_id")
    url = data.get("url")
    if not isinstance(url, str):
        raise ValueError("Missing loopback URL")
    try:
        p = urlparse(url)
        valid_url = (p.scheme == "http" and p.hostname in ("127.0.0.1", "localhost")
                     and p.port in PORTS[app] and p.username is None and p.password is None
                     and not p.query and not p.fragment and p.path in ("", "/"))
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("Unapproved application loopback port")
    version = data.get("version", "")
    if not isinstance(version, str) or len(version) > 40:
        raise ValueError("Invalid version")
    online = data.get("online", True)
    if not isinstance(online, bool):
        raise ValueError("Invalid online field")
    auth = data.get("auth", {})
    if not isinstance(auth, dict):
        raise ValueError("Invalid auth object")
    kind = auth.get("kind", "none")
    if kind not in VALID_KINDS:
        raise ValueError("Invalid auth kind")
    code = auth.get("access_code")
    if code is None:
        code = ""
    if not isinstance(code, str) or len(code) > 512:
        raise ValueError("Invalid access code")
    if kind == "none" and code:
        raise ValueError("Access code with no auth kind")
    return (app, url, version, kind, code)


def accept(data, root, registration=None):
    app, url, version, kind, code = validate(data)
    if registration is None:
        try:
            from .app_registry import register
        except ImportError:
            from app_registry import register
        registration = register
    # Runs inside SYSTEM process only; no unprivileged filesystem access.
    registration(Path(root), app, url, code, version, auth_kind=kind)
    return app


def submit_report(data, endpoint=ENDPOINT, timeout=6):
    # No proxy, even if host process has HTTP_PROXY environment variables.
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise ValueError("Payload too large")
    req = Request(endpoint, method="POST", data=body, headers={
        "Content-Type": "application/json",
        "X-VMO-Local-Reporter": "v1",
    })
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            result = json.loads(resp.read(1024).decode("utf-8"))
    except HTTPError as exc:
        # Do not log the rejected body or application code.
        raise RuntimeError(f"Local reporter rejected request (HTTP {exc.code})") from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("Guest Agent local app reporter unavailable") from None
    if not result.get("accepted"):
        raise RuntimeError("Local reporter did not accept application")
    return result


class LocalReportServer:
    def __init__(self, root, logger=None, host=LISTEN_ADDR, port=LISTEN_PORT):
        self.root = Path(root)
        self.logger = logger
        self.lock = threading.RLock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *_args):
                # Prevent request contents/secret values from entering logs.
                pass

            def send_json(self, status, data):
                payload = json.dumps(data, separators=(",", ":")).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/health":
                    self.send_json(200, {"running": True})
                else:
                    self.send_json(404, {"error": "not_found"})

            def do_OPTIONS(self):
                self.send_json(405, {"error": "method_not_allowed"})

            def do_POST(self):
                if self.path != "/v1/apps/report":
                    return self.send_json(404, {"error": "not_found"})
                # Browser CSRF prevention: no Origin, no cross-site fetch, and
                # a non-safelisted header. This remains intentionally accessible
                # to any LOCAL OS user or process, not to browser pages.
                host_hdr = self.headers.get("Host", "")
                if host_hdr not in (f"127.0.0.1:{owner.port}", f"localhost:{owner.port}") or \
                        self.headers.get("Origin") is not None or \
                        self.headers.get("Sec-Fetch-Site") is not None or \
                        self.headers.get("X-VMO-Local-Reporter") != "v1":
                    return self.send_json(403, {"error": "forbidden"})
                ct = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if ct != "application/json" or self.headers.get("Transfer-Encoding"):
                    return self.send_json(415, {"error": "unsupported_media_type"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    return self.send_json(400, {"error": "invalid_length"})
                if not 0 < length <= MAX_BYTES:
                    return self.send_json(413, {"error": "invalid_size"})
                self.connection.settimeout(5)
                try:
                    body = self.rfile.read(length)
                    data = json.loads(body.decode("utf-8"))
                    with owner.lock:
                        app = accept(data, owner.root)
                except (ValueError, UnicodeDecodeError, TimeoutError, OSError):
                    return self.send_json(400, {"error": "invalid_report"})
                except Exception:
                    if owner.logger:
                        owner.logger.warning("Local app registration failed (internal error)")
                    return self.send_json(500, {"error": "register_failed"})
                self.send_json(200, {"accepted": True, "app_id": app})

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.server.request_queue_size = 16
        self.port = self.server.server_address[1]
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.3}, daemon=True)
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=3)
