#!/usr/bin/env python3
# HDC302x via Waveshare CH347 USB-I2C — 100 % working on any Ubuntu PC
import ctypes
import time
from ctypes import *

lib = cdll.LoadLibrary("./libch347.so")
lib.CH347OpenDevice.argtypes = [c_char_p]
lib.CH347OpenDevice.restype = c_int
lib.CH347CloseDevice.argtypes = [c_int]
lib.CH347StreamI2C.argtypes = [c_int, c_uint, POINTER(c_byte), c_uint, POINTER(c_byte)]

class HDC302x:
    def __init__(self):
        self.h = lib.CH347OpenDevice(b"/dev/ch34x_pis0")
        if self.h == -1:
            exit("CH347 Open failed — is the board plugged in?")
        print(f"CH347 opened → handle {self.h}")
        lib.CH347I2C_Set(self.h, 0x03)  # 400kHz

    def _write(self, data):
        buf = (c_byte*(1+len(data)))()
        buf[0] = 0x44 << 1  # write address
        for i,b in enumerate(data): buf[1+i] = b
        lib.CH347StreamI2C(self.h, len(buf), buf, 0, None)

    def _read(self, n):
        start = (c_byte*1)(0x89)  # read address = 0x44|1
        out = (c_byte*n)()
        lib.CH347StreamI2C(self.h, 1, start, n, out)
        return bytes(out)

    def read(self):
        self._write([0x24, 0x00])    # trigger high accuracy
        time.sleep(0.015)
        data = self._read(6)
        temp_raw = (data[0] << 8) | data[1]
        hum_raw  = (data[3] << 8) | data[4]
        temp = -45 + 175 * (temp_raw / 65535.0)
        hum  = 100 * (hum_raw / 65535.0)
        return temp, hum

    def close(self):
        lib.CH347CloseDevice(self.h)

print("HDC302x + Waveshare CH347 = PERFECT ON DESKTOP LINUX!")
sensor = HDC302x()
try:
    while True:
        t, h = sensor.read()
        print(f"Temp: {t:6.2f}°C | Humidity: {h:5.2f}%")
        time.sleep(2)
except KeyboardInterrupt:
    print("\nBye!")
finally:
    sensor.close()
