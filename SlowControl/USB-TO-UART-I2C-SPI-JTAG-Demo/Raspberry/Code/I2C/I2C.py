#! /usr/bin/env python
#coding=utf-8
import ctypes
import os
import time
from ctypes import *

DevIndex = '/dev/ch34x_pis0'
DevIndex_c = ctypes.c_char_p(DevIndex.encode('utf-8'))		#Convert DevIndex to const char *

class USBI2C():
    ch347 = cdll.LoadLibrary("./libch347.so")
    def __init__(self, usb_dev = DevIndex_c, i2c_dev = 0x20):
        self.usb_id   = usb_dev
        self.dev_addr = i2c_dev
        self.handle = USBI2C.ch347.CH347OpenDevice(self.usb_id)
        if self.handle != -1:
            print("Open the devices:", self.handle)
            USBI2C.ch347.CH347CloseDevice(self.handle)
        else:
            print("USB CH347 Open Failed!")

    def read(self):
        if USBI2C.ch347.CH347OpenDevice(self.usb_id) != -1:
            
            rec  = (c_byte * 1)()
            ibuf = (c_byte * 9)()
            rec[0] = self.dev_addr
            
            USBI2C.ch347.CH347StreamI2C(self.handle, 1, rec, 9, ibuf)
            USBI2C.ch347.CH347CloseDevice(self.handle)
            
            return ibuf
        else:
            print("USB CH347 Open Failed!")
            return 0

    def write(self,cmd,size):
        if USBI2C.ch347.CH347OpenDevice(self.usb_id) != -1:
            tcmd = (c_byte * (size + 1))()
            ibuf = (c_byte * 1)()
            tcmd[0] = self.dev_addr
            
            for i in range (size):
                tcmd[i+1] = cmd[i] & 0xff
            USBI2C.ch347.CH347StreamI2C(self.handle, 6, tcmd, 0, ibuf)
            USBI2C.ch347.CH347CloseDevice(self.handle)
        else:
            print("USB CH347 Open Failed!")

if __name__ == "__main__":
    cmd = (c_byte * 5)(0x5a,0x05,0x00,0x01,0x60)
    size = sizeof(cmd) 
    q = USBI2C()
    while True:
        q.write(cmd,size)
        rec =q.read()
        dist    =((rec[2]&0xff)+(rec[3]&0xff)*256)
        strengh =((rec[4]&0xff)+(rec[5]&0xff)*256)
        temp    =((rec[6]&0xff)+(rec[7]&0xff)*256)/8-256
        print("Dist:",dist,"Strengh:",strengh,"Temp:",temp)
  
        time.sleep(0.05) #50ms
    
