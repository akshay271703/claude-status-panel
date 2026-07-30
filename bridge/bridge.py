# bridge/bridge.py
import json
import os
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import serial

import serial_port
from state_manager import StateManager
from process_utils import is_pid_alive, project_name_for_pid
from usage import UsageTracker

BAUD_RATE = 9600
HTTP_HOST = os.environ.get("STATUS_PANEL_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("STATUS_PANEL_HTTP_PORT", "8765"))
LIVENESS_POLL_SECONDS = 5
PING_INTERVAL_SECONDS = 3
STATUS_INTERVAL_SECONDS = 2
USAGE_INTERVAL_SECONDS = 5
PERSISTENCE_PATH = Path(__file__).parent / ".bridge_state.json"
DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


class Bridge:
    def __init__(self, port):
        self.port = port
        self.ser = None
        self._lock = threading.Lock()
        self._reconnecting = False
        self.started_at = time.time()
        self._hw = None        # last parsed STATUS reply, or None if unavailable
        self._hw_at = None     # when that reply arrived
        self._usage_tracker = UsageTracker()
        self._usage = {}       # session_id -> token totals
        self._open_serial_blocking()
        self.state = StateManager(persistence_path=PERSISTENCE_PATH, liveness_check=is_pid_alive)

    def _open_serial(self):
        """One attempt. Returns True on success. Never raises."""
        try:
            self.ser = serial.Serial(self.port, BAUD_RATE, timeout=1, write_timeout=1)
            time.sleep(2)  # let the board finish its reset before we send anything
            return True
        except (OSError, serial.SerialException) as e:
            print(f"Could not open {self.port}: {e}")
            self.ser = None
            return False

    def _open_serial_blocking(self):
        """Startup only: without a port there is nothing useful to do."""
        while not self._open_serial():
            print("Retrying in 5s...")
            time.sleep(5)

    def _start_reconnect(self):
        """Retry the port on a background thread.

        Deliberately does NOT hold the lock while sleeping -- doing so would
        park every incoming HTTP thread behind an unplugged Arduino, and stop
        StateManager from tracking sessions while the cable is out.
        """
        if self._reconnecting:
            return
        self._reconnecting = True

        def loop():
            while True:
                time.sleep(5)
                if self._open_serial():
                    print(f"Reconnected to {self.port}.")
                    with self._lock:
                        self._reconnecting = False
                        # Repaint from the state we kept tracking while dark.
                        self._send_locked(self.state.current_commands())
                    return

        threading.Thread(target=loop, daemon=True).start()

    def _send_locked(self, commands):
        """Write commands. Caller must hold self._lock. Never blocks."""
        if self.ser is None:
            return
        for cmd in commands:
            try:
                self.ser.write((cmd + "\n").encode("utf-8"))
            except (OSError, serial.SerialException) as e:
                print(f"Serial write failed ({e}); dropping {cmd!r} and reconnecting.")
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self._start_reconnect()
                return

    def handle_event(self, session_id, event, claude_pid):
        with self._lock:
            commands = self.state.handle_event(session_id, event, claude_pid)
            self._send_locked(commands)

    def liveness_loop(self):
        while True:
            time.sleep(LIVENESS_POLL_SECONDS)
            try:
                with self._lock:
                    commands = self.state.check_liveness()
                    self._send_locked(commands)
            except Exception as e:
                # Must never escape: this thread dying silently disables the
                # entire Ctrl+C / crash fallback for the life of the process.
                print(f"Liveness poll error (continuing): {e!r}")

    def ping_loop(self):
        while True:
            time.sleep(PING_INTERVAL_SECONDS)
            try:
                with self._lock:
                    self._send_locked(["PING"])
                    if self.ser is not None:
                        # The firmware answers every command with "OK <line>"
                        # and we never read it, so drop it rather than let it
                        # accumulate in the OS buffer for the process lifetime.
                        self.ser.reset_input_buffer()
            except Exception as e:
                # Must never escape: this thread dying takes the panel's only
                # liveness signal with it, silently.
                print(f"Ping error (continuing): {e!r}")

    def usage_loop(self):
        """Refresh per-session token totals from their transcripts.

        Reads files, so it runs off the request path and off the serial lock;
        the lock is taken only to read the session list and publish results.
        """
        while True:
            time.sleep(USAGE_INTERVAL_SECONDS)
            try:
                with self._lock:
                    snap = self.state.snapshot()
                live = {m["session_id"] for m in snap["modules"] if m["session_id"]}
                live |= {q["session_id"] for q in snap["queue"]}

                fresh = {}
                for session_id in live:
                    totals = self._usage_tracker.totals_for(session_id)
                    if totals is not None:
                        fresh[session_id] = totals

                with self._lock:
                    for gone in set(self._usage) - live:
                        self._usage_tracker.forget(gone)
                    self._usage = fresh
            except Exception as e:
                print(f"Usage poll error (continuing): {e!r}")

    def status_loop(self):
        """Poll the firmware for its own view of itself.

        Runs on its own thread and caches the result, so the HTTP handler
        never does serial I/O on a request path.
        """
        while True:
            time.sleep(STATUS_INTERVAL_SECONDS)
            try:
                hw = self._query_status()
                with self._lock:
                    if hw is not None:
                        self._hw = hw
                        self._hw_at = time.time()
                    elif self._hw_at is not None and time.time() - self._hw_at > 10:
                        # Stop presenting telemetry we can no longer confirm.
                        self._hw = None
            except Exception as e:
                print(f"Status poll error (continuing): {e!r}")

    def _query_status(self):
        with self._lock:
            if self.ser is None:
                return None
            try:
                self.ser.reset_input_buffer()
                self.ser.write(b"STATUS\n")
                # A few lines of slack: an OK echo from a concurrent command
                # may arrive before the STATUS reply does.
                for _ in range(4):
                    line = self.ser.readline().decode("utf-8", "replace").strip()
                    if not line:
                        break
                    if line.startswith("STATUS "):
                        return parse_status(line)
                return None
            except (OSError, serial.SerialException) as e:
                print(f"Status query failed ({e}); marking port dead.")
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self._start_reconnect()
                return None

    def recover(self):
        with self._lock:
            commands = self.state.load_and_recover()
            self._send_locked(commands)

    def status(self):
        """Dashboard payload. Cheap enough to poll once a second."""
        with self._lock:
            snap = self.state.snapshot()
            serial_connected = self.ser is not None
            reconnecting = self._reconnecting
            hw = dict(self._hw) if self._hw else None
            usage = dict(self._usage)

        # Name resolution is deliberately outside the lock -- it can touch the
        # OS process table, and nothing about it needs the bridge held still.
        for entry in snap["modules"]:
            entry["project"] = project_name_for_pid(entry["claude_pid"])
            entry["usage"] = usage.get(entry["session_id"])
        for entry in snap["queue"]:
            entry["project"] = project_name_for_pid(entry["claude_pid"])
            entry["usage"] = usage.get(entry["session_id"])

        snap["serial"] = {
            "port": self.port,
            "connected": serial_connected,
            "reconnecting": reconnecting,
        }
        snap["uptime_seconds"] = round(time.time() - self.started_at, 1)

        if hw is not None:
            # The firmware's own module view vs. what the bridge believes it
            # sent. A mismatch means a command was lost or rejected -- which
            # nothing could detect before this.
            hw["desync"] = [
                m["module"]
                for m in snap["modules"]
                if hw["modules"][m["module"] - 1] not in (None, m["state"])
            ]
        snap["hardware"] = hw
        return snap


def parse_status(line):
    """Parse `STATUS up=1 dim=5 stale=0 buzz=0 ram=1561 m1=OFF ...`.

    Returns None on anything unparseable rather than raising -- a firmware
    that answers differently should degrade to "no telemetry", not break
    the dashboard.
    """
    try:
        fields = dict(
            part.split("=", 1)
            for part in line.split()[1:]
            if "=" in part
        )
        return {
            "uptime_seconds": round(int(fields["up"]) / 1000, 1),
            "brightness_pct": int(fields["dim"]),
            "stale": fields["stale"] == "1",
            "buzzer": fields["buzz"] == "1",
            "free_ram_bytes": int(fields["ram"]),
            "modules": [fields.get(f"m{i}") for i in (1, 2, 3)],
        }
    except Exception:
        return None


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send(self, code, body, content_type):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            # Polled once a second; never let a proxy or the browser serve a
            # stale panel, which would be worse than showing nothing.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            try:
                if self.path in ("/", "/index.html"):
                    self._send(200, DASHBOARD_PATH.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                    return
                if self.path == "/api/status":
                    self._send(200, json.dumps(bridge.status()), "application/json")
                    return
                self.send_response(404)
                self.end_headers()
            except Exception as e:
                print(f"GET {self.path} failed: {e!r}")
                try:
                    self.send_response(500)
                    self.end_headers()
                except Exception:
                    pass

        def do_POST(self):
            if self.path != "/event":
                self.send_response(404)
                self.end_headers()
                return
            body = None
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                bridge.handle_event(body["session_id"], body["event"], body["claude_pid"])
                self.send_response(200)
            except Exception as e:
                # Drop bad events, but never silently: without this line a
                # bridge rejecting every event looks identical to a healthy
                # one, and "the LED didn't change" is the only symptom.
                print(f"Rejected event {body!r}: {e!r}")
                self.send_response(400)
            self.end_headers()

    return Handler


def main():
    try:
        port = serial_port.resolve()
    except RuntimeError as e:
        print(f"Cannot start: {e}")
        raise SystemExit(1)

    print(f"Opening serial port {port}@{BAUD_RATE}...")
    bridge = Bridge(port)
    bridge.recover()

    threading.Thread(target=bridge.liveness_loop, daemon=True).start()
    threading.Thread(target=bridge.ping_loop, daemon=True).start()
    threading.Thread(target=bridge.status_loop, daemon=True).start()
    threading.Thread(target=bridge.usage_loop, daemon=True).start()

    server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), make_handler(bridge))
    print(f"Bridge listening on http://{HTTP_HOST}:{HTTP_PORT}/event")
    server.serve_forever()


if __name__ == "__main__":
    main()
