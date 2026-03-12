# Lid Controller - Hardware Wiring & Upload Notes

This project uses an Arduino Nano Every or Arduino Uno-compatible board to drive a stepper via STEP/DIR and an EN pin (TMC2209 or similar).

Wiring (pins referenced in firmware):

- `EN_PIN` = Arduino `D9` -> Driver `EN` (active LOW)
- `STEP_PIN` = Arduino `D6` -> Driver `STEP`
- `DIR_PIN` = Arduino `D3` -> Driver `DIR`
- `buttonPin` = Arduino `D7` -> Single manual button (active LOW)
- `limitOpenPin` = Arduino `D4` -> Dedicated OPEN limit switch (active LOW)
- `limitClosePin` = Arduino `D5` -> Dedicated CLOSE limit switch (active LOW)

Limit switches / buttons
- Manual push-button: wire the button between `D7` and `GND` so the input reads `LOW` when pressed. The firmware uses the internal `INPUT_PULLUP`.
- Dedicated limit switches: wire each limit switch between the limit pin (`D4`/`D5`) and `GND` (active LOW). The firmware reports the live state over Serial and uses it to gate commands.

Driver notes
- TMC2209 `EN` is active LOW. The firmware drives `EN` low to enable the driver.

Behavior notes
- The firmware emits live limit switch state changes as `EVT LIMIT_STATE open=0/1 close=0/1`.
- `OPEN`/`CLOSE` commands, and the manual button, are gated when the corresponding end-stop is active.
- Manual button behavior on `D7`:
- If the lid is moving, pressing the button stops motion; the next press moves in the opposite direction.
- If the lid is idle, the button only acts when a limit switch is active (`OPEN` limit -> `CLOSE`, `CLOSE` limit -> `OPEN`).

Flat LED panel (optional; PWM via MOSFET)
- The Arduino drives a MOSFET module with PWM on `FLAT_PWM_PIN = D10`.
- On Arduino Uno-class boards, the firmware configures `D10` for higher-frequency PWM using Timer1.
- On Arduino Nano Every, the firmware falls back to the board core's default `analogWrite()` PWM because the Uno Timer1 registers are not available on the ATmega4809.

Important note for EL inverters
- If you are powering a DC-AC micro-inverter (for example, for an EL panel) through the MOSFET, low-frequency PWM power switching can prevent the inverter from starting.
- On Uno-class boards, the firmware drives `D10` at a higher PWM frequency (about 31 kHz) to improve inverter behavior, but some inverters still require steady DC input.
- If the panel lights when the inverter is connected directly to `12V` but not when PWM'd, try:
- testing with `Brightness=255` (near steady ON),
- using ON/OFF control only (no dimming), or
- using an inverter that supports dimming via a dedicated control input.

Typical wiring (low-side switching)
- `12V` supply `+` -> Flat panel `+`
- Flat panel `-` -> MOSFET module load output (often labeled `OUT-`, `LOAD-`, or `DRAIN`)
- `12V` supply `-` -> MOSFET module `GND` / `SOURCE`
- Arduino `GND` -> `12V` supply `-` (common ground)
- Arduino `D10` -> MOSFET module signal input (often labeled `IN`, `SIG`, or `PWM`)
- If your MOSFET module requires a logic supply (often `VCC`): Arduino `5V` -> module `VCC`

Notes
- Do not connect `12V` to the Arduino.
- Use a logic-level MOSFET module that accepts `5V` PWM on its input.
- Brightness uses Arduino `analogWrite()` (`0..255`). Default at boot is OFF.

Uploading the firmware
- Open `arduino_firmware_lid_led_controller_v5.ino` in the Arduino IDE (or PlatformIO).
- Select the correct board and serial port:
- `Arduino Nano Every` -> `arduino:megaavr:nona4809`
- `Arduino Uno` -> `arduino:avr:uno`
- Compile and upload. Serial runs at `9600` baud.
- Example `arduino-cli` commands for the Nano Every on `COM5`:
- `arduino-cli compile --fqbn arduino:megaavr:nona4809 <sketch_folder>`
- `arduino-cli upload -p COM5 --fqbn arduino:megaavr:nona4809 <sketch_folder>`

If you want different pin assignments (buttons/limits), change the pin constants in `arduino_firmware_lid_led_controller_v5.ino` and update this README accordingly.

Note: `D8` is not used by the current firmware.
