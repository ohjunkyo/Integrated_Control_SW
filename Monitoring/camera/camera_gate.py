#!/usr/bin/env python3
"""Login gate in front of the MediaMTX HLS camera stream.

Serves /camera (login form or player), /camera/login and /camera/stream/<rest>
(authenticated proxy to MediaMTX HLS). Standard library only.
"""

import argparse
import base64
import hashlib
import hmac
import http.client
import http.cookies
import ipaddress
import json
import math
import os
import secrets
import stat
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
DEFAULT_CONFIG = os.path.join(HOME, ".config", "precal_camera_gate.json")
DEFAULT_STATE = os.path.join(HOME, ".cache", "precal", "camera_auth.json")
DEFAULT_LOG = os.path.join(HOME, "logs", "camera_gate.log")

COOKIE_NAME = "camgate"
SESSION_SECONDS = 12 * 3600
FAIL_LOG_INTERVAL = 10.0
MAX_BODY = 4096
UPSTREAM_TIMEOUT = 30.0
MAX_UPSTREAM_REDIRECTS = 3
CHUNK = 64 * 1024

HLS_JS_URL = "https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js"
HLS_JS_SRI = "sha384-w6Gb3fXHb5e1LUYa/hYA5Q41bEDglN5ZPCG7Jvnoo8/X90oGnlPqBlBJCe38mEMm"
STREAM_URL = "/camera/stream/index.m3u8"


def load_config(path):
    """Read the gate config; exit if missing, not mode 600, or incomplete."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        sys.exit(f"camera_gate: config {path} not found (run set_camera_password.sh)")
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600:
        sys.exit(f"camera_gate: config {path} must be a regular file with mode 600")
    if st.st_uid != os.getuid():
        sys.exit(f"camera_gate: config {path} must be owned by the current user")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        sys.exit(f"camera_gate: cannot read config {path}: {e.__class__.__name__}")
    for key in ("viewer_hash", "upstream_user", "upstream_pass"):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            sys.exit(f"camera_gate: config {path} lacks '{key}'")
    if not cfg["viewer_hash"].startswith("sha256:"):
        sys.exit("camera_gate: viewer_hash must start with 'sha256:'")
    return cfg


def mediamtx_hash(password):
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return "sha256:" + base64.b64encode(digest).decode("ascii")


class Gate:
    """Shared state: sessions (memory only), failure counters (persisted), log."""

    def __init__(self, args, cfg):
        self.viewer_hash = cfg["viewer_hash"].encode("ascii")
        token = f"{cfg['upstream_user']}:{cfg['upstream_pass']}".encode("utf-8")
        self.upstream_auth = "Basic " + base64.b64encode(token).decode("ascii")
        up = urllib.parse.urlsplit(args.upstream)
        self.up_host = up.hostname or "127.0.0.1"
        self.up_port = up.port or 80
        self.state_path = args.state
        self.log_path = args.log
        self.max_failures = args.max_failures
        self.lock_seconds = args.lock_seconds
        self.lock = threading.Lock()
        self.sessions = {}
        self.clients = {}
        self.last_fail_log = {}
        self.upstream_cookies = {}
        self._load_state()

    # ---------- persistence ----------
    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            print(f"camera_gate: ignoring unreadable state file ({e.__class__.__name__})",
                  file=sys.stderr)
            return
        now = time.time()
        for ip, entry in (data.get("clients") or {}).items():
            try:
                fails = int(entry.get("fails", 0))
                until = float(entry.get("locked_until", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if until and until <= now:
                continue  # lock expired while the gate was down: counter resets
            if fails > 0 or until > now:
                self.clients[ip] = {"fails": fails, "locked_until": until}

    def _save_state(self):
        """Atomic write with mode 600. Caller holds self.lock."""
        d = os.path.dirname(self.state_path)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{os.path.basename(self.state_path)}.{os.getpid()}.tmp")
        payload = json.dumps({"version": 1, "clients": self.clients}, indent=1)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        except OSError as e:
            print(f"camera_gate: cannot write state file ({e})", file=sys.stderr)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _log(self, tag, ip, left):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{tag}] ip={ip} left={left}\n"
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            print(f"camera_gate: cannot write log ({e})", file=sys.stderr)

    # ---------- sessions ----------
    def new_session(self):
        sid = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            self.sessions[sid] = now + SESSION_SECONDS
        return sid

    def valid_session(self, sid):
        if not sid:
            return False
        with self.lock:
            exp = self.sessions.get(sid)
            if exp is None:
                return False
            if exp <= time.time():
                del self.sessions[sid]
                return False
            return True

    # ---------- failure limiting ----------
    def _refresh(self, ip, now):
        """Drop an expired lock (counter reset). Caller holds self.lock."""
        entry = self.clients.get(ip)
        if entry and entry["locked_until"] and entry["locked_until"] <= now:
            del self.clients[ip]
            self._save_state()
            self._log("UNLOCK", ip, self.max_failures)
            return None
        return entry

    def lock_remaining(self, ip):
        """Seconds left on this IP's lock, 0 if not locked."""
        with self.lock:
            now = time.time()
            entry = self._refresh(ip, now)
            if entry and entry["locked_until"] > now:
                return entry["locked_until"] - now
            return 0.0

    def attempt(self, ip, password):
        """Returns ('ok', None) | ('fail', attempts_left) | ('locked', seconds_left)."""
        with self.lock:
            now = time.time()
            entry = self._refresh(ip, now)
            if entry and entry["locked_until"] > now:
                return "locked", entry["locked_until"] - now
            if hmac.compare_digest(mediamtx_hash(password).encode("ascii"), self.viewer_hash):
                if ip in self.clients:
                    del self.clients[ip]
                    self._save_state()
                self._log("OK", ip, self.max_failures)
                return "ok", None
            entry = self.clients.setdefault(ip, {"fails": 0, "locked_until": 0.0})
            entry["fails"] += 1
            left = max(self.max_failures - entry["fails"], 0)
            if left == 0:
                entry["locked_until"] = now + self.lock_seconds
                self._log("LOCK", ip, 0)
            elif now - self.last_fail_log.get(ip, 0.0) >= FAIL_LOG_INTERVAL:
                self.last_fail_log[ip] = now
                self._log("FAIL", ip, left)
            if len(self.last_fail_log) > 10000:
                self.last_fail_log = {k: v for k, v in self.last_fail_log.items()
                                      if now - v < FAIL_LOG_INTERVAL}
            self._save_state()
            return "fail", left

    # ---------- upstream cookies (MediaMTX cookieCheck) ----------
    def upstream_cookie_header(self):
        with self.lock:
            return "; ".join(f"{k}={v}" for k, v in self.upstream_cookies.items())

    def store_upstream_cookies(self, headers):
        for raw in headers:
            try:
                c = http.cookies.SimpleCookie()
                c.load(raw)
            except http.cookies.CookieError:
                continue
            with self.lock:
                for k, morsel in c.items():
                    self.upstream_cookies[k] = morsel.value


# ---------- HTML ----------
PAGE_CSS = """
body{margin:0;background:#111;color:#eee;font-family:system-ui,sans-serif}
main{max-width:960px;margin:0 auto;padding:16px}
form{max-width:360px;margin:12vh auto 0;display:flex;flex-direction:column;gap:10px}
input,button{font-size:16px;padding:10px;border-radius:6px;border:1px solid #555}
input{background:#222;color:#eee}
button{background:#2d6cdf;color:#fff;border:0;cursor:pointer}
.msg{color:#ff8a80;margin:0}
.ko{color:#bbb;margin:0;font-size:14px}
video{width:100%;background:#000;border-radius:6px}
#status{color:#bbb;font-size:14px}
"""


def csp(nonce):
    return ("default-src 'self'; "
            f"script-src 'nonce-{nonce}' https://cdn.jsdelivr.net; "
            f"style-src 'nonce-{nonce}'; "
            "media-src 'self' blob:; worker-src 'self' blob:; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'; object-src 'none'")


def login_page(nonce, msg_en="", msg_ko=""):
    msg = ""
    if msg_en:
        msg = f'<p class="msg">{msg_en}</p><p class="ko">{msg_ko}</p>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Camera login</title><style nonce="{nonce}">{PAGE_CSS}</style></head>
<body><main><form method="post" action="/camera/login" autocomplete="off">
<h1>Camera</h1>
<label for="pw">Password</label>
<input id="pw" name="password" type="password" required autofocus
 autocomplete="current-password" maxlength="512">
{msg}
<button type="submit">Log in</button>
</form></main></body></html>
"""


def player_page(nonce):
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Camera</title><style nonce="{nonce}">{PAGE_CSS}</style></head>
<body><main>
<video id="v" controls autoplay muted playsinline></video>
<p id="status">Connecting... (the camera can take a few seconds to start)</p>
</main>
<script nonce="{nonce}" src="{HLS_JS_URL}" integrity="{HLS_JS_SRI}" crossorigin="anonymous"></script>
<script nonce="{nonce}">
(function () {{
  var src = "{STREAM_URL}";
  var video = document.getElementById("v");
  var status = document.getElementById("status");
  function say(t) {{ status.textContent = t; }}
  video.addEventListener("playing", function () {{ say("Live"); }});
  if (window.Hls && Hls.isSupported()) {{
    var hls = null;
    var start = function () {{
      if (hls) hls.destroy();
      hls = new Hls({{ lowLatencyMode: true, manifestLoadingMaxRetry: 6,
                      manifestLoadingRetryDelay: 1000 }});
      hls.on(Hls.Events.ERROR, function (ev, data) {{
        if (!data.fatal) return;
        var code = data.response && data.response.code;
        if (code === 401) {{ location.reload(); return; }}
        say("Stream error (" + (code || data.details) + "), retrying...");
        setTimeout(start, 3000);
      }});
      hls.loadSource(src);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, function () {{
        video.play().catch(function () {{}});
      }});
    }};
    start();
  }} else if (video.canPlayType("application/vnd.apple.mpegurl")) {{
    video.src = src;
    video.addEventListener("error", function () {{
      say("Stream error, retrying...");
      setTimeout(function () {{ video.src = src; video.play().catch(function () {{}}); }}, 3000);
    }});
  }} else {{
    say("This browser cannot play HLS.");
  }}
}})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "camera-gate"
    sys_version = ""
    timeout = 120
    gate = None  # set in main()

    def log_message(self, fmt, *args):
        pass

    def version_string(self):
        return self.server_version

    # ---------- helpers ----------
    def client_ip(self):
        peer = self.client_address[0]
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            xff = self.headers.get("X-Forwarded-For", "")
            first = xff.split(",")[0].strip()
            if first:
                try:
                    return str(ipaddress.ip_address(first))
                except ValueError:
                    pass
        return peer

    def session_id(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            c = http.cookies.SimpleCookie()
            c.load(raw)
        except http.cookies.CookieError:
            return None
        m = c.get(COOKIE_NAME)
        return m.value if m else None

    def send_empty(self, code, extra=None):
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_html(self, code, body_fn, *args, extra=None):
        nonce = secrets.token_urlsafe(16)
        body = body_fn(nonce, *args).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp(nonce))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def lock_msg(seconds):
        n = max(1, math.ceil(seconds / 60))
        return f"Locked. Try again in {n} min.", f"{n}분 뒤 다시 시도하세요"

    # ---------- routes ----------
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/camera", "/camera/"):
            if self.gate.valid_session(self.session_id()):
                self.send_html(200, player_page)
                return
            remaining = self.gate.lock_remaining(self.client_ip())
            if remaining > 0:
                self.send_html(200, login_page, *self.lock_msg(remaining))
            else:
                self.send_html(200, login_page)
        elif path.startswith("/camera/stream/"):
            self.proxy()
        else:
            self.send_empty(404)

    def do_HEAD(self):
        self.send_empty(404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path != "/camera/login":
            self.close_connection = True
            self.send_empty(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if "Transfer-Encoding" in self.headers:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self.send_empty(413)
            return
        body = self.rfile.read(length).decode("utf-8", "replace")
        password = urllib.parse.parse_qs(body).get("password", [""])[0]
        ip = self.client_ip()
        result, value = self.gate.attempt(ip, password)
        if result == "ok":
            sid = self.gate.new_session()
            self.send_empty(303, {
                "Location": "/camera/",
                "Set-Cookie": (f"{COOKIE_NAME}={sid}; HttpOnly; Secure; SameSite=Strict; "
                               f"Path=/camera; Max-Age={SESSION_SECONDS}"),
            })
        elif result == "locked":
            self.send_html(429, login_page, *self.lock_msg(value))
        else:
            en = f"Wrong password. {value} attempts left."
            ko = f"비밀번호가 틀렸습니다. 남은 시도 {value}회"
            if value == 0:
                l_en, l_ko = self.lock_msg(self.gate.lock_seconds)
                en, ko = f"{en} {l_en}", f"{ko}. {l_ko}"
            self.send_html(401, login_page, en, ko)

    # ---------- proxy ----------
    def proxy(self):
        if not self.gate.valid_session(self.session_id()):
            self.send_empty(401)
            return
        parts = urllib.parse.urlsplit(self.path)
        rest = parts.path[len("/camera/stream/"):]
        segs = urllib.parse.unquote(rest).split("/")
        if not rest or any(s in ("", ".", "..") for s in segs) or "\\" in rest:
            self.send_empty(404)
            return
        target = "/camera/" + rest + (("?" + parts.query) if parts.query else "")

        resp = conn = None
        try:
            for _ in range(MAX_UPSTREAM_REDIRECTS + 1):
                if conn:
                    conn.close()
                conn = http.client.HTTPConnection(self.gate.up_host, self.gate.up_port,
                                                  timeout=UPSTREAM_TIMEOUT)
                headers = {"Authorization": self.gate.upstream_auth,
                           "Accept-Encoding": "identity", "User-Agent": "camera-gate"}
                cookie = self.gate.upstream_cookie_header()
                if cookie:
                    headers["Cookie"] = cookie
                conn.request("GET", target, headers=headers)
                resp = conn.getresponse()
                self.gate.store_upstream_cookies(resp.headers.get_all("Set-Cookie") or [])
                loc = resp.getheader("Location")
                if resp.status in (301, 302, 303, 307, 308) and loc:
                    nxt = urllib.parse.urlsplit(urllib.parse.urljoin(target, loc))
                    if nxt.path.startswith("/camera/") and not nxt.netloc:
                        resp.read()
                        target = nxt.path + (("?" + nxt.query) if nxt.query else "")
                        continue
                break
        except (OSError, http.client.HTTPException):
            if conn:
                conn.close()
            self.send_empty(502)
            return

        try:
            self.send_response(resp.status)
            for h in ("Content-Type", "Cache-Control"):
                v = resp.getheader(h)
                if v:
                    self.send_header(h, v)
            loc = resp.getheader("Location")
            if loc:
                self.send_header("Location", self.rewrite_location(loc))
            self.send_header("X-Content-Type-Options", "nosniff")
            length = resp.getheader("Content-Length")
            chunked = length is None
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", length)
            self.end_headers()
            while True:
                data = resp.read1(CHUNK)
                if not data:
                    break
                if chunked:
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                else:
                    self.wfile.write(data)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (OSError, http.client.HTTPException):
            self.close_connection = True
        finally:
            conn.close()

    @staticmethod
    def rewrite_location(loc):
        u = urllib.parse.urlsplit(loc)
        if u.path.startswith("/camera/"):
            new = "/camera/stream/" + u.path[len("/camera/"):]
            return new + (("?" + u.query) if u.query else "")
        if not u.scheme and not u.netloc and not u.path.startswith("/"):
            return loc  # relative, already resolves under /camera/stream/
        return "/camera/"


def main():
    ap = argparse.ArgumentParser(description="Login gate for the camera HLS stream")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--upstream", default="http://127.0.0.1:8888")
    ap.add_argument("--max-failures", type=int, default=5)
    ap.add_argument("--lock-seconds", type=float, default=15 * 60)
    args = ap.parse_args()

    cfg = load_config(args.config)
    Handler.gate = Gate(args, cfg)
    del cfg
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"camera_gate: listening on http://{args.host}:{args.port}/camera", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
