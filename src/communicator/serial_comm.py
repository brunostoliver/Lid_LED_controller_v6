# src/communicator/serial_comm.py
"""
Serial communications layer for the Lid/LED controller.

Protocol (Arduino):
- Boots with: READY LIDCTRL v1 POS=... EN=... MOV=...
- Commands:
    HELLO -> "HELLO LIDCTRL v1"
    PING  -> "PONG"
    STATUS_JSON? -> {"en":0/1,"mov":0/1,"pos":N,"max":MAX,"state":"OPEN|CLOSED|PARTIAL"}
    STATUS?, POS?, OPEN, CLOSE, STOP, ENABLE, DISABLE, BTN?
- Events (unsolicited):
    EVT MOVE_STARTED dir=OPEN|CLOSE
    EVT MOVE_DONE state=OPEN|CLOSED|PARTIAL pos=N
    EVT BTN_OPEN / EVT BTN_CLOSE
    EVT ENABLED / EVT DISABLED
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any, List

import serial
import serial.tools.list_ports


@dataclass
class SerialEvent:
    """Structured message surfaced to the controller/GUI."""
    type: str                 # 'ready', 'hello', 'pong', 'status', 'event', 'line', 'error'
    data: Dict[str, Any]      # parsed payload when available
    raw: str                  # the raw line from the device (if applicable)


def list_serial_ports() -> List[str]:
    """Return a list of available COM/tty ports as user-facing strings."""
    ports = []
    for p in serial.tools.list_ports.comports():
        # e.g., "COM5 - USB-SERIAL CH340 (VID:PID=1A86:7523)"
        label = p.device
        if p.description:
            label += f" - {p.description}"
        ports.append(label)
    return ports


class SerialClient:
    """
    Thin wrapper around pyserial with:
    - connection handshake (READY -> HELLO -> STATUS_JSON?)
    - background reader thread
    - heartbeat PING/PONG
    - line-oriented send
    """

    def __init__(
        self,
        on_message: Callable[[SerialEvent], None],
        baud: int = 9600,
        read_timeout: float = 1.0,
        heartbeat_interval: float = 10.0,
    ) -> None:
        self.on_message = on_message
        self.baud = baud
        self.read_timeout = read_timeout
        self.heartbeat_interval = heartbeat_interval

        self._ser: Optional[serial.Serial] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_stop = threading.Event()
        self._lock = threading.Lock()

    # ---------------------------- Public API ---------------------------------

    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def connect(self, port_name: str) -> bool:
        """
        Open the port, perform handshake, start background threads.
        Returns True on success, False on failure (and cleans up).
        """
        try:
            # Extract bare device path if user picked a "label"
            device = port_name.split(" ")[0]

            self._ser = serial.Serial(
                device, self.baud, timeout=self.read_timeout
            )

            # Uno resets on open; give it time to reboot & print the READY line.
            time.sleep(1.8)

            if not self._wait_for_ready_banner(timeout=4.0):
                self._emit("error", {"reason": "No READY banner"}, raw="")
                self.disconnect()
                return False

            # HELLO handshake
            if not self._exchange_expect("HELLO\n", prefix="HELLO "):
                self._emit("error", {"reason": "HELLO failed"}, raw="")
                self.disconnect()
                return False

            # Get initial JSON status to prime the GUI
            status = self.request_status_json()
            if status is None:
                self._emit("error", {"reason": "STATUS_JSON? failed"}, raw="")
                self.disconnect()
                return False

            # Start reader + heartbeat
            self._start_reader()
            self._start_heartbeat()
            return True

        except Exception as ex:
            self._emit("error", {"exception": repr(ex)}, raw="")
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Stop threads and close the port."""
        self._stop_heartbeat()
        self._stop_reader()
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
            self._ser = None

    def send(self, line: str) -> None:
        """Send a single command line, appending newline if needed."""
        if not self.is_connected():
            return
        if not line.endswith("\n"):
            line += "\n"
        data = line.encode("utf-8", errors="ignore")
        with self._lock:
            try:
                self._ser.write(data)
                self._ser.flush()
            except Exception as ex:
                self._emit("error", {"reason": "send failed", "exception": repr(ex)}, raw=line)

    def request_status_json(self) -> Optional[Dict[str, Any]]:
        """Send STATUS_JSON? synchronously and parse one JSON line reply."""
        if not self.is_connected():
            return None
        try:
            self.send("STATUS_JSON?")
            # Temporarily read directly with a short timeout to catch the next JSON line
            deadline = time.time() + 1.5
            while time.time() < deadline:
                raw = self._readline_blocking()
                if raw is None:
                    continue
                s = raw.strip()
                if s.startswith("{") and s.endswith("}"):
                    try:
                        data = json.loads(s)
                        self._emit("status", data, raw=s)
                        return data
                    except Exception:
                        # keep listening; a different JSON might follow
                        pass
                else:
                    # surface any non-JSON line we got while waiting
                    self._emit("line", {}, raw=s)
            return None
        except Exception as ex:
            self._emit("error", {"reason": "status_json exception", "exception": repr(ex)}, raw="")
            return None

    # -------------------------- Internal helpers -----------------------------

    def _emit(self, typ: str, data: Dict[str, Any], raw: str) -> None:
        try:
            self.on_message(SerialEvent(type=typ, data=data, raw=raw))
        except Exception:
            # Avoid crashing background threads if user callback fails
            pass

    def _readline_blocking(self) -> Optional[str]:
        """Read one line (blocking up to read_timeout). Returns None on timeout."""
        if not self._ser:
            return None
        try:
            raw = self._ser.readline()
            if not raw:
                return None
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return None

    def _wait_for_ready_banner(self, timeout: float) -> bool:
        """Wait for a 'READY ...' line and emit it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self._readline_blocking()
            if not s:
                continue
            line = s.strip()
            if line.startswith("READY "):
                self._emit("ready", {"line": line}, raw=line)
                return True
            else:
                # Also surface banners or noise (HELLO, tips, etc.)
                self._emit("line", {}, raw=line)
        return False

    def _exchange_expect(self, send_line: str, prefix: str, timeout: float = 1.5) -> bool:
        """Send a line and expect a response starting with `prefix`."""
        self.send(send_line)
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self._readline_blocking()
            if not s:
                continue
            line = s.strip()
            if line.startswith(prefix):
                if prefix.startswith("HELLO "):
                    self._emit("hello", {"line": line}, raw=line)
                elif prefix == "PONG":
                    self._emit("pong", {}, raw=line)
                else:
                    self._emit("line", {}, raw=line)
                return True
            else:
                self._emit("line", {}, raw=line)
        return False

    # ----------------------- Background reader thread ------------------------

    def _start_reader(self) -> None:
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, name="SerialReader", daemon=True)
        self._reader_thread.start()

    def _stop_reader(self) -> None:
        self._reader_stop.set()
        if self._reader_thread and self._reader_thread.is_alive():
            try:
                self._reader_thread.join(timeout=1.0)
            except Exception:
                pass
        self._reader_thread = None

    def _reader_loop(self) -> None:
        while not self._reader_stop.is_set() and self.is_connected():
            s = self._readline_blocking()
            if not s:
                continue
            line = s.strip()

            # JSON status?
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    self._emit("status", data, raw=line)
                    continue
                except Exception:
                    pass

            # Events or other lines
            if line.startswith("EVT "):
                # try to parse some key=value pairs
                evt = {"text": line}
                parts = line.split()
                if len(parts) >= 2:
                    evt["name"] = parts[1]
                for tok in parts[2:]:
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        evt[k] = v
                self._emit("event", evt, raw=line)
            elif line == "PONG":
                self._emit("pong", {}, raw=line)
            else:
                self._emit("line", {}, raw=line)

    # --------------------------- Heartbeat thread -----------------------------

    def _start_heartbeat(self) -> None:
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, name="SerialHeartbeat", daemon=True)
        self._hb_thread.start()

    def _stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_thread and self._hb_thread.is_alive():
            try:
                self._hb_thread.join(timeout=1.0)
            except Exception:
                pass
        self._hb_thread = None

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.is_set() and self.is_connected():
            time.sleep(self.heartbeat_interval)
            if self._hb_stop.is_set() or not self.is_connected():
                break
            # Send PING; reader thread will emit 'pong' when it sees it.
            try:
                self.send("PING")
            except Exception as ex:
                self._emit("error", {"reason": "heartbeat send failed", "exception": repr(ex)}, raw="")
