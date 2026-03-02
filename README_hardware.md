# Lid Controller — Hardware Wiring & Upload Notes

This project uses an Arduino Uno (or equivalent) to drive a stepper via STEP/DIR and an EN pin (TMC2209 or similar).

Wiring (pins referenced in firmware):

- `EN_PIN`  = Arduino D9  -> Driver `EN` (active LOW)
- `STEP_PIN`= Arduino D6  -> Driver `STEP`
- `DIR_PIN` = Arduino D3  -> Driver `DIR`
- `buttonPin` = Arduino D7 -> Single manual button (active-LOW)
- `limitOpenPin` = Arduino D4 -> Dedicated OPEN limit switch (active-LOW)
- `limitClosePin` = Arduino D5 -> Dedicated CLOSE limit switch (active-LOW)

Limit switches / buttons
- Manual push-button: wire the button between D7 and GND so the input reads LOW when pressed. The firmware uses the internal `INPUT_PULLUP`.
- Dedicated limit switches: wire each limit switch between the limit pin (D4/D5) and GND (active-LOW). The firmware reports the live state over Serial and uses it to gate commands.

Driver notes
- TMC2209 `EN` is active-LOW. The firmware drives `EN` low to enable the driver.

Behavior notes
- The firmware emits live limit switch state changes as:
  - `EVT LIMIT_STATE open=0/1 close=0/1`
- OPEN/CLOSE commands (and the manual button) are gated when the corresponding end-stop is active.
- Manual button behavior (single button on D7):
  - If the lid is moving, pressing the button stops motion; the next press moves in the opposite direction.
  - If the lid is idle, the button only acts when a limit switch is active (OPEN limit -> CLOSE, CLOSE limit -> OPEN).

Flat LED panel (optional; PWM via MOSFET)
- This applies to the firmware/GUI version that includes flat-panel PWM support (the `feature/flat-panel-pwm` branch).
- The Arduino drives a MOSFET module with PWM on `FLAT_PWM_PIN = D10`.

Typical wiring (low-side switching)
- 12V supply `+` -> Flat panel `+`
- Flat panel `-` -> MOSFET module load output (often labeled `OUT-`, `LOAD-`, or `DRAIN`)
- 12V supply `-` -> MOSFET module `GND` / `SOURCE`
- Arduino `GND` -> 12V supply `-` (common ground)
- Arduino `D10` -> MOSFET module signal input (often labeled `IN`, `SIG`, or `PWM`)
- If your MOSFET module requires a logic supply (often `VCC`): Arduino `5V` -> module `VCC`

Notes
- Do not connect 12V to the Arduino.
- Use a logic-level MOSFET module that accepts 5V PWM on its input.
- Brightness uses Arduino `analogWrite()` (0..255). Default at boot is OFF.

Uploading the firmware
- Open `arduino_firmware_lid_led_controller_v5.ino` in the Arduino IDE (or PlatformIO).
- Select `Arduino Uno` (or your board) and the correct serial port.
- Compile and upload. Serial runs at 9600 baud.

If you want different pin assignments (buttons/limits), change the pin constants in `arduino_firmware_lid_led_controller_v5.ino` and update this README accordingly.

Note: D8 is not used by the current firmware.
