# src/gui/main_window.py
"""
Minimal Tkinter GUI:
- COM port selector + Refresh/Connect/Disconnect
- Compact Status row (plain text): Lid, Torque
- Open / Close (auto-disabled at end-stops) / Stop
- Enable / Disable Holding Torque (button gating)
- Calibration window (teach mode)
- Live device log
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont

from src.controller.lid_controller import LidController
from src.config.settings import APP_TITLE, WINDOW_MIN_W, WINDOW_MIN_H
from src.gui.calibration_window import CalibrationWindow


class MainWindow(ttk.Frame):
    POLL_MS = 100  # UI queue poll interval (ms)

    def __init__(self, master: tk.Tk, controller: LidController):
        super().__init__(master, padding=10)
        self.controller = controller
        self.pack(fill=tk.BOTH, expand=True)

        master.title(APP_TITLE)
        master.minsize(WINDOW_MIN_W, WINDOW_MIN_H)

        # Track last move direction for OPENING/CLOSING text on the Lid line
        self._last_move_dir_open: bool | None = None

        self.cal_win: CalibrationWindow | None = None

        self._build_widgets()
        self._refresh_ports()

        # Initial status
        self._set_lid_text(state="CLOSED", moving=0)
        self._refresh_torque_ui(en=0)
        self._refresh_open_close_buttons(state="CLOSED", moving=0)

        self.after(self.POLL_MS, self._poll_ui_queue)

    # ------------------------------ UI ---------------------------------------

    def _build_widgets(self):
        # Top: connection bar
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(0, 6))

        self.port_var = tk.StringVar()
        # make the combobox a bit narrower for a compact layout
        self.ports_cb = ttk.Combobox(bar, textvariable=self.port_var, state="readonly", width=30)
        self.ports_cb.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(bar, text="Refresh", command=self._refresh_ports, width=12).pack(side=tk.LEFT, padx=(0, 8))
        self.btn_connect = ttk.Button(bar, text="Connect", command=self._connect, width=12)
        self.btn_connect.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_disconnect = ttk.Button(bar, text="Disconnect", command=self._disconnect, state=tk.DISABLED, width=12)
        self.btn_disconnect.pack(side=tk.LEFT)

        # Right-align the device connection indicator in the same bar for symmetry
        ttk.Label(bar, text="Device:").pack(side=tk.RIGHT)
        self.conn_text_var = tk.StringVar(value="Disconnected")
        self.conn_indicator = tk.Label(bar, textvariable=self.conn_text_var, bg="#d9534f", fg="white", padx=6)
        self.conn_indicator.pack(side=tk.RIGHT, padx=(6, 8))

        # ===== Compact Status Row (plain text) =====
        status = ttk.Frame(self)
        status.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(status, text="Lid:").pack(side=tk.LEFT)
        self.lid_text_var = tk.StringVar(value="CLOSED")
        ttk.Label(status, textvariable=self.lid_text_var).pack(side=tk.LEFT, padx=(6, 18))

        ttk.Label(status, text="Connection:").pack(side=tk.LEFT)
        ttk.Label(status, textvariable=self.conn_text_var).pack(side=tk.LEFT, padx=(6, 18))

        ttk.Label(status, text="Torque:").pack(side=tk.LEFT)
        self.torque_text_var = tk.StringVar(value="DISABLED")
        ttk.Label(status, textvariable=self.torque_text_var).pack(side=tk.LEFT, padx=(6, 0))

        # ===== Controls =====
        ctrl = ttk.LabelFrame(self, text="Controls")
        # Expand controls to take the available space so buttons can form a square grid
        ctrl.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        # Inner grid for buttons (3 columns x 2 rows) — buttons will expand equally
        btn_grid = ttk.Frame(ctrl, padding=6)
        btn_grid.pack(fill=tk.BOTH, expand=True)

        # Create a smaller font for control buttons (75% of default size) and reduced padding
        try:
            base_font = tkfont.nametofont("TkDefaultFont")
            orig_size = base_font.cget("size") or 10
            small_font = tkfont.Font(family=base_font.cget("family"), size=max(6, int(orig_size * 0.75)))
            btn_padding = 4
        except Exception:
            small_font = None
            btn_padding = 4

        # Define a ttk style for small buttons so we can apply the font safely
        style = ttk.Style()
        try:
            if small_font:
                style.configure("Small.TButton", font=small_font)
            else:
                style.configure("Small.TButton", font=())
        except Exception:
            pass

        # Control buttons use the smaller font and reduced internal padding
        self.btn_open = ttk.Button(btn_grid, text="Open", command=self.controller.open_lid, padding=btn_padding, style="Small.TButton")
        self.btn_close = ttk.Button(btn_grid, text="Close", command=self.controller.close_lid, padding=btn_padding, style="Small.TButton")
        self.btn_stop = ttk.Button(btn_grid, text="Stop", command=self.controller.stop, padding=btn_padding, style="Small.TButton")

        self.btn_enable_torque = ttk.Button(btn_grid, text="Enable Holding Torque", command=self.controller.enable, padding=btn_padding, style="Small.TButton")
        self.btn_disable_torque = ttk.Button(btn_grid, text="Disable Holding Torque", command=self.controller.disable, padding=btn_padding, style="Small.TButton")
        self.btn_calibrate = ttk.Button(btn_grid, text="Calibration…", command=self._open_calibration, padding=btn_padding, style="Small.TButton")

        # Place buttons in a 3x2 grid
        self.btn_open.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.btn_close.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        self.btn_stop.grid(row=0, column=2, sticky="nsew", padx=6, pady=6)

        self.btn_enable_torque.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.btn_disable_torque.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)
        self.btn_calibrate.grid(row=1, column=2, sticky="nsew", padx=6, pady=6)

        # Make all columns/rows expand equally so cells remain square when window is square
        for c in range(3):
            btn_grid.grid_columnconfigure(c, weight=1)
        for r in range(2):
            btn_grid.grid_rowconfigure(r, weight=1)

        # Device log removed — logs go to stdout

    # --------------------------- Connection actions --------------------------

    def _refresh_ports(self):
        ports = self.controller.get_ports()
        try:
            self.ports_cb["values"] = ports
            if ports and not self.port_var.get():
                self.port_var.set(ports[0])
        except Exception:
            pass
        return ports

    def _connect(self):
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("No Port", "Select a COM port first.")
            return
        ok = self.controller.connect(port)
        if ok:
            self._append(f"[info] Connected to {port}")
            try:
                self.btn_connect.config(state=tk.DISABLED)
                self.btn_disconnect.config(state=tk.NORMAL)
                self.ports_cb.config(state=tk.DISABLED)
            except Exception:
                pass
            self._refresh_connection_indicator()
        else:
            self._append(f"[error] Failed to connect to {port}")

    def _disconnect(self):
        self.controller.disconnect()
        try:
            self.btn_connect.config(state=tk.NORMAL)
            self.btn_disconnect.config(state=tk.DISABLED)
            self.ports_cb.config(state="readonly")
        except Exception:
            pass
        self._append("[info] Disconnected")
        self._refresh_connection_indicator()

    # --------------------------- UI Queue pump -------------------------------

    def _poll_ui_queue(self):
        q = self.controller.ui_queue
        try:
            while True:
                msg = q.get_nowait()
                self._handle_msg(msg)
        except Exception:
            pass
        # Update the connection indicator frequently
        try:
            self._refresh_connection_indicator()
        except Exception:
            pass
        self.after(self.POLL_MS, self._poll_ui_queue)

    def _handle_msg(self, msg):
        mtype = msg.get("type")
        if mtype == "status":
            st = msg["status"]
            en  = int(st.get("en", 0))
            mov = int(st.get("mov", 0))
            state = st.get("state", "CLOSED")

            self._set_lid_text(state=state, moving=mov)
            self._refresh_open_close_buttons(state=state, moving=mov)
            self._refresh_torque_ui(en=en)

            # Forward to calibration window if open
            if self.cal_win:
                self.cal_win.on_status(st)

        elif mtype == "event":
            raw = msg.get("raw", "")
            # Capture direction so we can show OPENING/CLOSING on the Lid line
            if "EVT MOVE_STARTED" in raw:
                self._last_move_dir_open = ("dir=OPEN" in raw)
            elif "EVT MOVE_DONE" in raw:
                self._last_move_dir_open = None
            self._append(f"[event] {raw}")

            if self.cal_win:
                self.cal_win.on_event(raw)

        elif mtype == "ready":
            self._append(f"[ready] {msg.get('raw','')}")
        elif mtype == "hello":
            self._append(f"[hello] {msg.get('raw','')}")
        elif mtype == "pong":
            self._append("[pong] heartbeat OK")
        elif mtype == "error":
            self._append(f"[error] {msg.get('data')}")
        elif mtype == "log":
            self._append(msg.get("text", ""))

    # ----------------------------- Status helpers ----------------------------

    def _set_lid_text(self, state: str, moving: int):
        """Plain text for lid: OPEN/CLOSED/PARTIAL or OPENING/CLOSING while moving."""
        if moving:
            if self._last_move_dir_open is True:
                self.lid_text_var.set("OPENING")
            elif self._last_move_dir_open is False:
                self.lid_text_var.set("CLOSING")
            else:
                self.lid_text_var.set("MOVING")
        else:
            self.lid_text_var.set(state)

    def _refresh_torque_ui(self, en: int):
        """Update torque label text and gate torque buttons."""
        self.torque_text_var.set("ENABLED" if en else "DISABLED")
        if en:
            self.btn_enable_torque.config(state=tk.DISABLED)
            self.btn_disable_torque.config(state=tk.NORMAL)
        else:
            self.btn_enable_torque.config(state=tk.NORMAL)
            self.btn_disable_torque.config(state=tk.DISABLED)

    def _refresh_open_close_buttons(self, state: str, moving: int):
        """
        Disable Open when already OPEN.
        Disable Close when already CLOSED.
        While moving, disable both (Stop stays active).
        """
        if moving:
            self.btn_open.config(state=tk.DISABLED)
            self.btn_close.config(state=tk.DISABLED)
            return

        if state == "OPEN":
            self.btn_open.config(state=tk.DISABLED)
            self.btn_close.config(state=tk.NORMAL)
        elif state == "CLOSED":
            self.btn_open.config(state=tk.NORMAL)
            self.btn_close.config(state=tk.DISABLED)
        else:  # PARTIAL
            self.btn_open.config(state=tk.NORMAL)
            self.btn_close.config(state=tk.NORMAL)

    # ------------------------------ Calibration ------------------------------

    def _open_calibration(self):
        if self.cal_win and self.cal_win.winfo_exists():
            self.cal_win.lift()
            return
        self.cal_win = CalibrationWindow(self.winfo_toplevel(), self.controller)
        # When window is destroyed, drop the reference
        def _cleanup(_evt=None):
            if self.cal_win and not self.cal_win.winfo_exists():
                self.cal_win = None
        self.cal_win.bind("<Destroy>", _cleanup)

    # ------------------------- Connection indicator ------------------------

    def _refresh_connection_indicator(self):
        try:
            if self.controller.is_connected():
                self.conn_text_var.set("Connected")
                self.conn_indicator.config(bg="#5cb85c")
            else:
                self.conn_text_var.set("Disconnected")
                self.conn_indicator.config(bg="#d9534f")
        except Exception:
            pass

    # ------------------------------ Logging ----------------------------------

    def _append(self, text: str):
        # Device log removed from GUI; print to stdout so logs remain visible in terminal
        try:
            print(text)
        except Exception:
            pass


def run_app():
    root = tk.Tk()
    # Start with a compact square window to match the new grid layout
    root.geometry("480x480")
    ctrl = LidController()
    MainWindow(root, ctrl)
    root.mainloop()
