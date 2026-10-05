"""Loopback-only web interface for the local browser agent."""

import atexit
import json
import os
import secrets
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .agent import Agent
from .questions import MAX_STEPS

ROOT = Path(__file__).parent
PORT = int(os.environ.get("BROWSER_AGENT_UI_PORT", os.environ.get("TYPESAFE_DEMO_PORT", "8766")))
ORIGIN = f"http://127.0.0.1:{PORT}"
DEFAULT_URL = "https://www.google.com/"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
AGENT = None
RESTART_REQUESTED = threading.Event()
STOP_REQUESTED = threading.Event()


def load_environment(*, overwrite=False):
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                if overwrite or key not in os.environ:
                    os.environ[key] = value


def response_state():
    state = AGENT.snapshot() if AGENT else {"page": None, "status": "idle", "history": [], "decision": None}
    expected = state.get("expect_text")
    verified = None
    if expected and state.get("status") == "done" and state.get("page"):
        visible = f"{state['page'].get('title', '')}\n{state['page'].get('text', '')}"
        verified = expected.casefold() in visible.casefold()
    return {
        **state,
        "verified": verified,
        "service_control": True,
        "text_model": os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
        if os.environ.get("DEEPSEEK_API_KEY")
        else os.environ.get("TEXT_MODEL", "deepseek/deepseek-chat"),
        "max_steps": MAX_STEPS,
    }


def close_browser():
    global AGENT
    if AGENT:
        AGENT.close()
        AGENT = None


def command(name, body):
    global AGENT
    if name == "reset":
        goal = body.get("goal", "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        url = (body.get("url") or DEFAULT_URL).strip()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname or parsed_url.username:
            raise ValueError("Starting URL must be a valid http(s) URL without embedded credentials")
        expect_text = body.get("expect_text", "").strip()
        if len(expect_text) > 300:
            raise ValueError("Verification text must be 300 characters or fewer")
        close_browser()
        AGENT = Agent(
            url,
            goal,
            screenshots=True,
        )
        AGENT.state["expect_text"] = expect_text or None
    else:
        if AGENT is None:
            raise ValueError("Start a demo first")
        AGENT.command(name, body)
    return response_state()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="application/json"):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path == "/api/state":
            with LOCK:
                return self.send(200, json.dumps(response_state()))
        if path == "/demo.mp4":
            video = ROOT.parent / "docs" / "demo.mp4"
            if video.exists():
                return self.send(200, video.read_bytes(), "video/mp4")
        files = {
            "/": ("index.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
            "/fixture.html": ("fixture.html", "text/html"),
        }
        if path not in files:
            return self.send(404, "Not found", "text/plain")
        name, mime = files[path]
        content = (ROOT / "static" / name).read_text().replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def do_POST(self):
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "Local demo requests only"}))
        if not LOCK.acquire(blocking=False):
            return self.send(409, json.dumps({"error": "A browser step is already running"}))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 8192:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            route = self.path.removeprefix("/api/")
            if route in {"service/restart", "service/stop"}:
                self.send(
                    202,
                    json.dumps({"service": "restarting" if route.endswith("restart") else "stopping"}),
                )
                if route.endswith("restart"):
                    RESTART_REQUESTED.set()
                else:
                    STOP_REQUESTED.set()
                threading.Timer(0.2, self.server.shutdown).start()
                return
            result = command(route, body)
            self.send(200, json.dumps(result))
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            error_id = secrets.token_hex(4)
            print(f"[{error_id}] Unhandled local agent error at {self.path}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            self.send(
                500,
                json.dumps({"error": f"Local agent error ({error_id}); inspect the server terminal. No browser action was retried."}),
            )
        finally:
            LOCK.release()

    def log_message(self, *_args):
        pass


def main():
    load_environment()
    atexit.register(close_browser)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        print(f"Browser Agent: {ORIGIN}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        close_browser()
    if RESTART_REQUESTED.is_set():
        print("Restarting Browser Agent process; previous task state was cleared.", flush=True)
        load_environment(overwrite=True)
        os.execv(sys.executable, [sys.executable, "-m", "jev_ultrafast.demo"])


if __name__ == "__main__":
    main()
