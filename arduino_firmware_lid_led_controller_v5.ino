/*
  Lid Controller – Step/Dir + Buttons (TMC2209, Arduino Uno R3)

  Pins & constants per user spec:
    EN_PIN  = 9
    STEP_PIN= 6
    DIR_PIN = 3
    buttonPin = 7

  Serial: 9600 baud
  Timing: pulseDelay = 500   (microseconds between step edges)
  Travel: default 10500 steps (calibrated value can be saved in EEPROM)
  Debounce: debounceDelay = 50 ms

  Serial commands (case-insensitive):
    OPEN         -> move to calibrated max
    CLOSE        -> move to 0
    STOP         -> stop motion ASAP
    POS?         -> report current step position (0..max)
    STATUS?      -> report enabled state, moving state, position
    ENABLE       -> EN low
    DISABLE      -> EN high

  Notes:
    - Direction "OPEN" is a logical direction; if your lid runs the wrong way,
      set INVERT_DIR to true OR swap one motor coil pair.
    - Manual button is wired active-LOW to pin 7 and uses INPUT_PULLUP.
      Wire the switch between the pin and GND.
*/

#include <EEPROM.h>

/////////////////////// User-Specified Pins & Constants ///////////////////////
const int EN_PIN   = 9;
const int STEP_PIN = 6;
const int DIR_PIN  = 3;

const int buttonPin = 7;        // single manual button (active-LOW)

// Dedicated limit switch pins (separate from manual buttons)
const int limitOpenPin  = 4;    // limit switch for fully OPEN (active-LOW)
const int limitClosePin = 5;    // limit switch for fully CLOSED (active-LOW)

// Flat panel PWM output (MOSFET module control)
const int FLAT_PWM_PIN = 10;    // Uno PWM pin (Timer1)

const long DEFAULT_MAX_STEPS = 10500;
const int  pulseDelay = 500;                  // microseconds
const unsigned long debounceDelay = 50UL;     // milliseconds
const long STEPS_PER_CHUNK = 1;               // step granularity
const long LIMIT_POS_TOL = 20;                // steps tolerance when validating end-stop state

// Flat panel brightness range (analogWrite on Uno is 8-bit)
const int FLAT_PWM_MIN = 0;
const int FLAT_PWM_MAX = 255;

// EEPROM calibration storage
const int EEPROM_MAGIC_ADDR = 0;
const int EEPROM_MAX_ADDR = EEPROM_MAGIC_ADDR + sizeof(unsigned long);
const unsigned long EEPROM_MAGIC = 0x4C494443UL; // "LIDC"

/////////////////////// Behavior Tweaks ///////////////////////////////////////
// If your lid moves the opposite way, flip this to true.
const bool INVERT_DIR = false;

// Safety: minimum enable delay before stepping (us)
const unsigned long ENABLE_SETTLE_US = 1000;

/////////////////////// State Variables ///////////////////////////////////////
volatile bool stopRequested = false;
bool enabled = false;
bool moving  = false;
bool calibrationActive = false;

// Flat panel state
bool flatOn = false;
int flatPwm = 0; // 0..255

long positionSteps = 0; // 0 = fully closed; maxSteps = fully open
long maxSteps = DEFAULT_MAX_STEPS;
long teachOpenPos = -1;
long teachClosedPos = -1;

// Button debouncing (single button)
int lastBtnReading = HIGH;     // using INPUT_PULLUP -> HIGH = not pressed
int stableBtnState = HIGH;
unsigned long lastBtnChangeMs = 0;

// Single-button behavior:
// - If moving: press stops motion and arms a "reverse" action
// - If idle: button only works when a limit is active (OPEN->CLOSE, CLOSE->OPEN)
// - If idle and reverse is armed: press moves to opposite end-stop
bool reverseArmed = false;
bool lastMoveDirOpen = false;
bool lastMoveDirValid = false;

// Track current move direction so pollButtons can detect limit switches
bool currentDirOpen = false;

// Last-published physical limit states (active-LOW switches)
bool lastLimitOpenActive = false;
bool lastLimitCloseActive = false;

/////////////////////// Forward Declarations (fix compile order) //////////////
void pollButtons(bool allowImmediateAction = true);
void emitStatusJSON();
void saveCalibratedMaxSteps();
void loadCalibratedMaxSteps();
void serviceSerialDuringMove(); // Add forward declaration
void applyFlatOutput();
void setFlatOn(bool on);
void setFlatBrightness(int pwm);

/////////////////////// Helpers ///////////////////////////////////////////////
void setEnable(bool en)
{
  enabled = en;
  // TMC22xx EN pin is active-LOW: LOW = enabled, HIGH = disabled
  digitalWrite(EN_PIN, en ? LOW : HIGH);
  if (en) {
    delayMicroseconds(ENABLE_SETTLE_US);
  }
}

inline void setDir(bool dirOpen)
{
  // dirOpen true = logical OPEN direction
  bool level = INVERT_DIR ? !dirOpen : dirOpen;
  digitalWrite(DIR_PIN, level ? HIGH : LOW);
}

// Single step with symmetric pulse spacing
inline void singleStep()
{
  digitalWrite(STEP_PIN, HIGH);
  delayMicroseconds(pulseDelay);
  digitalWrite(STEP_PIN, LOW);
  delayMicroseconds(pulseDelay);
}

void applyFlatOutput()
{
  if (flatOn && flatPwm > 0) {
    analogWrite(FLAT_PWM_PIN, constrain(flatPwm, FLAT_PWM_MIN, FLAT_PWM_MAX));
  } else {
    analogWrite(FLAT_PWM_PIN, 0);
  }
}

void setFlatOn(bool on)
{
  flatOn = on;
  applyFlatOutput();
  Serial.print(F("EVT FLAT on="));
  Serial.print(flatOn ? 1 : 0);
  Serial.print(F(" pwm="));
  Serial.println(flatPwm);
  emitStatusJSON();
}

void setFlatBrightness(int pwm)
{
  flatPwm = constrain(pwm, FLAT_PWM_MIN, FLAT_PWM_MAX);
  applyFlatOutput();
  Serial.print(F("EVT FLAT pwm="));
  Serial.println(flatPwm);
  emitStatusJSON();
}

// Move toward a target position (blocking but responsive to STOP & buttons)
void moveTo(long targetSteps)
{
  targetSteps = constrain(targetSteps, 0L, maxSteps);
  if (positionSteps == targetSteps) return;

  if (!enabled) setEnable(true);
  stopRequested = false;
  moving = true;

  const bool dirOpen = (targetSteps > positionSteps);
  setDir(dirOpen);

  // remember last move direction (for single-button reverse after STOP)
  lastMoveDirOpen = dirOpen;
  lastMoveDirValid = true;

  // remember direction for limit detection
  currentDirOpen = dirOpen;

  // Announce move started
  Serial.print(F("EVT MOVE_STARTED "));
  Serial.print(F("dir=")); Serial.println(dirOpen ? F("OPEN") : F("CLOSE"));
  // Emit JSON status so GUI shows "OPENING" or "CLOSING" immediately
  emitStatusJSON();

  while (!stopRequested && positionSteps != targetSteps)
  {
    // One step
    serviceSerialDuringMove();

    singleStep();
    positionSteps += dirOpen ? STEPS_PER_CHUNK : -STEPS_PER_CHUNK;

    // Clamp just in case
    if (positionSteps < 0) positionSteps = 0;
    if (positionSteps > maxSteps) positionSteps = maxSteps;

    // Poll buttons each iteration (debounced in-line)
    pollButtons(true);
  }

  moving = false;

  // If the move completed normally (not stopped), disarm reverse.
  if (!stopRequested) {
    reverseArmed = false;
  }

  // Announce move done and current state
  {
    const char *state = (positionSteps >= maxSteps) ? "OPEN" : ((positionSteps <= 0) ? "CLOSED" : "PARTIAL");
    Serial.print(F("EVT MOVE_DONE state=")); Serial.print(state);
    Serial.print(F(" pos=")); Serial.println(positionSteps);
    // Provide a status snapshot
    printStatus();
  }

  // clear remembered direction
  currentDirOpen = false;
}

// Debounce & handle button actions. If allowImmediateAction, buttons trigger moves.
void pollButtons(bool allowImmediateAction)
{
  unsigned long nowMs = millis();

  // Read raw for manual button
  int btnReading = digitalRead(buttonPin);

  // Read raw for dedicated limit switches (separate from manual buttons)
  int limitOpenReading  = digitalRead(limitOpenPin);
  int limitCloseReading = digitalRead(limitClosePin);

  // Limit states (active-LOW switches)
  bool limitOpenActive = (limitOpenReading == LOW);
  bool limitCloseActive = (limitCloseReading == LOW);

  // Publish live limit state changes (works while idle and while moving)
  if (limitOpenActive != lastLimitOpenActive || limitCloseActive != lastLimitCloseActive) {
    lastLimitOpenActive = limitOpenActive;
    lastLimitCloseActive = limitCloseActive;
    Serial.print(F("EVT LIMIT_STATE open="));
    Serial.print(limitOpenActive ? 1 : 0);
    Serial.print(F(" close="));
    Serial.println(limitCloseActive ? 1 : 0);
    emitStatusJSON();
  }
  // NOTE: Limits no longer auto-stop motion. They only report state and can block commands.

  // Debounce button
  if (btnReading != lastBtnReading) {
    lastBtnChangeMs = nowMs;
    lastBtnReading = btnReading;
  } else if ((nowMs - lastBtnChangeMs) >= debounceDelay) {
    if (stableBtnState != btnReading) {
      stableBtnState = btnReading;
      if (stableBtnState == LOW) {
        // Button pressed (active LOW)
        if (!allowImmediateAction) {
          return;
        }

        if (moving) {
          // Press while moving: STOP and arm a reverse action.
          stopRequested = true;
          reverseArmed = true;
          Serial.println(F("EVT BTN_STOP"));
          emitStatusJSON();
          return;
        }

        // Idle behavior:
        // - If reverse is armed from a prior STOP, go opposite direction.
        // - Otherwise, only act if a limit is active.
        if (reverseArmed && lastMoveDirValid) {
          reverseArmed = false;
          stopRequested = false;
          moveTo(lastMoveDirOpen ? 0 : maxSteps);
          return;
        }

        if (limitCloseActive) {
          stopRequested = false;
          moveTo(maxSteps);
          return;
        }

        if (limitOpenActive) {
          stopRequested = false;
          moveTo(0);
          return;
        }

        // Neither limit active: do nothing by design.
        Serial.println(F("EVT BTN_IGNORED reason=NO_LIMIT"));
        emitStatusJSON();
      }
    }
  }
}

// Trim and uppercase a String
String cleaned(const String& s)
{
  String t = s;
  t.trim();
  t.toUpperCase();
  return t;
}

void printHelp()
{
  Serial.println(F("Commands: OPEN | CLOSE | STOP | POS? | STATUS? | STATUS_JSON? | LIMITS? | ENABLE | DISABLE | FLAT.ON | FLAT.OFF | FLAT.BRIGHT N(0-255) | CAL.START | CAL.SETOPEN | CAL.SETCLOSED | CAL.SAVE | CAL.ABORT | CAL.DEFAULTS | CAL.STATUS? | J+ N | J- N"));
}

void printStatus()
{
  bool limitOpenActive = (digitalRead(limitOpenPin) == LOW);
  bool limitCloseActive = (digitalRead(limitClosePin) == LOW);
  Serial.print(F("ENABLED=")); Serial.print(enabled ? F("YES") : F("NO"));
  Serial.print(F("  MOVING=")); Serial.print(moving  ? F("YES") : F("NO"));
  Serial.print(F("  POS="));    Serial.print(positionSteps);
  Serial.print(F("/"));          Serial.print(maxSteps);
  Serial.print(F("  LIMIT_OPEN="));  Serial.print(limitOpenActive ? F("YES") : F("NO"));
  Serial.print(F("  LIMIT_CLOSE=")); Serial.print(limitCloseActive ? F("YES") : F("NO"));
  Serial.print(F("  CAL=")); Serial.println(calibrationActive ? F("YES") : F("NO"));
}

void emitStatusJSON()
{
  // Emit JSON status so the PC app updates the GUI immediately
  bool limitOpenActive = (digitalRead(limitOpenPin) == LOW);
  bool limitCloseActive = (digitalRead(limitClosePin) == LOW);
  
  // State derived from physical limits (ground truth), not software position
  const char *state;
  if (moving) {
    state = "MOVING";
  } else if (limitOpenActive && limitCloseActive) {
    state = "PARTIAL";
  } else if (limitOpenActive) {
    state = "OPEN";
  } else if (limitCloseActive) {
    state = "CLOSED";
  } else {
    state = "UNKNOWN";
  }
  
  Serial.print(F("{\"en\":"));
  Serial.print(enabled ? 1 : 0);
  Serial.print(F(",\"mov\":"));
  Serial.print(moving ? 1 : 0);
  Serial.print(F(",\"pos\":"));
  Serial.print(positionSteps);
  Serial.print(F(",\"max\":"));
  Serial.print(maxSteps);
  Serial.print(F(",\"cal\":"));
  Serial.print(calibrationActive ? 1 : 0);
  Serial.print(F(",\"lim_open\":"));
  Serial.print(limitOpenActive ? 1 : 0);
  Serial.print(F(",\"lim_close\":"));
  Serial.print(limitCloseActive ? 1 : 0);
  Serial.print(F(",\"flat_on\":"));
  Serial.print(flatOn ? 1 : 0);
  Serial.print(F(",\"flat_pwm\":"));
  Serial.print(flatPwm);
  Serial.print(F(",\"state\":\""));
  Serial.print(state);
  Serial.println(F("\"}"));
}

void saveCalibratedMaxSteps()
{
  EEPROM.put(EEPROM_MAGIC_ADDR, EEPROM_MAGIC);
  EEPROM.put(EEPROM_MAX_ADDR, maxSteps);
}

void loadCalibratedMaxSteps()
{
  unsigned long magic = 0;
  long savedMax = DEFAULT_MAX_STEPS;
  EEPROM.get(EEPROM_MAGIC_ADDR, magic);
  if (magic == EEPROM_MAGIC) {
    EEPROM.get(EEPROM_MAX_ADDR, savedMax);
    if (savedMax >= 100 && savedMax <= 200000) {
      maxSteps = savedMax;
      return;
    }
  }
  maxSteps = DEFAULT_MAX_STEPS;
}

/////////////////////// Arduino Core //////////////////////////////////////////
void setup()
{
  // Pins
  pinMode(EN_PIN,   OUTPUT);
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN,  OUTPUT);

  pinMode(FLAT_PWM_PIN, OUTPUT);
  pinMode(buttonPin,    INPUT_PULLUP);
  pinMode(limitOpenPin,   INPUT_PULLUP);
  pinMode(limitClosePin,  INPUT_PULLUP);

  // Safe default: disabled on boot
  setEnable(false);

  // Safe default: flat panel off on boot
  flatOn = false;
  flatPwm = 0;
  applyFlatOutput();

  // Load persisted calibrated travel
  loadCalibratedMaxSteps();

  // Serial
  Serial.begin(9600);
  while (!Serial) { /* wait for native USB boards; Uno will skip */ }

  Serial.println(F("\nLid Controller Ready."));
  
  // Initialize position based on physical limit state (not hardcoded to 0)
  lastLimitOpenActive = (digitalRead(limitOpenPin) == LOW);
  lastLimitCloseActive = (digitalRead(limitClosePin) == LOW);
  
  if (lastLimitOpenActive) {
    positionSteps = maxSteps;
    Serial.println(F("Detected OPEN limit active on startup - position set to OPEN."));
  } else if (lastLimitCloseActive) {
    positionSteps = 0;
    Serial.println(F("Detected CLOSE limit active on startup - position set to CLOSED."));
  } else {
    positionSteps = 0;
    Serial.println(F("No limit active on startup - position assumed CLOSED (0)."));
  }
  
  printHelp();
  emitStatusJSON();
}

void loop()
{
  // Handle serial commands
  if (Serial.available())
  {
    String cmd = cleaned(Serial.readStringUntil('\n'));

    if      (cmd == F("OPEN"))    {
      // Check if open limit is active; if so, block the command
      if ((digitalRead(limitOpenPin) == LOW) && (positionSteps >= (maxSteps - LIMIT_POS_TOL))) {
        Serial.println(F("EVT OPEN_BLOCKED reason=LIMIT_OPEN"));
        emitStatusJSON();
      } else {
        stopRequested = false;
        moveTo(maxSteps);
        Serial.println(F("OPEN done."));
      }
    }
    else if (cmd == F("CLOSE"))   {
      // Check if close limit is active; if so, block the command
      if ((digitalRead(limitClosePin) == LOW) && (positionSteps <= LIMIT_POS_TOL)) {
        Serial.println(F("EVT CLOSE_BLOCKED reason=LIMIT_CLOSE"));
        emitStatusJSON();
      } else {
        stopRequested = false;
        moveTo(0);
        Serial.println(F("CLOSE done."));
      }
    }
    else if (cmd == F("STOP"))    { stopRequested = true;  Serial.println(F("STOP requested.")); }
    else if (cmd == F("POS?"))    { Serial.print(F("POS=")); Serial.println(positionSteps); }
    else if (cmd == F("STATUS?")) { printStatus(); }
    else if (cmd == F("STATUS_JSON?")) { emitStatusJSON(); }
    else if (cmd == F("LIMITS?")) { emitStatusJSON(); }
    else if (cmd == F("ENABLE"))  { setEnable(true);  Serial.println(F("Enabled (EN=LOW).")); }
    else if (cmd == F("DISABLE")) { setEnable(false); Serial.println(F("Disabled (EN=HIGH).")); }
    else if (cmd == F("FLAT.ON")) {
      setFlatOn(true);
    }
    else if (cmd == F("FLAT.OFF")) {
      setFlatOn(false);
    }
    else if (cmd.startsWith(F("FLAT.BRIGHT"))) {
      // Accept: FLAT.BRIGHT 0..255
      int spaceIdx = cmd.indexOf(' ');
      int pwm = 0;
      if (spaceIdx > 0 && spaceIdx < (int)cmd.length() - 1) {
        pwm = cmd.substring(spaceIdx + 1).toInt();
      }
      setFlatBrightness(pwm);
    }
    else if (cmd == F("CAL.START")) {
      calibrationActive = true;
      teachOpenPos = -1;
      teachClosedPos = -1;
      Serial.println(F("EVT CAL_STARTED"));
      emitStatusJSON();
    }
    else if (cmd == F("CAL.SETOPEN")) {
      if (!calibrationActive) {
        Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
      } else {
        teachOpenPos = positionSteps;
        Serial.print(F("EVT CAL_OPEN_SET pos=")); Serial.println(teachOpenPos);
      }
      emitStatusJSON();
    }
    else if (cmd == F("CAL.SETCLOSED")) {
      if (!calibrationActive) {
        Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
      } else {
        teachClosedPos = positionSteps;
        Serial.print(F("EVT CAL_CLOSED_SET pos=")); Serial.println(teachClosedPos);
      }
      emitStatusJSON();
    }
    else if (cmd == F("CAL.SAVE")) {
      if (!calibrationActive) {
        Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
      } else if (teachOpenPos < 0 || teachClosedPos < 0) {
        Serial.println(F("EVT CAL_ERROR reason=MISSING_SETPOINT"));
      } else {
        long newMax = labs(teachOpenPos - teachClosedPos);
        if (newMax < 100 || newMax > 200000) {
          Serial.println(F("EVT CAL_ERROR reason=RANGE"));
        } else {
          maxSteps = newMax;
          positionSteps = constrain(positionSteps - teachClosedPos, 0L, maxSteps);
          saveCalibratedMaxSteps();
          Serial.print(F("EVT CAL_SAVED max=")); Serial.println(maxSteps);
        }
      }
      emitStatusJSON();
    }
    else if (cmd == F("CAL.ABORT")) {
      calibrationActive = false;
      teachOpenPos = -1;
      teachClosedPos = -1;
      Serial.println(F("EVT CAL_ABORTED"));
      emitStatusJSON();
    }
    else if (cmd == F("CAL.DEFAULTS")) {
      maxSteps = DEFAULT_MAX_STEPS;
      positionSteps = constrain(positionSteps, 0L, maxSteps);
      saveCalibratedMaxSteps();
      Serial.print(F("EVT CAL_DEFAULTS max=")); Serial.println(maxSteps);
      emitStatusJSON();
    }
    else if (cmd == F("CAL.STATUS?")) {
      emitStatusJSON();
    }
    else if (cmd.startsWith(F("J+"))) {
      if (!calibrationActive) {
        Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
      } else {
        long steps = cmd.substring(2).toInt();
        if (steps <= 0) steps = 1;
        moveTo(constrain(positionSteps + steps, 0L, maxSteps));
      }
    }
    else if (cmd.startsWith(F("J-"))) {
      if (!calibrationActive) {
        Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
      } else {
        long steps = cmd.substring(2).toInt();
        if (steps <= 0) steps = 1;
        moveTo(constrain(positionSteps - steps, 0L, maxSteps));
      }
    }
    else if (cmd.length() > 0)    { Serial.println(F("Unknown cmd.")); printHelp(); }
  }

  // Poll buttons when idle
  if (!moving) {
    pollButtons(true);
  }
}

// Service serial commands during move (to receive STOP, etc.)
void serviceSerialDuringMove()
{
  static char cmdBuf[64];
  static uint8_t cmdLen = 0;

  while (Serial.available() > 0)
  {
    char ch = (char)Serial.read();

    if (ch == '\r') {
      continue;
    }

    if (ch == '\n')
    {
      if (cmdLen == 0) {
        continue;
      }

      cmdBuf[cmdLen] = '\0';
      cmdLen = 0;

      String cmd = cleaned(String(cmdBuf));

      if (cmd == F("STOP"))
      {
        stopRequested = true;
        Serial.println(F("STOP requested."));
      }
      else if (cmd == F("FLAT.ON"))
      {
        setFlatOn(true);
      }
      else if (cmd == F("FLAT.OFF"))
      {
        setFlatOn(false);
      }
      else if (cmd.startsWith(F("FLAT.BRIGHT")))
      {
        int spaceIdx = cmd.indexOf(' ');
        int pwm = 0;
        if (spaceIdx > 0 && spaceIdx < (int)cmd.length() - 1) {
          pwm = cmd.substring(spaceIdx + 1).toInt();
        }
        setFlatBrightness(pwm);
      }
      else if (cmd == F("ENABLE"))
      {
        setEnable(true);
        Serial.println(F("Enabled (EN=LOW)."));
      }
      else if (cmd == F("DISABLE"))
      {
        setEnable(false);
        Serial.println(F("Disabled (EN=HIGH)."));
      }
      else if (cmd == F("STATUS_JSON?") || cmd == F("LIMITS?"))
      {
        emitStatusJSON();
      }
      else if (cmd == F("STATUS?"))
      {
        printStatus();
      }
      else if (cmd == F("POS?"))
      {
        Serial.print(F("POS="));
        Serial.println(positionSteps);
      }

      continue;
    }

    if (cmdLen < (sizeof(cmdBuf) - 1)) {
      cmdBuf[cmdLen++] = ch;
    } else {
      cmdLen = 0;
    }
  }
}
