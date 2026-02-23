# Lid Controller — Hardware Wiring & Upload Notes

This project uses an Arduino Uno (or equivalent) to drive a stepper via STEP/DIR and an EN pin (TMC2209 or similar).

Wiring (pins referenced in firmware):

- `EN_PIN`  = Arduino D9  -> Driver `EN` (active LOW)
- `STEP_PIN`= Arduino D6  -> Driver `STEP`
- `DIR_PIN` = Arduino D3  -> Driver `DIR`
- `buttonOpenPin` = Arduino D7 -> Open limit switch / Open button (active-LOW)
- `buttonClosePin` = Arduino D8 -> Close limit switch / Close button (active-LOW)
 - `buttonOpenPin` = Arduino D7 -> Manual Open button (active-LOW)
 - `buttonClosePin` = Arduino D8 -> Manual Close button (active-LOW)
 - `limitOpenPin` = Arduino D4 -> Dedicated OPEN limit switch (active-LOW)
 - `limitClosePin` = Arduino D5 -> Dedicated CLOSE limit switch (active-LOW)

Limit switches / buttons
- Manual push-buttons: wire each button between the pin (D7/D8) and GND so the input reads LOW when pressed. The firmware uses the internal `INPUT_PULLUP`.
- Dedicated limit switches: wire each limit switch between the limit pin (A0/A1) and GND (active-LOW). The firmware checks these during motion and treats a trip as a limit event.

Driver notes
- TMC2209 `EN` is active-LOW. The firmware drives `EN` low to enable the driver.

Behavior notes
- During motion the firmware now detects limit-switch activation: if a limit switch trips while moving in the corresponding direction the firmware stops, sets the position to `0` (closed) or `MAX_STEPS` (open), and emits an event over Serial:
  - `EVT LIMIT_OPEN pos=<N>`
  - `EVT LIMIT_CLOSED pos=<N>`
- The firmware also emits `EVT MOVE_STARTED dir=OPEN|CLOSE` and `EVT MOVE_DONE state=... pos=...`, plus a status snapshot. The PC app listens for `EVT ` lines and updates the UI accordingly.

Uploading the firmware
- Open `arduino_firmware_lid_led_controller_v5.ino` in the Arduino IDE (or PlatformIO).
- Select `Arduino Uno` (or your board) and the correct serial port.
- Compile and upload. Serial runs at 9600 baud.

If you want to use separate physical limit switches (distinct from manual push-buttons), wire them to D7/D8 (as described) and avoid using those pins for separate momentary buttons, or change the pin assignment in the `.ino` accordingly.
