#!/usr/bin/env python3
"""
KoNova Slow Control — Main Acquisition Daemon (kodaq)
Reads all sensors, writes daily log files, serves live data over HTTP.
Pressure is stored and displayed in kPa throughout.
"""

import glob
import json
import os
import signal
import smtplib
import subprocess
import sys
import time
import threading
import urllib.parse
from collections import deque
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from sensors import CH347, BME280, HDC302x, HWT905, A1Temp, LinuxTemp

# ── Global state shared with the HTTP server ────────────────────────────
live_data = {}          # sensor_id → {label, type, temp, hum, press, timestamp}
history = []            # list of (timestamp_iso, {sensor_id: {temp, hum, press}})
HISTORY_MAX = 17280     # 1 day at 5s intervals (~8MB)
live_lock = threading.Lock()
config = {}
data_directory = "./data"

# ── PETSys DAQ process state ─────────────────────────────────────────────
_daqd_process = None          # subprocess.Popen or None
_daqd_lock    = threading.Lock()
_asic_power   = threading.Event()   # .is_set() = ON, .clear() = OFF
_ASIC_POWER_FLAG = "/tmp/konova_asic_power_off"
_mains_power  = {"status": "unknown", "last_check": None}  # "on", "off", "unknown"

def _asic_power_on():
    """Set ASIC power ON — remove flag so sensor_worker reads A1Temp."""
    _asic_power.set()
    try:
        os.remove(_ASIC_POWER_FLAG)
    except FileNotFoundError:
        pass

def _asic_power_off():
    """Set ASIC power OFF — create flag so sensor_worker skips A1Temp."""
    _asic_power.clear()
    try:
        with open(_ASIC_POWER_FLAG, "w") as f:
            f.write("off")
    except Exception:
        pass
_petsys_log   = []            # list of {seq, kind, text, ts}
_petsys_lock  = threading.Lock()
_petsys_seq   = 0             # monotonically increasing, never resets
PETSYS_LOG_MAX = 2000


def _petsys_append(kind: str, text: str):
    """Append a line to the PETSys log (thread-safe)."""
    global _petsys_seq
    with _petsys_lock:
        _petsys_seq += 1
        _petsys_log.append({"seq": _petsys_seq, "kind": kind, "text": text,
                            "ts": datetime.now().strftime("%H:%M:%S")})
        if len(_petsys_log) > PETSYS_LOG_MAX:
            del _petsys_log[:len(_petsys_log) - PETSYS_LOG_MAX]


_acq_log_path = ""   # set when acquisition starts
_err_log_path = ""   # persistent error log — set in main()

def _err_log(msg: str):
    """Append to the persistent error log."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {msg}\n"
    print(line, end="")
    if _err_log_path:
        try:
            with open(_err_log_path, "a") as f:
                f.write(line)
        except Exception:
            pass


def _acq_log(msg: str):
    """Write to both the petsys ring buffer and the persistent acquisition log."""
    _petsys_append("info", msg)
    if _acq_log_path:
        try:
            with open(_acq_log_path, "a") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        except Exception:
            pass


def _build_dir() -> str:
    """Return the PETSys build directory from config (expanded)."""
    for sid, s in config.get("sensors", {}).items():
        if s.get("type") == "A1Temp":
            return os.path.expanduser(s.get("build_dir", ""))
    return ""


def _daqd_is_running() -> bool:
    global _daqd_process
    if _daqd_process is not None and _daqd_process.poll() is None:
        return True
    # Also detect externally started daqd
    try:
        r = subprocess.run(["pidof", "daqd"], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def _run_petsys_cmd(cmd: str):
    """Execute a PETSys command in a background thread, streaming output to the log."""
    def _safe_worker(c):
        try:
            _petsys_worker(c)
        except Exception as e:
            import traceback as _tb
            _petsys_append("fail", f"Command '{c}' crashed: {e}")
            _petsys_append("fail", _tb.format_exc().strip().splitlines()[-1])
    threading.Thread(target=_safe_worker, args=(cmd,), daemon=True).start()


def _run_power_cmd(name, args, build=None):
    """Run a PETSys power executable (set_fem_power / set_bias). Returns True on success."""
    if build is None:
        build = _build_dir()
    exe = os.path.join(build, name)
    if not os.path.isfile(exe):
        _petsys_append("fail", f"✘ {name} not found: {exe}")
        return False
    try:
        proc = subprocess.run(
            [exe] + args, cwd=build,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=30,
        )
        for line in proc.stdout.strip().splitlines():
            if line.strip():
                _petsys_append("info", line)
        return proc.returncode == 0
    except Exception as e:
        _petsys_append("fail", f"✘ {name} error: {e}")
        return False


def _petsys_worker(cmd: str):
    global _daqd_process
    build = _build_dir()
    if cmd == "daq_on":
        with _daqd_lock:
            if _daqd_is_running():
                _petsys_append("warn", "daqd is already running.")
                return
            exe = os.path.join(build, "daqd")
            if not os.path.isfile(exe):
                _petsys_append("fail", f"daqd not found: {exe}")
                return
            # Remove stale socket file from a previous run
            sock = "/tmp/d.sock"
            if os.path.exists(sock):
                try:
                    os.remove(sock)
                    _petsys_append("info", f"Removed stale socket: {sock}")
                except Exception as e:
                    _petsys_append("warn", f"Could not remove {sock}: {e}")
            _petsys_append("cmd", "→ Starting daqd...")
            try:
                _daqd_process = subprocess.Popen(
                    [exe, "--socket-name=/tmp/d.sock", "--daq-type=GBE"],
                    cwd=build,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception as e:
                _petsys_append("fail", f"Failed to start daqd: {e}")
                return
        # Sleep and check outside the lock so other threads aren't blocked
        time.sleep(1.5)
        if _daqd_process.poll() is not None:
            try:
                out = _daqd_process.stdout.read(500)
            except Exception:
                out = "(no output)"
            _daqd_process = None
            _petsys_append("fail", f"daqd exited immediately: {out}")
            return
        # Drain stdout in background to prevent pipe buffer filling
        def _drain(proc):
            try:
                for _ in proc.stdout:
                    pass
            except Exception:
                pass
        import threading as _th
        _th.Thread(target=_drain, args=(_daqd_process,), daemon=True).start()
        _petsys_append("ok", f"✔ daqd started (PID {_daqd_process.pid})")

    elif cmd == "daq_off":
        with _daqd_lock:
            if not _daqd_is_running():
                _petsys_append("info", "daqd is not running.")
                return
            _petsys_append("cmd", "→ Stopping daqd...")
            try:
                if _daqd_process is not None:
                    pid = _daqd_process.pid
                    _daqd_process.send_signal(signal.SIGINT)
                    for _ in range(10):
                        time.sleep(1.0)
                        if _daqd_process.poll() is not None:
                            break
                    if _daqd_process.poll() is None:
                        _daqd_process.kill()
                        _petsys_append("warn", f"daqd (PID {pid}) killed.")
                    else:
                        _petsys_append("ok", f"✔ daqd (PID {pid}) stopped.")
                    _daqd_process = None
                else:
                    # Kill externally started daqd
                    subprocess.run(["pkill", "-INT", "daqd"])
                    time.sleep(2)
                    _petsys_append("ok", "✔ daqd stopped.")
            except Exception as e:
                _petsys_append("fail", f"Error stopping daqd: {e}")
            _asic_power_off()  # power goes away with daqd

    elif cmd in ("i2c_test", "lvds_test", "link_test", "comms_test"):
        exe_map = {
            "i2c_test":   "read_temperature_sensors_JLG",
            "lvds_test":  "init_system",
            "link_test":  "check_asic_links",
            "comms_test": "read_communication_counters",
        }
        label_map = {
            "i2c_test":   "I2C TEST",
            "lvds_test":  "LVDS TEST",
            "link_test":  "LINK TEST",
            "comms_test": "COMMS TEST",
        }
        exe  = os.path.join(build, exe_map[cmd])
        label = label_map[cmd]
        if not os.path.isfile(exe):
            _petsys_append("fail", f"✘ {label}: executable not found: {exe}")
            return
        _petsys_append("cmd", f"→ Running {label}...")
        try:
            proc = subprocess.run(
                [exe], cwd=build,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=60,
            )
            for line in proc.stdout.splitlines():
                if not line.strip():
                    continue
                lo = line.lower()
                import re as _re
                is_zero_error = bool(_re.search(
                    r'0\s+errors?|errors?[/\s]*s?:\s*0|error/s:\s*0', lo))
                kind = ("fail" if not is_zero_error and any(
                                w in lo for w in ("fail","error","fault","no signal"))
                        else "warn" if "warn" in lo and not is_zero_error
                        else "info")
                _petsys_append(kind, line)
            if proc.returncode == 0:
                _petsys_append("ok", f"✔ {label} PASSED")
            else:
                _petsys_append("fail", f"✘ {label} FAILED (exit {proc.returncode})")
        except subprocess.TimeoutExpired:
            _petsys_append("fail", f"✘ {label} timed out after 60 s")
        except Exception as e:
            _petsys_append("fail", f"✘ {label} error: {e}")

    elif cmd in ("asic_power_on", "asic_power_off", "asic_power_cycle"):
        if not _daqd_is_running():
            _petsys_append("fail", "✘ daqd must be running to control ASIC power.")
            return

        if cmd == "asic_power_on":
            _petsys_append("cmd", "→ Turning ASIC power ON (FEM + BIAS)...")
            if _run_power_cmd("set_fem_power", ["--power", "on"]):
                _petsys_append("ok", "✔ FEM power ON")
                time.sleep(0.5)
                if _run_power_cmd("set_bias", ["--power", "on"]):
                    _asic_power_on()
                    _petsys_append("ok", "✔ BIAS power ON — ASICs powered")
                else:
                    _petsys_append("fail", "✘ BIAS power failed")
            else:
                _petsys_append("fail", "✘ FEM power failed")

        elif cmd == "asic_power_off":
            _petsys_append("cmd", "→ Turning ASIC power OFF (BIAS then FEM)...")
            # If acquisition is running, stop it first — otherwise the next
            # cycle will re-power the ASICs automatically.
            if _acq_running:
                _petsys_append("warn", "Acquisition is running — stopping it so ASICs stay off.")
                _stop_acquire()
                time.sleep(3)
            _run_power_cmd("set_bias", ["--power", "off"])
            _petsys_append("info", "BIAS power OFF")
            time.sleep(0.5)
            _run_power_cmd("set_fem_power", ["--power", "off"])
            _asic_power_off()
            _petsys_append("ok", "✔ ASIC power OFF (FEM + BIAS)")

        elif cmd == "asic_power_cycle":
            _petsys_append("cmd", "→ Power cycling ASICs...")
            _run_power_cmd("set_bias", ["--power", "off"])
            _petsys_append("info", "BIAS OFF")
            time.sleep(0.5)
            _run_power_cmd("set_fem_power", ["--power", "off"])
            _asic_power_off()
            _petsys_append("info", "FEM OFF")
            _petsys_append("info", "Waiting 3 seconds...")
            time.sleep(3)
            if _run_power_cmd("set_fem_power", ["--power", "on"]):
                _petsys_append("info", "FEM ON")
                time.sleep(0.5)
                if _run_power_cmd("set_bias", ["--power", "on"]):
                    _asic_power_on()
                    _petsys_append("ok", "✔ ASIC power cycle complete — ASICs powered")
                else:
                    _petsys_append("fail", "✘ BIAS failed on power-up")
            else:
                _petsys_append("fail", "✘ FEM failed on power-up")

    elif cmd == "panic":
        _petsys_append("cmd", "🛑 PANIC — emergency shutdown sequence initiated")
        # 1. Stop acquisition
        if _acq_running:
            _petsys_append("info", "PANIC: stopping acquisition...")
            _stop_acquire()
            time.sleep(3)
        # 2. Turn off ASIC power (BIAS then FEM)
        if _daqd_is_running():
            _petsys_append("info", "PANIC: turning off ASIC power...")
            _run_power_cmd("set_bias", ["--power", "off"])
            time.sleep(0.5)
            _run_power_cmd("set_fem_power", ["--power", "off"])
            _asic_power_off()
            _petsys_append("info", "PANIC: ASIC power OFF")
            time.sleep(1)
        # 3. Stop daqd
        _petsys_append("info", "PANIC: stopping daqd...")
        with _daqd_lock:
            if _daqd_process is not None and _daqd_process.poll() is None:
                try:
                    _daqd_process.send_signal(signal.SIGINT)
                    for _ in range(10):
                        time.sleep(1)
                        if _daqd_process.poll() is not None:
                            break
                    if _daqd_process.poll() is None:
                        _daqd_process.kill()
                except Exception as e:
                    _petsys_append("warn", f"PANIC: error stopping daqd: {e}")
                _daqd_process = None
            sock = "/tmp/d.sock"
            if os.path.exists(sock):
                try:
                    os.remove(sock)
                except Exception:
                    pass
        _petsys_append("ok", "🛑 PANIC complete — acquisition, ASIC power, and daqd all OFF")

    else:
        _petsys_append("fail", f"Unknown command: {cmd}")


# ── Independent acquisition commands ────────────────────────────────────
_acq_running = False   # True while acquire_sipm_data loop is active
_acq_stop    = False   # set True to stop the acquisition loop
_acq_cycle   = 0
_acq_filename = ""     # current cycle filename for display
_acq_task    = ""      # current task: "temp_stable" | "calibrating" | "acquiring" | ""
_acq_lock    = threading.Lock()
_acq_proc    = None    # currently running subprocess — killed on stop


def _run_step(label: str, args: list, cwd: str, timeout: int = 3600) -> bool:
    """Run a subprocess, stream output to log in real time. Returns True on success."""
    global _acq_proc
    _petsys_append("cmd", f"→ {label}")
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            args, cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
            env=env,
        )
        # Auto-answer any Y/n prompts (e.g. data loss warning)
        try:
            proc.stdin.write("Y\n")
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass
        _acq_proc = proc

        import queue as _queue
        line_queue = _queue.Queue()

        def _reader():
            try:
                for line in proc.stdout:
                    line_queue.put(line)
            except Exception:
                pass
            finally:
                line_queue.put(None)   # sentinel — stdout closed

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        # Silence watchdog: allow full cycle_time + 5 min buffer for acquire_sipm_data.
        # For calibration steps use the passed timeout (3600s).
        silence_timeout = timeout
        last_output = time.time()

        while True:
            try:
                line = line_queue.get(timeout=5)   # check every 5s
            except _queue.Empty:
                # No output in 5s — check watchdog
                silent_for = time.time() - last_output
                if silent_for > silence_timeout:
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: proc.kill()
                    _acq_proc = None
                    _petsys_append("fail",
                        f"✘ {label} — no output for {int(silent_for)}s, process killed.")
                    return False
                continue

            if _acq_stop:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill()
                _acq_proc = None
                _petsys_append("warn", "Stop requested — process terminated.")
                return False

            if line is None:
                break   # stdout closed — process finished

            last_output = time.time()
            line = line.rstrip()
            if not line:
                continue
            lo = line.lower()
            # Only flag as error if the line contains "error"/"fail"/"fault"
            # AND is not reporting a zero count (e.g. "0 errors", "error/s: 0")
            import re as _re
            is_zero_error = bool(_re.search(r'0\s+errors?|errors?[/\s]*s?:\s*0|error/s:\s*0', lo))
            kind = ("fail" if not is_zero_error and any(
                            w in lo for w in ("fail","error","fault","traceback"))
                    else "warn" if "warn" in lo and not is_zero_error
                    else "info")
            _petsys_append(kind, line)

        _acq_proc = None
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.returncode == 0:
            _petsys_append("ok", f"✔ {label} done (exit 0)")
            return True
        else:
            _petsys_append("fail", f"✘ {label} failed (exit {proc.returncode})")
            # Write to persistent log so exit code is never lost
            if _acq_log_path:
                try:
                    with open(_acq_log_path, "a") as f:
                        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
                                f"EXIT CODE {proc.returncode}: {label}\n")
                except Exception:
                    pass
            return False
    except Exception as e:
        _acq_proc = None
        _petsys_append("fail", f"✘ {label} error: {e}")
        return False


def _temp_stable_worker():
    global _acq_stop
    build = _build_dir()
    exe   = os.path.join(build, "read_temperature_sensors_JLG")
    _petsys_append("cmd", "── TEMPERATURE STABILISATION ──────────────────")
    _petsys_append("info", "Press ④ STOP ACQ to abort at any time.")
    _acq_stop = False
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            ["python3", "-u", exe, "--startup"],
            cwd=build,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=1, universal_newlines=True, env=env,
        )
        while True:
            line = proc.stdout.readline()
            if line == '' and proc.poll() is not None:
                break
            if _acq_stop:
                proc.terminate()
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired: proc.kill()
                _petsys_append("warn", "Temperature stabilisation stopped by user.")
                return
            line = line.rstrip()
            if not line:
                continue
            lo = line.lower()
            kind = ("ok"   if "stable" in lo
                    else "fail" if any(w in lo for w in ("error","traceback","fault"))
                    else "warn" if "warn" in lo
                    else "info")
            _petsys_append(kind, line)
        proc.wait(timeout=10)
        if proc.returncode == 0:
            _petsys_append("ok", "✔ Temperature stabilisation complete.")
        else:
            _petsys_append("fail", f"✘ read_temperature_sensors_JLG exited ({proc.returncode})")
    except Exception as e:
        _petsys_append("fail", f"✘ Temperature stabilisation error: {e}")


def _get_thresholds(pcfg: dict, config_file: str = None) -> tuple:
    """Return (vth_t1, vth_t2, vth_e) from config_profiles if available,
    falling back to explicit vth_* values. Defaults to 50,0,0 for config_muons.ini."""
    cfg_name = config_file or pcfg.get("config_file", "config_muons.ini")
    profiles = pcfg.get("config_profiles", {})
    if cfg_name in profiles:
        p = profiles[cfg_name]
        return p.get("vth_t1", 50), p.get("vth_t2", 0), p.get("vth_e", 0)
    return pcfg.get("vth_t1", 50), pcfg.get("vth_t2", 0), pcfg.get("vth_e", 0)


def _calibrate_worker():
    pcfg   = config.get("petsys", {})
    build  = _build_dir()
    data   = pcfg.get("data_dir", "")
    ini_name = pcfg.get("config_file", "config_muons.ini")
    ini    = os.path.join(data, ini_name)
    vth_t1, vth_t2, vth_e = _get_thresholds(pcfg, ini_name)

    _petsys_append("cmd", "── CALIBRATION ──────────────────────────────")
    _petsys_append("info", f"Config:     {ini}")
    _petsys_append("info", f"Thresholds: vth_t1={vth_t1}  vth_t2={vth_t2}  vth_e={vth_e}")

    steps = [
        # Step 0: Generate bias calibration table (maps are user-provided, not regenerated)
        ("make_bias_calibration_table",
         [os.path.join(build, "make_bias_calibration_table"),
          "-o", os.path.join(data, "bias_calibration.tsv")]),
        # Step 2: Threshold calibration
        ("acquire_threshold_calibration",
         ["python3", "-u", os.path.join(build, "acquire_threshold_calibration"),
          "--config", ini, "-o", os.path.join(data, "disc_calibration")]),
        ("process_threshold_calibration",
         ["python3", "-u", os.path.join(build, "process_threshold_calibration"),
          "--config", ini,
          "-i", os.path.join(data, "disc_calibration"),
          "-o", os.path.join(data, "disc_calibration")]),
        ("make_simple_disc_settings_table",
         ["python3", "-u", os.path.join(build, "make_simple_disc_settings_table"),
          "--config", ini,
          f"--vth_t1={vth_t1}", f"--vth_t2={vth_t2}", f"--vth_e={vth_e}",
          "-o", os.path.join(data, "disc_settings.tsv")]),
        # Step 3: TDC calibration
        ("acquire_tdc_calibration",
         ["python3", "-u", os.path.join(build, "acquire_tdc_calibration"),
          "--config", ini, "-o", os.path.join(data, "tdc_calibration")]),
        ("process_tdc_calibration",
         [os.path.join(build, "process_tdc_calibration"),
          "--config", ini,
          "-i", os.path.join(data, "tdc_calibration"),
          "-o", os.path.join(data, "tdc_calibration")]),
        # Step 4: QDC calibration
        ("acquire_qdc_calibration",
         ["python3", "-u", os.path.join(build, "acquire_qdc_calibration"),
          "--config", ini, "-o", os.path.join(data, "qdc_calibration")]),
        ("process_qdc_calibration",
         [os.path.join(build, "process_qdc_calibration"),
          "--config", ini,
          "-i", os.path.join(data, "qdc_calibration"),
          "-o", os.path.join(data, "qdc_calibration")]),
    ]

    for label, args in steps:
        if _acq_stop:
            _petsys_append("warn", "Calibration stopped by user.")
            return
        if not _run_step(label, args, cwd=build, timeout=3600):
            _petsys_append("fail", "✘ Calibration aborted.")
            return

    _petsys_append("ok", "✔ All calibrations complete.")

    # Write calib_params.json so future runs can verify thresholds
    try:
        import json as _json
        params = {"vth_t1": vth_t1, "vth_t2": vth_t2, "vth_e": vth_e,
                  "config_file": ini_name, "created": datetime.now().isoformat()}
        with open(os.path.join(data, "calib_params.json"), "w") as f:
            _json.dump(params, f, indent=2)
    except Exception as e:
        _petsys_append("warn", f"Could not write calib_params.json: {e}")


# ── Calibration folder management ───────────────────────────────────────

def _get_avg_asic_temp() -> float | None:
    """Return the current average A1 ASIC temperature from live_data, or None."""
    with live_lock:
        snap = dict(live_data)
    vals = []
    for d in snap.values():
        if d.get("type") == "A1Temp" and d.get("status") == "ok":
            vals += [v for k, v in d.items() if k.startswith("asic_")]
    return round(sum(vals) / len(vals), 1) if vals else None


def _get_max_asic_temp() -> float | None:
    """Return the current maximum A1 ASIC temperature from live_data, or None."""
    with live_lock:
        snap = dict(live_data)
    vals = []
    for d in snap.values():
        if d.get("type") == "A1Temp" and d.get("status") == "ok":
            vals += [v for k, v in d.items() if k.startswith("asic_")]
    return round(max(vals), 1) if vals else None


def _calib_temp_target(avg_temp: float, step: int = 3) -> int:
    """Round temperature to nearest calibration step (e.g. 44.7 → 45 for step=3: 42,45,48)."""
    return round(avg_temp / step) * step


def _find_calib_folders(calib_dir: str) -> dict:
    """Return dict of {temp_int: folder_path} for all calXX folders found."""
    import re as _re
    result = {}
    try:
        for entry in os.scandir(calib_dir):
            if entry.is_dir():
                m = _re.match(r'^cal(\d+)$', entry.name)
                if m:
                    result[int(m.group(1))] = entry.path
    except Exception:
        pass
    return result


def _nearest_calib(avg_temp: float, calib_dir: str) -> tuple[int | None, str | None]:
    """Return (temp, path) of the nearest available calibration folder, or (None, None)."""
    folders = _find_calib_folders(calib_dir)
    if not folders:
        return None, None
    nearest_temp = min(folders.keys(), key=lambda t: abs(t - avg_temp))
    return nearest_temp, folders[nearest_temp]


def _run_auto_calibration(target_temp: int, calib_dir: str,
                          data_dir: str, build: str, ini_name: str) -> str | None:
    """Run a full 7-step calibration into a new calXX folder.
    Returns the folder path on success, None on failure."""
    folder = os.path.join(calib_dir, f"cal{target_temp}")
    os.makedirs(folder, exist_ok=True)
    _petsys_append("cmd", f"── AUTO CALIBRATION → {folder} ─────────────")

    # Copy static files from data_dir to the new folder (always overwrite)
    static_files = [
        "bias_calibration.tsv", "bias_settings.tsv",
        "map_channel.tsv", "map_trigger.tsv",
        "disc_settings.tsv",
        "acquisition_mode_settings.tsv",
        "hwtrigger_empirical_calibration.tsv",
        ini_name,
    ]
    for f in static_files:
        src = os.path.join(data_dir, f)
        dst = os.path.join(folder, f)
        if os.path.isfile(src):
            try:
                import shutil
                shutil.copy2(src, dst)
            except Exception as e:
                _petsys_append("warn", f"Could not copy {f}: {e}")

    ini = os.path.join(folder, ini_name)
    vth_t1, vth_t2, vth_e = _get_thresholds(config.get('petsys', {}), ini_name)
    steps = [
        # Static files (maps, bias) are already copied above — only run temperature-dependent steps
        # Threshold calibration
        ("acquire_threshold_calibration",
         ["python3", "-u", os.path.join(build, "acquire_threshold_calibration"),
          "--config", ini, "-o", os.path.join(folder, "disc_calibration")]),
        ("process_threshold_calibration",
         ["python3", "-u", os.path.join(build, "process_threshold_calibration"),
          "--config", ini,
          "-i", os.path.join(folder, "disc_calibration"),
          "-o", os.path.join(folder, "disc_calibration")]),
        ("make_simple_disc_settings_table",
         ["python3", "-u", os.path.join(build, "make_simple_disc_settings_table"),
          "--config", ini,
          f"--vth_t1={vth_t1}", f"--vth_t2={vth_t2}", f"--vth_e={vth_e}",
          "-o", os.path.join(folder, "disc_settings.tsv")]),
        # TDC calibration
        ("acquire_tdc_calibration",
         ["python3", "-u", os.path.join(build, "acquire_tdc_calibration"),
          "--config", ini, "-o", os.path.join(folder, "tdc_calibration")]),
        ("process_tdc_calibration",
         [os.path.join(build, "process_tdc_calibration"),
          "--config", ini,
          "-i", os.path.join(folder, "tdc_calibration"),
          "-o", os.path.join(folder, "tdc_calibration")]),
        # QDC calibration
        ("acquire_qdc_calibration",
         ["python3", "-u", os.path.join(build, "acquire_qdc_calibration"),
          "--config", ini, "-o", os.path.join(folder, "qdc_calibration")]),
        ("process_qdc_calibration",
         [os.path.join(build, "process_qdc_calibration"),
          "--config", ini,
          "-i", os.path.join(folder, "qdc_calibration"),
          "-o", os.path.join(folder, "qdc_calibration")]),
    ]

    for label, args in steps:
        if _acq_stop:
            return None
        if not _run_step(label, args, cwd=build, timeout=3600):
            _petsys_append("fail", f"✘ Auto-calibration failed at {label}")
            return None

    _petsys_append("ok", f"✔ Auto-calibration complete → cal{target_temp}")

    # Write calib_params.json so future runs can verify thresholds
    vth_t1, vth_t2, vth_e = _get_thresholds(config.get("petsys", {}), ini_name)
    try:
        import json as _json
        params = {"vth_t1": vth_t1, "vth_t2": vth_t2, "vth_e": vth_e,
                  "config_file": ini_name, "created": datetime.now().isoformat()}
        with open(os.path.join(folder, "calib_params.json"), "w") as f:
            _json.dump(params, f, indent=2)
    except Exception as e:
        _petsys_append("warn", f"Could not write calib_params.json: {e}")

    return folder


# ── Acquisition state ────────────────────────────────────────────────────
_acq_calib_name = ""   # currently active calibration folder name

# ── Disk space history ───────────────────────────────────────────────────
_disk_history  = []    # list of (timestamp_iso, free_pct)
_disk_alarms_triggered = set()
# Consecutive alarm poll counts per sensor — used for sustained alarm logic
# Format: {sid: {param: count}}
_alarm_counts = {}
DISK_ALARM_THRESHOLDS = [40, 30, 20, 10, 5]   # % free remaining


def _cycle_json_path(run_dir, run_num, cycle_str, base_filename=None):
    """Return path for the cycle JSON file, named after the data file."""
    if base_filename:
        name = os.path.basename(base_filename)  # e.g. KNVA-20260518-03-00001
    else:
        name = f"cycle_{cycle_str}"
    return os.path.join(run_dir, f"{name}.json")

def _write_cycle_json(path, data):
    """Write cycle JSON atomically."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)

def _count_coinc_events(dat_file):
    """Count coincident events (lines with exactly 2 fields) using awk in subprocess."""
    try:
        r = subprocess.run(
            ["awk", "NF==2{c++}END{print c+0}", dat_file],
            capture_output=True, text=True, timeout=30
        )
        return int(r.stdout.strip()) if r.returncode == 0 else -1
    except Exception:
        return -1


def _acquire_worker():
    global _acq_running, _acq_stop, _acq_cycle, _acq_filename, _acq_log_path, _acq_calib_name, _acq_calib_name

    pcfg       = config.get("petsys", {})
    build      = _build_dir()
    data       = pcfg.get("data_dir", "")
    calib_dir  = pcfg.get("calib_dir", data)
    ini_name   = pcfg.get("config_file", "config_muons.ini")
    calib_mode = pcfg.get("calib_mode", "off")
    calib_step = pcfg.get("calib_step_deg", 3)
    vth_t1, vth_t2, vth_e = _get_thresholds(pcfg, ini_name)
    cycle_time = pcfg.get("cycle_time_s", 600)
    output_fmt = pcfg.get("output_format", "textCompact")

    if output_fmt == "binaryCompact":
        fmt_args = ["--output-format", "binaryCompact", "--timeref", "wall"]
    else:
        fmt_args = ["--output-format", "textCompact"]
        output_fmt = "textCompact"

    # Determine initial config file
    def _resolve_ini():
        """Pick the best available calibration ini for current temperature."""
        global _acq_calib_name
        if calib_mode == "off":
            with _acq_lock:
                _acq_calib_name = ""
            return os.path.join(data, ini_name)

        avg_temp = _get_avg_asic_temp()
        if avg_temp is None:
            _petsys_append("warn", "ASIC temp unavailable — using default config")
            with _acq_lock:
                _acq_calib_name = ""
            return os.path.join(data, ini_name)

        target = _calib_temp_target(avg_temp, calib_step)
        folders = _find_calib_folders(calib_dir)

        if target in folders:
            cal_path = folders[target]
            # Validate thresholds match expected values
            params_file = os.path.join(cal_path, "calib_params.json")
            if os.path.isfile(params_file):
                try:
                    import json as _json
                    saved = _json.load(open(params_file))
                    exp_t1, exp_t2, exp_e = _get_thresholds(pcfg, ini_name)
                    if (saved.get("vth_t1") != exp_t1 or
                        saved.get("vth_t2") != exp_t2 or
                        saved.get("vth_e")  != exp_e):
                        _petsys_append("warn",
                            f"⚠ cal{target} thresholds mismatch! "
                            f"Saved: {saved.get('vth_t1')},{saved.get('vth_t2')},{saved.get('vth_e')} "
                            f"Expected: {exp_t1},{exp_t2},{exp_e} — skipping this folder")
                        _acq_log(f"THRESHOLD MISMATCH in cal{target} — skipping")
                        # Don't use this calibration
                        del folders[target]
                        cal_temp, cal_path = _nearest_calib(avg_temp, calib_dir)
                        if cal_path:
                            with _acq_lock:
                                _acq_calib_name = f"cal{cal_temp}"
                            return os.path.join(cal_path, ini_name)
                        return os.path.join(data, ini_name)
                except Exception:
                    pass
            with _acq_lock:
                _acq_calib_name = f"cal{target}"
            return os.path.join(cal_path, ini_name)

        # Target not found
        if calib_mode == "auto":
            # Run auto-calibration for this temperature
            _petsys_append("warn",
                f"No cal{target} found — running auto-calibration at {avg_temp:.1f}°C")
            new_folder = _run_auto_calibration(
                target, calib_dir, data, build, ini_name)
            if new_folder:
                with _acq_lock:
                    _acq_calib_name = f"cal{target}"
                return os.path.join(new_folder, ini_name)
            else:
                _petsys_append("warn", "Auto-calibration failed — using nearest")

        # Fall back to nearest existing
        cal_temp, cal_path = _nearest_calib(avg_temp, calib_dir)
        if cal_path:
            with _acq_lock:
                _acq_calib_name = f"cal{cal_temp}"
            _petsys_append("warn",
                f"⚠ Using nearest calibration cal{cal_temp} "
                f"(target was cal{target}, ASIC avg {avg_temp:.1f}°C)")
            return os.path.join(cal_path, ini_name)

        _petsys_append("warn", "No calibration folders found — using default config")
        with _acq_lock:
            _acq_calib_name = ""
        return os.path.join(data, ini_name)

    ini = _resolve_ini()

    # Always use disc_settings.tsv from the base data directory —
    # thresholds are a run parameter, not temperature-dependent.
    ini_dir = os.path.dirname(ini)
    if ini_dir != data:
        src = os.path.join(data, "disc_settings.tsv")
        dst = os.path.join(ini_dir, "disc_settings.tsv")
        _petsys_append("info", f"Copying disc_settings: {src} → {dst}")
        if os.path.isfile(src):
            try:
                import shutil
                shutil.copy2(src, dst)
                _petsys_append("ok", f"✔ disc_settings.tsv copied to {os.path.basename(ini_dir)}")
            except Exception as e:
                _petsys_append("warn", f"Failed to copy disc_settings: {e}")
        else:
            _petsys_append("warn", f"disc_settings.tsv not found in base data dir: {src}")
    else:
        _petsys_append("info", "Using disc_settings.tsv from base data dir (no cal folder)")

    # Generate base filename — try make_filename.sh first, fall back to Python
    try:
        make_fn = os.path.join(build, "make_filename.sh")
        result  = subprocess.run(["bash", make_fn, data],
                                 capture_output=True, text=True, cwd=data)
        base_filename = result.stdout.strip()
        if not base_filename:
            raise ValueError("empty output from make_filename.sh")
        if not os.path.isabs(base_filename):
            base_filename = os.path.join(data, base_filename)
        # Extract run number from filename for log naming
        run_num = int(os.path.basename(base_filename).split("-")[2]) \
                  if len(os.path.basename(base_filename).split("-")) > 2 else 0
    except Exception as e:
        _petsys_append("warn", f"make_filename.sh failed ({e}) — generating filename.")
        sys_id = config.get("system", {}).get("id", "01")
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"KNVA-{sys_id}-{today}-"
        import glob as _glob
        existing = _glob.glob(os.path.join(data, f"{prefix}*"))
        nums = []
        for p in existing:
            bn = os.path.basename(p)
            parts = bn.replace(prefix, "").split("-")
            try: nums.append(int(parts[0]))
            except (ValueError, IndexError): pass
        run_num = (max(nums) + 1) if nums else 1
        base_filename = os.path.join(data, f"{prefix}{run_num:02d}")

    _petsys_append("ok", f"Base filename: {base_filename}")
    _acq_cycle = 0

    # All cycle JSON files and logs go directly in the data folder
    # All log files go in data/log/
    log_dir = os.path.join(data, "log")
    os.makedirs(log_dir, exist_ok=True)

    global _err_log_path
    _err_log_path   = os.path.join(log_dir, f"kodaq_errors_run{run_num:02d}.log")
    rclone_log_path = os.path.join(log_dir, f"rclone_run{run_num:02d}.log")

    # Open persistent acquisition log — named with run number
    _acq_log_path = os.path.join(log_dir,
        f"kodaq_acq_{datetime.now().strftime('%Y%m%d')}_run{run_num:02d}.log")
    try:
        with open(_acq_log_path, "w") as f:
            f.write(f"KoDAQ Acquisition Log — {datetime.now()}\n")
            f.write(f"Run:        {run_num:02d}\n")
            f.write(f"Config:     {ini}\n")
            f.write(f"Calib mode: {calib_mode}\n")
            f.write(f"Cycle time: {cycle_time}s  vth_t1={vth_t1} vth_t2={vth_t2} vth_e={vth_e}\n")
            f.write("-" * 60 + "\n")
        _petsys_append("ok", f"Persistent log: {_acq_log_path}")
    except Exception as e:
        _petsys_append("warn", f"Could not open persistent log: {e}")
        _acq_log_path = ""

    with _acq_lock:
        _acq_running = True
        _acq_filename = ""
        _acq_task = "acquiring"

    _petsys_append("ok", f"Acquisition loop started.")

    MAX_RETRIES = 3
    while not _acq_stop:
        # ── Thermal protection: check ASIC temperature before each cycle ──
        thermal_cfg = config.get("petsys", {}).get("thermal", {})
        max_asic_limit = thermal_cfg.get("max_asic_temp", None)
        if max_asic_limit is not None:
            max_temp = _get_max_asic_temp()
            if max_temp is not None and max_temp >= max_asic_limit:
                _acq_log(f"THERMAL SHUTDOWN — ASIC temp {max_temp}°C ≥ limit {max_asic_limit}°C")
                _petsys_append("fail",
                    f"⚠ THERMAL SHUTDOWN — ASIC temp {max_temp}°C reached limit "
                    f"{max_asic_limit}°C. Executing panic shutdown.")
                if emailer and emailer.enabled:
                    try:
                        emailer._send_email({"__thermal__": {
                            "label": "ASIC Thermal", "type": "ThermalShutdown",
                            "alarm": [f"ASIC temperature reached {max_temp}°C "
                                      f"(limit {max_asic_limit}°C). Panic shutdown executed: "
                                      f"acquisition, ASIC power, and daqd all turned off."],
                            "timestamp": datetime.now().isoformat(),
                        }})
                    except Exception:
                        pass
                # Full panic shutdown
                _run_petsys_cmd("panic")
                break

        _acq_cycle += 1
        cycle_str     = f"{_acq_cycle:05d}"
        this_filename = f"{base_filename}-{cycle_str}"
        with _acq_lock:
            _acq_filename = os.path.basename(this_filename)
        _petsys_append("cmd", f"── Cycle {cycle_str} → {os.path.basename(this_filename)}")

        # Log system stats at start of each cycle
        try:
            import shutil as _shutil
            mem_free = next((int(l.split()[1]) for l in
                             open("/proc/meminfo").read().splitlines()
                             if l.startswith("MemAvailable")), 0)
            disk     = _shutil.disk_usage(data)
            disk_free_gb  = disk.free  / 1024**3
            disk_total_gb = disk.total / 1024**3
            disk_free_pct = 100.0 * disk.free / disk.total
            load = os.getloadavg()
            _acq_log(f"RUN {run_num:02d} CYCLE {cycle_str} START")
            _acq_log(f"  RAM available (MB)           : {mem_free//1024}")
            _acq_log(f"  Disk free (GB)               : {disk_free_gb:.1f}")
            _acq_log(f"  Disk free (%)                : {disk_free_pct:.1f}")
            _acq_log(f"  Load (1,5,15 min)            : {load[0]:.1f}, {load[1]:.1f}, {load[2]:.1f}")

            # Record disk history point
            _disk_history.append((datetime.now().isoformat(),
                                   round(disk_free_pct, 2),
                                   round(disk_free_gb, 2)))
            if len(_disk_history) > 10000:
                del _disk_history[:len(_disk_history) - 10000]

            # Disk space alarms (fire once per threshold crossing)
            for thresh in DISK_ALARM_THRESHOLDS:
                if disk_free_pct <= thresh and thresh not in _disk_alarms_triggered:
                    _disk_alarms_triggered.add(thresh)
                    msg = (f"⚠ DISK SPACE ALARM: only {disk_free_pct:.1f}% free "
                           f"({disk_free_gb:.1f}GB of {disk_total_gb:.0f}GB)")
                    _petsys_append("fail", msg)
                    _acq_log(f"DISK ALARM {thresh}%: {disk_free_gb:.1f}GB free of {disk_total_gb:.0f}GB")
                elif disk_free_pct > thresh and thresh in _disk_alarms_triggered:
                    # Reset if space is freed (e.g. files deleted)
                    _disk_alarms_triggered.discard(thresh)
        except Exception as e:
            _acq_log(f"RUN {run_num:02d} CYCLE {cycle_str} START — stats error: {e}")

        # Run communication counters check — warns if any ASIC link errors detected
        try:
            comms_exe = os.path.join(build, "read_communication_counters")
            if os.path.isfile(comms_exe):
                comms_result = subprocess.run(
                    [comms_exe], cwd=build,
                    capture_output=True, text=True, timeout=15,
                )
                comms_output = comms_result.stdout + comms_result.stderr
                # Parse for any non-zero error counts
                import re as _re
                error_lines = []
                for line in comms_output.splitlines():
                    # Match lines like "0 transmitted  0 received (3 errors)"
                    m = _re.search(r'\((\d+)\s+errors?\)', line)
                    if m and int(m.group(1)) > 0:
                        error_lines.append(line.strip())
                if error_lines:
                    _petsys_append("warn",
                        f"⚠ COMMS errors detected at cycle {cycle_str}:")
                    for el in error_lines:
                        _petsys_append("warn", f"  {el}")
                    _acq_log(f"CYCLE {cycle_str} COMMS ERRORS: {'; '.join(error_lines)}")
                else:
                    _acq_log(f"  COMMS link errors            : none")
        except Exception as e:
            _acq_log(f"CYCLE {cycle_str} COMMS check failed: {e}")

        # Log a human-readable sensor summary for this cycle
        try:
            with live_lock:
                snap = dict(live_data)
            temps, hums, presses, asics, tilts, mags, linux = [], [], [], [], [], [], []
            for sid, d in snap.items():
                if not isinstance(d, dict) or d.get("status") != "ok":
                    continue
                t = d.get("type", "")
                if t == "BME280":
                    if d.get("temp") is not None: temps.append(f"{d['temp']:.1f}")
                    if d.get("hum")  is not None: hums.append(f"{d['hum']:.1f}")
                    if d.get("press")is not None: presses.append(f"{d['press']:.2f}")
                elif t == "HDC302x":
                    if d.get("temp") is not None: temps.append(f"{d['temp']:.1f}")
                    if d.get("hum")  is not None: hums.append(f"{d['hum']:.1f}")
                elif t == "A1Temp":
                    for k in sorted(k for k in d if k.startswith("asic_")):
                        asics.append(f"{d[k]:.1f}")
                elif t == "HWT905":
                    if d.get("tilt_x") is not None:
                        tilts.append(f"{d['tilt_x']:.2f}, {d['tilt_y']:.2f}")
                    if d.get("mag_x") is not None:
                        mags.append(f"{d['mag_x']:.0f}, {d['mag_y']:.0f}, {d['mag_z']:.0f}")
                elif t == "LinuxTemp":
                    zone_names = []
                    zone_vals  = []
                    for k, v in d.items():
                        if k not in ("label","type","status","timestamp","alarm"):
                            # Shorten common zone names for readability
                            short = k.replace("x86_pkg_temp","x86") \
                                     .replace("iwlwifi_1","wifi") \
                                     .replace("iwlwifi","wifi") \
                                     .replace("_"," ")
                            zone_names.append(short)
                            zone_vals.append(f"{v:.1f}")
                    if zone_names:
                        linux.append(("(" + ",".join(zone_names) + ")", ", ".join(zone_vals)))
            if temps:    _acq_log(f"  Temp (°C)                    : {', '.join(temps)}")
            if hums:     _acq_log(f"  Humidity (%)                 : {', '.join(hums)}")
            if presses:  _acq_log(f"  Pressure (kPa)               : {', '.join(presses)}")
            if asics:    _acq_log(f"  ASIC temp (1,2,3,4) °C       : {', '.join(asics)}")
            if tilts:    _acq_log(f"  Tilt (X,Y) °                 : {tilts[0]}")
            if mags:     _acq_log(f"  Mag field (X,Y,Z) µT         : {mags[0]}")
            for names, vals in linux:
                _acq_log(f"  Linux CPU {names} °C        : {vals}")
        except Exception as e:
            _acq_log(f"CYCLE {cycle_str} sensor summary error: {e}")

        # ── Initialise cycle JSON file ───────────────────────────────────
        cycle_start_time = datetime.now()
        cycle_json = {
            "run":          f"{run_num:02d}",
            "cycle":        cycle_str,
            "data_file":    os.path.basename(this_filename) + "_coinc.dat",
            "calibration":  _acq_calib_name or "default",
            "config_file":  os.path.basename(ini),
            "calib_mode":   calib_mode,
            "cycle_time_s": cycle_time,
            "vth_t1": vth_t1, "vth_t2": vth_t2, "vth_e": vth_e,
            "cycle_start":  cycle_start_time.isoformat(timespec="seconds"),
            "cycle_end":    None,
            "elapsed_s":    None,
            "coincident_events": None,
            "data_file_size_MB": None,
            "system": {
                "ram_available_MB": mem_free // 1024 if "mem_free" in dir() else None,
                "disk_free_GB":     round(disk_free_gb, 1) if "disk_free_gb" in dir() else None,
                "disk_free_pct":    round(disk_free_pct, 1) if "disk_free_pct" in dir() else None,
                "load_1min":  round(load[0], 1) if "load" in dir() else None,
                "load_5min":  round(load[1], 1) if "load" in dir() else None,
                "load_15min": round(load[2], 1) if "load" in dir() else None,
            },
            "slow_control": {
                "temp_C":       [float(v) for v in temps]    if temps    else [],
                "humidity_pct": [float(v) for v in hums]     if hums     else [],
                "pressure_kPa": [float(v) for v in presses]  if presses  else [],
                "asic_temp_C":  [float(v) for v in asics]    if asics    else [],
                "tilt_deg":     [float(v) for v in tilts[0].split(", ")] if tilts else [],
                "mag_uT":       [float(v) for v in mags[0].split(", ")]  if mags  else [],
                "linux_cpu_C":  [float(v) for _, v in linux] if linux else [],
            },
            "comms_errors": "none",
            "gdrive_transfer": None,
        }
        cjson_path = _cycle_json_path(data, run_num, cycle_str, this_filename)
        try:
            _write_cycle_json(cjson_path, cycle_json)
        except Exception as e:
            _acq_log(f"CYCLE {cycle_str} JSON write error: {e}")
        for attempt in range(1, MAX_RETRIES + 1):
            if _acq_stop:
                break
            if attempt > 1:
                _petsys_append("warn",
                    f"Retry {attempt}/{MAX_RETRIES} for cycle {cycle_str}...")
                _acq_log(f"CYCLE {cycle_str} RETRY {attempt}/{MAX_RETRIES}")
                # On retry, attempt a full DAQ restart — may recover a lost ASIC link
                _petsys_append("warn", "Attempting DAQ restart to recover ASIC links...")
                _acq_log(f"CYCLE {cycle_str} — restarting daqd before retry {attempt}")
                try:
                    # Stop daqd
                    global _daqd_process
                    with _daqd_lock:
                        if _daqd_process is not None and _daqd_process.poll() is None:
                            _daqd_process.send_signal(signal.SIGINT)
                            for _ in range(10):
                                time.sleep(1)
                                if _daqd_process.poll() is not None:
                                    break
                            if _daqd_process.poll() is None:
                                _daqd_process.kill()
                            _daqd_process = None
                        # Remove stale socket
                        sock = "/tmp/d.sock"
                        if os.path.exists(sock):
                            os.remove(sock)
                        # Restart daqd
                        exe = os.path.join(build, "daqd")
                        _daqd_process = subprocess.Popen(
                            [exe, "--socket-name=/tmp/d.sock", "--daq-type=GBE"],
                            cwd=build,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                        )
                        def _drain2(proc):
                            try:
                                for _ in proc.stdout: pass
                            except Exception: pass
                        threading.Thread(target=_drain2, args=(_daqd_process,),
                                        daemon=True).start()
                        time.sleep(5)   # give daqd time to initialise
                        if _daqd_process.poll() is not None:
                            _petsys_append("fail", "daqd failed to restart.")
                            _acq_log(f"CYCLE {cycle_str} — daqd restart failed")
                        else:
                            _petsys_append("ok",
                                f"daqd restarted (PID {_daqd_process.pid})")
                            _acq_log(f"CYCLE {cycle_str} — daqd restarted OK")
                except Exception as e:
                    _petsys_append("warn", f"DAQ restart error: {e}")
                time.sleep(5)

            ok = _run_step(
                f"acquire_sipm_data cycle {cycle_str}",
                [os.path.join(build, "acquire_sipm_data"),
                 "--enable-realtime-processing",
                 "--output-type", "coincidences",
                 "--writeMultipleHits", "64",
                 "--config", ini,
                 "--mode", "qdc",
                 "--time", str(cycle_time),
                 "-o", this_filename]
                + fmt_args,
                cwd=data, timeout=cycle_time + 300,  # cycle time + 5 min buffer
            )
            if ok or _acq_stop:
                break

        if not ok and not _acq_stop:
            _acq_log(f"CYCLE {cycle_str} FAILED after {MAX_RETRIES} attempts — stopping.")
            _petsys_append("fail",
                f"✘ Cycle {cycle_str} failed after {MAX_RETRIES} attempts — stopping.")
            break

        if not _acq_stop:
            _acq_log(f"CYCLE {cycle_str} COMPLETE")
            cycle_end_time = datetime.now()

            # Check size of completed file and count coincident events
            FILE_SIZE_ALARM_MB = 50
            coinc_file = None
            size_mb = 0.0
            for ext in ("_coinc.dat", ".dat", ""):
                fpath = this_filename + ext
                if os.path.isfile(fpath):
                    size_mb = os.path.getsize(fpath) / 1024**2
                    coinc_file = fpath
                    _acq_log(f"  Data file size (MB)          : {size_mb:.1f}")
                    if size_mb > FILE_SIZE_ALARM_MB:
                        msg = (f"⚠ FILE SIZE ALARM: {os.path.basename(fpath)} "
                               f"is {size_mb:.0f}MB (limit {FILE_SIZE_ALARM_MB}MB)")
                        _petsys_append("warn", msg)
                        with live_lock:
                            if "__system__" in live_data:
                                existing = live_data["__system__"].get("alarm", [])
                                if "file_size" not in str(existing):
                                    live_data["__system__"]["alarm"] = existing + [f"file>{FILE_SIZE_ALARM_MB}MB"]
                    break

            # Count coincident events
            n_coinc = 0
            if coinc_file:
                n_coinc = _count_coinc_events(coinc_file)
                _acq_log(f"  Coincident events            : {n_coinc}")

            # Update cycle JSON with end-of-cycle data
            elapsed = (cycle_end_time - cycle_start_time).total_seconds()
            cycle_json["cycle_end"]          = cycle_end_time.isoformat(timespec="seconds")
            cycle_json["elapsed_s"]          = round(elapsed, 1)
            cycle_json["coincident_events"]  = n_coinc
            cycle_json["data_file_size_MB"]  = round(size_mb, 2)
            try:
                _write_cycle_json(cjson_path, cycle_json)
            except Exception as e:
                _acq_log(f"CYCLE {cycle_str} JSON update error: {e}")

            # Check if calibration should switch (On or Auto mode)
            if calib_mode != "off":
                avg_temp = _get_avg_asic_temp()
                if avg_temp is not None:
                    target = _calib_temp_target(avg_temp, calib_step)
                    with _acq_lock:
                        current = _acq_calib_name
                    if current != f"cal{target}":
                        _petsys_append("info",
                            f"Temperature {avg_temp:.1f}°C → switching "
                            f"from {current or 'default'} to cal{target}")
                        _acq_log(f"  Calibration used             : {_acq_calib_name or 'default'}")
                        new_ini = _resolve_ini()
                        if new_ini != ini:
                            ini = new_ini
                            # Copy disc_settings from base dir into new cal folder
                            new_dir = os.path.dirname(ini)
                            if new_dir != data:
                                src = os.path.join(data, "disc_settings.tsv")
                                dst = os.path.join(new_dir, "disc_settings.tsv")
                                if os.path.isfile(src):
                                    try:
                                        import shutil
                                        shutil.copy2(src, dst)
                                    except Exception:
                                        pass
                            _petsys_append("ok",
                                f"✔ Now using: {_acq_calib_name or 'default config'}")

            # ── IDrive transfer via AWS CLI ───────────────────────────────
            s3_endpoint = config.get("petsys", {}).get("s3_endpoint", "")
            s3_bucket   = config.get("petsys", {}).get("s3_bucket", "")
            s3_profile  = config.get("petsys", {}).get("s3_profile", "idrive")
            if s3_endpoint and s3_bucket:
                for ext in ("_coinc.dat", ".dat", ""):
                    fpath = this_filename + ext
                    if os.path.isfile(fpath):
                        def _s3_transfer(fp=fpath,
                                         endpoint=s3_endpoint,
                                         bucket=s3_bucket,
                                         profile=s3_profile,
                                         rlog=rclone_log_path,
                                         cjson=cjson_path,
                                         cjson_data=dict(cycle_json)):
                            fname = os.path.basename(fp)
                            cmd = [
                                "aws", "s3",
                                "--endpoint-url", endpoint,
                                "--profile", profile,
                                "cp", fp,
                                f"s3://{bucket}/{fname}",
                            ]
                            for attempt in range(1, 4):
                                try:
                                    r = subprocess.run(
                                        cmd, capture_output=True,
                                        text=True, timeout=120)
                                    if r.returncode == 0:
                                        _acq_log(f"  IDrive transfer OK           : {fname}")
                                        _petsys_append("ok", f"☁ IDrive ✔ {fname}")
                                        cjson_data["idrive_transfer"] = "ok"
                                        try:
                                            _write_cycle_json(cjson, cjson_data)
                                        except Exception:
                                            pass
                                        # Also transfer the cycle JSON file
                                        if os.path.isfile(cjson):
                                            try:
                                                jname = os.path.basename(cjson)
                                                subprocess.run([
                                                    "aws", "s3",
                                                    "--endpoint-url", endpoint,
                                                    "--profile", profile,
                                                    "cp", cjson,
                                                    f"s3://{bucket}/{jname}",
                                                ], capture_output=True, text=True, timeout=60)
                                            except Exception:
                                                pass
                                        return
                                    else:
                                        raise RuntimeError(r.stderr.strip()[:200])
                                except Exception as e:
                                    if attempt < 3:
                                        time.sleep(30)
                                    else:
                                        msg = f"⚠ IDrive transfer FAILED after 3 attempts: {fname} — {e}"
                                        _acq_log(msg)
                                        _petsys_append("warn", msg)
                                        cjson_data["idrive_transfer"] = f"failed: {e}"
                                        try:
                                            _write_cycle_json(cjson, cjson_data)
                                        except Exception:
                                            pass
                                        if emailer:
                                            try:
                                                emailer._send_email({"__gdrive__": {
                                                    "label": "IDrive Transfer",
                                                    "type": "Transfer",
                                                    "alarm": [msg],
                                                    "timestamp": datetime.now().isoformat(),
                                                }})
                                            except Exception:
                                                pass
                        threading.Thread(target=_s3_transfer, daemon=True).start()
                        break
                        
                        
            # ── KoBold S3 transfer via AWS CLI ────────────────────────────
            # Second destination, independent of the IDrive block above.
            # Real AWS, so no --endpoint-url. Sends the same two files the
            # IDrive block sends. A failure here cannot affect IDrive:
            # separate thread, separate status, separate alert cooldown.
            _pcfg      = config.get("petsys", {})
            kb_enabled = _pcfg.get("kobold_s3_enabled", False)
            kb_bucket  = _pcfg.get("kobold_s3_bucket", "")
            kb_prefix  = _pcfg.get("kobold_s3_prefix", "")
            kb_profile = _pcfg.get("kobold_s3_profile", "")
            kb_region  = _pcfg.get("kobold_s3_region", "")
            if kb_enabled and kb_bucket:
                for ext in ("_coinc.dat", ".dat", ""):
                    fpath = this_filename + ext
                    if os.path.isfile(fpath):
                        def _kb_transfer(fp=fpath,
                                         bucket=kb_bucket,
                                         prefix=kb_prefix,
                                         profile=kb_profile,
                                         region=kb_region,
                                         cjson=cjson_path):
                            failed = []

                            def _cp(src):
                                key = f"{prefix}{os.path.basename(src)}"
                                cmd = ["aws", "s3"]
                                if profile:
                                    cmd += ["--profile", profile]
                                if region:
                                    cmd += ["--region", region]
                                cmd += ["cp", src, f"s3://{bucket}/{key}",
                                        "--only-show-errors"]
                                r = subprocess.run(cmd, capture_output=True,
                                                   text=True, timeout=120)
                                if r.returncode != 0:
                                    raise RuntimeError(r.stderr.strip()[:200])
                                return key

                            def _cp_retry(src):
                                for attempt in range(1, 4):
                                    try:
                                        return _cp(src)
                                    except Exception as e:
                                        if attempt < 3:
                                            time.sleep(30)
                                        else:
                                            failed.append(
                                                f"{os.path.basename(src)} — {e}")
                                return None

                            key = _cp_retry(fp)
                            if key:
                                _acq_log(f"  KoBold S3 transfer OK        : {key}")
                                _petsys_append("ok",
                                               f"☁ KoBold S3 ✔ {os.path.basename(fp)}")
                                # Also transfer the cycle JSON file
                                if os.path.isfile(cjson):
                                    _cp_retry(cjson)

                            if failed:
                                msg = ("⚠ KoBold S3 transfer FAILED after 3 "
                                       "attempts: " + "; ".join(failed))
                                _acq_log(msg)
                                _err_log(msg)
                                _petsys_append("warn", msg)
                                if emailer:
                                    try:
                                        emailer.maybe_send({"__kbs3__": {
                                            "label":     "KoBold S3 Transfer",
                                            "type":      "Transfer",
                                            "status":    "error",
                                            "error":     msg,
                                            "alarm":     [msg],
                                            "timestamp": datetime.now().isoformat(),
                                        }})
                                    except Exception:
                                        pass
                        threading.Thread(target=_kb_transfer, daemon=True).start()
                        break                        

            # ── KoBold S3 config and calibration push ─────────────────────
            # Additive: nothing above is modified, nothing on disk is removed.
            # Sends the config set once per run, and a calXX folder the first
            # time a cycle reports using it. Markers in .kobold_s3/ stop a
            # restart re-sending 137 MB.
            #
            # aws s3 cp, not sync: sync compares against the destination and
            # needs ListBucket, which the write-only device key lacks.
            if kb_enabled and _pcfg.get("kobold_s3_config_enabled", False):
                def _kb_support(ddir=_pcfg.get("data_dir", ""),
                                cpfx=_pcfg.get("kobold_s3_config_prefix", ""),
                                cal=str((cycle_json or {}).get("calibration") or "").strip(),
                                run=run_num,
                                bucket=kb_bucket,
                                profile=kb_profile,
                                region=kb_region):
                    if not (ddir and cpfx and os.path.isdir(ddir)):
                        return
                    mdir = os.path.join(ddir, ".kobold_s3")
                    try:
                        os.makedirs(mdir, exist_ok=True)
                    except Exception:
                        return

                    def _send(marker, args, label):
                        mpath = os.path.join(mdir, marker)
                        if os.path.exists(mpath):
                            return
                        cmd = ["aws", "s3"]
                        if profile:
                            cmd += ["--profile", profile]
                        if region:
                            cmd += ["--region", region]
                        cmd += args + ["--only-show-errors"]
                        try:
                            r = subprocess.run(cmd, capture_output=True,
                                               text=True, timeout=1800)
                            if r.returncode == 0:
                                open(mpath, "w").close()
                                _acq_log(f"  KoBold S3 {label} OK")
                                _petsys_append("ok", f"☁ KoBold S3 ✔ {label}")
                                return
                            err = r.stderr.strip()[:200]
                        except Exception as e:
                            err = str(e)[:200]
                        msg = f"⚠ KoBold S3 {label} FAILED: {err}"
                        _acq_log(msg)
                        _err_log(msg)
                        _petsys_append("warn", msg)

                    # 1. The config in use now. Keyed on the run, so a change
                    #    between runs is picked up. Top level only: cal*/ is
                    #    handled below, and KNVA*.tsv is slow control.
                    _send(f"cfg_run{run:02d}",
                          ["cp", ddir, f"s3://{bucket}/{cpfx}", "--recursive",
                           "--exclude", "*",
                           "--include", "*.tsv", "--include", "*.ini",
                           "--include", "config.json",
                           "--exclude", "KNVA*.tsv",
                           "--exclude", "cal*/*",
                           "--exclude", "log/*",
                           "--exclude", ".kobold_s3/*"],
                          "config")

                    # 2. The calibration this cycle used. Written once and
                    #    never modified, so one permanent marker per folder
                    #    is correct. A folder no cycle uses is never sent.
                    if cal and os.path.isdir(os.path.join(ddir, cal)):
                        _send(f"cal_{cal}",
                              ["cp", os.path.join(ddir, cal),
                               f"s3://{bucket}/{cpfx}{cal}/", "--recursive"],
                              cal)

                threading.Thread(target=_kb_support, daemon=True).start()

            _petsys_append("info", "Pausing 10s between cycles...")
            for _ in range(10):
                if _acq_stop:
                    break
                time.sleep(1)

    with _acq_lock:
        _acq_running = False
        _acq_filename = ""
        _acq_task = ""

    if not _acq_stop:
        # Acquisition ended without user request — send alert
        msg = "⚠ Acquisition stopped unexpectedly — check the log."
        _petsys_append("fail", msg)
        _acq_log(msg)
        _err_log(msg)
        try:
            from datetime import datetime as _dt
            alert = {
                "__acq_alert__": {
                    "label": "Acquisition", "type": "AcqAlert",
                    "status": "error",
                    "error": msg,
                    "timestamp": _dt.now().isoformat(),
                    "alarm": ["acq_stopped_unexpectedly"],
                }
            }
            emailer.maybe_send(alert)
        except Exception:
            pass
    else:
        _petsys_append("warn", "Acquisition stopped by user.")

def _run_temp_stable():
    def _wrapper():
        global _acq_running, _acq_stop, _acq_task
        _acq_stop = False
        with _acq_lock:
            _acq_running = True
            _acq_task = "temp_stable"
        try:
            _temp_stable_worker()
        finally:
            with _acq_lock:
                _acq_running = False
                _acq_task = ""
    threading.Thread(target=_wrapper, daemon=True).start()

def _run_calibrate():
    def _wrapper():
        global _acq_running, _acq_stop, _acq_task
        _acq_stop = False
        with _acq_lock:
            _acq_running = True
            _acq_task = "calibrating"
        try:
            _calibrate_worker()
        finally:
            with _acq_lock:
                _acq_running = False
                _acq_task = ""
    threading.Thread(target=_wrapper, daemon=True).start()

def _run_acquire():
    global _acq_stop
    _acq_stop = False
    threading.Thread(target=_acquire_worker, daemon=True).start()

def _stop_acquire():
    global _acq_stop, _acq_proc
    _acq_stop = True
    _petsys_append("warn", "Stop requested — terminating current process...")
    if _acq_proc is not None:
        try:
            _acq_proc.terminate()
        except Exception:
            pass


def _sequence_worker(steps: list):
    """Run steps in order: any subset of ['temp_stable','calibrate','acquire'].
    Stops on failure or if _acq_stop is set."""
    global _acq_task
    _petsys_append("cmd",
        f"── SEQUENCE: {' → '.join(s.replace('_',' ').upper() for s in steps)} ──")

    for step in steps:
        if _acq_stop:
            _petsys_append("warn", "Sequence stopped by user.")
            return

        with _acq_lock:
            _acq_task = "temp_stable" if step == "temp_stable" else \
                        "calibrating" if step == "calibrate" else "acquiring"

        if step == "temp_stable":
            _temp_stable_worker()
            if _acq_stop:
                _petsys_append("warn", "Sequence stopped by user.")
                return

        elif step == "calibrate":
            _calibrate_worker()
            if _acq_stop:
                _petsys_append("warn", "Sequence stopped by user.")
                return
            with _petsys_lock:
                last = _petsys_log[-1]["text"] if _petsys_log else ""
            if "aborted" in last.lower() or "failed" in last.lower():
                _petsys_append("fail", "✘ Sequence stopped — calibration failed.")
                return

        elif step == "acquire":
            _acquire_worker()   # runs until stopped — sequence ends here
            return

    _petsys_append("ok", "✔ Sequence complete.")


def _run_sequence(steps: list):
    """Start a sequence in a background thread."""
    global _acq_stop
    _acq_stop = False

    def _wrapper():
        global _acq_running
        with _acq_lock:
            _acq_running = True
        try:
            _sequence_worker(steps)
        finally:
            with _acq_lock:
                global _acq_task
                _acq_running = False
                _acq_task = ""

    threading.Thread(target=_wrapper, daemon=True).start()


config_path = "config.json"   # set at startup, used by save_config

emailer = None   # initialised in main() after EmailAlerter is defined

def load_config(path="config.json"):
    global config_path
    config_path = os.path.abspath(path)
    with open(config_path) as f:
        cfg = json.load(f)
    # Substitute {user} and {id} placeholders throughout the config
    system = cfg.get("system", {})
    subs = {
        "user": system.get("user", "konova"),
        "id":   system.get("id", "01"),
    }
    def _apply_subs(obj):
        if isinstance(obj, str):
            for k, v in subs.items():
                obj = obj.replace("{" + k + "}", v)
            return obj
        elif isinstance(obj, dict):
            return {k: _apply_subs(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_apply_subs(v) for v in obj]
        return obj
    return _apply_subs(cfg)


def save_config(cfg, path=None):
    target = path or config_path
    # Write the config exactly as it is in memory — no substitution.
    # The user maintains {user}/{id} placeholders manually; the code never
    # adds or removes them. Only runtime sections (alarms, thresholds,
    # autostart, etc.) are ever modified, never the path fields.
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=4)
    os.replace(tmp, target)   # atomic on Linux — no partial write possible


# ── File-based history reader ───────────────────────────────────────────

def read_tsv_history(directory, minutes):
    """Read TSV log files and return history points within the time range."""
    cutoff = datetime.now() - timedelta(minutes=minutes)
    data_dir = Path(directory)
    if not data_dir.exists():
        return []

    # Determine prefix from config
    sys_id = config.get("system", {}).get("id", "01")
    prefix = f"KNVA{sys_id}"

    # Figure out which day files we need
    today = date.today()
    days_back = max((minutes // 1440) + 2, 2)
    needed_dates = set()
    for i in range(days_back):
        needed_dates.add((today - timedelta(days=i)).isoformat())

    # Collect matching TSV files — try current prefix and legacy 'konova_'
    tsv_files = []
    for pfx in (prefix, "konova"):
        for d in needed_dates:
            f = data_dir / f"{pfx}_{d}.tsv"
            if f.exists():
                tsv_files.append(f)
            for f2 in sorted(data_dir.glob(f"{pfx}_{d}_*.tsv")):
                suffix = f2.stem.replace(f"{pfx}_{d}_", "")
                if suffix.isdigit() and len(suffix) == 6:
                    tsv_files.append(f2)
    tsv_files = sorted(set(tsv_files))

    if not tsv_files:
        return []

    result = []
    for fpath in tsv_files:
        try:
            with open(fpath) as f:
                header = f.readline().strip().split("\t")
                if not header or header[0] != "timestamp":
                    continue
                # Parse column names → sensor_id + param
                # Build a set of known sensor IDs to split columns correctly
                known_sids = set(config.get("sensors", {}).keys())

                col_map = []  # list of (sensor_id, param)
                for col in header[1:]:
                    if col == "ram_MB":
                        col_map.append(("__sys__", "ram_used_MB"))
                        continue
                    matched = False
                    for sid in sorted(known_sids, key=len, reverse=True):
                        if col.startswith(sid + "_"):
                            param = col[len(sid) + 1:]
                            col_map.append((sid, param))
                            matched = True
                            break
                    if not matched:
                        parts = col.rsplit("_", 1)
                        if len(parts) == 2:
                            col_map.append((parts[0], parts[1]))
                        else:
                            col_map.append((col, "?"))

                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    fields = line.split("\t")
                    if len(fields) < 2:
                        continue
                    try:
                        ts = datetime.strptime(fields[0], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue

                    point = {}
                    for i, (sid, param) in enumerate(col_map):
                        if i + 1 >= len(fields):
                            break
                        try:
                            val = float(fields[i + 1])
                        except ValueError:
                            continue
                        if sid not in point:
                            point[sid] = {}
                        if sid == "__sys__" and param == "ram_MB":
                            point["__sys__"]["ram_MB"] = int(val)
                        elif param == "T":
                            point[sid]["temp"] = round(val, 2)
                        elif param == "H":
                            point[sid]["hum"] = round(val, 2)
                        elif param == "P":
                            point[sid]["press"] = round(val, 2)
                        # New IMU axis names
                        elif param == "tiltX":
                            point[sid]["tilt_x"] = round(val, 3)
                        elif param == "tiltY":
                            point[sid]["tilt_y"] = round(val, 3)
                        elif param == "magX":
                            point[sid]["mag_x"] = round(val, 2)
                        elif param == "magY":
                            point[sid]["mag_y"] = round(val, 2)
                        elif param == "magZ":
                            point[sid]["mag_z"] = round(val, 2)
                        # Old IMU axis names (backward compat)
                        elif param == "tiltZ":
                            point[sid]["tilt_x"] = round(-val, 3)  # remap
                        elif param == "mx":
                            point[sid]["mag_z"] = round(val, 2)    # remap
                        elif param == "my":
                            point[sid]["mag_y"] = round(val, 2)
                        elif param == "mz":
                            point[sid]["mag_x"] = round(-val, 2)   # remap
                        elif param.startswith("A") and param[1:].isdigit():
                            point[sid][f"asic_{param[1:]}"] = round(val, 2)
                        else:
                            point[sid][param] = round(val, 2)
                            stype = config.get("sensors", {}).get(sid, {}).get("type", "")
                            if stype == "LinuxTemp":
                                point[sid][param] = round(val, 1)
                    if point:
                        result.append((ts.isoformat(), point))
        except Exception as e:
            print(f"  Warning: failed to read {fpath}: {e}")

    result.sort(key=lambda p: p[0])
    return result


def load_startup_history(directory, minutes=60):
    """Load history from TSV files on startup to pre-populate charts."""
    points = read_tsv_history(directory, minutes)
    print(f"Loaded {len(points)} history points from log files")
    return points


# ── Email alarm notifications ───────────────────────────────────────────

class EmailAlerter:
    """Sends alarm emails via Gmail SMTP with per-sensor cooldown."""

    def __init__(self, cfg):
        self.enabled = cfg.get("enabled", False)
        self.smtp_server = cfg.get("smtp_server", "smtp.gmail.com")
        self.smtp_port = cfg.get("smtp_port", 587)
        self.sender = cfg.get("sender", "")
        self.password = cfg.get("app_password", "")
        self.recipients = cfg.get("recipients", [])
        self.cooldown_s = cfg.get("cooldown_s", 3600)
        self._last_sent = {}

    def reload(self, cfg):
        self.enabled = cfg.get("enabled", False)
        self.sender = cfg.get("sender", "")
        self.password = cfg.get("app_password", "")
        self.recipients = cfg.get("recipients", [])
        self.cooldown_s = cfg.get("cooldown_s", 3600)

    def maybe_send(self, snapshot):
        if not self.enabled or not self.recipients or not self.sender:
            return
        alarmed = {sid: d for sid, d in snapshot.items()
                   if d.get("alarm") and len(d["alarm"]) > 0}
        if not alarmed:
            return
        now = time.time()
        to_notify = {}
        for sid, d in alarmed.items():
            last = self._last_sent.get(sid, 0)
            if now - last >= self.cooldown_s:
                to_notify[sid] = d
                self._last_sent[sid] = now
        if not to_notify:
            return
        print(f"  ✉ Sending alarm email for: {list(to_notify.keys())}")
        threading.Thread(
            target=self._send_email, args=(to_notify,), daemon=True
        ).start()

    def _send_email(self, alarmed_sensors):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sys_id = config.get("system", {}).get("id", "??")
            sys_name = f"KNVA-{sys_id}"
            n = len(alarmed_sensors)
            subject = f"{sys_name} ALARM — {n} sensor(s)"

            # Pushover plain text
            push_lines = [f"⚠ {sys_name} Alarm · {ts}", ""]
            for sid, d in alarmed_sensors.items():
                label = d.get("label", sid)
                alarms_str = ", ".join(d["alarm"])
                t = d.get("temp", "?")
                h = d.get("hum", "?")
                p = d.get("press")
                press_str = f" | {p} kPa" if p is not None else ""
                push_lines.append(f"{label}")
                push_lines.append(f"  ⚠ {alarms_str}")
                push_lines.append(f"  {t}°C | {h}%{press_str}")
                push_lines.append("")
            push_lines.append(f"Next alert in {self.cooldown_s // 60} min.")
            push_body = "\n".join(push_lines)

            # Full email text
            full_lines = [f"{sys_name} Slow Control — Alarm Report", f"Time: {ts}", ""]
            for sid, d in alarmed_sensors.items():
                label = d.get("label", sid)
                alarms_str = ", ".join(d["alarm"])
                full_lines.append(f"  {label}:")
                full_lines.append(f"    Alarm: {alarms_str}")
                full_lines.append(f"    Temp:  {d.get('temp', '?')} °C")
                full_lines.append(f"    Hum:   {d.get('hum', '?')} %")
                if d.get("press") is not None:
                    full_lines.append(f"    Press: {d['press']} kPa")
                full_lines.append("")
            full_lines.append(f"Next email in {self.cooldown_s // 60} min if alarm persists.")
            full_body = "\n".join(full_lines)

            # HTML email
            rows_html = ""
            for sid, d in alarmed_sensors.items():
                label = d.get("label", sid)
                alarms_str = ", ".join(d["alarm"])
                temp = d.get("temp", "?")
                hum = d.get("hum", "?")
                press = d.get("press", "")
                press_cell = f"<td style='padding:6px 12px'>{press} kPa</td>" if press != "" else ""
                rows_html += (
                    f"<tr><td style='padding:6px 12px;font-weight:bold'>{label}</td>"
                    f"<td style='padding:6px 12px;color:#ef5350'>{alarms_str}</td>"
                    f"<td style='padding:6px 12px'>{temp} °C</td>"
                    f"<td style='padding:6px 12px'>{hum} %</td>{press_cell}</tr>"
                )
            html = f"""<html><body style="font-family:monospace;background:#0c0e13;color:#c8cdd8;padding:20px">
<h2 style="color:#ef5350">⚠ {sys_name} Alarm</h2>
<p style="color:#5a6070">{ts}</p>
<table style="border-collapse:collapse;margin:16px 0">
<tr style="border-bottom:1px solid #1e2230;color:#5a6070">
<th style="padding:6px 12px;text-align:left">Sensor</th>
<th style="padding:6px 12px;text-align:left">Alarm</th>
<th style="padding:6px 12px;text-align:left">Temp</th>
<th style="padding:6px 12px;text-align:left">Hum</th></tr>
{rows_html}</table>
<p style="color:#5a6070;font-size:12px">Next alert in {self.cooldown_s // 60} min if alarm persists.</p>
</body></html>"""

            push_addrs = [r for r in self.recipients if "pomail.net" in r]
            email_addrs = [r for r in self.recipients if "pomail.net" not in r]

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender, self.password)
                if push_addrs:
                    msg_push = MIMEText(push_body, "plain")
                    msg_push["Subject"] = subject
                    msg_push["From"] = self.sender
                    msg_push["To"] = ", ".join(push_addrs)
                    server.sendmail(self.sender, push_addrs, msg_push.as_string())
                if email_addrs:
                    msg_email = MIMEMultipart("alternative")
                    msg_email["Subject"] = subject
                    msg_email["From"] = self.sender
                    msg_email["To"] = ", ".join(email_addrs)
                    msg_email.attach(MIMEText(full_body, "plain"))
                    msg_email.attach(MIMEText(html, "html"))
                    server.sendmail(self.sender, email_addrs, msg_email.as_string())

            print(f"  ✉ Alarm email sent to {', '.join(push_addrs + email_addrs)}")
            _petsys_append("ok", f"✉ Alarm email sent: {list(alarmed_sensors.keys())}")
        except Exception as e:
            msg = f"✉ Email FAILED: {e}"
            print(f"  {msg}")
            _err_log(msg)
            _petsys_append("fail", msg)


# ── Daily log file management ───────────────────────────────────────────

class DailyLogger:
    """Writes one TSV file per calendar day. Pressure logged in kPa."""

    def __init__(self, directory, sensor_ids, interval_s=30, prefix="konova"):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sensor_ids = sensor_ids
        self.interval_s = interval_s
        self.prefix = prefix
        self._current_date = None
        self._file = None
        self._last_write = 0

    def _header(self):
        cols = ["timestamp", "ram_MB"]
        for sid in self.sensor_ids:
            s = config["sensors"][sid]
            if s["type"] == "BME280":
                cols += [f"{sid}_T", f"{sid}_H", f"{sid}_P"]
            elif s["type"] == "HWT905":
                cols += [f"{sid}_tiltX", f"{sid}_tiltY",
                         f"{sid}_magX", f"{sid}_magY", f"{sid}_magZ"]
            elif s["type"] == "A1Temp":
                n = s.get("num_asics", 4)
                cols += [f"{sid}_A{i}" for i in range(1, n + 1)]
            elif s["type"] == "LinuxTemp":
                from sensors import LinuxTemp as _LT
                label = s.get("label", sid)
                exclude = s.get("exclude_zones", [])
                _tmp = _LT(label=label, zones=s.get("zones"), exclude_zones=exclude)
                cols += [f"{sid}_{z}" for z in _tmp.zone_names]
            else:
                cols += [f"{sid}_T", f"{sid}_H"]
        return "\t".join(cols) + "\n"

    def _open_today(self):
        today = date.today()
        if self._current_date == today and self._file is not None:
            return
        if self._file:
            self._file.close()
        expected_header = self._header()
        fname = self.directory / f"{self.prefix}_{today.isoformat()}.tsv"
        # Check if existing file header matches current expected header
        if fname.exists():
            try:
                with open(fname) as f:
                    existing_header = f.readline()
                if existing_header.strip() != expected_header.strip():
                    # Header mismatch — start a new file with timestamp suffix
                    ts = datetime.now().strftime("%H%M%S")
                    fname = self.directory / f"{self.prefix}_{today.isoformat()}_{ts}.tsv"
                    print(f"  TSV header mismatch — new file: {fname.name}")
            except Exception:
                pass
        is_new = not fname.exists()
        self._file = open(fname, "a")
        if is_new:
            self._file.write(expected_header)
            self._file.flush()
        self._current_date = today

    def maybe_write(self, data):
        now = time.time()
        if now - self._last_write < self.interval_s:
            return
        self._open_today()
        row = [datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
        # RAM available
        try:
            mem_info = open("/proc/meminfo").read().splitlines()
            mem_total = next((int(l.split()[1]) for l in mem_info if l.startswith("MemTotal")), 0)
            mem_avail = next((int(l.split()[1]) for l in mem_info if l.startswith("MemAvailable")), 0)
            row.append(f"{(mem_total - mem_avail) // 1024}")
        except Exception:
            row.append("0")
        for sid in self.sensor_ids:
            d = data.get(sid, {})
            stype = config["sensors"][sid]["type"]
            if stype == "BME280":
                row += [f"{d.get('temp', 0):.2f}",
                        f"{d.get('hum', 0):.2f}",
                        f"{d.get('press', 0):.2f}"]
            elif stype == "HWT905":
                row += [f"{d.get('tilt_x', 0):.3f}",
                        f"{d.get('tilt_y', 0):.3f}",
                        f"{d.get('mag_x', 0):.2f}",
                        f"{d.get('mag_y', 0):.2f}",
                        f"{d.get('mag_z', 0):.2f}"]
            elif stype == "A1Temp":
                n = config["sensors"][sid].get("num_asics", 4)
                for i in range(1, n + 1):
                    row.append(f"{d.get(f'asic_{i}', 0):.2f}")
            elif stype == "LinuxTemp":
                # Write zone values in the same order as the header
                from sensors import LinuxTemp as _LT
                s = config["sensors"][sid]
                _tmp = _LT(label=s.get("label", sid),
                           zones=s.get("zones"),
                           exclude_zones=s.get("exclude_zones", []))
                for z in _tmp.zone_names:
                    row.append(f"{d.get(z, 0):.1f}")
            else:
                row += [f"{d.get('temp', 0):.2f}",
                        f"{d.get('hum', 0):.2f}"]
        self._file.write("\t".join(row) + "\n")
        self._file.flush()
        self._last_write = now

    def close(self):
        if self._file:
            self._file.close()


# ── HTTP server for the GUI ─────────────────────────────────────────────

class DataHandler(BaseHTTPRequestHandler):
    """Serves live sensor data, history, config, and the dashboard."""

    DASHBOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "dashboard.html")

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        try:
            self._do_GET_inner()
        except BrokenPipeError:
            pass   # client disconnected — harmless
        except Exception as e:
            import traceback
            print(f"do_GET ERROR: {e}\n{traceback.format_exc()}")
            try:
                self._respond(500, f'{{"error":"{e}"}}', "application/json")
            except Exception:
                pass

    def _do_GET_inner(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/data":
            with live_lock:
                data_payload = dict(live_data)
            data_payload["__meta__"] = {
                "email_enabled": config.get("email", {}).get("enabled", False)
            }
            self._respond(200, json.dumps(data_payload), "application/json")

        elif path == "/config":
            self._respond(200, json.dumps(config), "application/json")

        elif path == "/history":
            since = params.get("since", [None])[0]
            with live_lock:
                if since:
                    payload = json.dumps([p for p in history if p[0] > since])
                else:
                    payload = json.dumps(history)
            self._respond(200, payload, "application/json")

        elif path == "/filehistory":
            # Read from TSV files: /filehistory?minutes=1440
            minutes = int(params.get("minutes", [60])[0])
            points = read_tsv_history(data_directory, minutes)
            # Downsample for large ranges to ~500 points without averaging
            # Target: 500 points; just take every Nth point
            MAX_POINTS = 500
            if len(points) > MAX_POINTS:
                step = len(points) // MAX_POINTS
                points = points[::step]
            self._respond(200, json.dumps(points), "application/json")

        elif path == "/email/toggle":
            config["email"]["enabled"] = not config["email"].get("enabled", False)
            save_config(config)
            emailer.reload(config.get("email", {}))
            state = "enabled" if config["email"]["enabled"] else "disabled"
            _petsys_append("ok" if config["email"]["enabled"] else "warn",
                           f"✉ Email alerts {state}.")
            self._respond(200,
                f'{{"enabled":{str(config["email"]["enabled"]).lower()}}}',
                "application/json")

        elif path == "/email/test":
            try:
                if not emailer:
                    self._respond(400, '{"error":"emailer not initialised"}',
                                  "application/json")
                    return
                recipients = config.get("email", {}).get("recipients", [])
                if not recipients:
                    self._respond(400, '{"error":"no recipients configured"}',
                                  "application/json")
                    return
                sender = config.get("email", {}).get("sender", "")
                password = config.get("email", {}).get("app_password", "")
                if not sender or not password:
                    self._respond(400, '{"error":"email sender/password not configured"}',
                                  "application/json")
                    return
                # Send directly — bypasses enabled flag and cooldown
                test_alarmed = {
                    "__test__": {
                        "label": "Test Alert",
                        "type": "Test",
                        "alarm": ["test — KoNova Slow Control is working"],
                        "timestamp": datetime.now().isoformat(),
                    }
                }
                # Send synchronously so errors are returned to the caller
                try:
                    emailer._send_email(test_alarmed)
                    _petsys_append("ok", "✉ Test email sent.")
                    self._respond(200, '{"status":"sent"}', "application/json")
                except Exception as mail_err:
                    _petsys_append("fail", f"✉ Test email FAILED: {mail_err}")
                    self._respond(500, json.dumps({"error": str(mail_err)}),
                                  "application/json")
            except Exception as e:
                self._respond(500, json.dumps({"error": str(e)}),
                              "application/json")

        elif path == "/diskspace":
            import shutil as _shutil
            try:
                data_dir = config.get("petsys", {}).get("data_dir", "/")
                disk = _shutil.disk_usage(data_dir)
                free_pct  = round(100.0 * disk.free  / disk.total, 2)
                free_gb   = round(disk.free  / 1024**3, 2)
                total_gb  = round(disk.total / 1024**3, 1)
                used_pct  = round(100.0 * disk.used  / disk.total, 2)
            except Exception:
                free_pct = free_gb = total_gb = used_pct = 0
            payload = json.dumps({
                "free_pct":  free_pct,
                "free_gb":   free_gb,
                "total_gb":  total_gb,
                "used_pct":  used_pct,
                "history":   _disk_history[-2000:],
            })
            self._respond(200, payload, "application/json")

        elif path == "/petsys/status":
            # Client passes ?seq=N to get only entries with seq > N
            try:
                after_seq = int(urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query
                ).get("seq", ["0"])[0])
            except Exception:
                after_seq = 0
            with _petsys_lock:
                new_entries = [e for e in _petsys_log if e["seq"] > after_seq]
                max_seq = _petsys_log[-1]["seq"] if _petsys_log else 0
            with _acq_lock:
                acq_running  = _acq_running
                acq_cycle    = _acq_cycle
                acq_filename = _acq_filename
                acq_calib    = _acq_calib_name
                acq_task     = _acq_task
            payload = json.dumps({
                "daq_running":  _daqd_is_running(),
                "asic_power":   _asic_power.is_set() or acq_running,
                "mains_power":  _mains_power["status"],
                "mains_check":  _mains_power["last_check"],
                "acq_running":  acq_running,
                "acq_cycle":    acq_cycle,
                "acq_filename": acq_filename,
                "acq_calib":    acq_calib,
                "acq_task":     acq_task,
                "max_seq":      max_seq,
                "log":          new_entries,
            })
            self._respond(200, payload, "application/json")

        elif path in ("/", "/dashboard", "/dashboard.html"):
            try:
                with open(self.DASHBOARD) as f:
                    html = f.read()
                self._respond(200, html, "text/html")
            except FileNotFoundError:
                self._respond(404, "dashboard.html not found", "text/plain")
        elif path.startswith("/js/") and path.endswith(".js"):
            js_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js")
            fname = os.path.basename(path)
            js_path = os.path.join(js_dir, fname)
            try:
                with open(js_path, "r") as f:
                    self._respond(200, f.read(), "application/javascript")
            except FileNotFoundError:
                self._respond(404, f"{fname} not found", "text/plain")
        else:
            self._respond(404, '{"error":"not found"}', "application/json")

    def do_POST(self):
        try:
            self._do_POST_inner()
        except BrokenPipeError:
            pass   # client disconnected — harmless
        except Exception as e:
            import traceback
            print(f"do_POST ERROR: {e}\n{traceback.format_exc()}")
            try:
                self._respond(500, f'{{"error":"{e}"}}', "application/json")
            except Exception:
                pass

    def _do_POST_inner(self):
        if self.path == "/config":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                new_cfg = json.loads(body)
                # Re-read config from disk first — preserves any manual edits
                try:
                    disk_cfg = load_config(config_path)
                    config.update(disk_cfg)
                except Exception:
                    pass
                # Never allow data_dir or calib_dir to be set to empty string
                if "petsys" in new_cfg:
                    for key in ("data_dir", "calib_dir"):
                        if key in new_cfg["petsys"] and not new_cfg["petsys"][key]:
                            del new_cfg["petsys"][key]
                # Deep merge petsys section rather than overwriting
                petsys_update = None
                if "petsys" in new_cfg and "petsys" in config:
                    petsys_update = new_cfg.get("petsys", {})
                    config["petsys"].update(new_cfg.pop("petsys"))
                # Protect critical sections — only editable by hand in config.json
                PROTECTED_KEYS = {"sensors", "ch347", "log", "email"}
                for key in PROTECTED_KEYS:
                    new_cfg.pop(key, None)
                # Deep merge remaining dict sections
                for key, val in new_cfg.items():
                    if isinstance(val, dict) and key in config and isinstance(config[key], dict):
                        config[key].update(val)
                    else:
                        config[key] = val
                save_config(config)
                print(f"  Config saved → {config_path}")

                # If threshold values changed, regenerate disc_settings.tsv in base data dir
                if petsys_update and "config_profiles" in petsys_update:
                    def _regen_disc_settings():
                        try:
                            build = _build_dir()
                            if not build:
                                return
                            pcfg = config.get("petsys", {})
                            ini_name = pcfg.get("config_file", "config_muons.ini")
                            data = pcfg.get("data_dir", "")
                            if not data:
                                return
                            ini = os.path.join(data, ini_name)
                            vth_t1, vth_t2, vth_e = _get_thresholds(pcfg, ini_name)
                            exe = os.path.join(build, "make_simple_disc_settings_table")
                            if not os.path.isfile(exe):
                                _petsys_append("warn", f"Cannot regenerate disc_settings: {exe} not found")
                                return
                            if not os.path.isfile(os.path.join(data, "disc_calibration.tsv")):
                                _petsys_append("warn", "No disc_calibration.tsv found — run calibration first")
                                return
                            out_file = os.path.join(data, "disc_settings.tsv")
                            cmd = [
                                "python3", "-u", exe,
                                "--config", ini,
                                f"--vth_t1={vth_t1}",
                                f"--vth_t2={vth_t2}",
                                f"--vth_e={vth_e}",
                                "-o", out_file,
                            ]
                            _petsys_append("cmd",
                                f"→ Regenerating disc_settings (vth_t1={vth_t1}, vth_t2={vth_t2}, vth_e={vth_e})...")
                            proc = subprocess.run(cmd, cwd=build,
                                                  capture_output=True, text=True, timeout=30)
                            if proc.returncode == 0:
                                _petsys_append("ok", f"✔ disc_settings.tsv updated")
                            else:
                                _petsys_append("fail",
                                    f"✘ disc_settings regeneration failed: {proc.stderr[:200]}")
                        except Exception as e:
                            _petsys_append("fail", f"✘ disc_settings regeneration error: {e}")
                    threading.Thread(target=_regen_disc_settings, daemon=True).start()

                self._respond(200, '{"status":"ok"}', "application/json")
            except Exception as e:
                self._respond(400, json.dumps({"error": str(e)}),
                              "application/json")

        elif self.path == "/petsys/cmd":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                req = json.loads(body)
                cmd = req.get("cmd", "")
                if cmd not in ("daq_on", "daq_off", "i2c_test", "lvds_test",
                               "link_test", "comms_test",
                               "asic_power_on", "asic_power_off", "asic_power_cycle",
                               "panic"):
                    self._respond(400, '{"error":"unknown command"}', "application/json")
                    return
                _run_petsys_cmd(cmd)
                self._respond(200, '{"status":"queued"}', "application/json")
            except Exception as e:
                self._respond(400, json.dumps({"error": str(e)}), "application/json")

        elif self.path == "/petsys/acq":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                req    = json.loads(body)
                action = req.get("action", "")
                if action == "temp_stable":
                    _run_temp_stable()
                    self._respond(200, '{"status":"started"}', "application/json")
                elif action == "calibrate":
                    _run_calibrate()
                    self._respond(200, '{"status":"started"}', "application/json")
                elif action == "start":
                    if _acq_running:
                        self._respond(400, '{"error":"already running"}', "application/json")
                        return
                    _run_acquire()
                    self._respond(200, '{"status":"started"}', "application/json")
                elif action == "sequence":
                    if _acq_running:
                        self._respond(400, '{"error":"already running"}', "application/json")
                        return
                    steps = req.get("steps", [])
                    valid = {"temp_stable", "calibrate", "acquire"}
                    steps = [s for s in steps if s in valid]
                    if not steps:
                        self._respond(400, '{"error":"no valid steps"}', "application/json")
                        return
                    _run_sequence(steps)
                    self._respond(200, '{"status":"started"}', "application/json")
                elif action == "stop":
                    _stop_acquire()
                    self._respond(200, '{"status":"stopping"}', "application/json")
                else:
                    self._respond(400, '{"error":"unknown action"}', "application/json")
            except Exception as e:
                self._respond(400, json.dumps({"error": str(e)}), "application/json")

        else:
            self._respond(404, '{"error":"not found"}', "application/json")

    def _respond(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode())


def start_http(port=8880):
    srv = HTTPServer(("0.0.0.0", port), DataHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"HTTP data server on port {port}")
    return srv


# ── Sensor initialisation ──────────────────────────────────────────────

def init_sensors(cfg):
    ch_cfg = cfg["ch347"]
    bus = CH347(device=ch_cfg["device"].encode(), i2c_mode=ch_cfg["i2c_mode"])
    sensors = {}
    extra_closeable = []  # non-I2C devices to close on exit
    for sid, s in cfg["sensors"].items():
        try:
            if s["type"] == "BME280":
                addr = int(s["address"], 16)
                offsets = s.get("offsets", {})
                sensors[sid] = BME280(bus, addr, offsets)
            elif s["type"] == "HDC302x":
                addr = int(s["address"], 16)
                offsets = s.get("offsets", {})
                sensors[sid] = HDC302x(bus, addr, offsets)
            elif s["type"] == "HWT905":
                port = s.get("port", "/dev/ttyUSB0")
                baud = s.get("baud", 9600)
                try:
                    hw = HWT905(port=port, baud=baud)
                    sensors[sid] = hw
                    extra_closeable.append(hw)
                except Exception as e:
                    print(f"WARNING: {sid} — {e} (will retry during acquisition)")
                    sensors[sid] = None   # placeholder — reconnect logic will retry
            elif s["type"] == "A1Temp":
                build_dir = s.get("build_dir", "")
                label = s.get("label", "PETSys ASICs")
                sensors[sid] = A1Temp(build_dir=build_dir, label=label)
            elif s["type"] == "LinuxTemp":
                label = s.get("label", "Linux CPU")
                zones = s.get("zones", None)
                exclude = s.get("exclude_zones", [])
                sensors[sid] = LinuxTemp(label=label, zones=zones, exclude_zones=exclude)
            else:
                print(f"Unknown sensor type: {s['type']}")
        except Exception as e:
            print(f"WARNING: {sid} — {e}")
    return bus, sensors, extra_closeable


# ── Main loop ───────────────────────────────────────────────────────────

def main():
    global config, live_data, history, data_directory, emailer, _err_log_path
    config = load_config(
        sys.argv[1] if len(sys.argv) > 1 else "config.json"
    )

    log_cfg = config["log"]
    data_directory = log_cfg["directory"]
    os.makedirs(data_directory, exist_ok=True)

    # Persistent error log — one per run, in run directory
    # (run_dir is set later in _acquire_worker; for pre-acquisition errors use data_directory)
    _err_log_path = os.path.join(data_directory, "kodaq_errors.log")
    try:
        with open("/proc/uptime") as f:
            _uptime = float(f.read().split()[0])
    except Exception:
        _uptime = 9999
    _start_type = "REBOOT START" if _uptime < 180 else "MANUAL START"
    _err_log(f"── kodaq started ({_start_type}, uptime={_uptime:.0f}s) ──")
    # ASIC power starts OFF — flag file tells sensor_worker to skip A1Temp
    _asic_power_off()
    sys_id = config.get("system", {}).get("id", "01")
    tsv_prefix = f"KNVA{sys_id}"
    logger = DailyLogger(
        data_directory,
        list(config["sensors"].keys()),
        log_cfg["file_interval_s"],
        prefix=tsv_prefix,
    )
    gui_interval = log_cfg["gui_interval_s"]

    # Load history from log files on startup
    startup_history = load_startup_history(data_directory, minutes=60)
    with live_lock:
        history = startup_history

    # Email alerter
    emailer = EmailAlerter(config.get("email", {}))
    email_status = "enabled" if emailer.enabled else "disabled"

    srv = start_http()
    _petsys_append("ok", "── PETSys DAQ Control ready ──────────────────")
    _petsys_append("info", f"Build dir: {_build_dir()}")
    _petsys_append("info", f"Data  dir: {config.get('petsys',{}).get('data_dir','(not set)')}")

    # Auto-open dashboard
    import webbrowser
    try:
        webbrowser.open("http://localhost:8880/")
    except Exception:
        pass

    print(f"\nAcquisition running — {len(config['sensors'])} sensors, "
          f"GUI every {gui_interval}s, file every {log_cfg['file_interval_s']}s, "
          f"email alerts {email_status}")
    print("=" * 60)

    # ── Auto-start sequence on reboot ────────────────────────────────────
    autostart = config.get("autostart", {})
    if autostart.get("enabled", False):
        # Only auto-start if kodaq launched shortly after boot
        # (uptime < 3 minutes means this is a reboot start, not manual)
        try:
            with open("/proc/uptime") as f:
                uptime_s = float(f.read().split()[0])
        except Exception:
            uptime_s = 9999
        is_reboot = uptime_s < 180   # 3 minutes
        if not is_reboot:
            _petsys_append("info",
                "Auto-start skipped — manual start detected "
                f"(uptime {uptime_s:.0f}s > 180s)")
        else:
            def _autostart():
                delay = autostart.get("delay_s", 5)
                _petsys_append("info",
                    f"Auto-start enabled — waiting {delay}s before starting...")
                time.sleep(delay)
                MAX_AUTO_RETRIES = 3
                for attempt in range(1, MAX_AUTO_RETRIES + 1):
                    try:
                        _petsys_append("cmd",
                            f"── AUTO-START (attempt {attempt}/{MAX_AUTO_RETRIES}) ──")
                        pcfg = config.get("petsys", {})
                        for k in ("calib_mode","cycle_time_s","vth_t1","vth_t2","vth_e"):
                            if k in autostart:
                                pcfg[k] = autostart[k]
                        config["petsys"] = pcfg
                        _petsys_append("info", "Auto-start: turning DAQ ON...")
                        _run_petsys_cmd("daq_on")
                        time.sleep(6)
                        if not _daqd_is_running():
                            raise RuntimeError("daqd failed to start")
                        _petsys_append("ok", "Auto-start: DAQ ON ✔")
                        steps = autostart.get("sequence", ["temp_stable","acquire"])
                        _petsys_append("info",
                            f"Auto-start: running sequence {steps}...")
                        _run_sequence(steps)
                        return
                    except Exception as e:
                        msg = f"Auto-start attempt {attempt} failed: {e}"
                        _petsys_append("fail", f"✘ {msg}")
                        if emailer:
                            try:
                                emailer._send_email({"__autostart__": {
                                    "label": "Auto-start", "type": "AutoStart",
                                    "alarm": [msg],
                                    "timestamp": datetime.now().isoformat(),
                                }})
                            except Exception:
                                pass
                        if attempt < MAX_AUTO_RETRIES:
                            _petsys_append("info", "Retrying in 30s...")
                            time.sleep(30)
                _petsys_append("fail",
                    "✘ Auto-start failed after all retries — manual intervention required.")

            threading.Thread(target=_autostart, daemon=True).start()

    # ── Main power monitor ───────────────────────────────────────────────
    MCU_STATE_FILE = "/run/karbon-ignition-state"

    def _power_monitor():
        """Check main power via MCU state file every 60 seconds.
        On power loss: email → wait 2 min → recheck → email → shutdown."""
        POLL_INTERVAL = 60   # seconds between checks
        CONFIRM_WAIT  = 120  # seconds to wait before confirming power loss

        def _check_power():
            """Returns True if main power is on, False if on battery."""
            # Check if monitoring is disabled via config
            if not config.get("mcu", {}).get("monitor_enabled", True):
                _mains_power["status"] = "disabled"
                _mains_power["last_check"] = datetime.now().isoformat()
                return True
            try:
                with open(MCU_STATE_FILE) as f:
                    data = json.load(f)
                ignition = data.get("ignition")
                ts = data.get("timestamp", 0)
                # If state file is older than 5 minutes, consider it stale
                if time.time() - ts > 300:
                    _mains_power["status"] = "unknown"
                    _mains_power["last_check"] = datetime.now().isoformat()
                    print(f"  MCU state file stale ({time.time() - ts:.0f}s old)", file=sys.stderr)
                    return True  # assume power on if stale
                if ignition is True:
                    _mains_power["status"] = "on"
                    _mains_power["last_check"] = datetime.now().isoformat()
                    return True
                elif ignition is False:
                    _mains_power["status"] = "off"
                    _mains_power["last_check"] = datetime.now().isoformat()
                    return False
                else:
                    _mains_power["status"] = "unknown"
                    _mains_power["last_check"] = datetime.now().isoformat()
                    return True  # inconclusive = assume power on
            except Exception as e:
                _mains_power["status"] = "unknown"
                _mains_power["last_check"] = datetime.now().isoformat()
                print(f"  MCU state file error: {e}", file=sys.stderr)
                return True  # assume power is on if we can't read

        while True:
            time.sleep(POLL_INTERVAL)
            if not _check_power():
                # First detection: power is off
                _err_log("⚠ MAIN POWER LOST — running on battery")
                _petsys_append("warn", "⚠ MAIN POWER LOST — running on battery")
                if emailer and emailer.enabled:
                    try:
                        emailer._send_email({"__power__": {
                            "label": "Main Power", "type": "PowerLoss",
                            "alarm": ["Main power lost — running on battery. "
                                      "Will shut down PETSys in 2 minutes if power doesn't return."],
                            "timestamp": datetime.now().isoformat(),
                        }})
                    except Exception:
                        pass

                # Wait and recheck
                time.sleep(CONFIRM_WAIT)

                if not _check_power():
                    # Confirmed: still on battery — shut down PETSys
                    _err_log("⚠ MAIN POWER STILL OFF — shutting down PETSys")
                    _petsys_append("fail", "⚠ MAIN POWER STILL OFF — shutting down acquisition and ASIC power")
                    if emailer and emailer.enabled:
                        try:
                            emailer._send_email({"__power__": {
                                "label": "Main Power", "type": "PowerLoss",
                                "alarm": ["Main power still off after 2 minutes. "
                                          "Shutting down PETSys acquisition and ASIC power."],
                                "timestamp": datetime.now().isoformat(),
                            }})
                        except Exception:
                            pass

                    # Stop acquisition if running
                    if _acq_running:
                        _stop_acquire()
                        time.sleep(5)

                    # Turn off ASIC power
                    if _daqd_is_running():
                        _run_petsys_cmd("asic_power_off")
                        time.sleep(2)

                    _petsys_append("ok", "✔ PETSys shut down due to power loss")
                else:
                    _err_log("✔ Main power restored")
                    _petsys_append("ok", "✔ Main power restored")
                    if emailer and emailer.enabled:
                        try:
                            emailer._send_email({"__power__": {
                                "label": "Main Power", "type": "PowerRestored",
                                "alarm": ["Main power has been restored."],
                                "timestamp": datetime.now().isoformat(),
                            }})
                        except Exception:
                            pass

    # Start power monitor — waits for state file if not yet available
    threading.Thread(target=_power_monitor, daemon=True).start()
    if os.path.exists(MCU_STATE_FILE):
        try:
            with open(MCU_STATE_FILE) as f:
                data = json.load(f)
            _mains_power["status"] = "on" if data.get("ignition") else "off"
            _mains_power["last_check"] = datetime.now().isoformat()
            _err_log(f"Main power monitor started ({MCU_STATE_FILE})")
        except Exception:
            _err_log(f"Main power monitor started (waiting for {MCU_STATE_FILE})")
    else:
        _err_log(f"Main power monitor started (waiting for {MCU_STATE_FILE})")

    # ── Thermal watchdog — checks ASIC temperature every 30 seconds ──
    def _thermal_watchdog():
        last_trigger_time = 0
        REFIRE_COOLDOWN = 60  # seconds — won't re-fire within this window of the last trigger
        while True:
            time.sleep(15)
            thermal_cfg = config.get("petsys", {}).get("thermal", {})
            limit = thermal_cfg.get("max_asic_temp", None)
            if limit is None:
                continue
            max_temp = _get_max_asic_temp()
            if max_temp is None:
                continue
            if max_temp >= limit:
                # Only skip re-firing if we very recently triggered (avoids
                # hammering panic every 15s while daqd is shutting down).
                if time.monotonic() - last_trigger_time < REFIRE_COOLDOWN:
                    continue
                last_trigger_time = time.monotonic()
                _err_log(f"THERMAL WATCHDOG — ASIC temp {max_temp}°C ≥ limit {limit}°C")
                _petsys_append("fail",
                    f"⚠ THERMAL WATCHDOG — ASIC temp {max_temp}°C reached limit "
                    f"{limit}°C. Executing panic shutdown.")
                if emailer and emailer.enabled:
                    try:
                        emailer._send_email({"__thermal__": {
                            "label": "ASIC Thermal", "type": "ThermalShutdown",
                            "alarm": [f"ASIC temperature reached {max_temp}°C "
                                      f"(limit {limit}°C). Panic shutdown executed: "
                                      f"acquisition, ASIC power, and daqd all turned off."],
                            "timestamp": datetime.now().isoformat(),
                        }})
                    except Exception:
                        pass
                _run_petsys_cmd("panic")

    threading.Thread(target=_thermal_watchdog, daemon=True).start()
    _err_log("Thermal watchdog started")

    DISK_SAMPLE_EVERY = 1
    _disk_poll_count  = 0

    def _start_worker():
        """Launch sensor_worker.py as a subprocess."""
        worker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "sensor_worker.py")
        proc = subprocess.Popen(
            [sys.executable, "-u", worker_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,   # worker's print() goes to our terminal
            text=True,
        )
        # Send config to worker
        proc.stdin.write(json.dumps(config) + "\n")
        proc.stdin.flush()
        print(f"  Sensor worker started (PID {proc.pid})")
        return proc

    worker = _start_worker()

    try:
        while True:
            # Check if worker is alive; restart if it crashed
            if worker.poll() is not None:
                msg = f"⚠ Sensor worker died (exit {worker.returncode}) — restarting..."
                print(f"  {msg}")
                _petsys_append("warn", msg)
                _acq_log(msg)
                _err_log(msg)
                # Inject alarm into live_data so email fires
                with live_lock:
                    live_data["__system__"] = live_data.get("__system__", {})
                    live_data["__system__"]["alarm"] = \
                        live_data["__system__"].get("alarm", []) + ["sensor_worker_crash"]
                    live_data["__system__"]["status"] = "error"
                emailer.maybe_send({"__system__": live_data["__system__"]})
                worker = _start_worker()
                time.sleep(1)
                continue

            # Read one snapshot line from worker (non-blocking check)
            import select
            ready = select.select([worker.stdout], [], [], gui_interval)[0]
            if not ready:
                continue   # timeout — no data yet, loop again

            line = worker.stdout.readline()
            if not line:
                continue

            try:
                snapshot = json.loads(line)
            except json.JSONDecodeError:
                print(f"  Worker bad JSON: {line[:80]}")
                continue

            # ── Alarm check on worker snapshot ──────────────────────────
            alarm_active = False
            for sid, entry in snapshot.items():
                if entry.get("status") != "ok":
                    continue
                scfg = config["sensors"].get(sid, {})
                alarms_cfg = config.get("alarms", {}).get(sid, {})
                stype = scfg.get("type", "")

                # Alarm thresholds by sensor type:
                # Critical (immediate, 1 poll): disk, system events — handled separately
                # Slow (12 polls = ~60s): ASIC temps
                # Sustained (3 polls = ~15s): everything else
                if stype == "A1Temp":
                    REQUIRED_POLLS = 12
                else:
                    REQUIRED_POLLS = 3

                if stype == "HWT905":
                    check_params = [(k, entry.get(k)) for k in
                                    ("tilt_x","tilt_y","mag_x","mag_y","mag_z")]
                elif stype == "A1Temp":
                    check_params = [(k, entry.get(k)) for k in entry
                                    if k.startswith("asic_")]
                else:
                    check_params = [(k, entry.get(k)) for k in
                                    ("temp","hum","press")]

                if sid not in _alarm_counts:
                    _alarm_counts[sid] = {}

                triggered = []
                for param, val in check_params:
                    if val is None:
                        continue
                    lo = alarms_cfg.get(f"{param}_min")
                    hi = alarms_cfg.get(f"{param}_max")
                    in_alarm = ((lo is not None and val < lo) or
                                (hi is not None and val > hi))
                    if in_alarm:
                        _alarm_counts[sid][param] = \
                            _alarm_counts[sid].get(param, 0) + 1
                        if _alarm_counts[sid][param] >= REQUIRED_POLLS:
                            if lo is not None and val < lo:
                                triggered.append(f"{param}<{lo}")
                            if hi is not None and val > hi:
                                triggered.append(f"{param}>{hi}")
                    else:
                        _alarm_counts[sid][param] = 0  # reset on clear

                if triggered:
                    entry["alarm"] = triggered
                    alarm_active = True

                # Console print
                warn = f"  *** ALARM: {', '.join(triggered)}" if triggered else ""
                label = entry.get("label", sid)
                if stype == "BME280":
                    print(f"  {label:16s}  "
                          f"{entry.get('temp',0):6.2f} °C | "
                          f"{entry.get('hum',0):5.2f} % | "
                          f"{entry.get('press',0):7.3f} kPa{warn}")
                elif stype == "HDC302x" or stype == "SHT45":
                    print(f"  {label:16s}  "
                          f"{entry.get('temp',0):6.2f} °C | "
                          f"{entry.get('hum',0):5.2f} %{warn}")
                elif stype == "HWT905":
                    print(f"  {label:16s}  "
                          f"TiltX:{entry.get('tilt_x',0):7.3f}° "
                          f"TiltY:{entry.get('tilt_y',0):7.3f}° | "
                          f"Mag: X={entry.get('mag_x',0):7.1f} "
                          f"Y={entry.get('mag_y',0):7.1f} "
                          f"Z={entry.get('mag_z',0):7.1f} µT{warn}")
                elif stype == "A1Temp":
                    temps_str = "  ".join(
                        f"ASIC{k.replace('asic_','')}:{entry[k]:6.2f}°C"
                        for k in sorted(entry) if k.startswith("asic_"))
                    print(f"  {label:16s}  {temps_str}{warn}")
                elif stype == "LinuxTemp":
                    temps_str = "  ".join(
                        f"{k}:{entry[k]:.0f}°C"
                        for k in sorted(entry)
                        if isinstance(entry.get(k), (int, float))
                        and k not in ("status","timestamp","label","type"))
                    print(f"  {label:16s}  {temps_str}{warn}")

            # ── System health snapshot entry ─────────────────────────────
            try:
                import shutil as _shutil2
                _disk2 = _shutil2.disk_usage(data_directory)
                _free_pct = 100.0 * _disk2.free / _disk2.total
                _free_gb  = _disk2.free / 1024**3
                mem_info = open("/proc/meminfo").read().splitlines()
                mem_total = next((int(l.split()[1]) for l in mem_info if l.startswith("MemTotal")), 0)
                mem_avail = next((int(l.split()[1]) for l in mem_info if l.startswith("MemAvailable")), 0)
                ram_free_mb  = mem_avail // 1024
                ram_total_mb = mem_total // 1024
                sys_entry = {
                    "label": "System", "type": "SysHealth",
                    "disk_free_pct": round(_free_pct, 1),
                    "disk_free_gb":  round(_free_gb, 1),
                    "ram_free_MB":   ram_free_mb,
                    "ram_total_MB":  ram_total_mb,
                    "status": "ok",
                    "timestamp": datetime.now().isoformat(),
                }
                sys_alarms = []
                for thresh in DISK_ALARM_THRESHOLDS:
                    if _free_pct <= thresh:
                        sys_alarms.append(f"disk_free<{thresh}%")
                        break
                if sys_alarms:
                    sys_entry["alarm"] = sys_alarms
                snapshot["__system__"] = sys_entry
            except Exception:
                pass

            # ── Publish to live_data ─────────────────────────────────────
            with live_lock:
                live_data.clear()
                live_data.update(snapshot)

            hist_point = {"__sys__": {}}
            try:
                mem_info2 = open("/proc/meminfo").read().splitlines()
                mem_total2 = next((int(l.split()[1]) for l in mem_info2 if l.startswith("MemTotal")), 0)
                mem_avail2 = next((int(l.split()[1]) for l in mem_info2 if l.startswith("MemAvailable")), 0)
                hist_point["__sys__"]["ram_free_MB"]  = mem_avail2 // 1024
                hist_point["__sys__"]["ram_total_MB"] = mem_total2 // 1024
            except Exception:
                pass
            for sid, d in snapshot.items():
                if d.get("status") == "ok" and sid in config.get("sensors", {}):
                    stype = config["sensors"][sid]["type"]
                    if stype == "HWT905":
                        hp = {"tilt_x": d.get("tilt_x",0), "tilt_y": d.get("tilt_y",0),
                              "mag_x": d.get("mag_x",0), "mag_y": d.get("mag_y",0),
                              "mag_z": d.get("mag_z",0)}
                    elif stype == "A1Temp":
                        hp = {k: d[k] for k in d if k.startswith("asic_")}
                    elif stype == "LinuxTemp":
                        hp = {k: d[k] for k in d
                              if k not in ("label","type","status","timestamp","alarm")}
                    else:
                        hp = {"temp": d["temp"], "hum": d["hum"]}
                        if "press" in d:
                            hp["press"] = d["press"]
                    hist_point[sid] = hp
            history.append((datetime.now().isoformat(), hist_point))
            if len(history) > HISTORY_MAX:
                del history[:len(history) - HISTORY_MAX]

            logger.maybe_write(snapshot)
            # Re-read config from disk to pick up any manual edits
            # (e.g. updated email credentials)
            try:
                disk_cfg = load_config(config_path)
                config.update(disk_cfg)
            except Exception:
                pass
            emailer.reload(config.get("email", {}))
            if emailer:
                emailer.maybe_send(snapshot)

            # Sample disk space periodically
            _disk_poll_count += 1
            if _disk_poll_count >= DISK_SAMPLE_EVERY:
                _disk_poll_count = 0
                try:
                    import shutil as _shutil3
                    _dk = _shutil3.disk_usage(data_directory)
                    _free_pct3 = round(100.0 * _dk.free / _dk.total, 2)
                    _free_gb3  = round(_dk.free / 1024**3, 2)
                    _disk_history.append((datetime.now().isoformat(),
                                         _free_pct3, _free_gb3))
                    if len(_disk_history) > 10000:
                        del _disk_history[:len(_disk_history) - 10000]
                    for thresh in DISK_ALARM_THRESHOLDS:
                        if _free_pct3 <= thresh and thresh not in _disk_alarms_triggered:
                            _disk_alarms_triggered.add(thresh)
                            with live_lock:
                                if "__system__" in live_data:
                                    live_data["__system__"]["alarm"] = \
                                        live_data["__system__"].get("alarm", []) + \
                                        [f"disk_free<{thresh}%"]
                        elif _free_pct3 > thresh:
                            _disk_alarms_triggered.discard(thresh)
                except Exception:
                    pass

            print("-" * 60)

    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        try:
            worker.terminate()
            worker.wait(timeout=3)
        except Exception:
            pass
        logger.close()
        print("Clean exit.")


if __name__ == "__main__":
    main()
