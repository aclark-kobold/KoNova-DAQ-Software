#!/usr/bin/env python3
"""
KoNova Slow Control — Sensor Driver
BME280 (T/H/P) + HDC302x (T/H) over CH347 USB-I2C bridge.
"""

import time
from ctypes import c_short, c_byte


class CH347:
    """Thin wrapper around the CH347 shared library for I2C."""

    I2C_FREQ = {0: 20, 1: 100, 2: 400, 3: 1000}  # kHz

    def __init__(self, device=b"/dev/ch34x_pis0", i2c_mode=0):
        from ctypes import cdll, c_byte, c_int, c_uint, c_char_p, POINTER
        self._c_byte = c_byte
        self.lib = cdll.LoadLibrary("./libch347.so")
        self.lib.CH347OpenDevice.argtypes = [c_char_p]
        self.lib.CH347OpenDevice.restype = c_int
        self.lib.CH347CloseDevice.argtypes = [c_int]
        self.lib.CH347StreamI2C.argtypes = [
            c_int, c_uint, POINTER(c_byte), c_uint, POINTER(c_byte)
        ]
        self.lib.CH347I2C_Set.argtypes = [c_int, c_uint]
        self.handle = self.lib.CH347OpenDevice(device)
        if self.handle == -1:
            raise RuntimeError(f"CH347 open failed on {device}")
        self.lib.CH347I2C_Set(self.handle, i2c_mode)
        freq = self.I2C_FREQ.get(i2c_mode, "?")
        print(f"CH347 opened (handle {self.handle}), I2C @ {freq} kHz")

    def write(self, addr, data):
        c_byte = self._c_byte
        buf = (c_byte * (1 + len(data)))()
        buf[0] = addr << 1
        for i, b in enumerate(data):
            buf[1 + i] = b
        self.lib.CH347StreamI2C(self.handle, len(buf), buf, 0, None)

    def read_reg(self, addr, reg, n):
        c_byte = self._c_byte
        start = (c_byte * 2)(addr << 1, reg)
        out = (c_byte * n)()
        self.lib.CH347StreamI2C(self.handle, 2, start, n, out)
        return bytes(out)

    def read_raw(self, addr, n):
        c_byte = self._c_byte
        start = (c_byte * 1)(addr << 1 | 1)
        out = (c_byte * n)()
        self.lib.CH347StreamI2C(self.handle, 1, start, n, out)
        return bytes(out)

    def close(self):
        self.lib.CH347CloseDevice(self.handle)


class BME280:
    """BME280 temperature / humidity / pressure sensor.
    If mux and mux_channel are provided, selects the mux channel before
    every I2C transaction."""

    CHIP_ID = 0x60

    def __init__(self, bus, addr=0x76, offsets=None, mux=None, mux_channel=None):
        self.bus = bus
        self.addr = addr
        self.offsets = offsets or {"temp": 0.0, "hum": 0.0, "press": 0.0}
        self.mux = mux
        self.mux_channel = mux_channel

        if self.mux is not None:
            self.mux.select(self.mux_channel)
        try:
            chip_id = bus.read_reg(addr, 0xD0, 1)[0]
            if chip_id != self.CHIP_ID:
                raise RuntimeError(
                    f"BME280 not found at 0x{addr:02X} (got 0x{chip_id:02X})"
                )
            ch_info = f" (mux ch{mux_channel})" if mux else ""
            print(f"BME280 found at 0x{addr:02X}{ch_info}")
            self._load_calibration()
        finally:
            if self.mux is not None:
                self.mux.deselect()

    def _load_calibration(self):
        a = self.bus.read_reg
        addr = self.addr

        # Temperature
        c = a(addr, 0x88, 6)
        self.t1 = c[1] << 8 | c[0]
        self.t2 = c_short(c[3] << 8 | c[2]).value
        self.t3 = c_short(c[5] << 8 | c[4]).value

        # Pressure
        c = a(addr, 0x8E, 18)
        self.p1 = c[1] << 8 | c[0]
        self.p2 = c_short(c[3] << 8 | c[2]).value
        self.p3 = c_short(c[5] << 8 | c[4]).value
        self.p4 = c_short(c[7] << 8 | c[6]).value
        self.p5 = c_short(c[9] << 8 | c[8]).value
        self.p6 = c_short(c[11] << 8 | c[10]).value
        self.p7 = c_short(c[13] << 8 | c[12]).value
        self.p8 = c_short(c[15] << 8 | c[14]).value
        self.p9 = c_short(c[17] << 8 | c[16]).value

        # Humidity
        self.h1 = a(addr, 0xA1, 1)[0]
        c = a(addr, 0xE1, 7)
        self.h2 = c[1] << 8 | c[0]
        if self.h2 > 32767:
            self.h2 -= 65536
        self.h3 = c[2]
        self.h4 = (c[3] << 4) | (c[4] & 0x0F)
        if self.h4 & 0x800:
            self.h4 -= 4096
        self.h5 = (c[5] << 4) | (c[4] >> 4)
        if self.h5 & 0x800:
            self.h5 -= 4096
        self.h6 = c[6] if c[6] < 128 else c[6] - 256

    def read(self):
        """Trigger forced measurement and return (temp_C, press_hPa, hum_pct)."""
        if self.mux is not None:
            self.mux.select(self.mux_channel)
        try:
            self.bus.write(self.addr, [0xF2, 0x01])
            self.bus.write(self.addr, [0xF4, 0x27])
            time.sleep(0.010)

            d = self.bus.read_reg(self.addr, 0xF7, 8)
            pr = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
            tr = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
            hr = (d[6] << 8) | d[7]

            v1 = (tr / 16384.0 - self.t1 / 1024.0) * self.t2
            v2 = (tr / 131072.0 - self.t1 / 8192.0) ** 2 * self.t3
            t_fine = v1 + v2
            temp = t_fine / 5120.0

            v1 = t_fine / 2.0 - 64000.0
            v2 = v1 * v1 * self.p6 / 32768.0 + v1 * self.p5 * 2.0
            v2 = v2 / 4.0 + self.p4 * 65536.0
            v1 = (self.p3 * v1 * v1 / 524288.0 + self.p2 * v1) / 524288.0
            v1 = (1.0 + v1 / 32768.0) * self.p1
            if v1 != 0:
                p = (1048576.0 - pr - v2 / 4096.0) * 6250.0 / v1
            else:
                p = 0.0
            v1 = self.p9 * p * p / 2147483648.0
            v2 = p * self.p8 / 32768.0
            press = (p + (v1 + v2 + self.p7) / 16.0) / 100.0

            h = t_fine - 76800.0
            h = (hr - (self.h4 * 64.0 + self.h5 / 16384.0 * h)) * (
                self.h2 / 65536.0
                * (1.0 + self.h3 / 67108864.0 * h * (1.0 + self.h6 / 67108864.0 * h))
            )
            h *= 1.0 - self.h1 * h / 524288.0
            hum = max(0.0, min(100.0, h))

            temp += self.offsets["temp"]
            press += self.offsets["press"]
            hum += self.offsets["hum"]

            return temp, press, hum
        finally:
            if self.mux is not None:
                self.mux.deselect()


class HDC302x:
    """HDC302x temperature / humidity sensor."""

    def __init__(self, bus, addr=0x44, offsets=None):
        self.bus = bus
        self.addr = addr
        self.offsets = offsets or {"temp": 0.0, "hum": 0.0}
        print(f"HDC302x registered at 0x{addr:02X}")

    def read(self):
        """Trigger measurement and return (temp_C, hum_pct).
        Raises RuntimeError if the sensor is absent or not responding."""
        self.bus.write(self.addr, [0x24, 0x00])  # high-accuracy mode
        time.sleep(0.015)
        data = self.bus.read_raw(self.addr, 6)

        # Detect absent sensor: if all bytes are 0x00 or 0xFF, no real data
        if all(b in (0x00, 0xFF) for b in data):
            raise RuntimeError(f"HDC302x at 0x{self.addr:02X} not responding")

        temp_raw = (data[0] << 8) | data[1]
        hum_raw = (data[3] << 8) | data[4]
        temp = -45.0 + 175.0 * (temp_raw / 65535.0)
        hum = 100.0 * (hum_raw / 65535.0)

        temp += self.offsets["temp"]
        hum += self.offsets["hum"]

        return temp, hum


class PCA9546:
    """PCA9546/PCA9548 I2C multiplexer (4 or 8 channels).
    Same protocol — write a channel bitmask byte."""

    def __init__(self, bus, addr=0x70):
        self.bus = bus
        self.addr = addr
        self._current_ch = None
        print(f"PCA9546 mux registered at 0x{addr:02X}")

    def select(self, channel):
        if channel is None:
            mask = 0x00
        else:
            mask = 1 << channel
        if self._current_ch != channel:
            self.bus.write(self.addr, [mask])
            self._current_ch = channel

    def deselect(self):
        self.select(None)


class SHT45:
    """SHT45 high-precision temperature / humidity sensor (Sensirion).
    Address fixed at 0x44 — use PCA9546 mux for multiple sensors."""

    CMD_MEASURE_HP = 0xFD

    def __init__(self, bus, addr=0x44, offsets=None, mux=None, mux_channel=None):
        self.bus = bus
        self.addr = addr
        self.offsets = offsets or {"temp": 0.0, "hum": 0.0}
        self.mux = mux
        self.mux_channel = mux_channel
        ch_info = f" (mux ch{mux_channel})" if mux else ""
        print(f"SHT45 registered at 0x{addr:02X}{ch_info}")

    @staticmethod
    def _crc8(data):
        crc = 0xFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ 0x31
                else:
                    crc <<= 1
                crc &= 0xFF
        return crc

    def read(self):
        if self.mux is not None:
            self.mux.select(self.mux_channel)
        try:
            self.bus.write(self.addr, [self.CMD_MEASURE_HP])
            time.sleep(0.012)
            data = self.bus.read_raw(self.addr, 6)

            if all(b in (0x00, 0xFF) for b in data):
                raise RuntimeError(
                    f"SHT45 at 0x{self.addr:02X} ch{self.mux_channel} not responding")

            temp_raw = (data[0] << 8) | data[1]
            if self._crc8(data[0:2]) != data[2]:
                raise RuntimeError("SHT45 temperature CRC mismatch")
            hum_raw = (data[3] << 8) | data[4]
            if self._crc8(data[3:5]) != data[5]:
                raise RuntimeError("SHT45 humidity CRC mismatch")

            temp = -45.0 + 175.0 * (temp_raw / 65535.0)
            hum = -6.0 + 125.0 * (hum_raw / 65535.0)
            hum = max(0.0, min(100.0, hum))

            temp += self.offsets["temp"]
            hum += self.offsets["hum"]
            return temp, hum
        finally:
            if self.mux is not None:
                self.mux.deselect()


class HWT905:
    """HWT905 9-axis IMU — reads magnetometer and computes tilt from accelerometer.
    Mounted with X-axis vertical (pointing down). Tilt is computed from
    accelerometer to avoid Euler angle gimbal lock at 90° pitch."""

    HEADER = 0x55
    FRAME_ACCEL = 0x51
    FRAME_MAG = 0x54

    def __init__(self, port="/dev/ttyUSB0", baud=9600, timeout=2):
        import serial
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.port = port
        self.ser.reset_input_buffer()
        print(f"HWT905 opened on {port} @ {baud} baud (X-axis vertical)")

    @staticmethod
    def _checksum(data):
        return sum(data) & 0xFF

    def read(self):
        """Read until we have both accelerometer and magnetometer frames.
        Returns dict: {tilt_y, tilt_z, mx, my, mz}.
        tilt_y/tilt_z are deviation from vertical in degrees (0° = perfectly hanging).
        Raises RuntimeError on timeout."""
        import struct
        import math

        got_mag = False
        got_accel = False
        mx = my = mz = 0.0
        tilt_y = tilt_z = 0.0

        deadline = time.time() + 2.0
        while time.time() < deadline:
            b = self.ser.read(1)
            if len(b) == 0 or b[0] != self.HEADER:
                continue

            t = self.ser.read(1)
            if len(t) == 0:
                continue
            payload = self.ser.read(8)
            csum = self.ser.read(1)
            if len(payload) < 8 or len(csum) == 0:
                continue

            if self._checksum(b'\x55' + t + payload) != csum[0]:
                continue

            frame_type = t[0]

            if frame_type == self.FRAME_ACCEL:
                raw_ax, raw_ay, raw_az, _ = struct.unpack('<hhhh', payload)
                ax = raw_ax / 32768.0 * 16.0  # g
                ay = raw_ay / 32768.0 * 16.0
                az = raw_az / 32768.0 * 16.0
                # X is vertical (≈1g). Tilt = deviation from vertical.
                # tilt_y = angle of Y-component relative to X (vertical)
                # tilt_z = angle of Z-component relative to X (vertical)
                if abs(ax) > 0.01:
                    tilt_y = math.degrees(math.atan2(ay, ax))
                    tilt_z = math.degrees(math.atan2(az, ax))
                got_accel = True

            elif frame_type == self.FRAME_MAG:
                raw_mx, raw_my, raw_mz, _ = struct.unpack('<hhhh', payload)
                mx = raw_mx * 0.3  # µT
                my = raw_my * 0.3
                mz = raw_mz * 0.3
                got_mag = True

            if got_mag and got_accel:
                return {
                    "tilt_y": round(tilt_y, 3),
                    "tilt_z": round(tilt_z, 3),
                    "mx": round(mx, 2),
                    "my": round(my, 2),
                    "mz": round(mz, 2),
                }

        raise RuntimeError(f"HWT905 on {self.port} timeout — no complete reading")

    def close(self):
        self.ser.close()


class A1Temp:
    """PETSys ASIC temperature reader.

    Calls ``read_temperature_sensors_JLG`` and parses its table output.
    The ``asic_ids`` parameter defines which physical ASIC chipIDs are present
    (e.g. [1, 5, 8, 13]).  Even chipIDs read from the A0 column, odd from A1.
    Output keys are always asic_1, asic_2, asic_3, asic_4 in config order.
    """

    def __init__(self, build_dir: str, label: str = "PETSys ASICs",
                 asic_ids: list = None):
        import os
        self.exe = os.path.join(os.path.expanduser(build_dir), "read_temperature_sensors_JLG")
        self.label = label
        # Physical ASIC chipIDs — config defines the order
        self.asic_ids = asic_ids or [1, 5, 9, 13]
        # Build lookup: moduleID → (display_number, column)
        # moduleID = chipID // 2; even chipID → A0, odd chipID → A1
        self._lookup = {}
        for idx, chip_id in enumerate(self.asic_ids):
            mod_id = chip_id // 2
            col = "A0" if chip_id % 2 == 0 else "A1"
            display_num = idx + 1  # 1-based: asic_1, asic_2, asic_3, asic_4
            self._lookup[mod_id] = (display_num, col)
        print(f"A1Temp registered (exe: {self.exe}, ASICs: {self.asic_ids})")

    def read(self) -> dict:
        """Run the binary and return a dict of ASIC temperatures.

        Returns
        -------
        dict  keyed ``asic_1`` … ``asic_4`` with float °C values.

        Raises RuntimeError if the binary fails or no temperatures are found.
        """
        import subprocess
        import os

        if not os.path.isfile(self.exe):
            raise RuntimeError(f"read_temperature_sensors_JLG not found: {self.exe}")

        try:
            result = subprocess.run(
                [self.exe, "--no-power-on"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=os.path.dirname(self.exe),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("read_temperature_sensors_JLG timed out after 10 s")
        except Exception as e:
            raise RuntimeError(f"Failed to run read_temperature_sensors_JLG: {e}")

        output = result.stdout + result.stderr

        if "daqd.Connection" in output or "Connection refused" in output:
            raise RuntimeError("DAQ is OFF — start daqd before reading ASIC temperatures.")

        lines = output.splitlines()

        # Find header line containing "A0 Sensor" or "A1 Sensor"
        header_idx = None
        a0_col = None
        a1_col = None
        module_col_i = None
        for i, line in enumerate(lines):
            if 'A0 Sensor' in line or 'A1 Sensor' in line:
                header_idx = i
                parts = line.split('|')
                for col_i, part in enumerate(parts):
                    stripped = part.strip()
                    if 'A0' in stripped and 'Sensor' in stripped:
                        a0_col = col_i
                    elif 'A1' in stripped and 'Sensor' in stripped:
                        a1_col = col_i
                    if 'moduleid' in stripped.lower():
                        module_col_i = col_i
                break

        if header_idx is None or (a0_col is None and a1_col is None):
            if "No such file" in output:
                raise RuntimeError("DAQ is OFF — start daqd before reading ASIC temperatures.")
            raise RuntimeError(
                f"read_temperature_sensors_JLG produced no readable data.\n"
                f"stdout: {output[:400]}"
            )

        def _parse_temp(parts, col):
            if col is None or col >= len(parts):
                return None
            cell = parts[col].strip()
            cell = cell.replace('ºC', '').replace('°C', '').replace('º', '').replace('°', '').strip()
            try:
                return round(float(cell), 2)
            except ValueError:
                return None

        temps = {}
        seen_modules = set()
        separator_count = 0

        for line in lines[header_idx + 1:]:
            if line.strip().startswith('+'):
                separator_count += 1
                if separator_count >= 2:
                    break
                continue
            if '|' not in line:
                continue
            parts = line.split('|')

            # Get moduleID
            mod_id = None
            if module_col_i is not None and module_col_i < len(parts):
                try:
                    mod_id = int(parts[module_col_i].strip())
                    if mod_id in seen_modules:
                        continue
                    seen_modules.add(mod_id)
                except ValueError:
                    continue

            # Check if this moduleID is one we're looking for
            if mod_id not in self._lookup:
                continue

            display_num, column = self._lookup[mod_id]
            col_idx = a0_col if column == "A0" else a1_col
            val = _parse_temp(parts, col_idx)

            if val is not None and 0.0 <= val <= 80.0:
                temps[f"asic_{display_num}"] = val

        if not temps:
            raise RuntimeError(
                f"read_temperature_sensors_JLG table had no numeric data.\n"
                f"stdout: {output[:400]}"
            )

        return temps


class LinuxTemp:
    """Reads CPU/system temperatures from Linux thermal zones or lm-sensors.

    Reads /sys/class/thermal/thermal_zone*/temp — works on all Linux systems
    without any extra packages. Each zone reports millidegrees Celsius.
    Zones are labelled by their 'type' file (e.g. 'x86_pkg_temp', 'acpitz').
    """

    def __init__(self, label: str = "Linux CPU", zones: list = None,
                 exclude_zones: list = None):
        """zones: list of zone indices to include. exclude_zones: list of zone type names to skip."""
        import glob, os
        self.label = label
        exclude = set(exclude_zones or [])
        if zones is not None:
            all_paths = [f"/sys/class/thermal/thermal_zone{i}/temp" for i in zones]
        else:
            all_paths = sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp"))

        self.zone_paths = []
        self.zone_names = []
        for p in all_paths:
            type_path = p.replace("/temp", "/type")
            try:
                with open(type_path) as f:
                    ztype = f.read().strip()
            except Exception:
                ztype = os.path.basename(os.path.dirname(p))
            if ztype in exclude:
                continue
            self.zone_paths.append(p)
            self.zone_names.append(ztype)
        print(f"LinuxTemp: {len(self.zone_paths)} thermal zones: "
              f"{', '.join(self.zone_names)}")

    def read(self) -> dict:
        """Return dict of zone temperatures in °C, e.g. {'zone_0': 45.0, ...}"""
        temps = {}
        for path, name in zip(self.zone_paths, self.zone_names):
            try:
                with open(path) as f:
                    val = int(f.read().strip()) / 1000.0
                key = name.replace(" ", "_").replace("/", "_")[:16]
                temps[key] = round(val, 1)
            except Exception:
                pass
        if not temps:
            raise RuntimeError("No Linux thermal zones readable")
        return temps
