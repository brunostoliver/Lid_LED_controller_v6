/*
  Lid Controller – Step/Dir + Buttons (TMC2209, Arduino Uno R3)

  Pins & constants per user spec:
    EN_PIN  = 9
    STEP_PIN= 6
    DIR_PIN = 3
    buttonOpenPin  = 7
    buttonClosePin = 8

  Serial: 9600 baud
  Timing: pulseDelay = 500   (microseconds between step edges)
  Travel: MAX_STEPS  = 10500 (total travel from fully closed (0) to fully open)
  Debounce: debounceDelay = 50 ms

  Serial commands (case-insensitive):
    OPEN         -> move to MAX_STEPS
    CLOSE        -> move to 0
    STOP         -> stop motion ASAP
    POS?         -> report current step position (0..MAX_STEPS)
    STATUS?      -> report enabled state, moving state, position
    ENABLE       -> EN low
    DISABLE      -> EN high

  Notes:
    - Direction "OPEN" is a logical direction; if your lid runs the wrong way,
      set INVERT_DIR to true OR swap one motor coil pair.
    - Limit switches / buttons are wired active-LOW to pins 7 (OPEN) and 8 (CLOSE)
      and use the internal `INPUT_PULLUP` resistor. Wire each switch between
      the pin and GND.
*/

/////////////////////// User-Specified Pins & Constants ///////////////////////
const int EN_PIN   = 9;
const int STEP_PIN = 6;
const int DIR_PIN  = 3;

const int buttonOpenPin  = 7;   // manual open button (active-LOW)
const int buttonClosePin = 8;   // manual close button (active-LOW)

// Dedicated limit switch pins (separate from manual buttons)
const int limitOpenPin  = 4;    // limit switch for fully OPEN (active-LOW)
const int limitClosePin = 5;    // limit switch for fully CLOSED (active-LOW)

const long MAX_STEPS = 10500;
const int  pulseDelay = 500;                  // microseconds
const unsigned long debounceDelay = 50UL;     // milliseconds
const long STEPS_PER_CHUNK = 1;               // step granularity

/////////////////////// Behavior Tweaks ///////////////////////////////////////
// If your lid moves the opposite way, flip this to true.
const bool INVERT_DIR = false;

// Safety: minimum enable delay before stepping (us)
const unsigned long ENABLE_SETTLE_US = 1000;

/////////////////////// State Variables ///////////////////////////////////////
volatile bool stopRequested = false;
bool enabled = false;
bool moving  = false;

long positionSteps = 0; // 0 = fully closed; MAX_STEPS = fully open

// Button debouncing
int lastOpenReading  = HIGH;  // using INPUT_PULLUP -> HIGH = not pressed
int lastCloseReading = HIGH;
int stableOpenState  = HIGH;
int stableCloseState = HIGH;
unsigned long lastOpenChangeMs  = 0;
unsigned long lastCloseChangeMs = 0;

// Track current move direction so pollButtons can detect limit switches
bool currentDirOpen = false;

/////////////////////// Forward Declarations (fix compile order) //////////////
void pollButtons(bool allowImmediateAction = true);

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

// Move toward a target position (blocking but responsive to STOP & buttons)
void moveTo(long targetSteps)
{
  targetSteps = constrain(targetSteps, 0L, MAX_STEPS);
  if (positionSteps == targetSteps) return;

  if (!enabled) setEnable(true);
  stopRequested = false;
  moving = true;

  const bool dirOpen = (targetSteps > positionSteps);
  setDir(dirOpen);

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
    singleStep();
    positionSteps += dirOpen ? STEPS_PER_CHUNK : -STEPS_PER_CHUNK;

    // Clamp just in case
    if (positionSteps < 0) positionSteps = 0;
    if (positionSteps > MAX_STEPS) positionSteps = MAX_STEPS;

    // Poll buttons each iteration (debounced in-line)
    pollButtons(true);
  }

  moving = false;

  // Announce move done and current state
  {
    const char *state = (positionSteps >= MAX_STEPS) ? "OPEN" : ((positionSteps <= 0) ? "CLOSED" : "PARTIAL");
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

  // Read raw for manual buttons
  int openReading  = digitalRead(buttonOpenPin);
  int closeReading = digitalRead(buttonClosePin);

  // Read raw for dedicated limit switches (separate from manual buttons)
  int limitOpenReading  = digitalRead(limitOpenPin);
  int limitCloseReading = digitalRead(limitClosePin);

  // If a physical limit switch trips while moving, stop immediately.
  if (limitOpenReading == LOW && moving) {
    stopRequested = true;
    positionSteps = MAX_STEPS;
    Serial.print(F("EVT LIMIT_OPEN pos=")); Serial.println(positionSteps);
    emitStatusJSON(); // emit JSON so GUI updates immediately
    setEnable(false);
    return; // skip button processing this cycle
  }
  if (limitCloseReading == LOW && moving) {
    stopRequested = true;
    positionSteps = 0;
    Serial.print(F("EVT LIMIT_CLOSED pos=")); Serial.println(positionSteps);
    emitStatusJSON(); // emit JSON so GUI updates immediately
    setEnable(false);
    return; // skip button processing this cycle
  }

  // Debounce OPEN
  if (openReading != lastOpenReading) {
    lastOpenChangeMs = nowMs;
    lastOpenReading = openReading;
  } else if ((nowMs - lastOpenChangeMs) >= debounceDelay) {
    if (stableOpenState != openReading) {
      stableOpenState = openReading;
      if (stableOpenState == LOW) {
        // Limit switch pressed (active LOW).
        if (moving) {
          // Stop motion immediately, set software position to fully OPEN,
          // emit limit event and a status snapshot, and disable the driver for safety.
          stopRequested = true;
          positionSteps = MAX_STEPS;
          Serial.print(F("EVT LIMIT_OPEN pos=")); Serial.println(positionSteps);
          printStatus();
          setEnable(false); // hardware disable for extra safety
        } else if (allowImmediateAction) {
          // Open button pressed (active LOW)
          stopRequested = false;
          moveTo(MAX_STEPS);
        }
      }
    }
  }

  // Debounce CLOSE
  if (closeReading != lastCloseReading) {
    lastCloseChangeMs = nowMs;
    lastCloseReading = closeReading;
  } else if ((nowMs - lastCloseChangeMs) >= debounceDelay) {
    if (stableCloseState != closeReading) {
      stableCloseState = closeReading;
      if (stableCloseState == LOW) {
        // Limit switch pressed (active LOW).
        if (moving) {
          // Stop motion immediately, set software position to fully CLOSED,
          // emit limit event and a status snapshot, and disable the driver for safety.
          stopRequested = true;
          positionSteps = 0;
          Serial.print(F("EVT LIMIT_CLOSED pos=")); Serial.println(positionSteps);
          printStatus();
          setEnable(false); // hardware disable for extra safety
        } else if (allowImmediateAction) {
          // Close button pressed (active LOW)
          stopRequested = false;
          moveTo(0);
        }
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
  Serial.println(F("Commands: OPEN | CLOSE | STOP | POS? | STATUS? | ENABLE | DISABLE"));
}

void printStatus()
{
  Serial.print(F("ENABLED=")); Serial.print(enabled ? F("YES") : F("NO"));
  Serial.print(F("  MOVING=")); Serial.print(moving  ? F("YES") : F("NO"));
  Serial.print(F("  POS="));    Serial.print(positionSteps);
  Serial.print(F("/"));          Serial.println(MAX_STEPS);
}

void emitStatusJSON()
{
  // Emit JSON status so the PC app updates the GUI immediately
  const char *state = (positionSteps >= MAX_STEPS) ? "OPEN" : ((positionSteps <= 0) ? "CLOSED" : "PARTIAL");
  Serial.print(F("{\"en\":"));
  Serial.print(enabled ? 1 : 0);
  Serial.print(F(",\"mov\":"));
  Serial.print(moving ? 1 : 0);
  Serial.print(F(",\"pos\":"));
  Serial.print(positionSteps);
  Serial.print(F(",\"max\":"));
  Serial.print(MAX_STEPS);
  Serial.print(F(",\"state\":\""));
  Serial.print(state);
  Serial.println(F("\""));
}

/////////////////////// Arduino Core //////////////////////////////////////////
void setup()
{
  // Pins
  pinMode(EN_PIN,   OUTPUT);
  pinMode(STEP_PIN, OUTPUT);
  pinMode(DIR_PIN,  OUTPUT);

  pinMode(buttonOpenPin,  INPUT_PULLUP);
  pinMode(buttonClosePin, INPUT_PULLUP);
  pinMode(limitOpenPin,   INPUT_PULLUP);
  pinMode(limitClosePin,  INPUT_PULLUP);

  // Safe default: disabled on boot
  setEnable(false);

  // Serial
  Serial.begin(9600);
  while (!Serial) { /* wait for native USB boards; Uno will skip */ }

  Serial.println(F("\nLid Controller Ready."));
  Serial.println(F("Assuming position = 0 (CLOSED) at power-on."));
  printHelp();
}

void loop()
{
  // Handle serial commands
  if (Serial.available())
  {
    String cmd = cleaned(Serial.readStringUntil('\n'));

    if      (cmd == F("OPEN"))    { stopRequested = false; moveTo(MAX_STEPS); Serial.println(F("OPEN done.")); }
    else if (cmd == F("CLOSE"))   { stopRequested = false; moveTo(0);         Serial.println(F("CLOSE done.")); }
    else if (cmd == F("STOP"))    { stopRequested = true;  Serial.println(F("STOP requested.")); }
    else if (cmd == F("POS?"))    { Serial.print(F("POS=")); Serial.println(positionSteps); }
    else if (cmd == F("STATUS?")) { printStatus(); }
    else if (cmd == F("ENABLE"))  { setEnable(true);  Serial.println(F("Enabled (EN=LOW).")); }
    else if (cmd == F("DISABLE")) { setEnable(false); Serial.println(F("Disabled (EN=HIGH).")); }
    else if (cmd.length() > 0)    { Serial.println(F("Unknown cmd.")); printHelp(); }
  }

  // Poll buttons when idle
  if (!moving) {
    pollButtons(true);
  }
}
