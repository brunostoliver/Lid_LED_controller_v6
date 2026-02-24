# src/controller/lid_controller.py
"""
Application controller that bridges GUI <-> SerialClient and tracks device state.
Now includes calibration commands (teach mode).
"""

from __future__ import annotations

import queue
from typing import Optional, Dict, Any

from src.communicator.serial_comm import SerialClient, SerialEvent, list_serial_ports
from src.config.settings import BAUD_RATE, READ_TIMEOUT, HEARTBEAT_INTERVAL


class LidController:
    """
    High-level controller:
    - owns SerialClient
    - maintains latest status
    - exposes simple methods for GUI commands (incl. calibration)
    - pushes GUI updates via a thread-safe message queue
    """

    def __init__(self) -> None:
        self.client = SerialClient(
            on_message=self._on_serial_message,
            baud=BAUD_RATE,
            read_timeout=READ_TIMEOUT,
            heartbeat_interval=HEARTBEAT_INTERVAL,
        )
        self.ui_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()

        # latest status snapshot (defaults)
        self.status: Dict[str, Any] = {
            "en": 0,
            "mov": 0,
            "pos": 0,
            "max": 10500,
            "state": "CLOSED",
            "cal": 0,
        }

        self.connected_port: Optional[str] = None

    # ---------------------------- GUI-to-Controller API -----------------------

    def get_ports(self):
        return list_serial_ports()

    def connect(self, port_label: str) -> bool:
        ok = self.client.connect(port_label)
        if ok:
            self.connected_port = port_label
            self._post_ui({"type": "log", "text": f"Connected to {port_label}"})
        else:
            self.connected_port = None
            self._post_ui({"type": "log", "text": f"Failed to connect to {port_label}"})
        return ok

    def disconnect(self) -> None:
        if self.client.is_connected():
            self.client.disconnect()
        self._post_ui({"type": "log", "text": "Disconnected"})
        self.connected_port = None

    def is_connected(self) -> bool:
        return self.client.is_connected()

    # ----- motion / torque -----
    def open_lid(self):
        if int(self.status.get("lim_open", 0)) != 0:
            self._post_ui({"type": "log", "text": "Open blocked: OPEN limit switch is active."})
            self._post_ui({"type": "event", "name": "OPEN_BLOCKED", "raw": "EVT OPEN_BLOCKED reason=LIMIT_OPEN"})
            return
        self.client.send("OPEN")

    def close_lid(self):
        if int(self.status.get("lim_close", 0)) != 0:
            self._post_ui({"type": "log", "text": "Close blocked: CLOSE limit switch is active."})
            self._post_ui({"type": "event", "name": "CLOSE_BLOCKED", "raw": "EVT CLOSE_BLOCKED reason=LIMIT_CLOSE"})
            return
        self.client.send("CLOSE")

    def stop(self):            self.client.send("STOP")
    def enable(self):          self.client.send("ENABLE")
    def disable(self):         self.client.send("DISABLE")
    def request_status(self):  self.client.request_status_json()

    # ----- calibration (teach mode) -----
    def cal_start(self):       self.client.send("CAL.START")
    def cal_set_closed(self):  self.client.send("CAL.SETCLOSED")
    def cal_set_open(self):    self.client.send("CAL.SETOPEN")
    def cal_save(self):        self.client.send("CAL.SAVE")
    def cal_abort(self):       self.client.send("CAL.ABORT")
    def cal_defaults(self):    self.client.send("CAL.DEFAULTS")
    def cal_status(self):      self.client.send("CAL.STATUS?")
    def cal_jog_open(self, steps: int):  self.client.send(f"J+ {max(1, int(steps))}")
    def cal_jog_close(self, steps: int): self.client.send(f"J- {max(1, int(steps))}")

    # ---------------------------- Serial callbacks ---------------------------

    def _on_serial_message(self, evt: SerialEvent) -> None:
        """Called from SerialClient threads -> forward to GUI via queue."""
        if evt.type == "status":
            # JSON status update
            self.status.update(evt.data)
            self._post_ui({"type": "status", "status": dict(self.status)})

        elif evt.type == "event":
            name = evt.data.get("name", "EVT")
            # Nudge a fresh status on key events
            if name in ("MOVE_DONE", "MOVE_STARTED", "ENABLED", "DISABLED", "CAL_SAVED", "CAL_STARTED", "CAL_ABORTED"):
                self.request_status()
            self._post_ui({"type": "event", "name": name, "raw": evt.raw})

        elif evt.type in ("ready", "hello", "pong"):
            self._post_ui({"type": evt.type, "raw": evt.raw})

        elif evt.type == "error":
            self._post_ui({"type": "error", "data": evt.data})

        else:
            self._post_ui({"type": "log", "text": evt.raw})

    def _post_ui(self, msg: Dict[str, Any]) -> None:
        try:
            self.ui_queue.put_nowait(msg)
        except Exception:
            pass
