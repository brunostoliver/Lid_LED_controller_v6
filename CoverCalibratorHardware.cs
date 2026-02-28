// TODO fill in this information for your driver, then remove this line!
//
// ASCOM CoverCalibrator hardware class for Lid_cover_only_v6
//
// Implements: ASCOM CoverCalibrator (Cover only; Calibrator not implemented)
//

using ASCOM;
using ASCOM.DeviceInterface;
using ASCOM.Utilities;
using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO.Ports;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ASCOM.Lid_cover_only_v6.CoverCalibrator
{
    [HardwareClass()] // Dispose() will be called automatically when the LocalServer exits.
    internal static class CoverCalibratorHardware
    {
        // -------- Profile persistence keys --------
        internal const string comPortProfileName = "COM Port";
        internal const string comPortDefault = "COM1";
        internal const string traceStateProfileName = "Trace Level";
        internal const string traceStateDefault = "true";

        private static string DriverProgId = "";
        private static string DriverDescription = "";

        internal static string comPort;
        private static bool connectedState;
        private static bool runOnce = false;

        internal static Util utilities;
        internal static TraceLogger tl;

        private static readonly List<Guid> uniqueIds = new List<Guid>();

        // -------- Serial & concurrency plumbing --------
        private static SerialPort _port;
        private static CancellationTokenSource _cts;
        private static Task _readerTask;
        private static Task _writerTask;
        private static Task _statusPollTask;

        private static readonly ConcurrentQueue<string> _txQueue = new ConcurrentQueue<string>();
        private static readonly AutoResetEvent _txSignal = new AutoResetEvent(false);
        private static readonly object _snapGate = new object();
        private static readonly object _moveGate = new object();

        // Expectation watchdog: expected final state after a command (OPEN/CLOSED), with timeout
        private static string _expectedTargetState = null;   // "OPEN" | "CLOSED" | null
        private static DateTime _expectedExpiryUtc = DateTime.MinValue;

        // Movement controller (prevents early "not moving" from first JSON)
        private enum MoveMode { None, Opening, Closing, Halting }
        private static MoveMode _moveMode = MoveMode.None;
        private static DateTime _cmdIssuedUtc = DateTime.MinValue;
        private static DateTime _minHoldMovingTrueUntil = DateTime.MinValue; // keep CoverMoving true until this time
        private static bool _haveMovementEvidence = false; // pos changed or MOVE_STARTED seen
        private const double MIN_HOLD_SECONDS = 1.5;       // grace period to ride out early JSON
        private const int EVIDENCE_POS_DELTA = 3;          // steps change to count as motion evidence

        // Snapshot of device state reported by Arduino STATUS_JSON?
        private class Snap
        {
            public int En = 0;                 // holding torque enabled (1) / disabled (0)
            public int Mov = 0;                // moving flag (from Arduino snapshot)
            public int Pos = 0;                // steps
            public int Max = 10500;            // max steps (calibrated)
            public string State = "CLOSED";    // "OPEN" | "CLOSED" | "PARTIAL" | "HALTED" | "UNKNOWN"
            public DateTime LastUpdateUtc = DateTime.MinValue;
        }
        private static Snap _snap = new Snap();
        private static int _lastPos = 0;

        // Forced final-state latch (prevents STATUS_JSON from overriding immediately)
        private static bool _forcedFinalActive = false;
        private static string _forcedFinalState = null; // "OPEN" or "CLOSED"
        private static DateTime _forcedFinalUntilUtc = DateTime.MinValue;
        private const double FORCED_HOLD_SECONDS = 30.0; // how long we insist on the commanded final state

        private static Snap GetSnap()
        {
            lock (_snapGate)
            {
                return new Snap
                {
                    En = _snap.En,
                    Mov = _snap.Mov,
                    Pos = _snap.Pos,
                    Max = _snap.Max,
                    State = _snap.State,
                    LastUpdateUtc = _snap.LastUpdateUtc
                };
            }
        }
        private static void UpdateSnap(int? en = null, int? mov = null, int? pos = null, int? max = null, string state = null)
        {
            lock (_snapGate)
            {
                if (en.HasValue) _snap.En = en.Value;

                if (mov.HasValue) _snap.Mov = mov.Value;

                if (pos.HasValue)
                {
                    // record pos & movement evidence
                    int newPos = pos.Value;
                    if (Math.Abs(newPos - _lastPos) >= EVIDENCE_POS_DELTA)
                    {
                        lock (_moveGate) { _haveMovementEvidence = true; }
                    }
                    _snap.Pos = newPos;
                    _lastPos = newPos;
                }

                if (max.HasValue) _snap.Max = max.Value;

                if (state != null)
                {
                    string norm = NormalizeState(state);
                    bool allow = true;
                    if (_forcedFinalActive && DateTime.UtcNow < _forcedFinalUntilUtc)
                    {
                        // If incoming contradicts latched final state, ignore it during hold window
                        if (!string.Equals(norm, _forcedFinalState, StringComparison.OrdinalIgnoreCase))
                            allow = false;
                    }
                    if (allow) _snap.State = norm;
                }

                _snap.LastUpdateUtc = DateTime.UtcNow;

                // Derive explicit OPEN/CLOSED if we have boundaries and not moving
                RecomputeBoundaryState_NoLock();
            }
        }

        private static string NormalizeState(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw)) return "UNKNOWN";
            string s = raw.Trim().ToUpperInvariant();
            if (s == "OPEN" || s == "OPENED") return "OPEN";
            if (s == "CLOSE" || s == "CLOSED" || s == "CLOSING") return "CLOSED";
            if (s == "PARTIAL" || s == "PARTIALLY_OPEN") return "PARTIAL";
            if (s == "HALTED" || s == "STOPPED") return "HALTED";
            if (s == "MOVING") return "PARTIAL";
            return s; // UNKNOWN etc.
        }

        // When not moving, infer OPEN/CLOSED if near boundaries. Otherwise leave PARTIAL/HALTED => Unknown in CoverState.
        private static void RecomputeBoundaryState_NoLock()
        {
            // Expire latch if time passed
            if (_forcedFinalActive && DateTime.UtcNow >= _forcedFinalUntilUtc)
            {
                _forcedFinalActive = false;
                _forcedFinalState = null;
                _forcedFinalUntilUtc = DateTime.MinValue;
            }

            const int TOL = 2; // steps tolerance

            // Only infer when not "moving" per movement controller
            if (IsMovingNow_NoLock()) return;

            if (_snap.Pos <= TOL)
            {
                _snap.State = "CLOSED";
                if (_forcedFinalActive && _forcedFinalState == "CLOSED")
                {
                    _forcedFinalActive = false; _forcedFinalState = null; _forcedFinalUntilUtc = DateTime.MinValue;
                }
                ClearExpectation_NoLock();
                return;
            }
            if (_snap.Max > 0 && _snap.Pos >= (_snap.Max - TOL))
            {
                _snap.State = "OPEN";
                if (_forcedFinalActive && _forcedFinalState == "OPEN")
                {
                    _forcedFinalActive = false; _forcedFinalState = null; _forcedFinalUntilUtc = DateTime.MinValue;
                }
                ClearExpectation_NoLock();
                return;
            }
            // else leave whatever state string we had; CoverState will map PARTIAL/HALTED/UNKNOWN to Unknown
        }

        private static void SetExpectation(string target, double seconds)
        {
            lock (_snapGate)
            {
                _expectedTargetState = target?.ToUpperInvariant();
                _expectedExpiryUtc = DateTime.UtcNow.AddSeconds(seconds);
            }
        }
        private static void ClearExpectation_NoLock()
        {
            _expectedTargetState = null;
            _expectedExpiryUtc = DateTime.MinValue;
        }
        private static void ClearExpectation()
        {
            lock (_snapGate) ClearExpectation_NoLock();
        }

        private static void StartMove(MoveMode mode, string expect, double expectSeconds)
        {
            lock (_moveGate)
            {
                _moveMode = mode;
                _cmdIssuedUtc = DateTime.UtcNow;
                _minHoldMovingTrueUntil = _cmdIssuedUtc.AddSeconds(MIN_HOLD_SECONDS);
                _haveMovementEvidence = false;
            }
            // Clear any previous forced latch at the start of a new move
            lock (_snapGate)
            {
                _forcedFinalActive = false;
                _forcedFinalState = null;
                _forcedFinalUntilUtc = DateTime.MinValue;
            }
            SetExpectation(expect, expectSeconds);
        }
        private static void FinishMove()
        {
            lock (_moveGate)
            {
                _moveMode = MoveMode.None;
                _minHoldMovingTrueUntil = DateTime.MinValue;
                _haveMovementEvidence = false;
            }
            ClearExpectation();
        }

        // ------------- Static ctor & one-time init -------------
        static CoverCalibratorHardware()
        {
            try
            {
                tl = new TraceLogger("", "Lid_cover_only_v6.Hardware");
                DriverProgId = CoverCalibrator.DriverProgId;
                ReadProfile(); // loads tl.Enabled
                LogMessage("CoverCalibratorHardware", "Static initialiser completed.");
            }
            catch (Exception ex)
            {
                try { LogMessage("CoverCalibratorHardware", $"Initialisation exception: {ex}"); } catch { }
                MessageBox.Show($"CoverCalibratorHardware - {ex.Message}\r\n{ex}", $"Exception creating {CoverCalibrator.DriverProgId}", MessageBoxButtons.OK, MessageBoxIcon.Error);
                throw;
            }
        }

        internal static void InitialiseHardware()
        {
            LogMessage("InitialiseHardware", "Start.");
            if (!runOnce)
            {
                DriverDescription = CoverCalibrator.DriverDescription;
                connectedState = false;
                utilities = new Util();
                LogMessage("InitialiseHardware", $"ProgID: {DriverProgId}, Description: {DriverDescription}");
                runOnce = true;
            }
        }

        // -------------------- ICoverCalibrator common --------------------

        public static void SetupDialog()
        {
            if (IsConnected)
                MessageBox.Show("Already connected, just press OK");

            using (SetupDialogForm F = new SetupDialogForm(tl))
            {
                var result = F.ShowDialog();
                if (result == DialogResult.OK)
                {
                    WriteProfile();
                }
            }
        }

        public static ArrayList SupportedActions
        {
            get
            {
                LogMessage("SupportedActions Get", "Returning empty ArrayList");
                return new ArrayList();
            }
        }

        public static string Action(string actionName, string actionParameters)
        {
            LogMessage("Action", $"Action {actionName}, parameters {actionParameters} is not implemented");
            throw new ActionNotImplementedException("Action " + actionName + " is not implemented by this driver");
        }

        public static void CommandBlind(string command, bool raw)
        {
            CheckConnected("CommandBlind");
            throw new MethodNotImplementedException($"CommandBlind - Command:{command}, Raw: {raw}.");
        }

        public static bool CommandBool(string command, bool raw)
        {
            CheckConnected("CommandBool");
            throw new MethodNotImplementedException($"CommandBool - Command:{command}, Raw: {raw}.");
        }

        public static string CommandString(string command, bool raw)
        {
            CheckConnected("CommandString");
            throw new MethodNotImplementedException($"CommandString - Command:{command}, Raw: {raw}.");
        }

        public static void Dispose()
        {
            try { LogMessage("Dispose", "Disposing of assets and closing down."); } catch { }

            try { if (connectedState) HardwareDisconnect(); } catch { }

            try { tl.Enabled = false; tl.Dispose(); tl = null; } catch { }
            try { utilities?.Dispose(); utilities = null; } catch { }
        }

        public static void SetConnected(Guid uniqueId, bool newState)
        {
            if (newState)
            {
                if (uniqueIds.Contains(uniqueId))
                {
                    LogMessage("SetConnected", "Ignoring connect; instance already connected.");
                }
                else
                {
                    if (uniqueIds.Count == 0)
                    {
                        LogMessage("SetConnected", $"Connecting to hardware on {comPort}.");
                        HardwareConnect();
                    }
                    else
                    {
                        LogMessage("SetConnected", "Hardware already connected (other instances present).");
                    }
                    uniqueIds.Add(uniqueId);
                    LogMessage("SetConnected", $"Unique id {uniqueId} added to the connection list.");
                }
            }
            else
            {
                if (!uniqueIds.Contains(uniqueId))
                {
                    LogMessage("SetConnected", "Ignoring disconnect; instance not connected.");
                }
                else
                {
                    uniqueIds.Remove(uniqueId);
                    LogMessage("SetConnected", $"Unique id {uniqueId} removed from the connection list.");

                    if (uniqueIds.Count == 0)
                    {
                        LogMessage("SetConnected", "No connected instances remain; disconnecting hardware.");
                        HardwareDisconnect();
                    }
                    else
                    {
                        LogMessage("SetConnected", "Other instances still connected; keeping hardware up.");
                    }
                }
            }

            LogMessage("SetConnected", "Currently connected driver ids:");
            foreach (Guid id in uniqueIds) LogMessage("SetConnected", $" ID {id} is connected");
        }

        public static string Description
        {
            get { LogMessage("Description Get", DriverDescription); return DriverDescription; }
        }

        public static string DriverInfo
        {
            get
            {
                var version = System.Reflection.Assembly.GetExecutingAssembly().GetName().Version;
                string driverInfo = $"Lid cover controller for Arduino/TMC2209. Version: {version.Major}.{version.Minor}";
                LogMessage("DriverInfo Get", driverInfo);
                return driverInfo;
            }
        }

        public static string DriverVersion
        {
            get
            {
                var version = System.Reflection.Assembly.GetExecutingAssembly().GetName().Version;
                string driverVersion = $"{version.Major}.{version.Minor}";
                LogMessage("DriverVersion Get", driverVersion);
                return driverVersion;
            }
        }

        public static short InterfaceVersion
        {
            get { LogMessage("InterfaceVersion Get", "2"); return 2; }
        }

        public static string Name
        {
            get
            {
                string name = "Lid Cover (Arduino)";
                LogMessage("Name Get", name);
                return name;
            }
        }

        // -------------------- Cover implementation --------------------

        internal static CoverStatus CoverState
        {
            get
            {
                CheckConnected("CoverState");
                lock (_snapGate)
                {
                    // Always prioritize active movement reporting.
                    if (IsMovingNow_NoLock()) return CoverStatus.Moving;

                    // If we have a latched final state, return it
                    if (_forcedFinalActive && DateTime.UtcNow < _forcedFinalUntilUtc)
                    {
                        return (_forcedFinalState == "OPEN") ? CoverStatus.Open : CoverStatus.Closed;
                    }

                    // When not moving: map OPEN/CLOSED; partial/halted -> Unknown per ASCOM semantics
                    switch ((_snap.State ?? "UNKNOWN").ToUpperInvariant())
                    {
                        case "OPEN": return CoverStatus.Open;
                        case "CLOSED": return CoverStatus.Closed;
                        case "PARTIAL":
                        case "HALTED":
                        case "UNKNOWN":
                        default:
                            return CoverStatus.Unknown;
                    }
                }
            }
        }

        internal static bool CoverMoving
        {
            get
            {
                try { return CoverState == CoverStatus.Moving; }
                catch { return false; }
            }
        }

        internal static void OpenCover()
        {
            CheckConnected("OpenCover");

            // Start movement controller and immediately present "moving"
            StartMove(MoveMode.Opening, "OPEN", 12.0);
            UpdateSnap(mov: 1, state: "PARTIAL");

            LogMessage("OpenCover", "Sending OPEN");
            Send("OPEN");

            // Ask for an immediate status refresh
            Send("STATUS_JSON?");
        }

        internal static void CloseCover()
        {
            CheckConnected("CloseCover");

            // Start movement controller and immediately present "moving"
            StartMove(MoveMode.Closing, "CLOSED", 12.0);
            UpdateSnap(mov: 1, state: "PARTIAL");

            LogMessage("CloseCover", "Sending CLOSE");
            Send("CLOSE");

            // Ask for an immediate status refresh
            Send("STATUS_JSON?");
        }

        internal static void HaltCover()
        {
            CheckConnected("HaltCover");

            LogMessage("HaltCover", "Sending STOP");
            Send("STOP");

            // Stop immediately from the driver's perspective; report not moving and Unknown/HALTED position
            UpdateSnap(mov: 0, state: "HALTED");
            FinishMove();

            // Drop any forced latch because we're not moving to an end-stop
            lock (_snapGate)
            {
                _forcedFinalActive = false;
                _forcedFinalState = null;
                _forcedFinalUntilUtc = DateTime.MinValue;
            }

            // Request a status snapshot to reconcile exact position
            Send("STATUS_JSON?");
        }

        // -------------------- Calibrator (not used) --------------------

        internal static CalibratorStatus CalibratorState
        {
            get
            {
                LogMessage("CalibratorState Get", "Calibrator not present");
                return CalibratorStatus.NotPresent;
            }
        }

        internal static bool CalibratorChanging
        {
            get
            {
                try { return CalibratorState == CalibratorStatus.NotReady; }
                catch { return false; }
            }
        }

        internal static int Brightness
        {
            get { LogMessage("Brightness Get", "Calibrator not implemented"); throw new PropertyNotImplementedException("Brightness", false); }
        }

        internal static int MaxBrightness
        {
            get { LogMessage("MaxBrightness Get", "Calibrator not implemented"); throw new PropertyNotImplementedException("MaxBrightness", false); }
        }

        internal static void CalibratorOn(int Brightness)
        {
            LogMessage("CalibratorOn", "Calibrator not implemented");
            throw new MethodNotImplementedException("CalibratorOn");
        }

        internal static void CalibratorOff()
        {
            LogMessage("CalibratorOff", "Calibrator not implemented");
            throw new MethodNotImplementedException("CalibratorOff");
        }

        // -------------------- Private properties and methods --------------------

        private static bool IsConnected => connectedState;

        private static void CheckConnected(string message)
        {
            if (!IsConnected) throw new NotConnectedException(message);
        }

        internal static void ReadProfile()
        {
            using (Profile driverProfile = new Profile())
            {
                driverProfile.DeviceType = "CoverCalibrator";
                tl.Enabled = Convert.ToBoolean(driverProfile.GetValue(DriverProgId, traceStateProfileName, string.Empty, traceStateDefault));
                comPort = driverProfile.GetValue(DriverProgId, comPortProfileName, string.Empty, comPortDefault);
            }
        }

        internal static void WriteProfile()
        {
            using (Profile driverProfile = new Profile())
            {
                driverProfile.DeviceType = "CoverCalibrator";
                driverProfile.WriteValue(DriverProgId, traceStateProfileName, tl.Enabled.ToString());
                driverProfile.WriteValue(DriverProgId, comPortProfileName, comPort ?? comPortDefault);
            }
        }

        internal static void LogMessage(string identifier, string message)
        {
            tl.LogMessageCrLf(identifier, message);
        }

        internal static void LogMessage(string identifier, string message, params object[] args)
        {
            var msg = string.Format(message, args);
            LogMessage(identifier, msg);
        }

        // -------------------- Hardware connect / disconnect --------------------

        private static void HardwareConnect()
        {
            if (connectedState) return;

            try
            {
                if (string.IsNullOrWhiteSpace(comPort)) comPort = comPortDefault;

                _port = new SerialPort(comPort, 9600, Parity.None, 8, StopBits.One)
                {
                    NewLine = "\n",
                    Encoding = Encoding.ASCII,
                    ReadTimeout = 1000,
                    WriteTimeout = 1000,
                    DtrEnable = true,
                    RtsEnable = false
                };
                _port.Open();
                LogMessage("HardwareConnect", $"Serial port {_port.PortName} opened.");
            }
            catch (Exception ex)
            {
                LogMessage("HardwareConnect", $"ERROR opening port {comPort}: {ex}");
                throw new DriverException($"Failed to open {comPort}: {ex.Message}");
            }

            _cts = new CancellationTokenSource();
            _readerTask = Task.Factory.StartNew(() => ReaderLoop(_cts.Token), _cts.Token, TaskCreationOptions.LongRunning, TaskScheduler.Default);
            _writerTask = Task.Factory.StartNew(() => WriterLoop(_cts.Token), _cts.Token, TaskCreationOptions.LongRunning, TaskScheduler.Default);
            _statusPollTask = Task.Factory.StartNew(() => StatusPollLoop(_cts.Token), _cts.Token, TaskCreationOptions.LongRunning, TaskScheduler.Default);

            // Seed a status request
            Send("STATUS_JSON?");

            connectedState = true;
            LogMessage("HardwareConnect", "Connected.");
        }

        private static void HardwareDisconnect()
        {
            if (!connectedState) return;

            try { _cts?.Cancel(); } catch { }
            try { _txSignal.Set(); } catch { }
            try { Task.WaitAll(new[] { _readerTask, _writerTask, _statusPollTask }, 2000); } catch { }

            try { if (_port != null && _port.IsOpen) _port.Close(); } catch { }
            try { _port?.Dispose(); } catch { }
            _port = null;

            _readerTask = null;
            _writerTask = null;
            _statusPollTask = null;
            _cts = null;

            connectedState = false;
            LogMessage("HardwareDisconnect", "Disconnected.");
        }

        // -------------------- Serial tasks --------------------

        private static void ReaderLoop(CancellationToken ct)
        {
            try
            {
                while (!ct.IsCancellationRequested)
                {
                    string line = null;
                    try
                    {
                        line = _port.ReadLine();
                    }
                    catch (TimeoutException)
                    {
                        continue;
                    }
                    catch (Exception ex)
                    {
                        LogMessage("ReaderLoop", $"Read error: {ex.Message}");
                        break;
                    }

                    if (string.IsNullOrWhiteSpace(line)) continue;
                    line = line.Trim('\r', '\n');
                    LogMessage("RX", line);

                    if (line.StartsWith("{"))
                    {
                        TryParseStatusJson(line);
                        continue;
                    }

                    if (line.StartsWith("READY", StringComparison.OrdinalIgnoreCase))
                    {
                        Send("STATUS_JSON?");
                        continue;
                    }
                    if (line.StartsWith("HELLO", StringComparison.OrdinalIgnoreCase)) continue;
                    if (line.StartsWith("PONG", StringComparison.OrdinalIgnoreCase)) continue;

                    if (line.StartsWith("EVT", StringComparison.OrdinalIgnoreCase))
                    {
                        HandleEventLine(line);
                        continue;
                    }
                }
            }
            catch (Exception ex)
            {
                LogMessage("ReaderLoop", $"Fatal: {ex}");
            }
        }

        private static void WriterLoop(CancellationToken ct)
        {
            try
            {
                while (!ct.IsCancellationRequested)
                {
                    if (_txQueue.IsEmpty)
                    {
                        _txSignal.WaitOne(250);
                        if (ct.IsCancellationRequested) break;
                    }

                    while (_txQueue.TryDequeue(out var cmd))
                    {
                        try
                        {
                            if (_port != null && _port.IsOpen)
                            {
                                _port.Write(cmd);
                                _port.Write("\n");
                                LogMessage("TX", cmd);
                            }
                        }
                        catch (Exception ex)
                        {
                            LogMessage("WriterLoop", $"Write error: {ex.Message}");
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                LogMessage("WriterLoop", $"Fatal: {ex}");
            }
        }

        private static void StatusPollLoop(CancellationToken ct)
        {
            try
            {
                while (!ct.IsCancellationRequested)
                {
                    // Periodic status
                    Send("STATUS_JSON?");

                    // Expectation reconciliation: finalize if the watchdog expires
                    lock (_snapGate)
                    {
                        bool movingNow = IsMovingNow_NoLock();

                        if (_expectedTargetState != null)
                        {
                            // If the deadline has passed, force completion regardless of current state
                            if (DateTime.UtcNow >= _expectedExpiryUtc)
                            {
                                _snap.Mov = 0;
                                _snap.State = _expectedTargetState;

                                // Latch the final state so immediate STATUS_JSON can't override it
                                _forcedFinalActive = true;
                                _forcedFinalState = _expectedTargetState;
                                _forcedFinalUntilUtc = DateTime.UtcNow.AddSeconds(FORCED_HOLD_SECONDS);

                                LogMessage("Watchdog", $"Forcing final state to {_snap.State} after timeout (latched until {_forcedFinalUntilUtc:O}).");
                                // drop locks safely; FinishMove doesn't touch _snapGate
                                FinishMove();
                            }
                            else if (!movingNow)
                            {
                                // Natural completion if we reached a boundary or device reported final state
                                RecomputeBoundaryState_NoLock();
                                if (_snap.State == "OPEN" || _snap.State == "CLOSED")
                                {
                                    LogMessage("Watchdog", $"Observed final state {_snap.State}, finishing move.");
                                    FinishMove();
                                }
                            }
                        }
                    }

                    for (int i = 0; i < 15 && !ct.IsCancellationRequested; i++) Thread.Sleep(100); // ~1.5s cadence
                }
            }
            catch (Exception ex)
            {
                LogMessage("StatusPollLoop", $"Fatal: {ex}");
            }
        }

        private static void Send(string cmd)
        {
            if (!connectedState) return;
            _txQueue.Enqueue(cmd);
            _txSignal.Set();
        }

        // -------------------- Parsing helpers --------------------

        private static void HandleEventLine(string line)
        {
            try
            {
                if (line.IndexOf("MOVE_STARTED", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    lock (_moveGate) { _haveMovementEvidence = true; }
                    UpdateSnap(mov: 1, state: "PARTIAL");
                }
                else if (line.IndexOf("MOVE_DONE", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    int? pos = TryExtractInt(line, "pos=");
                    string state = TryExtractToken(line, "state=");
                    UpdateSnap(mov: 0, pos: pos, state: state);
                    FinishMove();
                }
                else if (line.IndexOf("ENABLED", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    UpdateSnap(en: 1);
                }
                else if (line.IndexOf("DISABLED", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    UpdateSnap(en: 0);
                }
                else if (line.IndexOf("LIMIT_STATE", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    // New Arduino EVT: "EVT LIMIT_STATE open=1 close=0" - handle gracefully
                    int? open = TryExtractInt(line, "open=");
                    int? close = TryExtractInt(line, "close=");
                    bool openActive = open.HasValue && open.Value != 0;
                    bool closeActive = close.HasValue && close.Value != 0;

                    LogMessage("HandleEventLine", $"Limit state reported open={openActive} close={closeActive}");

                    if (openActive)
                    {
                        // set physical position to max and latch final state
                        UpdateSnap(pos: _snap.Max, mov: 0, state: "OPEN");
                        lock (_snapGate)
                        {
                            _forcedFinalActive = true;
                            _forcedFinalState = "OPEN";
                            _forcedFinalUntilUtc = DateTime.UtcNow.AddSeconds(FORCED_HOLD_SECONDS);
                        }
                        FinishMove();
                    }
                    else if (closeActive)
                    {
                        UpdateSnap(pos: 0, mov: 0, state: "CLOSED");
                        lock (_snapGate)
                        {
                            _forcedFinalActive = true;
                            _forcedFinalState = "CLOSED";
                            _forcedFinalUntilUtc = DateTime.UtcNow.AddSeconds(FORCED_HOLD_SECONDS);
                        }
                        FinishMove();
                    }
                    else
                    {
                        // neither limit active
                        UpdateSnap(state: "PARTIAL");
                    }
                }
                else if (line.IndexOf("OPEN_BLOCKED", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    LogMessage("HandleEventLine", "Open blocked by hardware limit");
                    // reflect blocked state and request a status refresh
                    UpdateSnap(mov: 0, state: "OPEN");
                    lock (_snapGate)
                    {
                        _forcedFinalActive = true;
                        _forcedFinalState = "OPEN";
                        _forcedFinalUntilUtc = DateTime.UtcNow.AddSeconds(FORCED_HOLD_SECONDS);
                    }
                    FinishMove();
                }
                else if (line.IndexOf("CLOSE_BLOCKED", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    LogMessage("HandleEventLine", "Close blocked by hardware limit");
                    UpdateSnap(mov: 0, state: "CLOSED");
                    lock (_snapGate)
                    {
                        _forcedFinalActive = true;
                        _forcedFinalState = "CLOSED";
                        _forcedFinalUntilUtc = DateTime.UtcNow.AddSeconds(FORCED_HOLD_SECONDS);
                    }
                    FinishMove();
                }
            }
            catch (Exception ex)
            {
                LogMessage("HandleEventLine", $"Parse error: {ex.Message}");
            }
        }

        private static void TryParseStatusJson(string json)
        {
            try
            {
                int? en = TryExtractJsonInt(json, "\"en\":");
                int? movCandidate = TryExtractJsonInt(json, "\"mov\":");
                int? pos = TryExtractJsonInt(json, "\"pos\":");
                int? max = TryExtractJsonInt(json, "\"max\":");
                string state = TryExtractJsonString(json, "\"state\":");

                // New fields from firmware: lim_open, lim_close
                int? limOpen = TryExtractJsonInt(json, "\"lim_open\":");
                int? limClose = TryExtractJsonInt(json, "\"lim_close\":");

                // Decide whether to accept movCandidate=0 during the grace period
                int? movToApply = movCandidate;
                if (movCandidate.HasValue && movCandidate.Value == 0)
                {
                    bool suppress;
                    lock (_moveGate)
                    {
                        suppress = (_moveMode == MoveMode.Opening || _moveMode == MoveMode.Closing)
                                   && (DateTime.UtcNow < _minHoldMovingTrueUntil || !_haveMovementEvidence);
                    }
                    if (suppress) movToApply = 1; // keep reporting moving during grace
                }

                bool movingNow = movToApply.HasValue && movToApply.Value == 1;

                // If limits are present in JSON, use them as authoritative indicators of boundary
                // only when movement is not currently active.
                if (!movingNow && limOpen.HasValue && limOpen.Value != 0)
                {
                    // physical open limit asserted
                    // set pos to max, mark not moving and set OPEN
                    UpdateSnap(en, 0, _snap.Max, max, "OPEN");
                    lock (_snapGate)
                    {
                        _forcedFinalActive = true;
                        _forcedFinalState = "OPEN";
                        _forcedFinalUntilUtc = DateTime.UtcNow.AddSeconds(FORCED_HOLD_SECONDS);
                    }
                }
                else if (!movingNow && limClose.HasValue && limClose.Value != 0)
                {
                    UpdateSnap(en, 0, 0, max, "CLOSED");
                    lock (_snapGate)
                    {
                        _forcedFinalActive = true;
                        _forcedFinalState = "CLOSED";
                        _forcedFinalUntilUtc = DateTime.UtcNow.AddSeconds(FORCED_HOLD_SECONDS);
                    }
                }
                else
                {
                    UpdateSnap(en, movToApply, pos, max, state);
                }

                // If JSON shows not moving (after grace), clear expectation when boundary reached or explicit state given
                lock (_snapGate)
                {
                    if (!IsMovingNow_NoLock())
                    {
                        if (_snap.State == "OPEN" || _snap.State == "CLOSED")
                        {
                            FinishMove();
                        }
                        else
                        {
                            RecomputeBoundaryState_NoLock();
                            if (_snap.State == "OPEN" || _snap.State == "CLOSED") FinishMove();
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                LogMessage("TryParseStatusJson", $"Parse error: {ex.Message}");
            }
        }

        private static int? TryExtractJsonInt(string json, string key)
        {
            int idx = json.IndexOf(key, StringComparison.OrdinalIgnoreCase);
            if (idx < 0) return null;
            idx += key.Length;
            while (idx < json.Length && (json[idx] == ' ')) idx++;
            int start = idx;
            while (idx < json.Length && (char.IsDigit(json[idx]) || json[idx] == '-')) idx++;
            if (idx <= start) return null;
            if (int.TryParse(json.Substring(start, idx - start), out int val)) return val;
            return null;
        }

        private static string TryExtractJsonString(string json, string key)
        {
            int idx = json.IndexOf(key, StringComparison.OrdinalIgnoreCase);
            if (idx < 0) return null;
            idx += key.Length;
            while (idx < json.Length && (json[idx] == ' ')) idx++;
            if (idx >= json.Length || json[idx] != '\"') return null;
            idx++;
            int start = idx;
            while (idx < json.Length && json[idx] != '\"') idx++;
            if (idx <= start || idx >= json.Length) return null;
            return json.Substring(start, idx - start);
        }

        private static int? TryExtractInt(string line, string token)
        {
            int i = line.IndexOf(token, StringComparison.OrdinalIgnoreCase);
            if (i < 0) return null;
            i += token.Length;
            int start = i;
            while (i < line.Length && char.IsDigit(line[i])) i++;
            if (int.TryParse(line.Substring(start, i - start), out int v)) return v;
            return null;
        }

        private static string TryExtractToken(string line, string token)
        {
            int i = line.IndexOf(token, StringComparison.OrdinalIgnoreCase);
            if (i < 0) return null;
            i += token.Length;
            int start = i;
            while (i < line.Length && !char.IsWhiteSpace(line[i])) i++;
            if (i > start) return NormalizeState(line.Substring(start, i - start));
            return null;
        }

        // -------------------- Movement-state computation --------------------

        private static bool IsMovingNow_NoLock()
        {
            // Prefer movement controller state to raw JSON 'mov' (which can flicker to 0 very early)
            bool controllerSaysMoving;
            lock (_moveGate)
            {
                controllerSaysMoving =
                    (_moveMode == MoveMode.Opening || _moveMode == MoveMode.Closing) &&
                    // keep true through grace or until we saw actual motion
                    (DateTime.UtcNow < _minHoldMovingTrueUntil || _haveMovementEvidence);
            }

            if (controllerSaysMoving) return true;

            // After grace / with evidence, use device-reported mov flag
            return _snap.Mov == 1;
        }
    }
}

