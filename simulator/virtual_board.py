# simulator/virtual_board.py
#
# A software stand-in for status_ring.ino, so the whole real path -- Claude
# Code hooks -> report_event.py -> bridge.py -> state_manager.py -> serial
# protocol -- can be exercised and watched in a browser before any hardware
# exists. Nothing about the bridge, hooks, or state_manager is fake; only the
# firmware leg is replaced.
#
# `VirtualBoard` below is pure protocol/state logic (no I/O), deliberately
# mirroring status_ring.ino's processLine()/printStatus() line for line --
# see bridge/state_manager.py's own "no I/O" invariant for why that split is
# worth keeping. Everything below it is transport: a socket.socketpair() (not
# a PTY -- this project runs on Windows too, and PTYs are POSIX-only) whose
# two ends play the role of "the serial cable": one goes to VirtualBoard's
# line loop, the other is wrapped in SocketSerial and handed to the *real*
# bridge.Bridge via its serial_open injection seam.
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bridge"))
# Set before importing bridge -- it reads this env var at import time to pick
# PERSISTENCE_PATH. A distinct default file keeps a simulator run from racing
# writes against a real bridge's `.bridge_state.json` if one happens to be
# running too (they can't share port 8765, but nothing stops someone running
# the simulator on an alternate port alongside a real bridge).
os.environ.setdefault(
    "STATUS_PANEL_STATE_FILE", str(Path(__file__).parent / ".simulated_state.json")
)
import bridge  # noqa: E402  (path and env must be set up first)

NUM_SLOTS = 16
RED_TIMEOUT_S = 5.0
CHIRP_ON_S = 0.15
CHIRP_PERIOD_S = 3.0
BRIDGE_TIMEOUT_S = 10.0
MAX_LINE_LEN = 32

STATES = ("WORKING", "DISPATCHED", "BLOCKED", "IDLE", "RUNNING", "OFF")
STATE_CODES = {"WORKING": "W", "DISPATCHED": "D", "BLOCKED": "B", "IDLE": "I", "RUNNING": "R", "OFF": "O"}

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = int(os.environ.get("STATUS_PANEL_SIM_CONTROL_PORT", "8766"))


class VirtualBoard:
    """Pure port of status_ring.ino's protocol handling -- no sockets, no
    threads, no clock of its own beyond an injectable time source. This is
    what simulator/tests/test_virtual_board.py exercises directly.
    """

    def __init__(self, time_source=time.monotonic):
        self._now = time_source
        self._start = self._now()
        self.slots = ["OFF"] * NUM_SLOTS
        self.brightness_pct = 5
        self._blocked_since = {}   # slot index -> when it entered BLOCKED
        self._silenced = set()     # slot indices silenced since their last BLOCKED entry
        # Mirrors the firmware's "start already stale" rule (see
        # docs/decisions.md#the-board-starts-stale): a board with no host
        # attached should never look falsely healthy.
        self._last_contact = self._start - BRIDGE_TIMEOUT_S - 1

    def _note_contact(self):
        self._last_contact = self._now()

    @property
    def stale(self):
        return (self._now() - self._last_contact) > BRIDGE_TIMEOUT_S

    def _is_alarming(self, index):
        if self.slots[index] != "BLOCKED":
            return False
        if index in self._silenced:
            return False
        since = self._blocked_since.get(index)
        return since is not None and (self._now() - since) >= RED_TIMEOUT_S

    @property
    def buzzing(self):
        """True for the whole time any slot is alarming -- this is what
        STATUS's `buzz=` field reports (mirrors the firmware's `buzzerActive`,
        set to `anyAlarming` continuously). NOT gated by the chirp phase:
        the physical buzzer only actually sounds 150ms out of every 3s, but
        that's a real-speaker detail with no equivalent to simulate here --
        `chirp_phase` below is what a UI would use to pulse an icon in time
        with the real chirp, kept separate so this boolean stays a steady,
        pollable "is something alarming" signal.
        """
        if self.stale:
            return False
        return any(self._is_alarming(i) for i in range(NUM_SLOTS))

    @property
    def chirp_phase(self):
        """True during the ~150ms-every-3s window the real buzzer would be
        sounding, for a UI that wants to visually pulse in time with it.
        """
        return self.buzzing and (self._now() - self._start) % CHIRP_PERIOD_S < CHIRP_ON_S

    def press_button(self):
        """Mirrors handleButton(): silence every slot alarming *right now*.

        A press while stale is ignored -- the user can't see or hear what
        they'd be silencing, same rule as the real firmware.
        """
        if self.stale:
            return
        for i in range(NUM_SLOTS):
            if self._is_alarming(i):
                self._silenced.add(i)

    def handle_line(self, line):
        """One request line in, one response line out. Never raises."""
        line = line.strip()
        if len(line) > MAX_LINE_LEN:
            return "ERR: <line too long>"

        if line == "PING":
            self._note_contact()
            return "OK PING"

        if line == "STATUS":
            self._note_contact()
            return self._status_line()

        if line.startswith("DIM:"):
            try:
                pct = int(line[len("DIM:"):])
            except ValueError:
                return f"ERR: {line}"
            self.brightness_pct = max(0, min(100, pct))
            self._note_contact()
            return f"OK {line}"

        if ":" not in line:
            return f"ERR: {line}"
        slot_str, _, state_str = line.partition(":")
        try:
            slot = int(slot_str)
        except ValueError:
            return f"ERR: {line}"
        if not (1 <= slot <= NUM_SLOTS) or state_str not in STATES:
            return f"ERR: {line}"

        index = slot - 1
        if state_str == "BLOCKED" and self.slots[index] != "BLOCKED":
            self._blocked_since[index] = self._now()
            self._silenced.discard(index)
        self.slots[index] = state_str
        self._note_contact()
        return f"OK {line}"

    def _status_line(self):
        ring = "".join(STATE_CODES[s] for s in self.slots)
        up_ms = int((self._now() - self._start) * 1000)
        return (
            f"STATUS up={up_ms} dim={self.brightness_pct} "
            f"stale={1 if self.stale else 0} buzz={1 if self.buzzing else 0} "
            f"ram=1200 ver=2 ring={ring}"
        )


class SocketSerial:
    """Just enough of pyserial's surface for bridge.py: write/readline/
    reset_input_buffer/close, backed by a connected socket instead of a real
    device. bridge.py only ever calls these four methods on `self.ser`.
    """

    def __init__(self, sock, timeout=1.0):
        self._sock = sock
        self._sock.settimeout(timeout)
        self._timeout = timeout
        self._buf = b""

    def write(self, data):
        self._sock.sendall(data)

    def readline(self):
        deadline = time.monotonic() + self._timeout
        while b"\n" not in self._buf and time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(256)
            except socket.timeout:
                break
            if not chunk:
                break
            self._buf += chunk
        if b"\n" in self._buf:
            line, _, self._buf = self._buf.partition(b"\n")
            return line + b"\n"
        line, self._buf = self._buf, b""
        return line

    def reset_input_buffer(self):
        self._buf = b""
        self._sock.setblocking(False)
        try:
            while self._sock.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        finally:
            self._sock.settimeout(self._timeout)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def serve_board(board, sock):
    """Transport loop: lines in, VirtualBoard's responses out. Runs until the
    socket closes. Mirrors status_panel's handleSerial()/processLine() split
    -- this function is the "handleSerial", VirtualBoard.handle_line is the
    "processLine".
    """
    sock.settimeout(0.2)
    buf = b""
    while True:
        try:
            chunk = sock.recv(256)
        except socket.timeout:
            continue
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            raw, _, buf = buf.partition(b"\n")
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if not line:
                continue
            reply = board.handle_line(line)
            try:
                sock.sendall((reply + "\n").encode("utf-8"))
            except OSError:
                return


def make_control_handler(board):
    """Tiny HTTP server for the one thing that has no serial command at all:
    the physical silence button. Real hardware reads a GPIO pin directly: a
    simulated board needs *some* way for a browser click to reach it, and it
    isn't the bridge's concern (bridge.py has no notion of "button").
    CORS is wide open here -- this is a local, simulation-only control
    surface, not the bridge's real API.
    """
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.end_headers()

        def do_POST(self):
            if self.path == "/button":
                board.press_button()
                self.send_response(200)
                self._cors()
                self.end_headers()
                return
            self.send_response(404)
            self._cors()
            self.end_headers()

        def do_GET(self):
            if self.path == "/state":
                body = json.dumps({
                    "buzzing": board.buzzing,
                    "chirp_phase": board.chirp_phase,
                }).encode("utf-8")
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self._cors()
            self.end_headers()

    return Handler


def main():
    board = VirtualBoard()
    host_sock, board_sock = socket.socketpair()

    threading.Thread(target=serve_board, args=(board, board_sock), daemon=True).start()

    control = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), make_control_handler(board))
    threading.Thread(target=control.serve_forever, daemon=True).start()

    print("Virtual board running -- no hardware involved.")
    print(f"  Silence-button control: http://{CONTROL_HOST}:{CONTROL_PORT}/button (POST)")
    print(f"  Dashboard:  http://{bridge.HTTP_HOST}:{bridge.HTTP_PORT}/")
    print(f"  Simulator:  http://{bridge.HTTP_HOST}:{bridge.HTTP_PORT}/simulator")
    print("Start a NEW Claude Code session (with hooks installed) to see it react.")
    print()

    bridge.run("(virtual board)", serial_open=lambda: SocketSerial(host_sock))


if __name__ == "__main__":
    main()
