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
import re
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

            # Use short reads during handshake to keep connection snappy.
            original_timeout = self._ser.timeout
            self._ser.timeout = min(0.15, float(self.read_timeout)) if self.read_timeout > 0 else 0.15

            # Fast path: many devices can answer STATUS immediately.
            # This avoids waiting on READY/HELLO when the board did not reset on open.
            status = self.request_status_json(query_timeout=0.35)

            # Slow path fallback: Uno-style auto-reset boards need startup time.
            if status is None:
                # Optional READY wait: do not fail solely because READY was not observed.
                ready_seen = self._wait_for_ready_banner(timeout=1.2)

                # HELLO handshake (optional for backward-compatible firmware)
                if not self._exchange_expect("HELLO\n", prefix="HELLO ", timeout=0.3):
                    self._emit("line", {}, raw="HELLO handshake not supported by firmware; continuing")

                # Get initial status to prime the GUI (JSON preferred, text fallback)
                status = self.request_status_json(query_timeout=0.45)
                if status is None and ready_seen:
                    # Brief retry for boards that are still finishing startup prints.
                    time.sleep(0.12)
                    status = self.request_status_json(query_timeout=0.45)
            if status is None:
                self._emit("error", {"reason": "Initial STATUS query failed"}, raw="")
                self.disconnect()
                return False

            # Restore user's configured read timeout for normal operation.
            self._ser.timeout = original_timeout

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

    def request_status_json(self, query_timeout: float = 1.5) -> Optional[Dict[str, Any]]:
        """Send STATUS_JSON? synchronously and parse one JSON line reply.

        Falls back to STATUS? text format for older firmware variants.
        """
        if not self.is_connected():
            return None
        try:
            self.send("STATUS_JSON?")
            # Temporarily read directly with a short timeout to catch the next JSON line
            deadline = time.time() + max(0.2, query_timeout)
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

            # Fallback for firmware that only supports STATUS? plain text
            self.send("STATUS?")
            deadline = time.time() + max(0.2, query_timeout)
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
                        pass

                parsed = self._parse_text_status(s)
                if parsed is not None:
                    self._emit("status", parsed, raw=s)
                    return parsed

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
        """Wait for a device-ready banner and emit it.

        Supports both newer ('READY ...') and legacy ('Lid Controller Ready.')
        banners.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = self._readline_blocking()
            if not s:
                continue
            line = s.strip()
            if line.startswith("READY ") or ("LID CONTROLLER READY" in line.upper()):
                self._emit("ready", {"line": line}, raw=line)
                return True
            else:
                # Also surface banners or noise (HELLO, tips, etc.)
                self._emit("line", {}, raw=line)
        return False

    def _parse_text_status(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse text status lines like:
        ENABLED=YES  MOVING=NO  POS=123/10500  LIMIT_OPEN=NO  LIMIT_CLOSE=YES.
        """
        match = re.search(
            r"ENABLED=(YES|NO)\s+MOVING=(YES|NO)\s+POS=(\d+)\s*/\s*(\d+)",
            line,
            flags=re.IGNORECASE,
        )
        if not match:
            return None

        enabled_yes = match.group(1).upper() == "YES"
        moving_yes = match.group(2).upper() == "YES"
        pos = int(match.group(3))
        max_steps = int(match.group(4))

        if moving_yes:
            state = "PARTIAL"
        elif pos <= 0:
            state = "CLOSED"
        elif pos >= max_steps:
            state = "OPEN"
        else:
            state = "PARTIAL"

        limit_open_match = re.search(r"LIMIT_OPEN=(YES|NO)", line, flags=re.IGNORECASE)
        limit_close_match = re.search(r"LIMIT_CLOSE=(YES|NO)", line, flags=re.IGNORECASE)

        lim_open = 1 if (limit_open_match and limit_open_match.group(1).upper() == "YES") else 0
        lim_close = 1 if (limit_close_match and limit_close_match.group(1).upper() == "YES") else 0

        return {
            "en": 1 if enabled_yes else 0,
            "mov": 1 if moving_yes else 0,
            "pos": pos,
            "max": max_steps,
            "lim_open": lim_open,
            "lim_close": lim_close,
            "state": state,
        }

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
