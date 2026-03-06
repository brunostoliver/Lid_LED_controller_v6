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

import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import tkinter.font as tkfont
import ctypes

from src.controller.lid_controller import LidController
from src.config.settings import APP_TITLE, WINDOW_MIN_W, WINDOW_MIN_H
from src.gui.calibration_window import CalibrationWindow


class MainWindow(ttk.Frame):
    POLL_MS = 100  # UI queue poll interval (ms)
    PREFS_PATH = Path(__file__).resolve().parents[2] / "output" / "gui_prefs.json"
    DARK_BG = "#111418"
    DARK_SURFACE = "#1b2027"
    DARK_SURFACE_ALT = "#222a33"
    DARK_BORDER = "#2f3945"
    DARK_TEXT = "#d8dee9"
    DARK_MUTED = "#98a3b3"
    DARK_ACCENT = "#5eb3ff"
    DARK_DANGER = "#ff6b6b"

    def __init__(self, master: tk.Tk, controller: LidController):
        super().__init__(master, padding=10)
        self.controller = controller
        self._apply_dark_theme(master)
        self._apply_dark_title_bar(master)
        self.configure(style="Dark.TFrame")
        self.pack(fill=tk.BOTH, expand=True)

        master.title(APP_TITLE)
        master.minsize(WINDOW_MIN_W, WINDOW_MIN_H)

        # Track last move direction for OPENING/CLOSING text on the Lid line
        self._last_move_dir_open: bool | None = None
        self._limit_open_active = False
        self._limit_close_active = False

        # Flat panel UI state
        self._flat_ui_updating = False
        self._flat_send_after_id: str | None = None

        self.cal_win: CalibrationWindow | None = None

        self._build_widgets()
        self._refresh_ports()

        # Initial status (lid starts as UNKNOWN until firmware reports limit state)
        self._set_lid_text(state="UNKNOWN", moving=0)
        self._refresh_torque_ui(en=0)
        self._refresh_open_close_buttons(state="UNKNOWN", moving=0)
        self._refresh_limit_ui(open_triggered=False, close_triggered=False)
        self._refresh_flat_connected_state()

        self.after(self.POLL_MS, self._poll_ui_queue)

    # ------------------------------ UI ---------------------------------------

    def _apply_dark_theme(self, master: tk.Tk) -> None:
        try:
            master.configure(bg=self.DARK_BG)
        except Exception:
            pass

        style = ttk.Style(master)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            ".",
            background=self.DARK_BG,
            foreground=self.DARK_TEXT,
            fieldbackground=self.DARK_SURFACE,
            bordercolor=self.DARK_BORDER,
            lightcolor=self.DARK_BORDER,
            darkcolor=self.DARK_BORDER,
            troughcolor=self.DARK_SURFACE_ALT,
            insertcolor=self.DARK_TEXT,
        )
        style.configure("Dark.TFrame", background=self.DARK_BG)
        style.configure("TFrame", background=self.DARK_BG)
        style.configure("TLabel", background=self.DARK_BG, foreground=self.DARK_TEXT)
        style.configure("TLabelframe", background=self.DARK_BG, bordercolor=self.DARK_BORDER)
        style.configure("TLabelframe.Label", background=self.DARK_BG, foreground=self.DARK_TEXT)
        style.configure("TButton", background=self.DARK_SURFACE, foreground=self.DARK_TEXT, borderwidth=1)
        style.map(
            "TButton",
            background=[("active", self.DARK_SURFACE_ALT), ("disabled", self.DARK_BG)],
            foreground=[("disabled", self.DARK_MUTED)],
        )
        style.configure("TCheckbutton", background=self.DARK_BG, foreground=self.DARK_TEXT)
        style.map("TCheckbutton", foreground=[("disabled", self.DARK_MUTED)])
        style.configure(
            "TEntry",
            fieldbackground=self.DARK_SURFACE,
            foreground=self.DARK_TEXT,
            bordercolor=self.DARK_BORDER,
        )
        style.map("TEntry", fieldbackground=[("disabled", self.DARK_BG)])
        style.configure(
            "TCombobox",
            fieldbackground=self.DARK_SURFACE,
            background=self.DARK_SURFACE,
            foreground=self.DARK_TEXT,
            arrowcolor=self.DARK_TEXT,
            bordercolor=self.DARK_BORDER,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.DARK_SURFACE), ("disabled", self.DARK_BG)],
            foreground=[("readonly", self.DARK_TEXT), ("disabled", self.DARK_MUTED)],
            selectbackground=[("readonly", self.DARK_SURFACE_ALT)],
            selectforeground=[("readonly", self.DARK_TEXT)],
        )
        style.configure(
            "Horizontal.TScale",
            background=self.DARK_BG,
            troughcolor=self.DARK_SURFACE_ALT,
        )

    def _apply_dark_title_bar(self, window: tk.Misc) -> None:
        # Best-effort Windows title bar dark mode.
        if not str(window.tk.call("tk", "windowingsystem")).lower().startswith("win"):
            return
        try:
            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            value = ctypes.c_int(1)
            dwm = ctypes.windll.dwmapi
            for attr in (20, 19):  # Win10/11 attribute IDs
                dwm.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(attr),
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
        except Exception:
            pass

    def _build_widgets(self):
        # Shared larger button style for better readability
        try:
            base_font = tkfont.nametofont("TkDefaultFont")
            orig_size = base_font.cget("size") or 10
            # Keep buttons compact so other panels have more room
            button_font = tkfont.Font(family=base_font.cget("family"), size=int(orig_size), weight="normal")
        except Exception:
            button_font = None

        style = ttk.Style()
        try:
            if button_font:
                style.configure("Large.TButton", font=button_font)
            else:
                style.configure("Large.TButton", font=())
        except Exception:
            pass

        # Top: connection bar
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=(0, 6))

        self.port_var = tk.StringVar()
        # make the combobox a bit narrower for a compact layout
        self.ports_cb = ttk.Combobox(bar, textvariable=self.port_var, state="readonly", width=30)
        self.ports_cb.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(bar, text="Refresh", command=self._refresh_ports, width=10, style="Large.TButton").pack(side=tk.LEFT, padx=(0, 8))
        self.btn_connect = ttk.Button(bar, text="Connect", command=self._connect, width=10, style="Large.TButton")
        self.btn_connect.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_disconnect = ttk.Button(bar, text="Disconnect", command=self._disconnect, state=tk.DISABLED, width=10, style="Large.TButton")
        self.btn_disconnect.pack(side=tk.LEFT)

        # ===== Status Rows (larger and more readable) =====
        # Create larger font for status display
        try:
            base_font = tkfont.nametofont("TkDefaultFont")
            orig_size = base_font.cget("size") or 10
            status_font = tkfont.Font(family=base_font.cget("family"), size=int(orig_size + 2), weight="bold")
            label_font = tkfont.Font(family=base_font.cget("family"), size=int(orig_size + 1))
        except Exception:
            status_font = None
            label_font = None

        # Initialize connection status variable (used in status row)
        self.conn_text_var = tk.StringVar(value="Disconnected")

        # Status Row 1: Lid and Connection
        status1 = ttk.Frame(self)
        status1.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(status1, text="Lid:", font=label_font).pack(side=tk.LEFT)
        self.lid_text_var = tk.StringVar(value="UNKNOWN")
        ttk.Label(status1, textvariable=self.lid_text_var, font=status_font, foreground=self.DARK_ACCENT).pack(side=tk.LEFT, padx=(12, 24))

        ttk.Label(status1, text="Connection:", font=label_font).pack(side=tk.LEFT)
        ttk.Label(status1, textvariable=self.conn_text_var, font=status_font).pack(side=tk.LEFT, padx=(12, 0))

        # Status Row 2: Torque
        status2 = ttk.Frame(self)
        status2.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(status2, text="Torque:", font=label_font).pack(side=tk.LEFT)
        self.torque_text_var = tk.StringVar(value="DISABLED")
        ttk.Label(status2, textvariable=self.torque_text_var, font=status_font).pack(side=tk.LEFT, padx=(12, 0))

        # Status Row 3: Limit Switches (dedicated and prominent)
        limits_frame = ttk.LabelFrame(self, text="Limit Switches", padding=8)
        limits_frame.pack(fill=tk.X, pady=(0, 8))

        # Open Limit
        open_row = ttk.Frame(limits_frame)
        open_row.pack(fill=tk.X, pady=4)
        ttk.Label(open_row, text="Open Limit:", font=label_font).pack(side=tk.LEFT)
        self.limit_open_var = tk.StringVar(value="OFF")
        self.limit_open_label = ttk.Label(open_row, textvariable=self.limit_open_var, font=status_font, foreground=self.DARK_DANGER)
        self.limit_open_label.pack(side=tk.LEFT, padx=(12, 0))

        # Close Limit
        close_row = ttk.Frame(limits_frame)
        close_row.pack(fill=tk.X, pady=4)
        ttk.Label(close_row, text="Close Limit:", font=label_font).pack(side=tk.LEFT)
        self.limit_close_var = tk.StringVar(value="OFF")
        self.limit_close_label = ttk.Label(close_row, textvariable=self.limit_close_var, font=status_font, foreground=self.DARK_DANGER)
        self.limit_close_label.pack(side=tk.LEFT, padx=(12, 0))

        # ===== Main content area (Controls + Flat Panel share space) =====
        content = ttk.Frame(self)
        content.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)

        # ===== Controls =====
        ctrl = ttk.LabelFrame(content, text="Controls")
        ctrl.grid(row=0, column=0, sticky="nsew", pady=(0, 8))

        # Inner grid for buttons (3 columns x 2 rows)
        btn_grid = ttk.Frame(ctrl, padding=6)
        btn_grid.pack(fill=tk.BOTH, expand=True)

        btn_padding = 3

        # Control buttons use the (now compact) Large.TButton style
        self.btn_open = ttk.Button(btn_grid, text="Open", command=self.controller.open_lid, padding=btn_padding, style="Large.TButton")
        self.btn_close = ttk.Button(btn_grid, text="Close", command=self.controller.close_lid, padding=btn_padding, style="Large.TButton")
        self.btn_stop = ttk.Button(btn_grid, text="Stop", command=self.controller.stop, padding=btn_padding, style="Large.TButton")

        self.btn_enable_torque = ttk.Button(btn_grid, text="Enable Holding Torque", command=self.controller.enable, padding=btn_padding, style="Large.TButton")
        self.btn_disable_torque = ttk.Button(btn_grid, text="Disable Holding Torque", command=self.controller.disable, padding=btn_padding, style="Large.TButton")
        self.btn_calibrate = ttk.Button(btn_grid, text="Calibration…", command=self._open_calibration, padding=btn_padding, style="Large.TButton")

        # Place buttons in a 3x2 grid
        self.btn_open.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.btn_close.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        self.btn_stop.grid(row=0, column=2, sticky="nsew", padx=6, pady=6)

        self.btn_enable_torque.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.btn_disable_torque.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)
        self.btn_calibrate.grid(row=1, column=2, sticky="nsew", padx=6, pady=6)

        # Make all columns/rows expand equally
        for c in range(3):
            btn_grid.grid_columnconfigure(c, weight=1)
        for r in range(2):
            btn_grid.grid_rowconfigure(r, weight=1)

        # ===== Flat Panel =====
        flat = ttk.LabelFrame(content, text="Flat Panel", padding=8)
        flat.grid(row=1, column=0, sticky="nsew")

        flat_row1 = ttk.Frame(flat)
        flat_row1.pack(fill=tk.X)

        self.flat_on_var = tk.IntVar(value=0)
        self.chk_flat_on = ttk.Checkbutton(
            flat_row1,
            text="On",
            variable=self.flat_on_var,
            command=self._on_flat_toggle,
        )
        self.chk_flat_on.pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(flat_row1, text="Brightness (0-255):").pack(side=tk.LEFT)

        self.flat_pwm_entry_var = tk.StringVar(value="0")
        self.flat_pwm_entry = ttk.Entry(flat_row1, textvariable=self.flat_pwm_entry_var, width=6)
        self.flat_pwm_entry.pack(side=tk.LEFT, padx=(12, 0))
        self.flat_pwm_entry.bind("<Return>", self._on_flat_entry_commit)
        self.flat_pwm_entry.bind("<FocusOut>", self._on_flat_entry_commit)

        flat_row2 = ttk.Frame(flat)
        flat_row2.pack(fill=tk.X, pady=(10, 0))

        self.flat_pwm_var = tk.IntVar(value=0)
        self.flat_pwm_scale = ttk.Scale(
            flat_row2,
            from_=0,
            to=255,
            orient=tk.HORIZONTAL,
            command=self._on_flat_slider,
        )
        self.flat_pwm_scale.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.flat_pwm_scale.set(0)

        # Device log removed — logs go to stdout

    # --------------------------- Connection actions --------------------------

    def _load_last_port(self) -> str:
        try:
            if not self.PREFS_PATH.exists():
                return ""
            data = json.loads(self.PREFS_PATH.read_text(encoding="utf-8"))
            return str(data.get("last_port", "") or "")
        except Exception:
            return ""

    def _save_last_port(self, port_label: str) -> None:
        try:
            self.PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Persist the bare device (e.g., COM5), not the full label text.
            device = (port_label or "").split(" ")[0]
            self.PREFS_PATH.write_text(json.dumps({"last_port": device}, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _refresh_ports(self):
        ports = self.controller.get_ports()
        try:
            self.ports_cb["values"] = ports
            if not ports:
                self.port_var.set("")
                return ports

            selected = self.port_var.get()
            if selected and selected in ports:
                return ports

            last_port = self._load_last_port()
            if last_port:
                for label in ports:
                    if label.split(" ")[0] == last_port:
                        self.port_var.set(label)
                        return ports

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
            self._save_last_port(port)
            self._append(f"[info] Connected to {port}")
            try:
                self.btn_connect.config(state=tk.DISABLED)
                self.btn_disconnect.config(state=tk.NORMAL)
                self.ports_cb.config(state=tk.DISABLED)
            except Exception:
                pass
            self._refresh_connection_indicator()
            self._refresh_flat_connected_state()
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
        self._refresh_flat_connected_state()

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
            self._refresh_torque_ui(en=en)

            # Prefer live physical limit status from firmware if available.
            if "lim_open" in st or "lim_close" in st:
                open_on = int(st.get("lim_open", 0)) != 0
                close_on = int(st.get("lim_close", 0)) != 0
                self._limit_open_active = open_on
                self._limit_close_active = close_on
                self._refresh_limit_ui(open_triggered=open_on, close_triggered=close_on)
                # Update lid text after limit state changes from status
                self._set_lid_text(state=state, moving=mov)
            else:
                # Backward-compatible fallback for older firmware.
                if mov:
                    self._limit_open_active = False
                    self._limit_close_active = False
                    self._refresh_limit_ui(open_triggered=False, close_triggered=False)
                elif state == "OPEN":
                    self._limit_open_active = True
                    self._limit_close_active = False
                    self._refresh_limit_ui(open_triggered=True, close_triggered=False)
                elif state == "CLOSED":
                    self._limit_open_active = False
                    self._limit_close_active = True
                    self._refresh_limit_ui(open_triggered=False, close_triggered=True)
                else:
                    self._limit_open_active = False
                    self._limit_close_active = False
                    self._refresh_limit_ui(open_triggered=False, close_triggered=False)
                # Update lid text after fallback limit inference
                self._set_lid_text(state=state, moving=mov)

            self._refresh_open_close_buttons(state=state, moving=mov)

            # Flat panel state (optional fields)
            if "flat_on" in st or "flat_pwm" in st:
                self._sync_flat_ui_from_status(st)

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
            elif "EVT LIMIT_STATE" in raw:
                open_v = self._extract_evt_int(raw, "open")
                close_v = self._extract_evt_int(raw, "close")
                if open_v is not None and close_v is not None:
                    self._limit_open_active = (open_v != 0)
                    self._limit_close_active = (close_v != 0)
                    self._refresh_limit_ui(open_triggered=(open_v != 0), close_triggered=(close_v != 0))
                    # Update lid indicator to reflect new physical state
                    self._set_lid_text(state="CLOSED", moving=0)  # state is overridden by limits anyway
                    st = self.controller.status
                    self._refresh_open_close_buttons(state=st.get("state", "CLOSED"), moving=int(st.get("mov", 0)))
            elif "EVT LIMIT_OPEN" in raw:
                self._limit_open_active = True
                self._limit_close_active = False
                self._refresh_limit_ui(open_triggered=True, close_triggered=False)
                # Update lid indicator to show OPEN (physical limit active)
                self._set_lid_text(state="OPEN", moving=0)
                st = self.controller.status
                self._refresh_open_close_buttons(state=st.get("state", "CLOSED"), moving=int(st.get("mov", 0)))
            elif "EVT LIMIT_CLOSED" in raw:
                self._limit_open_active = False
                self._limit_close_active = True
                self._refresh_limit_ui(open_triggered=False, close_triggered=True)
                # Update lid indicator to show CLOSED (physical limit active)
                self._set_lid_text(state="CLOSED", moving=0)
                st = self.controller.status
                self._refresh_open_close_buttons(state=st.get("state", "CLOSED"), moving=int(st.get("mov", 0)))
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

        # Update flat control connected gating after any message
        try:
            self._refresh_flat_connected_state()
        except Exception:
            pass

    # ------------------------------ Flat panel ------------------------------

    def _refresh_flat_connected_state(self) -> None:
        connected = bool(self.controller.is_connected())
        state = tk.NORMAL if connected else tk.DISABLED
        try:
            self.chk_flat_on.config(state=state)
            self.flat_pwm_scale.config(state=state)
            self.flat_pwm_entry.config(state=state)
        except Exception:
            pass

    def _sync_flat_ui_from_status(self, st: dict) -> None:
        self._flat_ui_updating = True
        try:
            flat_on = int(st.get("flat_on", 0))
            flat_pwm = int(st.get("flat_pwm", 0))
            flat_pwm = max(0, min(255, flat_pwm))

            self.flat_on_var.set(1 if flat_on else 0)
            self.flat_pwm_var.set(flat_pwm)
            self.flat_pwm_scale.set(flat_pwm)
            self.flat_pwm_entry_var.set(str(flat_pwm))
        finally:
            self._flat_ui_updating = False

    def _on_flat_toggle(self) -> None:
        if self._flat_ui_updating:
            return
        on = int(self.flat_on_var.get()) != 0
        if on:
            # Ensure brightness is applied when turning on
            pwm = self._get_flat_pwm_from_ui()
            self.controller.flat_brightness(pwm)
            self.controller.flat_on()
        else:
            self.controller.flat_off()

    def _on_flat_slider(self, _val: str) -> None:
        if self._flat_ui_updating:
            return
        pwm = int(float(self.flat_pwm_scale.get()))
        pwm = max(0, min(255, pwm))
        self.flat_pwm_var.set(pwm)
        self.flat_pwm_entry_var.set(str(pwm))
        self._schedule_send_flat_brightness(pwm)

    def _on_flat_entry_commit(self, _evt=None) -> None:
        if self._flat_ui_updating:
            return
        pwm = self._get_flat_pwm_from_ui()
        self.flat_pwm_scale.set(pwm)
        self.flat_pwm_var.set(pwm)
        self.flat_pwm_entry_var.set(str(pwm))
        self._schedule_send_flat_brightness(pwm)

    def _get_flat_pwm_from_ui(self) -> int:
        try:
            pwm = int(str(self.flat_pwm_entry_var.get()).strip())
        except Exception:
            pwm = 0
        pwm = max(0, min(255, pwm))
        return pwm

    def _schedule_send_flat_brightness(self, pwm: int) -> None:
        # Throttle rapid slider movements to avoid spamming serial.
        try:
            if self._flat_send_after_id:
                self.after_cancel(self._flat_send_after_id)
        except Exception:
            pass
        self._flat_send_after_id = self.after(150, lambda: self._send_flat_brightness(pwm))

    def _send_flat_brightness(self, pwm: int) -> None:
        self._flat_send_after_id = None
        self.controller.flat_brightness(pwm)

    # ----------------------------- Status helpers ----------------------------

    def _set_lid_text(self, state: str, moving: int):
        """
        Plain text for lid: OPEN/CLOSED/PARTIAL/UNKNOWN or OPENING/CLOSING while moving.
        
        Priority: Physical limits (actual hardware state) > software state estimate.
        If a limit is active, that is the ground truth.
        UNKNOWN: neither limit active (position unknown until a limit is triggered).
        """
        if moving:
            if self._last_move_dir_open is True:
                self.lid_text_var.set("OPENING")
            elif self._last_move_dir_open is False:
                self.lid_text_var.set("CLOSING")
            else:
                self.lid_text_var.set("MOVING")
        else:
            # Use physical limits as source of truth when available
            if self._limit_open_active and self._limit_close_active:
                self.lid_text_var.set("PARTIAL")
            elif self._limit_open_active:
                self.lid_text_var.set("OPEN")
            elif self._limit_close_active:
                self.lid_text_var.set("CLOSED")
            else:
                # Use state from firmware (may be UNKNOWN if no limits active)
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

        if self._limit_open_active and self._limit_close_active:
            self.btn_open.config(state=tk.DISABLED)
            self.btn_close.config(state=tk.DISABLED)
            return

        if self._limit_open_active:
            self.btn_open.config(state=tk.DISABLED)
            self.btn_close.config(state=tk.NORMAL)
            return

        if self._limit_close_active:
            self.btn_open.config(state=tk.NORMAL)
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

    def _refresh_limit_ui(self, open_triggered: bool, close_triggered: bool):
        self.limit_open_var.set("ON" if open_triggered else "OFF")
        self.limit_close_var.set("ON" if close_triggered else "OFF")
        # Highlight limits when triggered (red when ON, normal when OFF)
        self.limit_open_label.config(foreground=self.DARK_DANGER if open_triggered else self.DARK_TEXT)
        self.limit_close_label.config(foreground=self.DARK_DANGER if close_triggered else self.DARK_TEXT)

    def _extract_evt_int(self, raw: str, key: str):
        token = f"{key}="
        for part in raw.split():
            if part.startswith(token):
                try:
                    return int(part[len(token):])
                except Exception:
                    return None
        return None

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
            else:
                self.conn_text_var.set("Disconnected")
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
