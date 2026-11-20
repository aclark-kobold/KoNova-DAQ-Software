#!/usr/bin/env python3
#coding=utf-8
import ctypes
import time
from ctypes import *

lib = cdll.LoadLibrary("./libch347.so")
lib.CH347OpenDevice.argtypes = [c_char_p]
lib.CH347OpenDevice.restype = c_int
lib.CH347CloseDevice.argtypes = [c_int]
lib.CH347StreamI2C.argtypes = [c_int, c_uint, POINTER(c_byte), c_uint, POINTER(c_byte)]
lib.CH347I2C_Set.argtypes = [c_int, c_uint]

class BME280:
    def __init__(self, dev_path="/dev/ch34x_pis0"):
        self.dev_path = dev_path.encode('utf-8')
        self.handle = lib.CH347OpenDevice(self.dev_path)
        if self.handle == -1:
            print("USB CH347 Open Failed!")
            exit(1)
        print(f"CH347 Opened! Handle: {self.handle}")
        lib.CH347I2C_Set(self.handle, 0x03)  # 400kHz

        self.addr = 0x76
        if not self._check_id():
            self.addr = 0x77
            if not self._check_id():
                print("BME280 not found!")
                self.close()
                exit(1)
        print(f"BME280 found at 0x{self.addr:02X}")
        self._load_calibration()

    def _check_id(self):
        return self._read(0xD0, 1)[0] == 0x60

    def _write(self, reg, data):
        # addr(1) + reg(1) + data(N)
        buf = (c_byte * (2 + len(data)))()
        buf[0] = self.addr << 1          # write address
        buf[1] = reg
        for i, b in enumerate(data):
            buf[2 + i] = b & 0xff
        dummy = (c_byte * 1)()
        lib.CH347StreamI2C(self.handle, 2 + len(data), buf, 0, dummy)

    def _read(self, reg, length):
        start = (c_byte * 2)()
        start[0] = self.addr << 1
        start[1] = reg
        data = (c_byte * length)()
        lib.CH347StreamI2C(self.handle, 2, start, length, data)
        return bytes(data)

    def _load_calibration(self):
        cal = self._read(0x88, 26)
        self.dig_T1 = cal[1] << 8 | cal[0]
        self.dig_T2 = c_short(cal[3] << 8 | cal[2]).value
        self.dig_T3 = c_short(cal[5] << 8 | cal[4]).value

        cal_p = self._read(0x8E, 18)
        self.dig_P1 = cal_p[1] << 8 | cal_p[0]
        self.dig_P2 = c_short(cal_p[3] << 8 | cal_p[2]).value
        self.dig_P3 = c_short(cal_p[5] << 8 | cal_p[4]).value
        self.dig_P4 = c_short(cal_p[7] << 8 | cal_p[6]).value
        self.dig_P5 = c_short(cal_p[9] << 8 | cal_p[8]).value
        self.dig_P6 = c_short(cal_p[11] << 8 | cal_p[10]).value
        self.dig_P7 = c_short(cal_p[13] << 8 | cal_p[12]).value
        self.dig_P8 = c_short(cal_p[15] << 8 | cal_p[14]).value
        self.dig_P9 = c_short(cal_p[17] << 8 | cal_p[16]).value

        cal_h = self._read(0xE1, 7)
        self.dig_H1 = cal_h[0]
        self.dig_H2 = c_short(cal_h[2] << 8 | cal_h[1]).value
        self.dig_H3 = cal_h[3]
        self.dig_H4 = c_short(cal_h[5] << 4 | (cal_h[4] & 0x0F)).value
        self.dig_H5 = c_short((cal_h[6] << 4) | (cal_h[5] >> 4)).value
        self.dig_H6 = c_byte(cal_h[6]).value

    def read_data(self):
        self._write(0xF2, [0x01])  # humidity oversampling x1
        self._write(0xF4, [0x27])  # forced mode, temp & press oversampling x1
        time.sleep(0.05)
        data = self._read(0xF7, 8)

        pres_raw = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
        temp_raw = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
        hum_raw  = (data[6] << 8)  | data[7]

        # Temperature
        var1 = (temp_raw / 16384.0 - self.dig_T1 / 1024.0) * self.dig_T2
        var2 = ((temp_raw / 131072.0 - self.dig_T1 / 8192.0) *
                (temp_raw / 131072.0 - self.dig_T1 / 8192.0)) * self.dig_T3
        t_fine = int(var1 + var2)
        temp = (var1 + var2) / 5120.0

        # Pressure
        var1 = t_fine / 2.0 - 64000.0
        var2 = var1 * var1 * self.dig_P6 / 32768.0
        var2 = var2 + var1 * self.dig_P5 * 2.0
        var2 = var2 / 4.0 + self.dig_P4 * 65536.0
        var1 = (self.dig_P3 * var1 * var1 / 524288.0 + self.dig_P2 * var1) / 524288.0
        var1 = (1.0 + var1 / 32768.0) * self.dig_P1
        pres = 0.0 if var1 == 0 else ((1048576.0 - pres_raw) - (var2 / 4096.0)) * 6250.0 / var1
        var1 = self.dig_P9 * pres * pres / 2147483648.0
        var2 = pres * self.dig_P8 / 32768.0
        pres = (pres + (var1 + var2 + self.dig_P7) / 16.0) / 100.0

        # Humidity
        h = t_fine - 76800.0
        h = (hum_raw - (self.dig_H4 * 64.0 + self.dig_H5 / 16384.0 * h)) * (
            self.dig_H2 / 65536.0 * (1.0 + self.dig_H6 / 67108864.0 * h *
                                     (1.0 + self.dig_H3 / 67108864.0 * h)))
        h = h * (1.0 - self.dig_H1 * h / 524288.0)
        h = max(0.0, min(100.0, h))

        return temp, pres, h

    def close(self):
        lib.CH347CloseDevice(self.handle)

if __name__ == "__main__":
    sensor = BME280()
    try:
        while True:
            t, p, h = sensor.read_data()
            print(f"Temperature: {t:5.2f}°C | Pressure: {p:7.2f} hPa | Humidity: {h*100:5.2f}%")
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nStopped")
    finally:
        sensor.close()


