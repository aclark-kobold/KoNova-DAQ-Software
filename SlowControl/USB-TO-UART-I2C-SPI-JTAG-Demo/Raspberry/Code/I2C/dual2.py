#!/usr/bin/env python3
# ALVARO'S DUAL SENSOR STATION — FINAL VICTORY EDITION
# BME280 + HDC302x — November 10, 2025 06:49 PM PST
import ctypes
import time
from ctypes import *

lib = cdll.LoadLibrary("./libch347.so")
lib.CH347OpenDevice.argtypes = [c_char_p]
lib.CH347OpenDevice.restype = c_int
lib.CH347CloseDevice.argtypes = [c_int]
lib.CH347StreamI2C.argtypes = [c_int, c_uint, POINTER(c_byte), c_uint, POINTER(c_byte)]
lib.CH347I2C_Set.argtypes = [c_int, c_uint]

class DualSensor:
    def __init__(self):
        self.h = lib.CH347OpenDevice(b"/dev/ch34x_pis0")
        if self.h == -1: exit("CH347 Open failed")
        print(f"CH347 opened → handle {self.h}")
        lib.CH347I2C_Set(self.h, 0x03)

        # Auto-detect
        self.bme_addr = 0x76 if self._read(0x76, 0xD0, 1) == b'\x60' else 0x77
        self.hdc_addr = 0x44
        print(f"BME280 @ 0x{self.bme_addr:02X} | HDC302x @ 0x{self.hdc_addr:02X}")
        self._load_bme_cal()

    def _write(self, addr, data):
        buf = (c_byte*(1+len(data)))()
        buf[0] = addr << 1
        for i,b in enumerate(data): buf[1+i] = b
        lib.CH347StreamI2C(self.h, len(buf), buf, 0, None)

    def _read(self, addr, reg, n):
        start = (c_byte*2)(addr<<1, reg)
        out = (c_byte*n)()
        lib.CH347StreamI2C(self.h, 2, start, n, out)
        return bytes(out)

    def _read_raw(self, addr, n):
        # **THIS IS THE FIX** — pure read, no register
        start = (c_byte*1)(addr << 1 | 1)   # read address only
        out = (c_byte*n)()
        lib.CH347StreamI2C(self.h, 1, start, n, out)
        return bytes(out)

    def _load_bme_cal(self):
        c = self._read(self.bme_addr, 0x88, 26)
        self.t1 = c[1]<<8|c[0]; self.t2 = c_short(c[3]<<8|c[2]).value; self.t3 = c_short(c[5]<<8|c[4]).value
        c = self._read(self.bme_addr, 0x8E, 18)
        self.p1 = c[1]<<8|c[0]; self.p2 = c_short(c[3]<<8|c[2]).value; self.p3 = c_short(c[5]<<8|c[4]).value
        self.p4 = c_short(c[7]<<8|c[6]).value; self.p5 = c_short(c[9]<<8|c[8]).value
        self.p6 = c_short(c[11]<<8|c[10]).value; self.p7 = c_short(c[13]<<8|c[12]).value
        self.p8 = c_short(c[15]<<8|c[14]).value; self.p9 = c_short(c[17]<<8|c[16]).value
        c = self._read(self.bme_addr, 0xE1, 7)
        self.h1 = c[0]; self.h2 = c_short(c[2]<<8|c[1]).value; self.h3 = c[3]
        self.h4 = c_short(c[5]<<4 | c[4]&0x0F).value; self.h5 = c_short(c[6]<<4 | c[5]>>4).value
        self.h6 = c_byte(c[6]).value

    def read_bme280(self):
    	self._write(self.bme_addr, [0xF2, 0x01])
    	self._write(self.bme_addr, [0xF4, 0x27])
    	time.sleep(0.05)
    	d = self._read(self.bme_addr, 0xF7, 8)
    	pr = (d[0]<<12)|(d[1]<<4)|(d[2]>>4)
    	tr = (d[3]<<12)|(d[4]<<4)|(d[5]>>4)
    	hr = (d[6]<<8)|d[7]

    	# TEMPERATURE
    	v1 = (tr/16384.0 - self.t1/1024.0)*self.t2
    	v2 = (tr/131072.0 - self.t1/8192.0)**2 * self.t3
    	t_fine = v1 + v2                     # ← FLOAT, NOT INT
    	temp = t_fine / 5120.0

    	# PRESSURE
    	v1 = t_fine/2.0 - 64000.0
    	v2 = v1*v1*self.p6/32768.0 + v1*self.p5*2.0
    	v2 = v2/4.0 + self.p4*65536.0
    	v1 = (self.p3*v1*v1/524288.0 + self.p2*v1)/524288.0
    	v1 = (1.0 + v1/32768.0)*self.p1
    	p = (1048576.0 - pr - v2/4096.0)*6250.0/v1
    	v1 = self.p9*p*p/2147483648.0
    	v2 = p*self.p8/32768.0
    	press = (p + (v1 + v2 + self.p7)/16.0)/100.0

    	# HUMIDITY — USES t_fine (float)
    	h = t_fine - 76800.0
    	h = (hr - (self.h4*64.0 + self.h5/16384.0*h)) * (
    	    self.h2/65536.0 * (1.0 + self.h6/67108864.0*h*(1.0 + self.h3/67108864.0*h)))
    	h *= (1.0 - self.h1*h/524288.0)
    	hum = max(0.0, min(100.0, h))

    	return temp, press, hum
    def read_hdc302x(self):
        self._write(self.hdc_addr, [0x24, 0x00])   # trigger high accuracy
        time.sleep(0.015)
        data = self._read_raw(self.hdc_addr, 6)    # ← **THIS IS THE FIX**
        temp_raw = (data[0] << 8) | data[1]
        hum_raw  = (data[3] << 8) | data[4]
        temp = -45 + 175 * (temp_raw / 65535.0)
        hum  = 100 * (hum_raw / 65535.0)
        return temp, hum

    def close(self):
        lib.CH347CloseDevice(self.h)

print("="*60)
print("ALVARO'S DUAL SENSOR STATION — FINAL VICTORY")
print("="*60)
s = DualSensor()
try:
    while True:
        t1, p, h1 = s.read_bme280()
        t2, h2     = s.read_hdc302x()
        print(f"BME280  → {t1:5.2f}°C | {p:6.2f} hPa | {100*h1:5.2f}%")
        print(f"HDC302x → {t2:5.2f}°C | {'—':>8} | {h2:5.2f}%")
        print("-" * 60)
        time.sleep(2)
except KeyboardInterrupt:
    print("\nPerfect shutdown.")
finally:
    s.close()
