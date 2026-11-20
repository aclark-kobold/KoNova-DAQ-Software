#!/usr/bin/env python3
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

file = open("THP.txt","a")
file.write("T1\tH1\tP1\tT2\tH2\tT3\tH3\tT4\tH4\tT5\tH5\n")


class DualSensor:
    def __init__(self,sensor):
        self.hdc_addr = 0x44
        self.h = lib.CH347OpenDevice(b"/dev/ch34x_pis0")
        if self.h == -1: exit("CH347 Open failed")
        print(f"CH347 opened → handle {self.h}")
        #i2c frequency: 0: 20khz   1: 100khz   2:400khz   3:1Mhz
        i2cMode=0x00
        freq = [20,100,400,1000]        
        lib.CH347I2C_Set(self.h, i2cMode)   #i2c frequency set for 20kHz
        print(f"I2C frequency set at {freq[i2cMode]:4d}kHz")
        print(f"HDC302x @ 0x{self.hdc_addr:02X}")


        # Auto-detect
        self.bme_addr = 0x76+sensor
        if self._read(self.bme_addr, 0xD0, 1) != b'\x60':
              print('bme sensor ', sensor,' not found')
#          if self._read(0x77, 0xD0, 1) != b'\x60':
#              print('bme sensor 1 not found')

        print(f"BME280 @ 0x{self.bme_addr:02X}")
        self._load_bme_cal()
#        self._load_bme_cal(1)

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
    # Humidity calibration
    # H1
        self.h1 = self._read(self.bme_addr, 0xA1, 1)[0]        
        c = self._read(self.bme_addr, 0xE1, 7)
        print("test1") 
        print([hex(x) for x in c])
        print("test2")
        print("E1 block:", [hex(b) for b in self._read(self.bme_addr, 0xE1, 7)])

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
        self.h6 = c[6] if c[6] < 128 else c[6]-256 
        print('H1 H2 H3 H4 H5 H6:', self.h1, self.h2, self.h3, self.h4, self.h5, self.h6) 
        print('T1 T2 T3:', self.t1, self.t2, self.t3)
        print('P1 P2 P3 P4 P5 P6 P7 P8 P9:', self.p1, self.p2, self.p3, self.p4, self.p5, self.p6, self.p7, self.p8, self.p9)

    def read_bme280(self):
    	print("read_bme280",self.bme_addr)
    	# === STEP 1: FULLY WAKE UP AND TRIGGER ===
    	self._write(self.bme_addr, [0xF2, 0x01])        # hum x1
    	self._write(self.bme_addr, [0xF4, 0x27])        # forced mode: temp x1, press x1, hum x1
    	time.sleep(0.006)  # ← 6ms MINIMUM (covers 4.9ms conversion)

    	# === STEP 2: READ ALL DATA IN ONE SHOT ===    	
    	d = self._read(self.bme_addr, 0xF7, 8)
    	pr = (d[0]<<12)|(d[1]<<4)|(d[2]>>4)
    	tr = (d[3]<<12)|(d[4]<<4)|(d[5]>>4)
    	hr = (d[6]<<8)|d[7]
    	#print('hr:',hr,end='')

    	# === STEP 3: COMPENSATION (t_fine as float) ===
    	v1 = (tr/16384.0 - self.t1/1024.0)*self.t2
    	v2 = (tr/131072.0 - self.t1/8192.0)**2 * self.t3
    	t_fine = v1 + v2
    	temp = t_fine / 5120.0

    	# Pressure
    	v1 = t_fine/2.0 - 64000.0
    	v2 = v1*v1*self.p6/32768.0 + v1*self.p5*2.0
    	v2 = v2/4.0 + self.p4*65536.0
    	v1 = (self.p3*v1*v1/524288.0 + self.p2*v1)/524288.0
    	v1 = (1.0 + v1/32768.0)*self.p1
    	if v1 !=0:
    	   p = (1048576.0 - pr - v2/4096.0)*6250.0/v1
    	else: p=1
    	v1 = self.p9*p*p/2147483648.0
    	v2 = p*self.p8/32768.0
    	press = (p + (v1 + v2 + self.p7)/16.0)/100.0
    	press = press + 10


    	# Humidity
    	h = t_fine - 76800.0
    	h = (hr - (self.h4*64.0 + self.h5/16384.0*h)) * (
    	    self.h2/65536.0 * (1.0 + self.h3/67108864.0*h*(1.0 + self.h6/67108864.0*h)))
    	h *= (1.0 - self.h1*h/524288.0)
    	hum = max(0.0, min(100.0, h))
    	hum=hum+9
    	#print('hum',hum)
    	return temp, press, hum


    def read_hdc302x(self,sensor):
        self._write(self.hdc_addr+sensor, [0x24, 0x00])   # trigger high accuracy
        time.sleep(0.015)
        data = self._read_raw(self.hdc_addr+sensor, 6)    # ← **THIS IS THE FIX**
        temp_raw = (data[0] << 8) | data[1]
        hum_raw  = (data[3] << 8) | data[4]
        temp = -45 + 175 * (temp_raw / 65535.0)
        hum  = 100 * (hum_raw / 65535.0)
        return temp, hum

    def close(self):
        lib.CH347CloseDevice(self.h)

print("="*60)
print("DUAL SENSOR STATION")
print("="*60)
s = DualSensor(0)
s2=DualSensor(1)
print(s.read_bme280())
print(s2.read_bme280())
try:
    while True:
        t0, p0, h0  = s.read_bme280()
        t1, p1, h1  = s2.read_bme280()
        t2, h2     = s.read_hdc302x(0)
        h2=h2-3 #offset correction
        t3, h3     = s.read_hdc302x(1)
        t4, h4     = s.read_hdc302x(2)
        t5, h5     = s.read_hdc302x(3)

        print(f"BME280_0  → {t0:5.2f}°C | {h0:5.2f}% | {p0:6.2f} hPa ")
        print(f"BME280_1  → {t1:5.2f}°C | {h1:5.2f}% | {p1:6.2f} hPa ")
        print(f"HDC3022_0 → {t2:5.2f}°C | {h2:5.2f}% |")
        print(f"HDC3022_1 → {t3:5.2f}°C | {h3:5.2f}% |")
        print(f"HDC3022_2 → {t4:5.2f}°C | {h4:5.2f}% |")
        print(f"HDC3022_3 → {t5:5.2f}°C | {h5:5.2f}% |")
        if ((t1>100)| (t1<0)): # | (t2<0) |(t3<0) |(t4<0)|(t5<0)):
          time.sleep(1)
#          s.__init__(0)
          s2.__init__(1)  #<--- error missing one positional argument
#          s.h = lib.CH347OpenDevice(b"/dev/ch34x_pis0")
#          if s.h == -1: 
#             exit("CH347 Open failed")
#          print(f"CH347 opened → handle {s.h}")
#          lib.CH347I2C_Set(s.h, 0x03)#

#          # Auto-detect
#          s.bme_addr = 0x76 if s._read(0x76, 0xD0, 1) == b'\x60' else 0x77
#          s.hdc_addr = 0x44
#          print(f"BME280 @ 0x{s.bme_addr:02X}|HDC302x @ 0x{s.hdc_addr:02X} | HDC302x @ 0x{s.hdc_addr+1:02X}")
#          s._load_bme_cal()

        #T1     H1     P1     T2     H2     T3     H3     T4     H4     T5     H5")
        file.write("{:4.1f}\t{:4.1f}\t{:6.1f}\t{:4.1f}\t{:4.1f}\t{:4.1f}\t{:4.1f}\t{:4.1f}\t{:4.1f}\t{:4.1f}\t{:4.1f}\n".format(t1,h1,p1,t2,h2,t3,h3,t4,h4,t5,h5))
#              '\t'+str(t2)+'\t'+str(h2)+
#              '\t'+str(t3)+'\t'+str(h3)+
#              '\t'+str(t4)+'\t'+str(h4)+
#              '\t'+str(t5)+'\t'+str(h5)+ '\n')

        print("-" * 60)
        time.sleep(5)
except KeyboardInterrupt:
    print("\nShutdown requested.")
finally:
    s.close()
