# src/gui/calibration_window.py
"""
Calibration (Teach Mode) pop-up window.

Behavior:
- Opens directly in Active calibration mode (sends CAL.START automatically).
- Instructions at top (Close is enabled after 'Teach Opened', and you can close
  the window after saving to end calibration).
- Actions: Save Calibration, Restore Defaults.
- Move Lid by Step Count: Open/Close by N steps (Close disabled while at pos=0; also
  explicitly enabled right after 'Teach Opened').
- Teach Opened / Teach Closed setpoints.
- Window close sends CAL.ABORT to exit teach mode safely if still active.

MainWindow forwards status/events via on_status()/on_event().
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
import tkinter.font as tkfont


class CalibrationWindow(tk.Toplevel):
    def __init__(self, master, controller):
        super().__init__(master)
        self.title("Calibration (Teach Mode)")
        self.controller = controller

        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Vars
        self.cal_state_var = tk.StringVar(value="ACTIVE")  # start in active mode
        self.pos_var = tk.IntVar(value=0)
        self.max_var = tk.IntVar(value=10500)
        self.steps_var = tk.IntVar(value=100)  # default step size
        self.info_var = tk.StringVar(value="")
        self.saved_state_var = tk.StringVar(value="Not saved")

        # Layout
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        # Shared larger button style for better readability
        try:
            base_font = tkfont.nametofont("TkDefaultFont")
            orig_size = base_font.cget("size") or 10
            button_font = tkfont.Font(family=base_font.cget("family"), size=int(orig_size + 2), weight="bold")
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

        # ===== Instructions (top) =====
        instructions = (
            "Calibration assumes the lid is CLOSED now.\n"
            "1) Use 'Move Lid by Step Count' → Open to reach fully OPEN.\n"
            "2) Click 'Teach Opened'. (The Close control will become active now.)\n"
            "3) Use Close to return to fully CLOSED.\n"
            "4) Click 'Teach Closed'.\n"
            "5) Click 'Save Calibration'.\n"
            "6) After saving, simply CLOSE THIS WINDOW to end calibration.\n"
            "   (You can use 'Restore Defaults' to revert to the factory travel.)"
        )
        ttk.Label(root, text=instructions, justify=tk.LEFT).pack(fill=tk.X, pady=(0, 8))

        # ===== Actions =====
        act = ttk.LabelFrame(root, text="Actions")
        act.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(act, text="Save Calibration", command=self.controller.cal_save, style="Large.TButton").pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(act, text="Restore Defaults", command=self.controller.cal_defaults, style="Large.TButton").pack(side=tk.LEFT, padx=18, pady=6)
        ttk.Label(act, text="Status:").pack(side=tk.LEFT, padx=(18, 6))
        ttk.Label(act, textvariable=self.saved_state_var, foreground="#006600").pack(side=tk.LEFT, padx=(0, 6))

        # ===== Status row =====
        r0 = ttk.Frame(root); r0.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(r0, text="Calibration:").pack(side=tk.LEFT)
        ttk.Label(r0, textvariable=self.cal_state_var, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=(6, 18))
        ttk.Label(r0, text="Position:").pack(side=tk.LEFT)
        ttk.Label(r0, textvariable=self.pos_var).pack(side=tk.LEFT, padx=(6, 18))
        ttk.Label(r0, text="Max:").pack(side=tk.LEFT)
        ttk.Label(r0, textvariable=self.max_var).pack(side=tk.LEFT, padx=(6, 0))

        # ===== Move Lid by Step Count =====
        movef = ttk.LabelFrame(root, text="Move Lid by Step Count")
        movef.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(movef, text="Steps:").pack(side=tk.LEFT, padx=(6, 6))
        steps_entry = ttk.Entry(movef, textvariable=self.steps_var, width=8)
        steps_entry.pack(side=tk.LEFT, padx=(0, 12))
        self.btn_open = ttk.Button(movef, text="Open", command=self._move_open, style="Large.TButton")
        self.btn_open.pack(side=tk.LEFT, padx=6, pady=6)
        self.btn_close = ttk.Button(movef, text="Close", command=self._move_close, style="Large.TButton")
        self.btn_close.pack(side=tk.LEFT, padx=6, pady=6)

        # ===== Teach setpoints =====
        sp = ttk.LabelFrame(root, text="Teach Setpoints")
        sp.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(sp, text="Teach Opened", command=self._teach_opened, style="Large.TButton").pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(sp, text="Teach Closed", command=self._teach_closed, style="Large.TButton").pack(side=tk.LEFT, padx=6, pady=6)

        # Info line
        ttk.Label(root, textvariable=self.info_var, foreground="#444").pack(fill=tk.X, pady=(6, 0))

        # Start calibration immediately and request fresh status
        try:
            self.controller.cal_start()
            self.info_var.set("Teach mode started. Follow the steps above, then Save Calibration and close this window.")
            self.controller.request_status()
            self.controller.cal_status()
        except Exception:
            pass

        # Initial button gating (assume closed at startup → disable Close)
        self._refresh_move_buttons(cal_active=True, pos=0)

    # --------- callbacks from MainWindow ---------

    def on_status(self, status: dict):
        pos = int(status.get("pos", 0))
        self.pos_var.set(pos)
        self.max_var.set(int(status.get("max", 10500)))
        cal = int(status.get("cal", 0))
        self.cal_state_var.set("ACTIVE" if cal else "IDLE")
        self._refresh_move_buttons(cal_active=bool(cal), pos=pos)

    def on_event(self, raw: str):
        if "CAL_STARTED" in raw:
            self.saved_state_var.set("Not saved")
            self.info_var.set("Teach mode started. Open → Teach Opened → Close → Teach Closed → Save, then close this window.")
        elif "CAL_SAVED" in raw:
            self.saved_state_var.set("Calibration saved")
            self.info_var.set("Calibration saved. You can now close this window to end calibration.")
        elif "CAL_ABORTED" in raw:
            self.saved_state_var.set("Not saved")
            self.info_var.set("Teach mode stopped without saving.")
        elif "CAL_DEFAULTS" in raw:
            self.saved_state_var.set("Calibration saved")
            self.info_var.set("Defaults restored and saved.")
        elif raw.startswith("CAL ") or raw.startswith("EVT "):
            self.info_var.set(raw)

    # --------- helpers ---------

    def _move_open(self):
        steps = max(1, int(self.steps_var.get() or 100))
        self.controller.cal_jog_open(steps)

    def _move_close(self):
        steps = max(1, int(self.steps_var.get() or 100))
        self.controller.cal_jog_close(steps)

    def _teach_opened(self):
        # Tell firmware, then proactively enable Close button so user can proceed
        self.controller.cal_set_open()
        self.saved_state_var.set("Not saved")
        self.btn_close.config(state=tk.NORMAL)
        self.info_var.set("Opened taught. You can now Close to fully CLOSED, Teach Closed, then Save and close this window.")
        try:
            self.controller.request_status()
        except Exception:
            pass

    def _teach_closed(self):
        self.controller.cal_set_closed()
        self.saved_state_var.set("Not saved")
        self.info_var.set("Closed taught. Click 'Save Calibration', then close this window to end calibration.")
        try:
            self.controller.request_status()
        except Exception:
            pass

    def _refresh_move_buttons(self, cal_active: bool, pos: int):
        """
        - Disable 'Close' while pos == 0 (assumed closed) to avoid stressing the mechanism.
        - After 'Teach Opened', we also explicitly enable Close immediately (handled in _teach_opened).
        - If not in calibration, disable both move buttons.
        """
        if not cal_active:
            self.btn_open.config(state=tk.DISABLED)
            self.btn_close.config(state=tk.DISABLED)
            return

        self.btn_open.config(state=tk.NORMAL)
        if pos <= 0:
            self.btn_close.config(state=tk.DISABLED)
        else:
            self.btn_close.config(state=tk.NORMAL)

    def _on_close(self):
        # Exit teach mode safely when the window is closed
        try:
            self.controller.cal_abort()
        except Exception:
            pass
        self.destroy()
