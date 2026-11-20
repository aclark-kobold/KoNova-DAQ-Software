#!/usr/bin/env python3
"""sensor_worker.py — runs as a subprocess of kodaq.py.

Reads all sensors every gui_interval seconds and writes JSON snapshots
to stdout. kodaq.py reads these and updates live_data. If this process
segfaults (e.g. CH347 driver crash), kodaq detects it and restarts it
automatically — acquisition is never interrupted.

Protocol:
  stdout: one JSON line per snapshot (newline-delimited)
  stdin:  single JSON config line on startup, then ignored
  stderr: debug/error messages
"""
import json
import os
import sys
import time
import traceback
from collections import deque
from datetime import datetime

# Add parent directory so we can import sensors.py
sys.path.insert(0, ".")
from sensors import CH347, BME280, HDC302x, HWT905, A1Temp, LinuxTemp, SHT45, PCA9546


def run():
    # Read config from stdin (sent by kodaq on startup)
    raw = sys.stdin.readline()
    config = json.loads(raw)

    ch_cfg  = config.get("ch347", {})
    sensors_cfg = config.get("sensors", {})
    interval = config.get("gui_interval_s", 5)

    # ── Open CH347 bus ───────────────────────────────────────────────────
    # Temporarily redirect stdout to stderr during init
    # so sensor print() messages don't corrupt the JSON stream
    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    try:
        bus = CH347(
            device=ch_cfg.get("device", "/dev/ch34x_pis0").encode(),
            i2c_mode=ch_cfg.get("i2c_mode", 0),
        )

        # ── Init sensors ─────────────────────────────────────────────────────
        sensors = {}
        asic_buffers = {}
        muxes = {}
        ASIC_AVG_N = 6

        for sid, s in sensors_cfg.items():
            try:
                t = s["type"]
                offsets = s.get("offsets", {})

                def _get_mux(s):
                    mux_addr_str = s.get("mux_address")
                    mux_ch = s.get("mux_channel")
                    if mux_addr_str is None or mux_ch is None:
                        return None, None
                    mux_addr = int(mux_addr_str, 16)
                    if mux_addr not in muxes:
                        muxes[mux_addr] = PCA9546(bus, mux_addr)
                    return muxes[mux_addr], mux_ch

                if t == "BME280":
                    mux, mux_ch = _get_mux(s)
                    sensors[sid] = BME280(bus, int(s["address"], 16),
                                          offsets=offsets, mux=mux,
                                          mux_channel=mux_ch)
                elif t == "HDC302x":
                    sensors[sid] = HDC302x(bus, int(s["address"], 16),
                                           offsets=offsets)
                elif t == "SHT45":
                    mux, mux_ch = _get_mux(s)
                    sensors[sid] = SHT45(
                        bus, int(s.get("address", "0x44"), 16),
                        offsets=offsets, mux=mux, mux_channel=mux_ch)
                elif t == "HWT905":
                    sensors[sid] = HWT905(
                        port=s.get("port", "/dev/ttyIMU"),
                        baud=s.get("baud", 9600),
                    )
                elif t == "A1Temp":
                    sensors[sid] = A1Temp(
                        build_dir=s.get("build_dir", ""),
                        label=s.get("label", "PETSys ASICs"),
                        asic_ids=s.get("asic_ids", [1, 5, 9, 13]),
                    )
                    asic_buffers[sid] = {}
                elif t == "LinuxTemp":
                    sensors[sid] = LinuxTemp(
                        label=s.get("label", "Linux CPU"),
                        zones=s.get("zones"),
                        exclude_zones=s.get("exclude_zones", []),
                    )
            except Exception as e:
                print(f"WARN: {sid} init failed: {e}", file=sys.stderr)
                sensors[sid] = None
    finally:
        sys.stdout = real_stdout

    retry_counts = {sid: 0 for sid in sensors}

    # ── Poll loop ─────────────────────────────────────────────────────────
    while True:
        snapshot = {}
        for sid, sensor in sensors.items():
            s = sensors_cfg[sid]
            t = s["type"]
            try:
                if sensor is None:
                    raise RuntimeError("sensor not initialised")

                entry = {"label": s.get("label", sid), "type": t,
                         "status": "ok", "timestamp": datetime.now().isoformat()}

                if t == "BME280":
                    temp, press_hpa, hum = sensor.read()  # offsets applied inside
                    press_kpa = press_hpa / 10.0
                    entry["temp"]  = round(temp,      2)
                    entry["hum"]   = round(hum,       2)
                    entry["press"] = round(press_kpa, 3)

                elif t == "HDC302x":
                    temp, hum = sensor.read()
                    entry["temp"] = round(temp, 2)
                    entry["hum"]  = round(hum,  2)

                elif t == "SHT45":
                    temp, hum = sensor.read()
                    entry["temp"] = round(temp, 2)
                    entry["hum"]  = round(hum,  2)

                elif t == "HWT905":
                    imu = sensor.read()
                    # Axis remapping: raw→display
                    # tilt_x = -tilt_z (raw)
                    # tilt_y =  tilt_y (raw)
                    # mag_x  = -mz (raw)
                    # mag_y  =  my (raw)
                    # mag_z  =  mx (raw)
                    entry["tilt_x"] = round(-imu["tilt_z"], 3)
                    entry["tilt_y"] = round( imu["tilt_y"], 3)
                    entry["mag_x"]  = round(-imu["mz"], 1)
                    entry["mag_y"]  = round( imu["my"], 1)
                    entry["mag_z"]  = round( imu["mx"], 1)

                elif t == "A1Temp":
                    # read_temperature_sensors_JLG runs with --no-power-on, so it
                    # never turns ASIC power on as a side effect. If ASICs are
                    # powered (either via dashboard or during acquisition), we get
                    # valid temperatures. If they're off, the read returns nothing
                    # useful and we simply report no data — without ever changing
                    # the power state.
                    try:
                        temps = sensor.read()
                        for k, v in temps.items():
                            buf = asic_buffers[sid].setdefault(k, deque(maxlen=ASIC_AVG_N))
                            buf.append(v)
                            entry[k] = round(sum(buf) / len(buf), 2)
                    except Exception:
                        # ASICs likely powered off — report no data, not an error
                        entry["status"] = "ok"
                        entry["_power_off"] = True

                elif t == "LinuxTemp":
                    zone_temps = sensor.read()
                    entry.update(zone_temps)

                retry_counts[sid] = 0

            except Exception as e:
                retry_counts[sid] += 1
                entry = {"label": s.get("label", sid), "type": t,
                         "status": "error", "error": str(e),
                         "timestamp": datetime.now().isoformat()}
                # HWT905 reconnect
                if t == "HWT905" and retry_counts[sid] % 3 == 0:
                    try:
                        if sensor: sensor.close()
                    except Exception:
                        pass
                    try:
                        sensors[sid] = HWT905(
                            port=s.get("port", "/dev/ttyIMU"),
                            baud=s.get("baud", 9600),
                        )
                        retry_counts[sid] = 0
                    except Exception:
                        sensors[sid] = None

            snapshot[sid] = entry

        # Write snapshot as single JSON line to stdout
        sys.stdout.write(json.dumps(snapshot) + "\n")
        sys.stdout.flush()
        time.sleep(interval)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
