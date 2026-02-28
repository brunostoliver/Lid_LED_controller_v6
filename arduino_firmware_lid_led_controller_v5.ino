/*
  Lid Controller – Step/Dir + Buttons + Magnetic Limits (TMC2209, Arduino Uno R3)

  Pins:
    EN_PIN        = 9   (TMC EN, active-LOW)
    STEP_PIN      = 6
    DIR_PIN       = 3
    buttonOpenPin = 7   (manual open button, active-LOW)
    buttonClosePin= 8   (manual close button, active-LOW)
    limitOpenPin  = 4   (OPEN limit, active-LOW; magnetic reed/hall)
    limitClosePin = 5   (CLOSE limit, active-LOW; magnetic reed/hall)

  Serial: 9600 baud, newline-terminated commands.

  Commands (case-insensitive):
    OPEN, CLOSE, STOP
    POS?, STATUS?, STATUS_JSON?, LIMITS?
    ENABLE, DISABLE
    CAL.START, CAL.SETOPEN, CAL.SETCLOSED, CAL.SAVE, CAL.ABORT, CAL.DEFAULTS, CAL.STATUS?
    J+ N, J- N

  Behavior:
    - While moving: JSON reports mov=1 and state="MOVING"
    - Periodic JSON while moving for robust PC-side motion detection.
    - STOP is serviced during motion.
    - OPEN/CLOSE commands are blocked if corresponding limit is already active.
    - Optional safety: stop motion if a limit becomes active during motion.

  Notes:
    - Magnetic limits often do not need debouncing, but EMI/noise is still possible.
      If you ever see flicker, add debounce later.
*/

#include <EEPROM.h>

/////////////////////// Pins & constants ///////////////////////
const int EN_PIN   = 9;
const int STEP_PIN = 6;
const int DIR_PIN  = 3;

const int buttonOpenPin  = 7;   // active-LOW (INPUT_PULLUP)
const int buttonClosePin = 8;   // active-LOW (INPUT_PULLUP)

const int limitOpenPin   = 4;   // active-LOW (INPUT_PULLUP)
const int limitClosePin  = 5;   // active-LOW (INPUT_PULLUP)

const long DEFAULT_MAX_STEPS = 10500;
const int  pulseDelayUs = 500;                 // microseconds between step edges (HIGH then LOW)
const unsigned long debounceDelayMs = 50UL;    // manual buttons only
const long STEPS_PER_CHUNK = 1;

const unsigned long STATUS_PERIOD_MS = 0UL;    // 0 disables unsolicited JSON while moving
const unsigned long ENABLE_SETTLE_US  = 1000UL;

// Direction invert if lid moves the wrong way
const bool INVERT_DIR = false;

// ===== Optional: stop motion if a limit trips during travel =====
// Set to 0 if you truly never want limits to stop motion.
#define STOP_ON_LIMIT_DURING_MOVE  1

/////////////////////// EEPROM storage ///////////////////////
const int EEPROM_MAGIC_ADDR = 0;
const int EEPROM_MAX_ADDR   = EEPROM_MAGIC_ADDR + sizeof(unsigned long);
const unsigned long EEPROM_MAGIC = 0x4C494443UL; // "LIDC"

/////////////////////// State ///////////////////////
volatile bool stopRequested = false;
bool enabled = false;
bool moving  = false;
bool calibrationActive = false;

long positionSteps = 0;               // 0 = fully closed; maxSteps = fully open
long maxSteps = DEFAULT_MAX_STEPS;

long teachOpenPos = -1;
long teachClosedPos = -1;

bool currentDirOpen = false;          // true while opening; false while closing

// Manual button debounce
int lastOpenReading  = HIGH;
int lastCloseReading = HIGH;
int stableOpenState  = HIGH;
int stableCloseState = HIGH;
unsigned long lastOpenChangeMs  = 0;
unsigned long lastCloseChangeMs = 0;

// Track last published limit states (raw)
bool lastLimitOpenActive  = false;
bool lastLimitCloseActive = false;

/////////////////////// Forward declarations ///////////////////////
String cleaned(const String& s);

void setEnable(bool en);
void setDir(bool dirOpen);
void singleStep();
void pollButtons(bool allowImmediateAction = true);

void emitStatusJSON();
void printStatus();
void printHelp();

void saveCalibratedMaxSteps();
void loadCalibratedMaxSteps();

void handleCommand(const String& cmd, bool fromMoveLoop);
void serviceSerialDuringMove();

void moveTo(long targetSteps);

/////////////////////// Helpers ///////////////////////
String cleaned(const String& s)
{
  String t = s;
  t.trim();
  t.toUpperCase();
  return t;
}

void setEnable(bool en)
{
  enabled = en;
  // TMC EN is active-LOW: LOW = enabled, HIGH = disabled
  digitalWrite(EN_PIN, en ? LOW : HIGH);
  if (en) delayMicroseconds(ENABLE_SETTLE_US);
}

void setDir(bool dirOpen)
{
  bool level = INVERT_DIR ? !dirOpen : dirOpen;
  digitalWrite(DIR_PIN, level ? HIGH : LOW);
}

void singleStep()
{
  digitalWrite(STEP_PIN, HIGH);
  delayMicroseconds(pulseDelayUs);
  digitalWrite(STEP_PIN, LOW);
  delayMicroseconds(pulseDelayUs);
}

static inline bool limitOpenActive()
{
  return (digitalRead(limitOpenPin) == LOW);
}
static inline bool limitCloseActive()
{
  return (digitalRead(limitClosePin) == LOW);
}

/////////////////////// Status ///////////////////////
void printHelp()
{
  Serial.println(F("Commands: OPEN | CLOSE | STOP | POS? | STATUS? | STATUS_JSON? | LIMITS? | ENABLE | DISABLE | CAL.START | CAL.SETOPEN | CAL.SETCLOSED | CAL.SAVE | CAL.ABORT | CAL.DEFAULTS | CAL.STATUS? | J+ N | J- N"));
}

void printStatus()
{
  Serial.print(F("ENABLED=")); Serial.print(enabled ? F("YES") : F("NO"));
  Serial.print(F("  MOVING=")); Serial.print(moving  ? F("YES") : F("NO"));
  Serial.print(F("  POS="));    Serial.print(positionSteps);
  Serial.print(F("/"));         Serial.print(maxSteps);
  Serial.print(F("  LIMIT_OPEN="));  Serial.print(limitOpenActive() ? F("YES") : F("NO"));
  Serial.print(F("  LIMIT_CLOSE=")); Serial.print(limitCloseActive() ? F("YES") : F("NO"));
  Serial.print(F("  CAL=")); Serial.println(calibrationActive ? F("YES") : F("NO"));
}

void emitStatusJSON()
{
  bool lo = limitOpenActive();
  bool lc = limitCloseActive();

  // Movement-aware state to avoid contradictory snapshots.
  const char* state;
  if (moving)
  {
    state = "MOVING";
  }
  else if (lo && lc)
  {
    state = "PARTIAL";
  }
  else if (lo)
  {
    state = "OPEN";
  }
  else if (lc)
  {
    state = "CLOSED";
  }
  else if (positionSteps > 0 && positionSteps < maxSteps)
  {
    state = "PARTIAL";
  }
  else
  {
    state = "UNKNOWN";
  }

  Serial.print(F("{\"en\":"));        Serial.print(enabled ? 1 : 0);
  Serial.print(F(",\"mov\":"));       Serial.print(moving ? 1 : 0);
  Serial.print(F(",\"pos\":"));       Serial.print(positionSteps);
  Serial.print(F(",\"max\":"));       Serial.print(maxSteps);
  Serial.print(F(",\"cal\":"));       Serial.print(calibrationActive ? 1 : 0);
  Serial.print(F(",\"lim_open\":"));  Serial.print(lo ? 1 : 0);
  Serial.print(F(",\"lim_close\":")); Serial.print(lc ? 1 : 0);
  Serial.print(F(",\"state\":\""));   Serial.print(state);
  Serial.println(F("\"}"));
}

/////////////////////// EEPROM ///////////////////////
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
  if (magic == EEPROM_MAGIC)
  {
    EEPROM.get(EEPROM_MAX_ADDR, savedMax);
    if (savedMax >= 100 && savedMax <= 200000)
    {
      maxSteps = savedMax;
      return;
    }
  }
  maxSteps = DEFAULT_MAX_STEPS;
}

/////////////////////// Command processing ///////////////////////
void handleCommand(const String& cmd, bool fromMoveLoop)
{
  if (cmd.length() == 0) return;

  // Always-allowed, even while moving
  if (cmd == F("STOP"))
  {
    stopRequested = true;
    Serial.println(F("STOP requested."));
    return;
  }
  if (cmd == F("POS?"))
  {
    Serial.print(F("POS="));
    Serial.println(positionSteps);
    return;
  }
  if (cmd == F("STATUS?"))
  {
    printStatus();
    return;
  }
  if (cmd == F("STATUS_JSON?") || cmd == F("LIMITS?"))
  {
    emitStatusJSON();
    return;
  }

  // While moving: ignore mutating commands (except STOP handled above)
  if (fromMoveLoop && moving)
  {
    Serial.println(F("BUSY moving."));
    return;
  }

  if (cmd == F("OPEN"))
  {
    if (limitOpenActive())
    {
      Serial.println(F("EVT OPEN_BLOCKED reason=LIMIT_OPEN"));
      emitStatusJSON();
    }
    else
    {
      stopRequested = false;
      moveTo(maxSteps);
      Serial.println(F("OPEN done."));
    }
    return;
  }

  if (cmd == F("CLOSE"))
  {
    if (limitCloseActive())
    {
      Serial.println(F("EVT CLOSE_BLOCKED reason=LIMIT_CLOSE"));
      emitStatusJSON();
    }
    else
    {
      stopRequested = false;
      moveTo(0);
      Serial.println(F("CLOSE done."));
    }
    return;
  }

  if (cmd == F("ENABLE"))
  {
    setEnable(true);
    Serial.println(F("Enabled (EN=LOW)."));
    return;
  }

  if (cmd == F("DISABLE"))
  {
    setEnable(false);
    Serial.println(F("Disabled (EN=HIGH)."));
    return;
  }

  // Calibration commands
  if (cmd == F("CAL.START"))
  {
    calibrationActive = true;
    teachOpenPos = -1;
    teachClosedPos = -1;
    Serial.println(F("EVT CAL_STARTED"));
    emitStatusJSON();
    return;
  }

  if (cmd == F("CAL.SETOPEN"))
  {
    if (!calibrationActive) Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
    else
    {
      teachOpenPos = positionSteps;
      Serial.print(F("EVT CAL_OPEN_SET pos="));
      Serial.println(teachOpenPos);
    }
    emitStatusJSON();
    return;
  }

  if (cmd == F("CAL.SETCLOSED"))
  {
    if (!calibrationActive) Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
    else
    {
      teachClosedPos = positionSteps;
      Serial.print(F("EVT CAL_CLOSED_SET pos="));
      Serial.println(teachClosedPos);
    }
    emitStatusJSON();
    return;
  }

  if (cmd == F("CAL.SAVE"))
  {
    if (!calibrationActive) Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
    else if (teachOpenPos < 0 || teachClosedPos < 0) Serial.println(F("EVT CAL_ERROR reason=MISSING_SETPOINT"));
    else
    {
      long newMax = labs(teachOpenPos - teachClosedPos);
      if (newMax < 100 || newMax > 200000) Serial.println(F("EVT CAL_ERROR reason=RANGE"));
      else
      {
        maxSteps = newMax;
        positionSteps = constrain(positionSteps - teachClosedPos, 0L, maxSteps);
        saveCalibratedMaxSteps();
        Serial.print(F("EVT CAL_SAVED max="));
        Serial.println(maxSteps);
      }
    }
    emitStatusJSON();
    return;
  }

  if (cmd == F("CAL.ABORT"))
  {
    calibrationActive = false;
    teachOpenPos = -1;
    teachClosedPos = -1;
    Serial.println(F("EVT CAL_ABORTED"));
    emitStatusJSON();
    return;
  }

  if (cmd == F("CAL.DEFAULTS"))
  {
    maxSteps = DEFAULT_MAX_STEPS;
    positionSteps = constrain(positionSteps, 0L, maxSteps);
    saveCalibratedMaxSteps();
    Serial.print(F("EVT CAL_DEFAULTS max="));
    Serial.println(maxSteps);
    emitStatusJSON();
    return;
  }

  if (cmd == F("CAL.STATUS?"))
  {
    emitStatusJSON();
    return;
  }

  if (cmd.startsWith(F("J+")))
  {
    if (!calibrationActive) Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
    else
    {
      long steps = cmd.substring(2).toInt();
      if (steps <= 0) steps = 1;
      moveTo(constrain(positionSteps + steps, 0L, maxSteps));
    }
    return;
  }

  if (cmd.startsWith(F("J-")))
  {
    if (!calibrationActive) Serial.println(F("EVT CAL_ERROR reason=NOT_ACTIVE"));
    else
    {
      long steps = cmd.substring(2).toInt();
      if (steps <= 0) steps = 1;
      moveTo(constrain(positionSteps - steps, 0L, maxSteps));
    }
    return;
  }

  Serial.println(F("Unknown cmd."));
  printHelp();
}

void serviceSerialDuringMove()
{
  static char cmdBuf[64];
  static uint8_t cmdLen = 0;

  while (Serial.available() > 0)
  {
    char ch = (char)Serial.read();

    if (ch == '\r')
    {
      continue;
    }

    if (ch == '\n')
    {
      if (cmdLen > 0)
      {
        cmdBuf[cmdLen] = '\0';
        String cmd = cleaned(String(cmdBuf));
        handleCommand(cmd, true);
        cmdLen = 0;
      }
      continue;
    }

    if (cmdLen < (sizeof(cmdBuf) - 1))
    {
      cmdBuf[cmdLen++] = ch;
    }
    else
    {
      // Overflow guard: drop malformed/overlong command frame.
      cmdLen = 0;
    }
  }
}

/////////////////////// Buttons + limit reporting ///////////////////////
void pollButtons(bool allowImmediateAction)
{
  unsigned long nowMs = millis();

  // Manual buttons (debounced)
  int openReading  = digitalRead(buttonOpenPin);
  int closeReading = digitalRead(buttonClosePin);

  // Raw magnetic limits
  bool lo = limitOpenActive();
  bool lc = limitCloseActive();

  // Publish limit state changes (raw)
  if (lo != lastLimitOpenActive || lc != lastLimitCloseActive)
  {
    lastLimitOpenActive  = lo;
    lastLimitCloseActive = lc;

    // Avoid unsolicited serial bursts while moving (can introduce step jitter at 9600 baud).
    if (!moving)
    {
      Serial.print(F("EVT LIMIT_STATE open="));
      Serial.print(lo ? 1 : 0);
      Serial.print(F(" close="));
      Serial.println(lc ? 1 : 0);

      emitStatusJSON();
    }
  }

  // Debounce OPEN button
  if (openReading != lastOpenReading)
  {
    lastOpenChangeMs = nowMs;
    lastOpenReading = openReading;
  }
  else if ((nowMs - lastOpenChangeMs) >= debounceDelayMs)
  {
    if (stableOpenState != openReading)
    {
      stableOpenState = openReading;
      if (stableOpenState == LOW && allowImmediateAction && !moving)
      {
        if (lo)
        {
          Serial.println(F("EVT OPEN_BLOCKED reason=LIMIT_OPEN"));
          emitStatusJSON();
        }
        else
        {
          stopRequested = false;
          moveTo(maxSteps);
        }
      }
    }
  }

  // Debounce CLOSE button
  if (closeReading != lastCloseReading)
  {
    lastCloseChangeMs = nowMs;
    lastCloseReading = closeReading;
  }
  else if ((nowMs - lastCloseChangeMs) >= debounceDelayMs)
  {
    if (stableCloseState != closeReading)
    {
      stableCloseState = closeReading;
      if (stableCloseState == LOW && allowImmediateAction && !moving)
      {
        if (lc)
        {
          Serial.println(F("EVT CLOSE_BLOCKED reason=LIMIT_CLOSE"));
          emitStatusJSON();
        }
        else
        {
          stopRequested = false;
          moveTo(0);
        }
      }
    }
  }
}

/////////////////////// Motion ///////////////////////
void moveTo(long targetSteps)
{
  targetSteps = constrain(targetSteps, 0L, maxSteps);
  if (positionSteps == targetSteps) return;

  if (!enabled) setEnable(true);

  stopRequested = false;
  moving = true;

  const bool dirOpen = (targetSteps > positionSteps);
  currentDirOpen = dirOpen;
  setDir(dirOpen);

  Serial.print(F("EVT MOVE_STARTED dir="));
  Serial.println(dirOpen ? F("OPEN") : F("CLOSE"));
  emitStatusJSON();

  unsigned long lastStatusMs = millis();

  while (!stopRequested && positionSteps != targetSteps)
  {
    // Allow STOP and status queries during movement
    serviceSerialDuringMove();

    // One step
    singleStep();
    positionSteps += dirOpen ? STEPS_PER_CHUNK : -STEPS_PER_CHUNK;

    if (positionSteps < 0) positionSteps = 0;
    if (positionSteps > maxSteps) positionSteps = maxSteps;

    // Poll buttons & publish limit changes
    pollButtons(true);

#if STOP_ON_LIMIT_DURING_MOVE
    // Safety stop: if a limit trips in the direction of travel, stop early.
    if (dirOpen && limitOpenActive())
    {
      positionSteps = maxSteps;
      break;
    }
    if (!dirOpen && limitCloseActive())
    {
      positionSteps = 0;
      break;
    }
#endif

    // Optional periodic JSON heartbeat while moving (disabled when STATUS_PERIOD_MS == 0)
    if (STATUS_PERIOD_MS > 0UL)
    {
      unsigned long nowMs = millis();
      if (nowMs - lastStatusMs >= STATUS_PERIOD_MS)
      {
        emitStatusJSON();
        lastStatusMs = nowMs;
      }
    }
  }

  moving = false;
  currentDirOpen = false;

  const char* state = (positionSteps >= maxSteps) ? "OPEN" :
                      ((positionSteps <= 0) ? "CLOSED" : "PARTIAL");

  Serial.print(F("EVT MOVE_DONE state="));
  Serial.print(state);
  Serial.print(F(" pos="));
  Serial.println(positionSteps);

  printStatus();
  emitStatusJSON();
}

/////////////////////// Arduino core ///////////////////////
void setup()
{
  pinMode(EN_PIN, OUTPUT);
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN, OUTPUT);

  pinMode(buttonOpenPin, INPUT_PULLUP);
  pinMode(buttonClosePin, INPUT_PULLUP);
  pinMode(limitOpenPin, INPUT_PULLUP);
  pinMode(limitClosePin, INPUT_PULLUP);

  setEnable(false);
  loadCalibratedMaxSteps();

  Serial.begin(9600);

  Serial.println(F("\nLid Controller Ready."));
  Serial.println(F("READY"));

  // Initialize position based on limit state
  lastLimitOpenActive  = limitOpenActive();
  lastLimitCloseActive = limitCloseActive();

  if (lastLimitOpenActive)
  {
    positionSteps = maxSteps;
    Serial.println(F("Detected OPEN limit active on startup - position set to OPEN."));
  }
  else if (lastLimitCloseActive)
  {
    positionSteps = 0;
    Serial.println(F("Detected CLOSE limit active on startup - position set to CLOSED."));
  }
  else
  {
    positionSteps = 0;
    Serial.println(F("No limit active on startup - position assumed CLOSED (0)."));
  }

  printHelp();
  emitStatusJSON();
}

void loop()
{
  if (Serial.available())
  {
    String cmd = cleaned(Serial.readStringUntil('\n'));
    handleCommand(cmd, false);
  }

  if (!moving)
  {
    pollButtons(true);
  }
}